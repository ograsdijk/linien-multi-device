"""Durable local spool for lock-acquisition records awaiting PostgreSQL.

Every record is written here first (SQLite, WAL) and only then pushed to
PostgreSQL by the writer thread, so a database outage, a full in-memory queue or
a gateway restart no longer loses records. One row per lock_acquisition_id: a
later update of the same attempt (e.g. its verification outcome) replaces the
payload and bumps ``revision``, and the writer only marks a record persisted if
the revision it sent is still the latest.

States
------
pending    waiting to be sent (new, updated, or recovered after a restart)
retrying   the last attempt failed with a temporary error; next try at
           ``next_attempt_at`` (exponential backoff)
persisted  PostgreSQL acknowledged the latest revision
failed     PostgreSQL rejected the record permanently (data/constraint
           error); kept for inspection, not retried

Retention: persisted records are pruned oldest-first past ``max_records`` or
``max_age_s``. Unsent records are never pruned for age, and are only dropped
(oldest first, with a warning) if unsent records alone exceed ``max_records``.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STATES = ("pending", "retrying", "persisted", "failed")
DEFAULT_MAX_RECORDS = 10_000
DEFAULT_MAX_AGE_S = 30 * 24 * 3600.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    lock_acquisition_id TEXT PRIMARY KEY,
    device_key TEXT,
    lock_source TEXT,
    attempt_started_at REAL,
    payload TEXT NOT NULL,
    state TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    persisted_at REAL
);
CREATE INDEX IF NOT EXISTS idx_records_due ON records (state, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_records_device ON records (device_key, attempt_started_at);
"""


class LockRecordSpool:
    def __init__(
        self,
        path: Path,
        *,
        max_records: int = DEFAULT_MAX_RECORDS,
        max_age_s: float = DEFAULT_MAX_AGE_S,
    ) -> None:
        self.path = Path(path)
        self.max_records = max(1, int(max_records))
        self.max_age_s = max(0.0, float(max_age_s))
        self._lock = threading.Lock()
        self.dropped_unsent = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        # NORMAL in WAL mode survives a process crash (only an OS crash or
        # power loss can drop the last commits) and keeps writes fast.
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --------------------------------------------------------------- writes

    def put(self, row: dict[str, Any]) -> None:
        """Insert or replace one record's payload; it becomes pending again."""
        acquisition_id = str(row["lock_acquisition_id"])
        payload = json.dumps(row)
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO records (
                    lock_acquisition_id, device_key, lock_source,
                    attempt_started_at, payload, state, revision,
                    next_attempt_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', 1, 0, ?, ?)
                ON CONFLICT (lock_acquisition_id) DO UPDATE SET
                    payload = excluded.payload,
                    device_key = excluded.device_key,
                    lock_source = excluded.lock_source,
                    attempt_started_at = excluded.attempt_started_at,
                    state = 'pending',
                    revision = records.revision + 1,
                    next_attempt_at = 0,
                    last_error = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    acquisition_id,
                    row.get("device_key"),
                    row.get("lock_source"),
                    row.get("attempt_started_at"),
                    payload,
                    now,
                    now,
                ),
            )
            self._prune_locked(now)

    def mark_persisted(self, acquisition_id: str, revision: int) -> bool:
        """Persisted only if no newer revision arrived while it was in flight."""
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE records SET state = 'persisted', persisted_at = ?,
                    last_error = NULL, updated_at = ?
                WHERE lock_acquisition_id = ? AND revision = ?
                """,
                (time.time(), time.time(), acquisition_id, int(revision)),
            )
            return cur.rowcount == 1

    def mark_retry(
        self, acquisition_id: str, revision: int, error: str, delay_s: float
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                UPDATE records SET state = 'retrying', attempts = attempts + 1,
                    next_attempt_at = ?, last_error = ?, updated_at = ?
                WHERE lock_acquisition_id = ? AND revision = ?
                """,
                (now + max(0.0, delay_s), error, now, acquisition_id, int(revision)),
            )

    def mark_failed(self, acquisition_id: str, revision: int, error: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE records SET state = 'failed', attempts = attempts + 1,
                    last_error = ?, updated_at = ?
                WHERE lock_acquisition_id = ? AND revision = ?
                """,
                (error, time.time(), acquisition_id, int(revision)),
            )

    def _prune_locked(self, now: float) -> None:
        if self.max_age_s > 0:
            self._conn.execute(
                "DELETE FROM records WHERE state = 'persisted' AND updated_at < ?",
                (now - self.max_age_s,),
            )
        total = self._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        excess = total - self.max_records
        if excess <= 0:
            return
        self._conn.execute(
            """
            DELETE FROM records WHERE lock_acquisition_id IN (
                SELECT lock_acquisition_id FROM records
                WHERE state = 'persisted' ORDER BY updated_at LIMIT ?
            )
            """,
            (excess,),
        )
        total = self._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        excess = total - self.max_records
        if excess <= 0:
            return
        # Only unsent (or permanently failed) records are left. Dropping one is
        # losing data, so it is loud.
        doomed = self._conn.execute(
            """
            SELECT lock_acquisition_id, state FROM records
            ORDER BY CASE state WHEN 'failed' THEN 0 ELSE 1 END, created_at
            LIMIT ?
            """,
            (excess,),
        ).fetchall()
        for acquisition_id, state in doomed:
            logger.warning(
                "Lock-record spool full (%d records): dropping %s record %s",
                self.max_records,
                state,
                acquisition_id,
            )
            self._conn.execute(
                "DELETE FROM records WHERE lock_acquisition_id = ?", (acquisition_id,)
            )
            if state != "failed":
                self.dropped_unsent += 1

    # ---------------------------------------------------------------- reads

    def due(self, limit: int = 20, now: float | None = None) -> list[dict[str, Any]]:
        """Records to send now, oldest change first."""
        ts = time.time() if now is None else float(now)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT lock_acquisition_id, payload, revision, attempts
                FROM records
                WHERE state IN ('pending', 'retrying') AND next_attempt_at <= ?
                ORDER BY updated_at
                LIMIT ?
                """,
                (ts, int(limit)),
            ).fetchall()
        return [
            {
                "lock_acquisition_id": acquisition_id,
                "row": json.loads(payload),
                "revision": revision,
                "attempts": attempts,
            }
            for acquisition_id, payload, revision, attempts in rows
        ]

    def next_due_at(self) -> float | None:
        with self._lock:
            value = self._conn.execute(
                "SELECT MIN(next_attempt_at) FROM records "
                "WHERE state IN ('pending', 'retrying')"
            ).fetchone()[0]
        return None if value is None else float(value)

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) FROM records GROUP BY state"
            ).fetchall()
        counts = {state: 0 for state in STATES}
        counts.update({state: int(n) for state, n in rows})
        return counts

    def query(
        self,
        *,
        device_key: str | None = None,
        since: float | None = None,
        until: float | None = None,
        lock_source: str | None = None,
        states: tuple[str, ...] | None = None,
        predicate: Any = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Records newest attempt first, filtered on the indexed columns in
        SQL and on anything else by ``predicate(row)``."""
        clauses: list[str] = []
        params: list[Any] = []
        if device_key is not None:
            clauses.append("device_key = ?")
            params.append(device_key)
        if since is not None:
            clauses.append("attempt_started_at >= ?")
            params.append(float(since))
        if until is not None:
            clauses.append("attempt_started_at < ?")
            params.append(float(until))
        if lock_source is not None:
            clauses.append("lock_source = ?")
            params.append(lock_source)
        if states:
            clauses.append(f"state IN ({', '.join('?' for _ in states)})")
            params.extend(states)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            cursor = self._conn.execute(
                f"SELECT payload, state FROM records {where} "
                "ORDER BY attempt_started_at DESC",
                params,
            )
            matched: list[dict[str, Any]] = []
            skipped = 0
            for payload, state in cursor:
                row = json.loads(payload)
                if predicate is not None and not predicate(row):
                    continue
                if skipped < offset:
                    skipped += 1
                    continue
                matched.append({"row": row, "state": state})
                if len(matched) >= limit:
                    break
            cursor.close()
        return matched

    def get(self, acquisition_id: str) -> dict[str, Any] | None:
        with self._lock:
            found = self._conn.execute(
                "SELECT payload, state, revision, attempts, last_error, persisted_at "
                "FROM records WHERE lock_acquisition_id = ?",
                (str(acquisition_id),),
            ).fetchone()
        if found is None:
            return None
        payload, state, revision, attempts, last_error, persisted_at = found
        return {
            "row": json.loads(payload),
            "state": state,
            "revision": revision,
            "attempts": attempts,
            "last_error": last_error,
            "persisted_at": persisted_at,
        }
