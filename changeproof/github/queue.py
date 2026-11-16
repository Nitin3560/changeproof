from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3


class DeliveryConflict(ValueError):
    pass


class JobStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued',
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    id TEXT PRIMARY KEY,
                    event TEXT NOT NULL,
                    body_sha256 TEXT NOT NULL,
                    job_id TEXT REFERENCES jobs(id),
                    outcome TEXT NOT NULL,
                    reason TEXT,
                    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                );
            """)

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def accept(self, delivery_id, event, digest, job=None, reason=None):
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
            if existing:
                if existing["body_sha256"] != digest or existing["event"] != event:
                    raise DeliveryConflict("Delivery ID was reused for different content")
                return {"outcome": existing["outcome"], "job_id": existing["job_id"],
                        "reason": existing["reason"], "duplicate": True}
            job_id, duplicate = None, False
            if job is not None:
                encoded = json.dumps(job, sort_keys=True, separators=(",", ":"))
                job_id = hashlib.sha256(encoded.encode()).hexdigest()
                cursor = conn.execute("INSERT OR IGNORE INTO jobs (id, payload) VALUES (?, ?)",
                                      (job_id, encoded))
                duplicate = cursor.rowcount == 0
            outcome = "queued" if job is not None else "ignored"
            conn.execute("""INSERT INTO deliveries (id, event, body_sha256, job_id, outcome, reason)
                            VALUES (?, ?, ?, ?, ?, ?)""",
                         (delivery_id, event, digest, job_id, outcome, reason))
            return {"outcome": outcome, "job_id": job_id, "reason": reason, "duplicate": duplicate}


def list_jobs(path, limit=50):
    if not 1 <= limit <= 100:
        raise ValueError("Job limit must be between 1 and 100")
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC, id LIMIT ?", (limit,)).fetchall()
        return [{"id": row["id"], "state": row["state"], "created_at": row["created_at"],
                 **json.loads(row["payload"])} for row in rows]
