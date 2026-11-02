"""PostgreSQL writer for lock-acquisition records.

Records go through a durable local spool (lock_spool.py) before PostgreSQL:
``enqueue_lock_result`` only writes the spool -- never touches the network --
and a background thread pushes due records with idempotent upserts keyed by
lock_acquisition_id, retrying temporary failures with exponential backoff.
A record counts as persisted only once PostgreSQL has acknowledged it.

Schema
------
The table lives in its own PostgreSQL schema (``linien`` by default,
configurable). Its definition is declared once below (``TABLE_COLUMNS``) and
rendered both into the writer's DDL and into
docker/postgres/postgres-init/01-init.sql (``render_init_sql``; a test keeps
the file in step). A one-row ``pdh_lock_schema`` table records the schema
version. The writer creates the schema on an empty database, but never alters
an existing table: if it finds an unknown version or an unexpected column set,
it reports a schema error, writes nothing, and keeps the records spooled.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from linien_common.config import USER_DATA_PATH

from .lock_spool import DEFAULT_MAX_AGE_S, DEFAULT_MAX_RECORDS, LockRecordSpool
from .manual_lock_record import _json_safe

try:
    import psycopg  # type: ignore
except Exception:  # pragma: no cover - exercised through runtime status/message
    psycopg = None  # type: ignore

LOCK_RESULT_POSTGRES_CONFIG_PATH = USER_DATA_PATH / "manual_lock_postgres.json"
LOCK_RECORD_SPOOL_PATH = USER_DATA_PATH / "lock_acquisition_spool.sqlite3"
# How long stop() waits for the writer to finish the record it is sending.
# Anything unsent stays in the spool and goes out after the next start.
SHUTDOWN_DRAIN_TIMEOUT_S = 5.0
RETRY_BASE_S = 1.0
RETRY_MAX_S = 300.0
WRITE_BATCH = 20
logger = logging.getLogger(__name__)
PostgresEventCallback = Callable[[int, str, str, str, dict[str, Any]], None]

DEFAULT_SCHEMA = "linien"
SCHEMA_VERSION = 1
TABLE_NAME = "pdh_lock_results"
VERSION_TABLE_NAME = "pdh_lock_schema"
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

LOCK_SOURCES = ("manual_lock", "auto_lock_scan", "staged_autolock", "auto_relock")


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"CHECK ({column} IN ({quoted}))"


# Every column of the table, in order: (name, SQL type and constraints).
# One row per lock-acquisition attempt, keyed by lock_acquisition_id.
TABLE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("lock_acquisition_id", "UUID PRIMARY KEY"),
    ("created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
    ("updated_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()"),
    ("record_version", "INTEGER NOT NULL"),
    ("device_key", "TEXT NOT NULL"),
    ("laser_name", "TEXT NOT NULL"),
    ("lock_source", "TEXT NOT NULL " + _in("lock_source", LOCK_SOURCES)),
    # Backward-compatible: the lock command was accepted. Not proof of a
    # stable lock -- see verification_status.
    ("success", "BOOLEAN"),
    ("attempt_started_at", "TIMESTAMPTZ NOT NULL"),
    ("lock_command_at", "TIMESTAMPTZ"),
    (
        "lock_command_status",
        "TEXT NOT NULL " + _in("lock_command_status", ("not_sent", "accepted", "rejected")),
    ),
    (
        "failure_stage",
        "TEXT " + _in(
            "failure_stage", ("trace_capture", "detection", "refinement", "lock_command")
        ),
    ),
    ("failure_reason", "TEXT"),
    (
        "verification_status",
        "TEXT NOT NULL " + _in(
            "verification_status",
            ("not_attempted", "pending", "verified", "failed", "unavailable"),
        ),
    ),
    ("verification_reason", "TEXT"),
    ("verification_completed_at", "TIMESTAMPTZ"),
    # Converted lock settings for convenient SQL; raw values in linien_config.
    ("modulation_frequency_hz", "DOUBLE PRECISION"),
    ("demod_phase_deg", "DOUBLE PRECISION"),
    ("signal_offset_volts", "DOUBLE PRECISION"),
    ("modulation_amplitude", "DOUBLE PRECISION"),
    ("pid_p", "DOUBLE PRECISION"),
    ("pid_i", "DOUBLE PRECISION"),
    ("pid_d", "DOUBLE PRECISION"),
    # The unlocked sweep the acquisition was decided on, and its geometry.
    # NULL when no trace was captured: never a placeholder waveform.
    ("sweep_center_v", "DOUBLE PRECISION"),
    ("sweep_amplitude_v", "DOUBLE PRECISION"),
    ("trace_x", "DOUBLE PRECISION[]"),
    ("trace_y", "DOUBLE PRECISION[]"),
    ("secondary_trace_y", "DOUBLE PRECISION[]"),
    ("trace_x_units", "TEXT"),
    ("trace_y_units", "TEXT"),
    ("secondary_trace_y_units", "TEXT"),
    ("trace_x_source", "TEXT"),
    ("trace_y_role", "TEXT"),
    ("secondary_trace_role", "TEXT"),
    ("trace_unavailable_reason", "TEXT"),
    ("trace_meta", "JSONB"),
    ("refinement", "JSONB"),
    # Raw Linien parameters at acquisition (schema_version inside).
    ("linien_config", "JSONB"),
    # Diagnostic description of the stored trace; the JSON holds the full
    # result and its units, the columns repeat the most queried values.
    ("characterization", "JSONB"),
    ("characterization_status", "TEXT"),
    ("zero_crossing_sweep_v", "DOUBLE PRECISION"),
    ("slope_error_v_per_sweep_v", "DOUBLE PRECISION"),
    ("discriminator_slope_v_per_mhz", "DOUBLE PRECISION"),
    ("peak_to_peak_v", "DOUBLE PRECISION"),
)
# Filled by the database, not by the writer.
_DB_MANAGED = frozenset({"created_at", "updated_at"})
INSERT_COLUMN_NAMES = tuple(
    name for name, _sql in TABLE_COLUMNS if name not in _DB_MANAGED
)
TIMESTAMP_COLUMNS = frozenset(
    name for name, sql in TABLE_COLUMNS
    if sql.startswith("TIMESTAMPTZ") and name not in _DB_MANAGED
)
JSONB_COLUMNS = frozenset(name for name, sql in TABLE_COLUMNS if sql.startswith("JSONB"))
EXPECTED_COLUMNS = frozenset(name for name, _sql in TABLE_COLUMNS)


def validate_schema_name(schema: str) -> str:
    if not isinstance(schema, str) or not _IDENTIFIER.match(schema):
        raise ValueError(
            f"Invalid PostgreSQL schema name {schema!r}: use lowercase letters, "
            "digits and underscores."
        )
    return schema


# psycopg sends a Python str as PostgreSQL ``text``, which does not coerce to
# ``jsonb`` or ``uuid`` implicitly, so those placeholders carry a cast.
# Timestamps travel as Unix seconds and are converted server-side.
def _placeholder(name: str, sql: str) -> str:
    if name in TIMESTAMP_COLUMNS:
        return f"to_timestamp(%({name})s::double precision)"
    if sql.startswith("JSONB"):
        return f"%({name})s::jsonb"
    if sql.startswith("UUID"):
        return f"%({name})s::uuid"
    return f"%({name})s"


def render_schema_statements(schema: str = DEFAULT_SCHEMA) -> list[str]:
    """The DDL that creates schema version SCHEMA_VERSION from scratch."""
    s = validate_schema_name(schema)
    columns = ",\n".join(f"    {name} {sql}" for name, sql in TABLE_COLUMNS)
    return [
        f"CREATE SCHEMA IF NOT EXISTS {s};",
        f"""CREATE TABLE IF NOT EXISTS {s}.{VERSION_TABLE_NAME} (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    schema_version INTEGER NOT NULL,
    installed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);""",
        f"CREATE TABLE IF NOT EXISTS {s}.{TABLE_NAME} (\n{columns}\n);",
        f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_device_attempt\n"
        f"    ON {s}.{TABLE_NAME} (device_key, attempt_started_at DESC);",
        f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_attempt\n"
        f"    ON {s}.{TABLE_NAME} (attempt_started_at DESC);",
        f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_source_attempt\n"
        f"    ON {s}.{TABLE_NAME} (lock_source, attempt_started_at DESC);",
        f"INSERT INTO {s}.{VERSION_TABLE_NAME} (schema_version)\n"
        f"    VALUES ({SCHEMA_VERSION}) ON CONFLICT (singleton) DO NOTHING;",
    ]


def render_init_sql(schema: str = DEFAULT_SCHEMA) -> str:
    """Contents of docker/postgres/postgres-init/01-init.sql."""
    header = (
        "-- Generated by linien-gateway/app/manual_lock_postgres.py (render_init_sql).\n"
        "-- Do not edit by hand; tests/test_manual_lock_postgres.py checks it.\n"
        f"-- Lock-acquisition records, schema version {SCHEMA_VERSION}: one row per\n"
        "-- lock attempt, keyed by lock_acquisition_id.\n"
    )
    return header + "\n" + "\n\n".join(render_schema_statements(schema)) + "\n"


def render_upsert_sql(schema: str = DEFAULT_SCHEMA) -> str:
    """Idempotent by lock_acquisition_id: sending the same attempt again (a
    retry, or a later update such as its verification outcome) updates its
    one row."""
    s = validate_schema_name(schema)
    by_name = dict(TABLE_COLUMNS)
    names = ",\n".join(f"    {name}" for name in INSERT_COLUMN_NAMES)
    values = ",\n".join(
        f"    {_placeholder(name, by_name[name])}" for name in INSERT_COLUMN_NAMES
    )
    updates = ",\n".join(
        f"    {name} = EXCLUDED.{name}"
        for name in INSERT_COLUMN_NAMES
        if name != "lock_acquisition_id"
    )
    return (
        f"INSERT INTO {s}.{TABLE_NAME} (\n{names}\n) VALUES (\n{values}\n)\n"
        f"ON CONFLICT (lock_acquisition_id) DO UPDATE SET\n{updates},\n"
        "    updated_at = NOW();"
    )


def prepare_row(row: dict[str, Any]) -> dict[str, Any]:
    """The writer's parameters for one row: every column present, JSON
    columns serialized (non-finite floats as null)."""
    params = {name: row.get(name) for name in INSERT_COLUMN_NAMES}
    for name in JSONB_COLUMNS:
        value = params.get(name)
        if value is not None and not isinstance(value, str):
            params[name] = json.dumps(_json_safe(value), allow_nan=False)
    return params


# ----------------------------------------------------------------- reads
#
# The read API returns records in one shape whichever store answers: the
# writer's row (epoch-second timestamps, JSON columns as objects, NaN as null),
# plus `source` ("postgres" or "spool") and `persistence` (the spool state, or
# "persisted"). Large fields are opt-in on list queries.

INCLUDE_GROUPS: dict[str, tuple[str, ...]] = {
    "traces": ("trace_x", "trace_y", "secondary_trace_y"),
    "config": ("linien_config",),
    "refinement": ("refinement",),
}
ALL_INCLUDES = frozenset(INCLUDE_GROUPS)
_OPTIONAL_COLUMNS = frozenset(
    column for columns in INCLUDE_GROUPS.values() for column in columns
)
VERIFICATION_STATUSES = ("not_attempted", "pending", "verified", "failed", "unavailable")
LOCK_COMMAND_STATUSES = ("not_sent", "accepted", "rejected")


@dataclass(frozen=True)
class RecordFilters:
    since: float | None = None
    until: float | None = None
    lock_source: str | None = None
    verification_status: str | None = None
    lock_command_status: str | None = None
    success: bool | None = None

    def matches(self, row: dict[str, Any]) -> bool:
        """The non-indexed filters, for the spool (PostgreSQL does them in SQL)."""
        if self.verification_status is not None and (
            row.get("verification_status") != self.verification_status
        ):
            return False
        if self.lock_command_status is not None and (
            row.get("lock_command_status") != self.lock_command_status
        ):
            return False
        if self.success is not None and bool(row.get("success")) != self.success:
            return False
        return True


def _select_list(include: frozenset[str]) -> str:
    wanted = {
        column for group in include for column in INCLUDE_GROUPS.get(group, ())
    }
    columns = [
        name for name, _sql in TABLE_COLUMNS
        if name not in _OPTIONAL_COLUMNS or name in wanted
    ]
    rendered = []
    for name in columns:
        sql = dict(TABLE_COLUMNS)[name]
        if name == "lock_acquisition_id":
            rendered.append(f"{name}::text AS {name}")
        elif sql.startswith("TIMESTAMPTZ"):
            rendered.append(f"EXTRACT(EPOCH FROM {name})::double precision AS {name}")
        else:
            rendered.append(name)
    return ", ".join(rendered)


def build_list_query(
    schema: str,
    device_key: str | None,
    filters: RecordFilters,
    include: frozenset[str],
    limit: int,
    offset: int,
) -> tuple[str, list[Any]]:
    s = validate_schema_name(schema)
    clauses: list[str] = []
    params: list[Any] = []
    if device_key is not None:
        clauses.append("device_key = %s")
        params.append(device_key)
    if filters.since is not None:
        clauses.append("attempt_started_at >= to_timestamp(%s)")
        params.append(float(filters.since))
    if filters.until is not None:
        clauses.append("attempt_started_at < to_timestamp(%s)")
        params.append(float(filters.until))
    for column in ("lock_source", "verification_status", "lock_command_status", "success"):
        value = getattr(filters, column)
        if value is not None:
            clauses.append(f"{column} = %s")
            params.append(value)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        f"SELECT {_select_list(include)} FROM {s}.{TABLE_NAME}{where} "
        "ORDER BY attempt_started_at DESC LIMIT %s OFFSET %s"
    )
    return sql, params + [int(limit), int(offset)]


def build_get_query(schema: str, acquisition_id: str) -> tuple[str, list[Any]]:
    s = validate_schema_name(schema)
    return (
        f"SELECT {_select_list(ALL_INCLUDES)} FROM {s}.{TABLE_NAME} "
        "WHERE lock_acquisition_id = %s::uuid",
        [str(acquisition_id)],
    )


def normalize_record(
    row: dict[str, Any],
    *,
    include: frozenset[str],
    source: str,
    persistence: str | None = None,
) -> dict[str, Any]:
    wanted = {
        column for group in include for column in INCLUDE_GROUPS.get(group, ())
    }
    record: dict[str, Any] = {}
    for name, value in row.items():
        if name in _OPTIONAL_COLUMNS and name not in wanted:
            continue
        if name in JSONB_COLUMNS and isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        if hasattr(value, "timestamp") and callable(value.timestamp):
            value = value.timestamp()
        record[name] = _json_safe(value)
    record["source"] = source
    record["persistence"] = persistence if persistence is not None else "persisted"
    return record

class SchemaMismatchError(RuntimeError):
    """The target database holds a table this writer must not write to."""


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
    db_schema: str = DEFAULT_SCHEMA
    spool_max_records: int = DEFAULT_MAX_RECORDS
    spool_max_age_days: float = DEFAULT_MAX_AGE_S / 86400.0


@dataclass
class LockResultPostgresStatus:
    active: bool = False
    last_test_ok: bool | None = None
    last_test_at: float | None = None
    last_write_ok: bool | None = None
    last_write_at: float | None = None
    last_error: str | None = None
    # Records accepted into the local spool (not yet persisted).
    enqueued_count: int = 0
    # Upserts PostgreSQL acknowledged.
    write_ok_count: int = 0
    write_error_count: int = 0
    # Records that could not be spooled, or were evicted from a full spool
    # before being sent.
    dropped_count: int = 0
    # pending + retrying in the spool.
    queue_size: int = 0
    pending_count: int = 0
    retrying_count: int = 0
    persisted_count: int = 0
    failed_count: int = 0
    schema_ok: bool | None = None
    schema_error: str | None = None


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
        config = LockResultPostgresConfig(**merged)
        validate_schema_name(config.db_schema)
        return config
    except (TypeError, ValueError):
        return LockResultPostgresConfig()


def save_lock_result_postgres_config(
    config: LockResultPostgresConfig, path: Path = LOCK_RESULT_POSTGRES_CONFIG_PATH
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")


def _error_types(*names: str) -> tuple[type, ...]:
    if psycopg is None:
        return ()
    found = []
    for name in names:
        candidate = getattr(psycopg, name, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            found.append(candidate)
    return tuple(found)


def _is_permanent(exc: BaseException) -> bool:
    """A row PostgreSQL will reject however often it is sent. Connection,
    timeout and server-side transient errors are not permanent."""
    permanent = _error_types("DataError", "IntegrityError", "NotSupportedError")
    return bool(permanent) and isinstance(exc, permanent)


_PASSWORD_IN_TEXT = re.compile(r"(password\s*=\s*)(\S+)", re.IGNORECASE)


def redact(message: str | None) -> str | None:
    """Errors are shown in the UI and logs; never let a password through."""
    if message is None:
        return None
    return _PASSWORD_IN_TEXT.sub(r"\1***", str(message))


def retry_delay_s(attempts: int) -> float:
    return min(RETRY_MAX_S, RETRY_BASE_S * (2 ** max(0, int(attempts))))


class LockResultPostgresService:
    def __init__(
        self,
        *,
        config_path: Path = LOCK_RESULT_POSTGRES_CONFIG_PATH,
        spool_path: Path | None = None,
    ) -> None:
        self._config_path = config_path
        self._config = load_lock_result_postgres_config(config_path)
        self._status = LockResultPostgresStatus(active=False)
        # Whether this process has checked/created the schema on the
        # configured database yet; reset whenever the target changes.
        self._schema_ready = False
        # Next to the config file by default; opened on first use so that
        # constructing the service touches no disk.
        self._spool_path = (
            spool_path
            if spool_path is not None
            else config_path.with_name(LOCK_RECORD_SPOOL_PATH.name)
        )
        self._spool_instance: LockRecordSpool | None = None
        self._spool_open_lock = threading.Lock()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._event_callback: PostgresEventCallback | None = None

    @property
    def spool(self) -> LockRecordSpool:
        with self._spool_open_lock:
            if self._spool_instance is None:
                self._spool_instance = LockRecordSpool(
                    self._spool_path,
                    max_records=self._config.spool_max_records,
                    max_age_s=self._config.spool_max_age_days * 86400.0,
                )
            return self._spool_instance

    @property
    def _spool(self) -> LockRecordSpool:
        return self.spool

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

    def stop(self) -> None:
        """Stop the writer. Unsent records stay in the spool for the next start."""
        self._stop_event.set()
        self._wake.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=SHUTDOWN_DRAIN_TIMEOUT_S)
        self._worker_thread = None
        with self._lock:
            self._status.active = False

    def get_state(self) -> dict[str, Any]:
        counts = self._spool.counts()
        with self._lock:
            status = asdict(self._status)
            config = asdict(self._config)
        # The stored password is write-only: report whether one is set.
        config["password_set"] = bool(config.get("password"))
        config["password"] = ""
        status.update(
            pending_count=counts["pending"],
            retrying_count=counts["retrying"],
            persisted_count=counts["persisted"],
            failed_count=counts["failed"],
            queue_size=counts["pending"] + counts["retrying"],
            dropped_count=status["dropped_count"] + self._spool.dropped_unsent,
        )
        return {"config": config, "status": status}

    def update_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = asdict(self._config)
            normalized = {key: payload.get(key, current[key]) for key in current}
            # get_state never returns the password, so a client saving back
            # the config it read sends a blank one: keep the stored password.
            if not normalized.get("password"):
                normalized["password"] = current["password"]
            validate_schema_name(normalized["db_schema"])
            self._config = LockResultPostgresConfig(**normalized)
            self._schema_ready = False
            self._status.schema_ok = None
            self._status.schema_error = None
            if self._spool_instance is not None:
                self._spool_instance.max_records = max(1, int(self._config.spool_max_records))
                self._spool_instance.max_age_s = max(
                    0.0, float(self._config.spool_max_age_days) * 86400.0
                )
            save_lock_result_postgres_config(self._config, self._config_path)
            if not self._config.enabled:
                self._status.active = False
                self._status.last_error = None
        if self._config.enabled:
            self.test_connection()
        self._wake.set()
        return self.get_state()

    def test_connection(self) -> tuple[bool, str]:
        try:
            self._ensure_schema()
        except Exception as exc:
            message = redact(str(exc)) or type(exc).__name__
            self._mark_test(False, message)
            return False, message
        self._mark_test(True, None)
        self._wake.set()
        return True, "Connection successful."

    def enqueue_lock_result(self, row: dict[str, Any]) -> bool:
        """Spool one record for PostgreSQL. Local disk only, never the network.

        True means spooled, not persisted. False when PostgreSQL logging is
        disabled (nothing is recorded) or the spool could not be written.
        """
        with self._lock:
            config_enabled = self._config.enabled
        if not config_enabled:
            return False
        try:
            self._spool.put(row)
        except Exception as exc:  # noqa: BLE001 - disk full, corrupt file, ...
            self._mark_drop(f"Spooling lock record failed: {exc}")
            return False
        with self._lock:
            self._status.enqueued_count += 1
        self._wake.set()
        return True

    # Backwards-compatible alias; retained temporarily while call sites migrate.
    def enqueue_manual_lock(self, row: dict[str, Any]) -> bool:
        return self.enqueue_lock_result(row)

    # ------------------------------------------------------------- worker

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                enabled = self._config.enabled
            batch = self._spool.due(WRITE_BATCH) if enabled and psycopg is not None else []
            if batch:
                self._send_batch(batch)
                continue
            timeout = 1.0
            next_due = self._spool.next_due_at() if enabled else None
            if next_due is not None:
                timeout = min(1.0, max(0.05, next_due - time.time()))
            self._wake.wait(timeout)
            self._wake.clear()

    def _send_batch(self, batch: list[dict[str, Any]]) -> None:
        """Upsert due records, one transaction each, on one connection."""
        try:
            conn_cm = psycopg.connect(**self._connect_kwargs())
        except Exception as exc:  # noqa: BLE001 - every record waits for the next try
            for item in batch:
                self._retry(item, exc)
            return
        try:
            with conn_cm as conn:
                if not self._schema_ready:
                    try:
                        with conn.cursor() as cur:
                            self._apply_schema(cur)
                        conn.commit()
                    except Exception as exc:  # noqa: BLE001
                        self._rollback(conn)
                        for item in batch:
                            self._retry(item, exc)
                        return
                    self._schema_ready = True
                upsert = render_upsert_sql(self._schema_name())
                for item in batch:
                    if self._stop_event.is_set():
                        return
                    try:
                        with conn.cursor() as cur:
                            cur.execute(upsert, prepare_row(item["row"]))
                        conn.commit()
                    except Exception as exc:  # noqa: BLE001
                        self._rollback(conn)
                        if _is_permanent(exc):
                            self._spool.mark_failed(
                                item["lock_acquisition_id"],
                                item["revision"],
                                redact(str(exc)),
                            )
                            self._mark_write(False, str(exc))
                        else:
                            self._retry(item, exc)
                            # Possibly a dropped connection: let the next batch
                            # reconnect rather than fail the rest of this one.
                            return
                        continue
                    self._spool.mark_persisted(item["lock_acquisition_id"], item["revision"])
                    self._mark_write(True, None)
        except Exception as exc:  # noqa: BLE001 - e.g. connection lost on close
            logger.debug("Postgres batch ended with an error: %s", exc)

    @staticmethod
    def _rollback(conn: Any) -> None:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass

    def _retry(self, item: dict[str, Any], exc: BaseException) -> None:
        delay = retry_delay_s(int(item.get("attempts", 0)))
        self._spool.mark_retry(
            item["lock_acquisition_id"], item["revision"], redact(str(exc)), delay
        )
        self._mark_write(False, str(exc))
        if isinstance(exc, SchemaMismatchError):
            self._schema_ready = False
            with self._lock:
                self._status.schema_ok = False
                self._status.schema_error = str(exc)

    def _schema_name(self) -> str:
        with self._lock:
            return self._config.db_schema

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

    def _apply_schema(self, cur: Any) -> None:
        """Create the schema on an empty database, or check an existing one.

        Never alters an existing table: an unknown version or column set
        raises SchemaMismatchError and nothing is written.
        """
        schema = self._schema_name()
        statements = render_schema_statements(schema)
        create_schema, create_version, create_table, *indexes, insert_version = statements
        cur.execute(create_schema)
        cur.execute(create_version)
        cur.execute(f"SELECT schema_version FROM {schema}.{VERSION_TABLE_NAME}")
        version_row = cur.fetchone()
        cur.execute("SELECT to_regclass(%s)", (f"{schema}.{TABLE_NAME}",))
        table_row = cur.fetchone()
        table_exists = bool(table_row and table_row[0])
        if version_row is None:
            if table_exists:
                raise SchemaMismatchError(
                    f"{schema}.{TABLE_NAME} exists but has no schema version; "
                    "refusing to write to a table this gateway did not create."
                )
            cur.execute(create_table)
            for statement in indexes:
                cur.execute(statement)
            cur.execute(insert_version)
        elif int(version_row[0]) != SCHEMA_VERSION:
            raise SchemaMismatchError(
                f"{schema}.{TABLE_NAME} is at schema version {version_row[0]}; "
                f"this gateway writes version {SCHEMA_VERSION}."
            )
        elif not table_exists:
            cur.execute(create_table)
            for statement in indexes:
                cur.execute(statement)
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s",
            (schema, TABLE_NAME),
        )
        found = {str(row[0]) for row in (cur.fetchall() or [])}
        if found != EXPECTED_COLUMNS:
            missing = sorted(EXPECTED_COLUMNS - found)
            extra = sorted(found - EXPECTED_COLUMNS)
            raise SchemaMismatchError(
                f"{schema}.{TABLE_NAME} does not match schema version "
                f"{SCHEMA_VERSION} (missing columns: {missing or 'none'}; "
                f"unexpected columns: {extra or 'none'})."
            )

    def _ensure_schema(self) -> None:
        if psycopg is None:
            raise RuntimeError("psycopg is not installed in gateway environment.")
        try:
            with psycopg.connect(**self._connect_kwargs()) as conn:
                with conn.cursor() as cur:
                    self._apply_schema(cur)
                conn.commit()
        except SchemaMismatchError as exc:
            with self._lock:
                self._status.schema_ok = False
                self._status.schema_error = str(exc)
            raise
        self._schema_ready = True
        with self._lock:
            self._status.schema_ok = True
            self._status.schema_error = None

    # ------------------------------------------------------------- reads

    def list_records(
        self,
        *,
        device_key: str | None,
        filters: RecordFilters,
        include: frozenset[str] = frozenset(),
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Acquisition records, newest first: from PostgreSQL when reachable,
        otherwise from the local spool. The response says which."""
        rows, error = self._postgres_rows(
            build_list_query(
                self._schema_name(), device_key, filters, include, limit, offset
            )
        )
        unsent = self._unsent_count()
        if rows is not None:
            items = [
                normalize_record(row, include=include, source="postgres")
                for row in rows
            ]
            return {
                "source": "postgres",
                "items": items,
                "count": len(items),
                "limit": limit,
                "offset": offset,
                # Spooled records PostgreSQL does not have yet; they are not
                # in this list (fetch one by ID, or wait for the writer).
                "unsent_records": unsent,
            }
        found = self.spool.query(
            device_key=device_key,
            since=filters.since,
            until=filters.until,
            lock_source=filters.lock_source,
            predicate=filters.matches,
            limit=limit,
            offset=offset,
        )
        items = [
            normalize_record(
                item["row"], include=include, source="spool", persistence=item["state"]
            )
            for item in found
        ]
        return {
            "source": "spool",
            "postgres_unavailable_reason": error,
            "items": items,
            "count": len(items),
            "limit": limit,
            "offset": offset,
            "unsent_records": unsent,
        }

    def get_record(self, acquisition_id: str) -> dict[str, Any] | None:
        """One full record (config and traces included), or None."""
        rows, _error = self._postgres_rows(
            build_get_query(self._schema_name(), acquisition_id)
        )
        if rows:
            return normalize_record(rows[0], include=ALL_INCLUDES, source="postgres")
        # Not in PostgreSQL (yet), or PostgreSQL unreachable: the spool holds
        # every record written while logging was enabled and not yet pruned.
        found = self.spool.get(acquisition_id)
        if found is None:
            return None
        return normalize_record(
            found["row"], include=ALL_INCLUDES, source="spool", persistence=found["state"]
        )

    def _unsent_count(self) -> int:
        counts = self.spool.counts()
        return counts["pending"] + counts["retrying"]

    def _postgres_rows(
        self, query: tuple[str, list[Any]]
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        """Rows, or (None, reason) when PostgreSQL cannot answer. The reason
        is generic on purpose: driver errors can name hosts and users."""
        with self._lock:
            enabled = self._config.enabled
        if not enabled:
            return None, "postgres_logging_disabled"
        if psycopg is None:
            return None, "psycopg_not_installed"
        sql, params = query
        try:
            with psycopg.connect(**self._connect_kwargs()) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    names = [column[0] for column in cur.description]
                    return [dict(zip(names, row)) for row in cur.fetchall()], None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Lock-record query failed: %s", exc)
            return None, "postgres_query_failed"

    # ------------------------------------------------------------- status

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
        error = redact(error)
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
                self._status.schema_ok = True
                self._status.schema_error = None
                self._status.active = bool(self._config.enabled)
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
