"""SQLite 持久化：场景与整定批次均以不可变 JSON 文档存储。

整定发布的并发控制全部落在单个 SQLite 写事务内（BEGIN IMMEDIATE）：
- 同 request_id 先查幂等回放，再核对各场景当前版本；
- 追加不可变批次、写入各场景项、原子推进版本号，要么全部生效要么全部回滚；
- WAL + busy_timeout 保证跨进程争用时写事务串行化，冲突中的一方拿到 409。
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

CREATE TABLE IF NOT EXISTS setting_batches (
    id                 TEXT PRIMARY KEY,
    request_id         TEXT NOT NULL UNIQUE,
    seq                INTEGER NOT NULL UNIQUE,
    created_at         TEXT NOT NULL,
    canonical_request  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS setting_batch_items (
    batch_id          TEXT NOT NULL,
    scenario_id       TEXT NOT NULL,
    position          INTEGER NOT NULL,
    expected_revision INTEGER NOT NULL,
    from_revision     INTEGER NOT NULL,
    to_revision       INTEGER NOT NULL,
    multipliers       TEXT NOT NULL,
    verification      TEXT NOT NULL,
    PRIMARY KEY (batch_id, scenario_id)
);

CREATE TABLE IF NOT EXISTS scenario_revisions (
    scenario_id TEXT PRIMARY KEY,
    revision    INTEGER NOT NULL,
    batch_id    TEXT NOT NULL
);
"""


class RevisionConflict(Exception):
    """写事务内发现 expected_revision 与当前版本不符（映射 HTTP 409）。"""

    def __init__(self, details: list[dict]):
        self.details = details
        super().__init__(json.dumps(details, ensure_ascii=False))


class IdempotencyConflict(Exception):
    """同 request_id 但规范化后请求内容不同（映射 HTTP 409）。"""

    def __init__(self, existing_batch_id: str):
        self.existing_batch_id = existing_batch_id
        super().__init__(f"request_id 已用于不同内容的批次: {existing_batch_id}")


class ScenarioStore:
    """线程安全的仓库（场景与整定批次创建后不可修改/删除）。"""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # 写锁最多等待 10s 而非立刻报 locked（跨进程争用时排队）。
        if self.db_path != ":memory:":
            self._conn.execute("PRAGMA busy_timeout=10000")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    def create(self, payload: dict) -> dict:
        """保存新场景，返回完整记录（含服务端生成的 id 与创建时间）。"""
        record = {
            "id": uuid.uuid4().hex,
            "created_at": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        with self._lock:
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
            self._conn.commit()
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
    # 多场景整定发布
    # ------------------------------------------------------------------
    def _load_batch(self, batch_row: sqlite3.Row, replayed: bool) -> dict | None:
        if batch_row is None:
            return None
        with self._lock:
            item_rows = self._conn.execute(
                "SELECT scenario_id, expected_revision, from_revision, "
                "to_revision, multipliers, verification "
                "FROM setting_batch_items WHERE batch_id = ? ORDER BY position",
                (batch_row["id"],),
            ).fetchall()
        return {
            "id": batch_row["id"],
            "request_id": batch_row["request_id"],
            "seq": batch_row["seq"],
            "created_at": batch_row["created_at"],
            "replayed": replayed,
            "scenarios": [
                {
                    "scenario_id": r["scenario_id"],
                    "expected_revision": r["expected_revision"],
                    "from_revision": r["from_revision"],
                    "to_revision": r["to_revision"],
                    "multipliers": json.loads(r["multipliers"]),
                    "verification": json.loads(r["verification"]),
                }
                for r in item_rows
            ],
        }

    def publish_setting_batch(
        self,
        request_id: str,
        canonical_request: str,
        items: list[dict],
    ) -> dict:
        """在单个写事务内原子发布整定批次。

        items 已由核心引擎核验通过（顺序即规范化后的场景顺序），每项含
        scenario_id / expected_revision / multipliers(BreakerMultiplierOut) /
        verification(VerifyResponseOut)。

        冲突语义（均整体回滚，不部分生效）：
        - 同 request_id + 同规范化内容：回放原批次（replayed=True）；
        - 同 request_id + 异文：抛 IdempotencyConflict（HTTP 409）；
        - 任一 expected_revision != 写事务内读到的当前版本：
          抛 RevisionConflict（HTTP 409）。
        """
        batch_id = uuid.uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = self._conn
            if self.db_path != ":memory:":
                # 惰性切换到 WAL（首次发布时一次生效）：允许读写并发。
                # 不放在构造器，避免仅导入模块/纯读取就改写数据库文件头。
                conn.execute("PRAGMA journal_mode=WAL")
            # autocommit 模式下显式开启立即写事务，跨进程抢同一把写锁。
            conn.isolation_level = None
            committed = False
            try:
                conn.execute("BEGIN IMMEDIATE")

                # 1) 幂等：同 request_id 必须先于版本检查回放
                existing = conn.execute(
                    "SELECT id FROM setting_batches WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                if existing is not None:
                    prior = conn.execute(
                        "SELECT id, request_id, seq, created_at, "
                        "canonical_request FROM setting_batches WHERE id = ?",
                        (existing["id"],),
                    ).fetchone()
                    conn.execute("COMMIT")
                    committed = True
                    if prior["canonical_request"] != canonical_request:
                        raise IdempotencyConflict(prior["id"])
                    return self._load_batch(prior, replayed=True)

                # 2) 同一写事务内核对所有场景当前版本
                conflicts: list[dict] = []
                current_rev: dict[str, int] = {}
                for item in items:
                    sid = item["scenario_id"]
                    row = conn.execute(
                        "SELECT revision FROM scenario_revisions "
                        "WHERE scenario_id = ?",
                        (sid,),
                    ).fetchone()
                    rev = row["revision"] if row is not None else 0
                    current_rev[sid] = rev
                    if item["expected_revision"] != rev:
                        conflicts.append(
                            {
                                "scenario_id": sid,
                                "expected_revision": item["expected_revision"],
                                "current_revision": rev,
                            }
                        )
                if conflicts:
                    raise RevisionConflict(conflicts)

                # 3) 追加不可变批次（seq 由 SQLite 单调分配）
                try:
                    conn.execute(
                        "INSERT INTO setting_batches "
                        "(id, request_id, seq, created_at, canonical_request) "
                        "VALUES (?, ?, (SELECT COALESCE(MAX(seq), 0) + 1 "
                        "FROM setting_batches), ?, ?)",
                        (batch_id, request_id, created_at, canonical_request),
                    )
                except sqlite3.IntegrityError as exc:
                    # 跨进程极端竞态（同 request_id 已由另一事务提交）-> 409
                    raise IdempotencyConflict(request_id) from exc

                # 4) 写入各场景项并原子推进版本
                for pos, item in enumerate(items):
                    sid = item["scenario_id"]
                    rev = current_rev[sid]
                    multipliers = [
                        m.model_dump(mode="json") for m in item["multipliers"]
                    ]
                    verification = item["verification"].model_dump(mode="json")
                    conn.execute(
                        "INSERT INTO setting_batch_items "
                        "(batch_id, scenario_id, position, expected_revision, "
                        "from_revision, to_revision, multipliers, verification) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            batch_id, sid, pos,
                            item["expected_revision"],
                            rev, rev + 1,
                            json.dumps(multipliers, ensure_ascii=False),
                            json.dumps(verification, ensure_ascii=False),
                        ),
                    )
                    conn.execute(
                        "INSERT INTO scenario_revisions (scenario_id, revision, batch_id) "
                        "VALUES (?, ?, ?) "
                        "ON CONFLICT(scenario_id) DO UPDATE SET "
                        "revision = excluded.revision, batch_id = excluded.batch_id",
                        (sid, rev + 1, batch_id),
                    )

                conn.execute("COMMIT")
                committed = True
            finally:
                if not committed:
                    conn.execute("ROLLBACK")
                conn.isolation_level = ""

            row = conn.execute(
                "SELECT id, request_id, seq, created_at, canonical_request "
                "FROM setting_batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
            return self._load_batch(row, replayed=False)

    # ------------------------------------------------------------------
    def list_setting_batches(self) -> list[dict]:
        """按提交顺序列出全部整定批次摘要。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT b.id, b.request_id, b.seq, b.created_at, "
                "COUNT(i.scenario_id) AS scenario_count "
                "FROM setting_batches b "
                "LEFT JOIN setting_batch_items i ON i.batch_id = b.id "
                "GROUP BY b.id ORDER BY b.seq"
            ).fetchall()
        return [
            {
                "id": r["id"],
                "request_id": r["request_id"],
                "seq": r["seq"],
                "created_at": r["created_at"],
                "scenario_count": r["scenario_count"],
            }
            for r in rows
        ]

    def get_setting_batch(self, batch_id: str) -> dict | None:
        """按批次 id 读取完整不可变批次（含各场景冻结的倍率与核验）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, request_id, seq, created_at, canonical_request "
                "FROM setting_batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
        return self._load_batch(row, replayed=False)

    def get_setting_batch_by_request(self, request_id: str) -> dict | None:
        """按 request_id 读取批次（幂等查询用）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, request_id, seq, created_at, canonical_request "
                "FROM setting_batches WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return self._load_batch(row, replayed=False)

    def get_effective_setting(
        self, scenario_id: str, revision: int | None = None
    ) -> dict | None:
        """读取单场景在指定版本的生效倍率与同源核验；单次读取绝不混合版本。

        - revision=None：读取当前生效版本（从未被覆盖时为修订 0）；
        - revision=0：初始状态（全部倍率 1，按场景原文核验）；
        - revision>0：读取产生该版本的批次中冻结的项；
        - 场景不存在或版本尚未产生 -> None。
        """
        scenario = self.get(scenario_id)
        if scenario is None:
            return None

        with self._lock:
            cur_row = self._conn.execute(
                "SELECT revision, batch_id FROM scenario_revisions "
                "WHERE scenario_id = ?",
                (scenario_id,),
            ).fetchone()
            current = cur_row["revision"] if cur_row is not None else 0

            if revision is None:
                revision = current
            if revision < 0 or revision > current:
                return None

            if revision == 0:
                return {
                    "scenario_id": scenario_id,
                    "revision": 0,
                    "batch_id": None,
                    "_item": None,
                }

            item_row = self._conn.execute(
                "SELECT i.multipliers, i.verification, i.batch_id "
                "FROM setting_batch_items i "
                "JOIN setting_batches b ON b.id = i.batch_id "
                "WHERE i.scenario_id = ? AND i.to_revision = ?",
                (scenario_id, revision),
            ).fetchone()
        if item_row is None:
            return None
        return {
            "scenario_id": scenario_id,
            "revision": revision,
            "batch_id": item_row["batch_id"],
            "_item": {
                "multipliers": json.loads(item_row["multipliers"]),
                "verification": json.loads(item_row["verification"]),
            },
        }

    def list_scenario_revisions(self, scenario_id: str) -> list[dict] | None:
        """列出场景历次生效版本（按版本升序）；场景不存在返回 None。"""
        if self.get(scenario_id) is None:
            return None
        with self._lock:
            rows = self._conn.execute(
                "SELECT batch_id, from_revision, to_revision "
                "FROM setting_batch_items WHERE scenario_id = ? "
                "ORDER BY to_revision",
                (scenario_id,),
            ).fetchall()
        return [
            {
                "revision": r["to_revision"],
                "batch_id": r["batch_id"],
                "from_revision": r["from_revision"],
            }
            for r in rows
        ]
