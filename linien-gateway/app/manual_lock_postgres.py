from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from linien_common.config import USER_DATA_PATH

try:
    import psycopg  # type: ignore
except Exception:  # pragma: no cover - exercised through runtime status/message
    psycopg = None  # type: ignore

LOCK_RESULT_POSTGRES_CONFIG_PATH = USER_DATA_PATH / "manual_lock_postgres.json"
# How long to let the writer thread drain queued rows on shutdown before
# giving up (pending lock results are lost past this).
SHUTDOWN_DRAIN_TIMEOUT_S = 5.0
logger = logging.getLogger(__name__)
PostgresEventCallback = Callable[[int, str, str, str, dict[str, Any]], None]
# The guarded-move columns, declared once and rendered into the CREATE, the
# migration and the INSERT below. Two hand-maintained copies of the same list
# had nothing keeping them in step.
APPROACH_COLUMNS: tuple[tuple[str, str], ...] = (
    ("sweep_center_v", "DOUBLE PRECISION"),
    ("sweep_amplitude_v", "DOUBLE PRECISION"),
    ("target_voltage_v", "DOUBLE PRECISION"),
    ("sweep_center_start_v", "DOUBLE PRECISION"),
    ("center_move_v", "DOUBLE PRECISION"),
    ("center_correction_v", "DOUBLE PRECISION"),
    ("center_offset_v", "DOUBLE PRECISION"),
    ("capture_tolerance_v", "DOUBLE PRECISION"),
    ("approach_enabled", "BOOLEAN"),
    ("approach_direct", "BOOLEAN"),
    ("approach_from_below", "BOOLEAN"),
    ("approach_attempts", "INTEGER"),
    ("approach_detail", "JSONB"),
)

# psycopg sends a Python str as PostgreSQL ``text`` (StrDumper.oid = 25), and
# ``text`` does not implicitly coerce to ``jsonb``, so a JSON column needs an
# explicit cast on the placeholder or every insert fails -- quietly, since the
# writer swallows errors into a log event.
_PARAM_CASTS = {"JSONB": "::jsonb"}


def _column_definitions() -> str:
    return "".join(f",\n    {name} {sql}" for name, sql in APPROACH_COLUMNS)


def _add_column_clauses() -> str:
    return ",\n".join(
        f"    ADD COLUMN IF NOT EXISTS {name} {sql}" for name, sql in APPROACH_COLUMNS
    )


def _insert_names() -> str:
    return ",\n".join(f"    {name}" for name, _sql in APPROACH_COLUMNS)


def _insert_values() -> str:
    return ",\n".join(
        f"    %({name})s{_PARAM_CASTS.get(sql, '')}" for name, sql in APPROACH_COLUMNS
    )


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pdh_lock_results (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    laser_name TEXT NOT NULL,
    lock_source TEXT NOT NULL DEFAULT 'manual_lock',
    success BOOLEAN,
    modulation_frequency_hz DOUBLE PRECISION,
    demod_phase_deg DOUBLE PRECISION,
    signal_offset_volts DOUBLE PRECISION,
    modulation_amplitude DOUBLE PRECISION,
    pid_p DOUBLE PRECISION,
    pid_i DOUBLE PRECISION,
    pid_d DOUBLE PRECISION,
    trace_x DOUBLE PRECISION[] NOT NULL,
    trace_y DOUBLE PRECISION[] NOT NULL,
    monitor_trace_y DOUBLE PRECISION[] NOT NULL,
    trace_x_units TEXT NOT NULL DEFAULT 'V',
    trace_y_units TEXT NOT NULL DEFAULT 'V',
    monitor_trace_y_units TEXT NOT NULL DEFAULT 'V'""" + _column_definitions() + """
);
"""
ALTER_TABLE_ADD_LOCK_SOURCE_SQL = """
ALTER TABLE pdh_lock_results
    ADD COLUMN IF NOT EXISTS lock_source TEXT NOT NULL DEFAULT 'manual_lock';
"""
# The approach columns landed after the table did, so existing deployments pick
# them up on the next startup rather than needing a hand-run migration. Same
# idempotent ADD COLUMN IF NOT EXISTS pattern as lock_source above.
# Existing deployments pick the approach columns up on the next startup rather
# than needing a hand-run migration, the same idempotent pattern lock_source uses.
ALTER_TABLE_ADD_APPROACH_SQL = """
ALTER TABLE pdh_lock_results
""" + _add_column_clauses() + """;
"""

CREATE_INDEX_CREATED_SQL = """
CREATE INDEX IF NOT EXISTS idx_pdh_lock_results_created_at
    ON pdh_lock_results (created_at DESC);
"""
CREATE_INDEX_LASER_SQL = """
CREATE INDEX IF NOT EXISTS idx_pdh_lock_results_laser_created_at
    ON pdh_lock_results (laser_name, created_at DESC);
"""
INSERT_SQL = """
INSERT INTO pdh_lock_results (
    laser_name,
    lock_source,
    success,
    modulation_frequency_hz,
    demod_phase_deg,
    signal_offset_volts,
    modulation_amplitude,
    pid_p,
    pid_i,
    pid_d,
    trace_x,
    trace_y,
    monitor_trace_y,
    trace_x_units,
    trace_y_units,
    monitor_trace_y_units,
""" + _insert_names() + """
) VALUES (
    %(laser_name)s,
    %(lock_source)s,
    %(success)s,
    %(modulation_frequency_hz)s,
    %(demod_phase_deg)s,
    %(signal_offset_volts)s,
    %(modulation_amplitude)s,
    %(pid_p)s,
    %(pid_i)s,
    %(pid_d)s,
    %(trace_x)s,
    %(trace_y)s,
    %(monitor_trace_y)s,
    %(trace_x_units)s,
    %(trace_y_units)s,
    %(monitor_trace_y_units)s,
""" + _insert_values() + """
);
"""


@dataclass
class LockResultPostgresConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 5432
    database: str = "experiment_db"
    user: str = "admin"
    password: str = "adminpassword"
    sslmode: str = "prefer"
    connect_timeout_s: float = 3.0


@dataclass
class LockResultPostgresStatus:
    active: bool = False
    last_test_ok: bool | None = None
    last_test_at: float | None = None
    last_write_ok: bool | None = None
    last_write_at: float | None = None
    last_error: str | None = None
    enqueued_count: int = 0
    write_ok_count: int = 0
    write_error_count: int = 0
    dropped_count: int = 0
    queue_size: int = 0


def load_lock_result_postgres_config(
    path: Path = LOCK_RESULT_POSTGRES_CONFIG_PATH,
) -> LockResultPostgresConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return LockResultPostgresConfig()
    except json.JSONDecodeError:
        return LockResultPostgresConfig()
    if not isinstance(raw, dict):
        return LockResultPostgresConfig()
    defaults = asdict(LockResultPostgresConfig())
    merged = {**defaults, **{k: v for k, v in raw.items() if k in defaults}}
    try:
        return LockResultPostgresConfig(**merged)
    except TypeError:
        return LockResultPostgresConfig()


def save_lock_result_postgres_config(
    config: LockResultPostgresConfig, path: Path = LOCK_RESULT_POSTGRES_CONFIG_PATH
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")


class LockResultPostgresService:
    def __init__(
        self,
        *,
        config_path: Path = LOCK_RESULT_POSTGRES_CONFIG_PATH,
        max_queue_size: int = 256,
    ) -> None:
        self._config_path = config_path
        self._config = load_lock_result_postgres_config(config_path)
        self._status = LockResultPostgresStatus(active=False)
        # Whether this process has applied the schema to the configured
        # database yet. _ensure_table only ran from test_connection(), i.e. when
        # someone re-saved the settings in the UI, so an upgraded gateway that
        # nobody touched would INSERT columns that had never been added -- and
        # the failure was swallowed by the best-effort writer, taking manual and
        # auto-relock logging down with it.
        self._schema_ready = False
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max_queue_size)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._event_callback: PostgresEventCallback | None = None

    def set_event_callback(self, callback: PostgresEventCallback | None) -> None:
        self._event_callback = callback

    def start(self) -> None:
        if self._worker_thread and self._worker_thread.is_alive():
            return
        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name="lock-result-postgres-writer",
        )
        self._worker_thread.start()
        with self._lock:
            self._status.active = bool(self._config.enabled and self._status.last_test_ok)
            self._status.queue_size = self._queue.qsize()

    def stop(self) -> None:
        self._stop_event.set()
        if self._worker_thread and self._worker_thread.is_alive():
            # Give the worker time to drain queued rows before exiting; too
            # short a timeout silently drops pending lock results on shutdown.
            self._worker_thread.join(timeout=SHUTDOWN_DRAIN_TIMEOUT_S)
        self._worker_thread = None
        with self._lock:
            self._status.active = False
            self._status.queue_size = self._queue.qsize()

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            status = asdict(self._status)
            status["queue_size"] = self._queue.qsize()
            return {
                "config": asdict(self._config),
                "status": status,
            }

    def update_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            defaults = asdict(LockResultPostgresConfig())
            normalized = {
                key: payload.get(key, defaults[key]) for key in defaults.keys()
            }
            self._config = LockResultPostgresConfig(**normalized)
            self._schema_ready = False
            save_lock_result_postgres_config(self._config, self._config_path)
            if not self._config.enabled:
                self._status.active = False
                self._status.last_error = None
        if self._config.enabled:
            self.test_connection()
        return self.get_state()

    def test_connection(self) -> tuple[bool, str]:
        try:
            self._ensure_table()
        except Exception as exc:
            self._mark_test(False, str(exc))
            return False, str(exc)
        self._mark_test(True, None)
        return True, "Connection successful."

    def enqueue_lock_result(self, row: dict[str, Any]) -> bool:
        with self._lock:
            config_enabled = self._config.enabled
        if not config_enabled:
            return False
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            self._mark_drop("Queue full, dropping lock-result row.")
            return False
        with self._lock:
            self._status.enqueued_count += 1
            self._status.queue_size = self._queue.qsize()
        return True

    # Backwards-compatible alias; retained temporarily while call sites migrate.
    def enqueue_manual_lock(self, row: dict[str, Any]) -> bool:
        return self.enqueue_lock_result(row)

    def _worker_loop(self) -> None:
        while True:
            if self._stop_event.is_set() and self._queue.empty():
                break
            try:
                row = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._write_row(row)
                self._mark_write(True, None)
            except Exception as exc:
                self._mark_write(False, str(exc))
            finally:
                self._queue.task_done()
                with self._lock:
                    self._status.queue_size = self._queue.qsize()

    def _connect_kwargs(self) -> dict[str, Any]:
        with self._lock:
            cfg = self._config
            return {
                "host": cfg.host,
                "port": int(cfg.port),
                "dbname": cfg.database,
                "user": cfg.user,
                "password": cfg.password,
                "sslmode": cfg.sslmode,
                "connect_timeout": max(1, int(round(cfg.connect_timeout_s))),
            }

    @staticmethod
    def _apply_schema(cur: Any) -> None:
        """Create the table and bring an older one up to date. Idempotent."""
        cur.execute(CREATE_TABLE_SQL)
        cur.execute(ALTER_TABLE_ADD_LOCK_SOURCE_SQL)
        cur.execute(ALTER_TABLE_ADD_APPROACH_SQL)
        cur.execute(CREATE_INDEX_CREATED_SQL)
        cur.execute(CREATE_INDEX_LASER_SQL)

    def _ensure_table(self) -> None:
        if psycopg is None:
            raise RuntimeError("psycopg is not installed in gateway environment.")
        connect_kwargs = self._connect_kwargs()
        with psycopg.connect(**connect_kwargs) as conn:
            with conn.cursor() as cur:
                self._apply_schema(cur)
            conn.commit()
        self._schema_ready = True

    def _write_row(self, row: dict[str, Any]) -> None:
        if psycopg is None:
            raise RuntimeError("psycopg is not installed in gateway environment.")
        with self._lock:
            if not self._config.enabled:
                return
        connect_kwargs = self._connect_kwargs()
        with psycopg.connect(**connect_kwargs) as conn:
            with conn.cursor() as cur:
                # The first write of a process migrates before inserting, on the
                # same connection. Nothing else guarantees the columns INSERT_SQL
                # names actually exist on the target database.
                if not self._schema_ready:
                    self._apply_schema(cur)
                cur.execute(INSERT_SQL, row)
            conn.commit()
        self._schema_ready = True

    def _mark_test(self, ok: bool, error: str | None) -> None:
        now = time.time()
        if not ok and error:
            logger.warning("Lock-result postgres test failed: %s", error)
            self._emit_event(
                level=logging.ERROR,
                code="postgres_test_failed",
                message="Postgres test connection failed.",
                details={"error": error},
            )
        with self._lock:
            self._status.last_test_ok = ok
            self._status.last_test_at = now
            self._status.last_error = error
            self._status.active = bool(self._config.enabled and ok)

    def _mark_write(self, ok: bool, error: str | None) -> None:
        now = time.time()
        if not ok and error:
            logger.warning("Lock-result postgres write failed: %s", error)
            self._emit_event(
                level=logging.ERROR,
                code="postgres_write_failed",
                message="Postgres lock write failed.",
                details={"error": error},
            )
        with self._lock:
            self._status.last_write_ok = ok
            self._status.last_write_at = now
            if ok:
                self._status.write_ok_count += 1
                self._status.last_error = None
                self._status.active = bool(self._config.enabled and self._status.last_test_ok)
            else:
                self._status.write_error_count += 1
                self._status.last_error = error
                self._status.active = False

    def _mark_drop(self, error: str) -> None:
        logger.warning("Lock-result postgres dropped row: %s", error)
        self._emit_event(
            level=logging.WARNING,
            code="postgres_queue_drop",
            message="Postgres lock row dropped.",
            details={"error": error},
        )
        with self._lock:
            self._status.dropped_count += 1
            self._status.last_error = error
            # A queue-full drop is transient back-pressure, not a connection
            # failure — keep `active` reflecting DB health (set by test/write)
            # rather than flipping it False on every burst.

    def _emit_event(
        self,
        *,
        level: int,
        code: str,
        message: str,
        details: dict[str, Any],
    ) -> None:
        callback = self._event_callback
        if callback is None:
            return
        try:
            callback(level, "postgres", code, message, details)
        except Exception:
            logger.debug("Postgres event callback failed", exc_info=True)


# Backwards-compatible aliases for previous naming.
POSTGRES_CONFIG_PATH = LOCK_RESULT_POSTGRES_CONFIG_PATH
ManualLockPostgresConfig = LockResultPostgresConfig
ManualLockPostgresStatus = LockResultPostgresStatus
ManualLockPostgresService = LockResultPostgresService
load_manual_lock_postgres_config = load_lock_result_postgres_config
save_manual_lock_postgres_config = save_lock_result_postgres_config
