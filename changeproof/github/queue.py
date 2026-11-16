from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4


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

    def migrate_worker(self):
        columns = {"attempts": "INTEGER NOT NULL DEFAULT 0", "lease_token": "TEXT",
                   "lease_until": "REAL", "next_attempt": "REAL NOT NULL DEFAULT 0",
                   "check_id": "INTEGER", "report_path": "TEXT", "conclusion": "TEXT", "last_error": "TEXT"}
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
            for name, definition in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")

    def claim(self, *, lease_seconds=600, max_attempts=3, now=None):
        self.migrate_worker()
        now = time.time() if now is None else now
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("""SELECT * FROM jobs WHERE
                ((state = 'queued' AND attempts < ? AND next_attempt <= ?) OR (state = 'running' AND lease_until <= ?))
                ORDER BY created_at, id LIMIT 1""", (max_attempts, now, now)).fetchone()
            if row is None:
                return None
            token = uuid4().hex
            conn.execute("UPDATE jobs SET state='running', attempts=attempts+1, lease_token=?, lease_until=? WHERE id=?",
                         (token, now + lease_seconds, row["id"]))
            return {**dict(row), "attempts": row["attempts"] + 1, "lease_token": token,
                    "lease_until": now + lease_seconds, "job": json.loads(row["payload"])}

    def update_claim(self, claim, *, now=None, **values):
        permitted = {"check_id", "report_path", "conclusion", "last_error", "state", "next_attempt"}
        if not values or not set(values) <= permitted:
            raise ValueError("Invalid job update")
        now = time.time() if now is None else now
        assignments = ", ".join(f"{name}=?" for name in values)
        with closing(self.connect()) as conn, conn:
            cursor = conn.execute(f"UPDATE jobs SET {assignments} WHERE id=? AND lease_token=? "
                                  "AND state='running' AND lease_until > ?",
                                  (*values.values(), claim["id"], claim["lease_token"], now))
            if cursor.rowcount != 1:
                raise RuntimeError("Worker lease was lost")

    def retry(self, job_id):
        self.migrate_worker()
        with closing(self.connect()) as conn, conn:
            cursor = conn.execute("""UPDATE jobs SET state='queued', attempts=0, lease_token=NULL,
                lease_until=NULL, next_attempt=0, last_error=NULL, conclusion=NULL WHERE id=? AND
                (state IN ('failed', 'skipped') OR (state='completed' AND conclusion='action_required'))""", (job_id,))
            return cursor.rowcount == 1

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
                 **json.loads(row["payload"]),
                 **{key: row[key] for key in ("attempts", "lease_until", "check_id", "report_path", "conclusion", "last_error")
                    if key in row.keys()}} for row in rows]
