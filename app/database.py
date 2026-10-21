import json
import sqlite3
import time
from contextlib import contextmanager
from uuid import uuid4

from app.config import Settings
from app.models import NotificationCreate


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(
            str(self.settings.db_path), timeout=self.settings.busy_timeout_ms / 1000
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        finally:
            connection.close()

    def initialize(self):
        self.settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            with connection:
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS notifications (
                        id TEXT PRIMARY KEY,
                        url TEXT NOT NULL,
                        method TEXT NOT NULL CHECK (method = 'POST'),
                        headers TEXT NOT NULL,
                        body TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (status IN
                            ('PENDING', 'PROCESSING', 'SUCCESS', 'FAILED')),
                        attempt_count INTEGER NOT NULL DEFAULT 0
                            CHECK (attempt_count BETWEEN 0 AND 5),
                        next_retry_at INTEGER,
                        lease_until INTEGER,
                        lease_token TEXT,
                        last_error TEXT,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS notifications_pending
                        ON notifications(status, next_retry_at);
                    CREATE INDEX IF NOT EXISTS notifications_leases
                        ON notifications(status, lease_until);
                """)

    def create(self, request: NotificationCreate):
        task_id = str(uuid4())
        now = int(time.time())
        with self.connect() as connection:
            with connection:
                connection.execute("""
                    INSERT INTO notifications
                    (id, url, method, headers, body, status, next_retry_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
                """, (task_id, request.url, request.method,
                      json.dumps(request.headers, allow_nan=False),
                      json.dumps(request.body, ensure_ascii=True, allow_nan=False,
                                 separators=(",", ":")), now, now, now))
        return {"id": task_id, "status": "PENDING"}

    def get(self, task_id: str):
        with self.connect() as connection:
            row = connection.execute("""
                SELECT id, status, attempt_count, last_error, next_retry_at,
                       created_at, updated_at
                FROM notifications WHERE id = ?
            """, (task_id,)).fetchone()
        return dict(row) if row else None

    def claim(self, clock=time.time, lease_seconds=60):
        with self.connect() as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                now = int(clock())
                connection.execute("""
                    UPDATE notifications SET status = 'FAILED',
                        last_error = 'lease expired after max attempts',
                        next_retry_at = NULL, lease_until = NULL, lease_token = NULL,
                        updated_at = ?
                    WHERE status = 'PROCESSING' AND lease_until <= ? AND attempt_count >= 5
                """, (now, now))
                eligible = """attempt_count < 5 AND (
                    (status = 'PENDING' AND next_retry_at <= ?) OR
                    (status = 'PROCESSING' AND lease_until <= ?))"""
                row = connection.execute(
                    "SELECT * FROM notifications WHERE " + eligible +
                    " ORDER BY created_at, id LIMIT 1", (now, now)
                ).fetchone()
                if row is None:
                    return None
                token = str(uuid4())
                changed = connection.execute("""
                    UPDATE notifications SET status = 'PROCESSING',
                        attempt_count = attempt_count + 1, lease_until = ?,
                        lease_token = ?, next_retry_at = NULL, updated_at = ?
                    WHERE id = ? AND """ + eligible,
                    (now + lease_seconds, token, now, row["id"], now, now)
                ).rowcount
                if not changed:
                    return None
                task = dict(connection.execute(
                    "SELECT * FROM notifications WHERE id = ?", (row["id"],)
                ).fetchone())
        return task

    def finish(self, task, status, error=None, delay=None, clock=time.time):
        if status not in {"PENDING", "SUCCESS", "FAILED"}:
            raise ValueError("Invalid result status")
        if status == "PENDING" and delay is None:
            raise ValueError("Retry requires a delay")
        with self.connect() as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                now = int(clock())
                changed = connection.execute("""
                    UPDATE notifications SET status = ?, last_error = ?,
                        next_retry_at = ?, lease_until = NULL, lease_token = NULL,
                        updated_at = ?
                    WHERE id = ? AND status = 'PROCESSING'
                        AND lease_token = ? AND lease_until > ?
                """, (status, error, now + delay if status == "PENDING" else None,
                      now, task["id"], task["lease_token"], now)).rowcount
        return changed == 1
