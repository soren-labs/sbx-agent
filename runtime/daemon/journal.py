"""On-disk transactional local journal (RFC 167 §03).

SQLite WAL with ``synchronous=FULL`` commits in a protected runtime path
outside the Worktree. Durable intent precedes process launch; terminal
evidence persists after stop.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
    operation_id    TEXT PRIMARY KEY,
    operation_kind  TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    request_digest  TEXT NOT NULL,
    envelope        TEXT NOT NULL,
    state           TEXT NOT NULL,          -- accepted|starting|started|terminal states
    result          TEXT,                   -- json of result payload/error
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS spool (
    local_seq   INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,              -- observation|operation_result|diagnostic
    payload     TEXT NOT NULL,
    committed   INTEGER NOT NULL DEFAULT 0  -- control-plane committed ack
);
"""


class Journal:
    """Single-writer durable journal. All methods fsync via sqlite commit."""

    def __init__(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.RLock()
        if self.meta_get("runtime_epoch") is None:
            import uuid

            self.meta_set("runtime_epoch", uuid.uuid4().hex)

    # -- meta ---------------------------------------------------------

    def meta_get(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def meta_set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    @property
    def runtime_epoch(self) -> str:
        epoch = self.meta_get("runtime_epoch")
        assert epoch is not None
        return epoch

    def reset_epoch(self) -> str:
        """A new empty incarnation gets a new epoch (RFC 167 §03)."""
        import uuid

        epoch = uuid.uuid4().hex
        self.meta_set("runtime_epoch", epoch)
        return epoch

    # -- operations ---------------------------------------------------

    def accept_operation(
        self, *, operation_id: str, kind: str, session_id: str, request_digest: str, envelope: dict
    ) -> tuple[str, dict | None]:
        """Durable acceptance, idempotent.

        Returns ("accepted", None) on first record; ("replay", prior row dict)
        for same id+body; ("conflict", prior row dict) for same id with a
        different body.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT request_digest, state, result, envelope FROM operations "
                "WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is not None:
                prior = {
                    "state": row[1],
                    "result": json.loads(row[2]) if row[2] else None,
                    "envelope": json.loads(row[3]),
                }
                if row[0] == request_digest:
                    return "replay", prior
                return "conflict", prior
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO operations(operation_id, operation_kind, session_id, "
                    "request_digest, envelope, state, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        operation_id,
                        kind,
                        session_id,
                        request_digest,
                        json.dumps(envelope, separators=(",", ":")),
                        "accepted",
                        time.time(),
                        time.time(),
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return "accepted", None

    def update_operation(self, operation_id: str, state: str, result: dict | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE operations SET state=?, result=COALESCE(?, result), updated_at=? "
                "WHERE operation_id=?",
                (
                    state,
                    json.dumps(result) if result is not None else None,
                    time.time(),
                    operation_id,
                ),
            )

    def get_operation(self, operation_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT operation_id, operation_kind, session_id, state, result, envelope, "
            "created_at, updated_at FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "operation_id": row[0],
            "operation_kind": row[1],
            "session_id": row[2],
            "state": row[3],
            "result": json.loads(row[4]) if row[4] else None,
            "envelope": json.loads(row[5]),
            "created_at": row[6],
            "updated_at": row[7],
        }

    def nonterminal_operations(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT operation_id FROM operations WHERE state IN ('accepted','starting','started')"
        ).fetchall()
        return [self.get_operation(r[0]) for r in rows]  # type: ignore[misc]

    def all_operation_ids(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT operation_id FROM operations ORDER BY created_at"
        ).fetchall()
        return [r[0] for r in rows]

    # -- spool ---------------------------------------------------------

    def spool_append(self, kind: str, payload: dict) -> int:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "INSERT INTO spool(kind, payload) VALUES(?, ?)",
                    (kind, json.dumps(payload, separators=(",", ":"))),
                )
                seq = int(cur.lastrowid)
                self._conn.execute("COMMIT")
                return seq
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def spool_uncommitted(self, limit: int = 256) -> list[tuple[int, str, dict]]:
        rows = self._conn.execute(
            "SELECT local_seq, kind, payload FROM spool WHERE committed=0 ORDER BY local_seq "
            "LIMIT ?",
            (limit,),
        ).fetchall()
        return [(int(r[0]), str(r[1]), json.loads(r[2])) for r in rows]

    def spool_max_seq(self) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(local_seq),0) FROM spool").fetchone()
        return int(row[0])

    def spool_ack(self, through_seq: int) -> int:
        """Mark ≤ ``through_seq`` committed; returns rows drained. The ack
        watermark is monotonic in meta — draining rows must never forget
        what was already committed upstream."""
        with self._lock:
            self.meta_set(
                "spool_ack_watermark",
                str(max(int(self.meta_get("spool_ack_watermark") or 0), int(through_seq))),
            )
            cur = self._conn.execute(
                "DELETE FROM spool WHERE committed=0 AND local_seq <= ?", (through_seq,)
            )
            return int(cur.rowcount)

    def committed_watermark(self) -> int:
        """Highest seq durably committed upstream (monotonic)."""
        meta = int(self.meta_get("spool_ack_watermark") or 0)
        row = self._conn.execute("SELECT MIN(local_seq) FROM spool WHERE committed=0").fetchone()
        if row[0] is not None:
            return max(meta, int(row[0]) - 1)
        return max(meta, self.spool_max_seq())

    def close(self) -> None:
        with self._lock:
            self._conn.close()
