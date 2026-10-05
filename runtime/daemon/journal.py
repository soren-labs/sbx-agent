import json
import sqlite3
import threading
from pathlib import Path
from uuid import uuid4

from protocol.runtime import ProtocolError, canonical


class Journal:
    def __init__(self, path: Path, max_spool_bytes=16_000_000):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,digest TEXT,kind TEXT,
           state TEXT,pid INTEGER,result TEXT);
          CREATE TABLE IF NOT EXISTS spool(seq INTEGER PRIMARY KEY AUTOINCREMENT,
           operation_id TEXT,type TEXT,payload TEXT,acked INTEGER DEFAULT 0);""")
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES(?,?)", ("epoch", uuid4().hex))
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES(?,?)", ("generation", "0"))
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES(?,?)", ("fence", "0"))
        self.max_spool_bytes = max_spool_bytes

    @property
    def epoch(self):
        return self.metadata("epoch")

    def metadata(self, key, value=None):
        with self.lock:
            if value is not None:
                self.db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (key, str(value)))
            row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None

    def accept(self, frame):
        frame.validate_body()
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                previous = self.get(frame.operation_id)
                if previous:
                    if (
                        previous["digest"] != frame.request_digest
                        or previous["kind"] != frame.operation_kind
                    ):
                        raise ProtocolError("idempotency_conflict")
                    self.db.execute("COMMIT")
                    return False
                if frame.resource_fence < int(self.metadata("fence")):
                    raise ProtocolError("version_conflict")
                self.metadata("fence", frame.resource_fence)
                self.db.execute(
                    "INSERT INTO operations VALUES(?,?,?,?,NULL,NULL)",
                    (frame.operation_id, frame.request_digest, frame.operation_kind, "accepted"),
                )
                self.db.execute("COMMIT")
                return True
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def get(self, operation):
        with self.lock:
            row = self.db.execute("SELECT * FROM operations WHERE id=?", (operation,)).fetchone()
            if not row:
                return None
            value = dict(row)
            value["result"] = json.loads(value["result"]) if value["result"] else None
            return value

    def update(self, operation, state, *, pid=None, result=None):
        with self.lock:
            self.db.execute(
                "UPDATE operations SET state=?,pid=coalesce(?,pid),result=? WHERE id=?",
                (state, pid, canonical(result) if result is not None else None, operation),
            )

    def append(self, operation, kind, payload):
        with self.lock:
            used = self.db.execute(
                "SELECT coalesce(sum(length(payload)),0) FROM spool WHERE acked=0"
            ).fetchone()[0]
            content = canonical(payload)
            if used + len(content.encode()) > self.max_spool_bytes:
                raise ProtocolError("spool_pressure")
            row = self.db.execute(
                "INSERT INTO spool(operation_id,type,payload) VALUES(?,?,?)",
                (operation, kind, content),
            )
            return row.lastrowid

    def events(self, after=0, limit=200):
        with self.lock:
            return [
                {
                    "local_seq": row["seq"],
                    "operation_id": row["operation_id"],
                    "type": row["type"],
                    "payload": json.loads(row["payload"]),
                }
                for row in self.db.execute(
                    "SELECT * FROM spool WHERE seq>? ORDER BY seq LIMIT ?", (after, min(limit, 500))
                ).fetchall()
            ]

    def ack(self, seq):
        with self.lock:
            self.db.execute("UPDATE spool SET acked=1 WHERE seq<=?", (seq,))
            self.db.execute("DELETE FROM spool WHERE acked=1")

    @property
    def watermark(self):
        with self.lock:
            row = self.db.execute("SELECT seq FROM sqlite_sequence WHERE name='spool'").fetchone()
            return row[0] if row else 0
