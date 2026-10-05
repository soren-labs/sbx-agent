"""Durable local operation journal and bounded evidence spool (SQLite WAL, FULL sync).

Lives in a protected runtime path outside the Worktree. The runtime epoch is
kept across restarts with a preserved journal; a fresh journal gets a new epoch.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class SpoolPressure(Exception):
    pass


class Journal:
    def __init__(self, path: Path, *, max_unacked: int = 20000, reserve: int = 64) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.lock = threading.RLock()
        self.max_unacked = max_unacked
        self.reserve = reserve
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS operations (
                  operation_id TEXT PRIMARY KEY, kind TEXT NOT NULL, request_digest TEXT NOT NULL,
                  session_id TEXT, lease_generation INTEGER, status TEXT NOT NULL,
                  result TEXT, pid INTEGER, created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS spool (
                  local_seq INTEGER PRIMARY KEY AUTOINCREMENT, execution_id TEXT, type TEXT NOT NULL,
                  payload TEXT NOT NULL, observed_at REAL NOT NULL);
                """
            )
        epoch = self.meta("runtime_epoch")
        self.recovered = existed and epoch is not None
        if epoch is None:
            epoch = "rte_" + uuid.uuid4().hex
            self.set_meta("runtime_epoch", epoch)
            self.set_meta("acked", "0")
        self.epoch = epoch

    def meta(self, key: str) -> str | None:
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # -- operations ------------------------------------------------------------------
    def op_get(self, operation_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT operation_id, kind, request_digest, session_id, lease_generation, status, result, pid"
                " FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        keys = (
            "operation_id",
            "kind",
            "request_digest",
            "session_id",
            "lease_generation",
            "status",
            "result",
            "pid",
        )
        out = dict(zip(keys, row, strict=True))
        out["result"] = json.loads(out["result"]) if out["result"] else None
        return out

    def op_insert(
        self, operation_id: str, kind: str, digest: str, session_id: str | None, generation: int
    ) -> None:
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT INTO operations (operation_id, kind, request_digest, session_id, lease_generation, status,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'accepted', ?, ?)",
                (operation_id, kind, digest, session_id, generation, now, now),
            )

    def op_update(
        self,
        operation_id: str,
        *,
        status: str | None = None,
        result: Any = None,
        pid: int | None = None,
    ) -> None:
        sets, params = ["updated_at = ?"], [time.time()]
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if result is not None:
            sets.append("result = ?")
            params.append(json.dumps(result))
        if pid is not None:
            sets.append("pid = ?")
            params.append(pid)
        with self.lock:
            self.conn.execute(
                f"UPDATE operations SET {', '.join(sets)} WHERE operation_id = ?",
                (*params, operation_id),
            )

    def ops_open(self) -> list[dict[str, Any]]:
        with self.lock:
            ids = [
                r[0]
                for r in self.conn.execute(
                    "SELECT operation_id FROM operations WHERE status IN ('accepted', 'starting', 'started')"
                )
            ]
        return [op for op in (self.op_get(i) for i in ids) if op]

    # -- spool --------------------------------------------------------------------
    def acked(self) -> int:
        return int(self.meta("acked") or 0)

    def last_seq(self) -> int:
        with self.lock:
            row = self.conn.execute("SELECT max(local_seq) FROM spool").fetchone()
        return int(row[0] or 0)

    def unacked(self) -> int:
        return self.last_seq() - self.acked()

    def append(
        self,
        execution_id: str | None,
        type: str,
        payload: dict[str, Any],
        *,
        terminal: bool = False,
    ) -> int:
        with self.lock:
            limit = self.max_unacked + (self.reserve if terminal else 0)
            if self.unacked() >= limit:
                raise SpoolPressure(f"unacknowledged evidence at limit {limit}")
            cur = self.conn.execute(
                "INSERT INTO spool (execution_id, type, payload, observed_at) VALUES (?, ?, ?, ?)",
                (execution_id, type, json.dumps(payload), time.time()),
            )
            return int(cur.lastrowid)

    def after(self, after: int, limit: int) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT local_seq, execution_id, type, payload, observed_at FROM spool WHERE local_seq > ? ORDER BY local_seq LIMIT ?",
                (after, limit),
            ).fetchall()
        return [
            {
                "local_seq": r[0],
                "execution_id": r[1],
                "type": r[2],
                "payload": json.loads(r[3]),
                "observed_at": r[4],
            }
            for r in rows
        ]

    def ack(self, through: int) -> int:
        with self.lock:
            through = min(int(through), self.last_seq())
            if through > self.acked():
                self.set_meta("acked", str(through))
            return self.acked()
