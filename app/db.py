"""SQLite 持久化：场景以不可变 JSON 文档存储。"""
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
"""


class ScenarioStore:
    """线程安全的场景仓库（场景创建后不可修改/删除）。"""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(_SCHEMA)
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
