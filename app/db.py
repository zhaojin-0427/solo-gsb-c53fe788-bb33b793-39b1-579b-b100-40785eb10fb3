"""SQLite 持久化：场景与整定批次均以不可变文档/追加记录存储。

并发模型：连接置于 autocommit 隔离级，写事务显式以 BEGIN IMMEDIATE 开启，
使跨进程的并发写在事务起点即串行化；配合 SQLite 文件锁（busy_timeout
等待）与 request_id 唯一约束，同场景争用至多一个批次生效，其余按版本
冲突回滚（HTTP 409），不存在部分生效。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = os.environ.get("DATABASE_PATH", "data/selectivity.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scenarios (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload    TEXT NOT NULL
);

-- 整定批次：不可变，request_id 为客户端幂等键（同内容重试回放，异文冲突）
CREATE TABLE IF NOT EXISTS tuning_batches (
    request_id        TEXT PRIMARY KEY,
    created_at        TEXT NOT NULL,
    canonical_payload TEXT NOT NULL
);

-- 批次在各场景的生效项：追加写入；(场景, 版本) 与 (批次, 场景) 均唯一
CREATE TABLE IF NOT EXISTS tuning_items (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id        TEXT NOT NULL REFERENCES tuning_batches(request_id),
    scenario_id       TEXT NOT NULL,
    expected_revision INTEGER NOT NULL,
    revision          INTEGER NOT NULL,
    multipliers_json  TEXT NOT NULL,
    UNIQUE(request_id, scenario_id),
    UNIQUE(scenario_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_tuning_items_scenario
    ON tuning_items(scenario_id, revision);
"""


class TuningConflict(Exception):
    """乐观锁/幂等异文冲突（API 层映射为 HTTP 409）。"""


class ScenarioStore:
    """线程安全的场景与整定批次仓库（内容不可修改/删除，仅可追加批次）。"""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # 可重入：写事务提交后在同一持锁段内回放读取（如 _fetch_batch）
        self._lock = threading.RLock()
        # autocommit：写事务由代码显式 BEGIN IMMEDIATE 控制
        self._conn = sqlite3.connect(
            self.db_path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        # 跨进程争用时等待而不是立即报 locked
        self._conn.execute(f"PRAGMA busy_timeout={5000}")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # 场景
    # ------------------------------------------------------------------
    def create(self, payload: dict) -> dict:
        """保存新场景，返回完整记录（含服务端生成的 id 与创建时间）。"""
        record = {
            "id": uuid.uuid4().hex,
            "created_at": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO scenarios (id, name, created_at, payload) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        record["id"],
                        payload["name"],
                        record["created_at"],
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return record

    def get(self, scenario_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, created_at, payload FROM scenarios WHERE id = ?",
                (scenario_id,),
            ).fetchone()
        if row is None:
            return None
        return {"id": row["id"], "created_at": row["created_at"],
                **json.loads(row["payload"])}

    def list(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, created_at, payload FROM scenarios "
                "ORDER BY created_at, id"
            ).fetchall()
        out = []
        for row in rows:
            payload = json.loads(row["payload"])
            out.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "created_at": row["created_at"],
                    "branch_count": len(payload.get("branches", [])),
                    "curve_count": len(payload.get("curves", [])),
                    "fault_count": len(payload.get("faults", [])),
                }
            )
        return out

    # ------------------------------------------------------------------
    # 整定批次
    # ------------------------------------------------------------------
    def publish_tuning_batch(
        self,
        request_id: str,
        canonical_payload: str,
        items: list[dict],
    ) -> tuple[dict, bool]:
        """在单个写事务内核对版本并追加不可变批次，原子切换各场景生效倍率。

        items 已按场景 ID 排序，每项形如
        {"scenario_id", "expected_revision", "multipliers"(dict)}。

        返回 (batch_row, replayed)；batch_row 含 request_id/created_at/items，
        items 附带 revision 与 multipliers。冲突抛出 TuningConflict（已回滚，
        无任何部分写入）。
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            committed = False
            try:
                row = self._conn.execute(
                    "SELECT request_id, created_at, canonical_payload "
                    "FROM tuning_batches WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                if row is not None:
                    # 幂等回放先于版本检查：同文回放，异文 409
                    if row["canonical_payload"] != canonical_payload:
                        raise TuningConflict(
                            f"request_id {request_id!r} 已用于内容不同的请求"
                        )
                    self._conn.execute("COMMIT")
                    committed = True
                    return self._fetch_batch(request_id), True

                created_at = datetime.now(timezone.utc).isoformat()
                prepared = []
                for item in items:
                    cur = self._conn.execute(
                        "SELECT COALESCE(MAX(revision), 0) AS rev "
                        "FROM tuning_items WHERE scenario_id = ?",
                        (item["scenario_id"],),
                    ).fetchone()
                    current_revision = cur["rev"]
                    if current_revision != item["expected_revision"]:
                        raise TuningConflict(
                            f"场景 {item['scenario_id']!r} 的当前整定版本为 "
                            f"{current_revision}，与期望版本 "
                            f"{item['expected_revision']} 不一致"
                        )
                    prepared.append((item, current_revision + 1))

                try:
                    self._conn.execute(
                        "INSERT INTO tuning_batches "
                        "(request_id, created_at, canonical_payload) "
                        "VALUES (?, ?, ?)",
                        (request_id, created_at, canonical_payload),
                    )
                    for item, revision in prepared:
                        self._conn.execute(
                            "INSERT INTO tuning_items "
                            "(request_id, scenario_id, expected_revision, revision, "
                            " multipliers_json) VALUES (?, ?, ?, ?, ?)",
                            (
                                request_id,
                                item["scenario_id"],
                                item["expected_revision"],
                                revision,
                                json.dumps(
                                    item["multipliers"],
                                    ensure_ascii=False,
                                    sort_keys=True,
                                ),
                            ),
                        )
                except sqlite3.IntegrityError as exc:
                    # 跨进程竞态下的兜底：request_id 唯一键或 (场景,版本) 唯一键
                    raise TuningConflict(
                        f"批次 {request_id!r} 与并发提交冲突: {exc}"
                    ) from exc
                self._conn.execute("COMMIT")
                committed = True
                return self._fetch_batch(request_id), False
            finally:
                if not committed:
                    self._conn.execute("ROLLBACK")

    def _fetch_batch(self, request_id: str) -> dict:
        """读取一个批次（含各场景项，按场景 ID 排序）。"""
        with self._lock:
            batch = self._conn.execute(
                "SELECT request_id, created_at FROM tuning_batches "
                "WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if batch is None:
                return None
            item_rows = self._conn.execute(
                "SELECT scenario_id, expected_revision, revision, multipliers_json "
                "FROM tuning_items WHERE request_id = ? ORDER BY scenario_id",
                (request_id,),
            ).fetchall()
        return {
            "request_id": batch["request_id"],
            "created_at": batch["created_at"],
            "items": [
                {
                    "scenario_id": r["scenario_id"],
                    "expected_revision": r["expected_revision"],
                    "revision": r["revision"],
                    "multipliers": json.loads(r["multipliers_json"]),
                }
                for r in item_rows
            ],
        }

    def get_tuning_batch(self, request_id: str) -> dict | None:
        return self._fetch_batch(request_id)

    def list_tuning_batches(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT b.request_id, b.created_at, "
                "       (SELECT COUNT(*) FROM tuning_items i "
                "        WHERE i.request_id = b.request_id) AS scenario_count "
                "FROM tuning_batches b ORDER BY b.created_at, b.request_id"
            ).fetchall()
        return [
            {
                "request_id": r["request_id"],
                "created_at": r["created_at"],
                "scenario_count": r["scenario_count"],
            }
            for r in rows
        ]

    def get_scenario_revision(self, scenario_id: str, revision: int) -> dict | None:
        """读取场景在指定整定版本下的生效项；版本 0 返回基线描述。

        返回 {"revision", "batch_request_id", "created_at", "multipliers"}；
        场景不存在或版本不存在返回 None。
        """
        if revision == 0:
            with self._lock:
                exists = self._conn.execute(
                    "SELECT 1 FROM scenarios WHERE id = ?", (scenario_id,)
                ).fetchone()
            if exists is None:
                return None
            return {
                "revision": 0,
                "batch_request_id": None,
                "created_at": None,
                "multipliers": {},
            }
        with self._lock:
            row = self._conn.execute(
                "SELECT i.revision, i.request_id, b.created_at, i.multipliers_json "
                "FROM tuning_items i JOIN tuning_batches b "
                "  ON b.request_id = i.request_id "
                "WHERE i.scenario_id = ? AND i.revision = ?",
                (scenario_id, revision),
            ).fetchone()
        if row is None:
            return None
        return {
            "revision": row["revision"],
            "batch_request_id": row["request_id"],
            "created_at": row["created_at"],
            "multipliers": json.loads(row["multipliers_json"]),
        }

    def get_current_scenario_revision(self, scenario_id: str) -> dict | None:
        """读取场景当前（最新）整定版本的生效项；无批次时为版本 0 基线。"""
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM scenarios WHERE id = ?", (scenario_id,)
            ).fetchone()
            if exists is None:
                return None
            row = self._conn.execute(
                "SELECT i.revision, i.request_id, b.created_at, i.multipliers_json "
                "FROM tuning_items i JOIN tuning_batches b "
                "  ON b.request_id = i.request_id "
                "WHERE i.scenario_id = ? "
                "ORDER BY i.revision DESC LIMIT 1",
                (scenario_id,),
            ).fetchone()
        if row is None:
            return {
                "revision": 0,
                "batch_request_id": None,
                "created_at": None,
                "multipliers": {},
            }
        return {
            "revision": row["revision"],
            "batch_request_id": row["request_id"],
            "created_at": row["created_at"],
            "multipliers": json.loads(row["multipliers_json"]),
        }
