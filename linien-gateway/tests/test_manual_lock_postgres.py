"""The lock-record writer: schema ownership, durable spool, idempotent retries.

A small in-memory fake stands in for PostgreSQL: it keeps rows by
lock_acquisition_id the way the real upsert does, can be taken down, and can
hold a table at another schema version.
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import pytest

import app.manual_lock_postgres as mlp
from app.lock_spool import LockRecordSpool
from app.manual_lock_postgres import (
    EXPECTED_COLUMNS,
    LockResultPostgresConfig,
    LockResultPostgresService,
    load_lock_result_postgres_config,
    render_init_sql,
    render_schema_statements,
    save_lock_result_postgres_config,
)

INIT_SQL = Path(__file__).resolve().parents[2] / "docker/postgres/postgres-init/01-init.sql"


class FakeOperationalError(Exception):
    pass


class FakeDataError(Exception):
    pass


class FakeDatabase:
    """Just enough of PostgreSQL for the writer's statements."""

    OperationalError = FakeOperationalError
    DataError = FakeDataError
    IntegrityError = type("FakeIntegrityError", (Exception,), {})
    NotSupportedError = type("FakeNotSupportedError", (Exception,), {})

    def __init__(self) -> None:
        self.down = False
        self.connect_delay_s = 0.0
        self.version: int | None = None
        self.table_exists = False
        self.columns: set[str] = set()
        self.rows: dict[str, dict[str, Any]] = {}
        self.upserts = 0
        self.statements: list[str] = []
        # Called before each upsert; may raise to simulate a failure.
        self.on_upsert: Callable[[dict[str, Any]], None] | None = None
        self.lock = threading.Lock()

    def connect(self, **_kwargs):
        if self.connect_delay_s:
            time.sleep(self.connect_delay_s)
        if self.down:
            raise FakeOperationalError("connection refused")
        return _FakeConnection(self)


class _FakeConnection:
    def __init__(self, db: FakeDatabase) -> None:
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def cursor(self):
        return _FakeCursor(self.db)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


class _FakeCursor:
    def __init__(self, db: FakeDatabase) -> None:
        self.db = db
        self.result: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        db = self.db
        text = sql.strip()
        with db.lock:
            db.statements.append(text)
            if text.startswith("SELECT schema_version"):
                self.result = [] if db.version is None else [(db.version,)]
            elif text.startswith("SELECT to_regclass"):
                self.result = [(params[0] if db.table_exists else None,)]
            elif "information_schema.columns" in text:
                self.result = [(name,) for name in sorted(db.columns)]
            elif text.startswith("CREATE TABLE IF NOT EXISTS") and ".pdh_lock_results" in text:
                if not db.table_exists:
                    db.table_exists = True
                    db.columns = set(EXPECTED_COLUMNS)
            elif text.startswith("INSERT INTO") and ".pdh_lock_schema" in text:
                if db.version is None:
                    db.version = 1
            elif text.startswith("INSERT INTO") and ".pdh_lock_results" in text:
                assert "ON CONFLICT (lock_acquisition_id) DO UPDATE" in text
                if db.on_upsert is not None:
                    db.on_upsert(params)
                db.rows[params["lock_acquisition_id"]] = dict(params)
                db.upserts += 1

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return list(self.result)


def _row(acquisition_id: str | None = None, **fields: Any) -> dict[str, Any]:
    row = {
        "lock_acquisition_id": acquisition_id or str(uuid.uuid4()),
        "record_version": 1,
        "device_key": "dev-1",
        "laser_name": "Laser A",
        "lock_source": "manual_lock",
        "success": True,
        "attempt_started_at": time.time(),
        "lock_command_status": "accepted",
        "verification_status": "pending",
        "trace_y": [0.0, 0.5, float("nan")],
        "trace_meta": {"n_samples": 3, "bad": float("inf")},
    }
    row.update(fields)
    return row


def _service(tmp_path: Path, db: FakeDatabase, monkeypatch, **config: Any):
    monkeypatch.setattr(mlp, "psycopg", db)
    monkeypatch.setattr(mlp, "RETRY_BASE_S", 0.02)
    monkeypatch.setattr(mlp, "RETRY_MAX_S", 0.2)
    cfg = LockResultPostgresConfig(enabled=True, **config)
    save_lock_result_postgres_config(cfg, tmp_path / "cfg.json")
    return LockResultPostgresService(
        config_path=tmp_path / "cfg.json", spool_path=tmp_path / "spool.sqlite3"
    )


def _wait(condition: Callable[[], bool], timeout_s: float = 5.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


# ------------------------------------------------------------------ config


def test_manual_lock_postgres_config_roundtrip(tmp_path: Path):
    config_path = tmp_path / "manual_lock_postgres.json"
    config = LockResultPostgresConfig(
        enabled=True,
        host="10.0.0.2",
        port=5433,
        database="locks",
        user="writer",
        password="secret",
        sslmode="require",
        connect_timeout_s=5,
        db_schema="lab_locks",
        spool_max_records=500,
        spool_max_age_days=7.0,
    )
    save_lock_result_postgres_config(config, config_path)
    loaded = load_lock_result_postgres_config(config_path)
    assert loaded == config


def test_an_invalid_schema_name_is_refused(tmp_path: Path):
    service = LockResultPostgresService(
        config_path=tmp_path / "cfg.json", spool_path=tmp_path / "s.sqlite3"
    )
    with pytest.raises(ValueError):
        service.update_config({"db_schema": "linien; DROP TABLE x"})


# ---------------------------------------------------- one schema definition


def test_the_docker_init_script_is_the_rendered_schema():
    """01-init.sql and the writer's own DDL come from one definition."""
    assert INIT_SQL.read_text(encoding="utf-8").replace("\r\n", "\n") == render_init_sql()


def test_an_empty_database_gets_exactly_the_init_script_schema(tmp_path, monkeypatch):
    db = FakeDatabase()
    service = _service(tmp_path, db, monkeypatch)

    ok, detail = service.test_connection()

    assert ok, detail
    for statement in render_schema_statements("linien"):
        assert statement.strip() in db.statements
    assert db.version == 1
    assert service.get_state()["status"]["schema_ok"] is True


def test_the_schema_name_is_configurable(tmp_path, monkeypatch):
    db = FakeDatabase()
    service = _service(tmp_path, db, monkeypatch, db_schema="lab_locks")

    service.test_connection()

    assert "CREATE SCHEMA IF NOT EXISTS lab_locks;" in db.statements
    assert any("lab_locks.pdh_lock_results" in s for s in db.statements)


@pytest.mark.parametrize(
    "setup, message",
    [
        (lambda db: setattr(db, "version", 2), "schema version 2"),
        (lambda db: (setattr(db, "table_exists", True), db.columns.update({"id"})), "no schema version"),
        (
            lambda db: (
                setattr(db, "version", 1),
                setattr(db, "table_exists", True),
                db.columns.update(EXPECTED_COLUMNS - {"linien_config"}),
            ),
            "missing columns: ['linien_config']",
        ),
    ],
)
def test_an_unexpected_table_is_reported_and_never_altered_or_written(
    tmp_path, monkeypatch, setup, message
):
    db = FakeDatabase()
    setup(db)
    columns_before = set(db.columns)
    service = _service(tmp_path, db, monkeypatch)

    ok, detail = service.test_connection()
    assert not ok and message in detail
    state = service.get_state()["status"]
    assert state["schema_ok"] is False and message in state["schema_error"]

    service.start()
    try:
        assert service.enqueue_lock_result(_row())
        assert _wait(lambda: service.get_state()["status"]["retrying_count"] == 1)
    finally:
        service.stop()
    assert db.rows == {}
    assert db.columns == columns_before
    assert not any(s.startswith("ALTER") or s.startswith("DROP") for s in db.statements)
    # The record is still in the spool, waiting.
    assert service.spool.counts()["persisted"] == 0


# ------------------------------------------------------ spool + retries


def test_enqueued_is_not_persisted_until_postgres_acknowledges(tmp_path, monkeypatch):
    db = FakeDatabase()
    db.down = True
    service = _service(tmp_path, db, monkeypatch)
    row = _row()

    assert service.enqueue_lock_result(row) is True
    status = service.get_state()["status"]
    assert status["enqueued_count"] == 1
    assert status["pending_count"] == 1
    assert status["persisted_count"] == 0
    assert status["write_ok_count"] == 0

    service.start()
    try:
        assert _wait(lambda: service.get_state()["status"]["retrying_count"] == 1)
        db.down = False
        assert _wait(lambda: service.get_state()["status"]["persisted_count"] == 1)
    finally:
        service.stop()
    assert list(db.rows) == [row["lock_acquisition_id"]]
    # NaN/inf never reach the jsonb column.
    assert db.rows[row["lock_acquisition_id"]]["trace_meta"] == '{"n_samples": 3, "bad": null}'


def test_retried_and_resent_writes_never_duplicate_rows(tmp_path, monkeypatch):
    db = FakeDatabase()
    failures = {"left": 2}

    def flaky(_params):
        if failures["left"]:
            failures["left"] -= 1
            raise FakeOperationalError("server closed the connection unexpectedly")

    db.on_upsert = flaky
    service = _service(tmp_path, db, monkeypatch)
    row = _row()
    service.start()
    try:
        service.enqueue_lock_result(row)
        assert _wait(lambda: service.get_state()["status"]["persisted_count"] == 1)
        # The same attempt again (e.g. its verification outcome).
        service.enqueue_lock_result({**row, "verification_status": "verified"})
        assert _wait(lambda: db.rows[row["lock_acquisition_id"]]["verification_status"] == "verified")
    finally:
        service.stop()
    assert len(db.rows) == 1
    assert db.upserts >= 2


def test_a_permanently_rejected_record_is_kept_as_failed_not_retried(tmp_path, monkeypatch):
    db = FakeDatabase()

    def reject(params):
        if params["laser_name"] == "bad":
            raise FakeDataError("value out of range")

    db.on_upsert = reject
    service = _service(tmp_path, db, monkeypatch)
    service.start()
    try:
        service.enqueue_lock_result(_row(laser_name="bad"))
        good = _row()
        service.enqueue_lock_result(good)
        assert _wait(lambda: service.get_state()["status"]["failed_count"] == 1)
        assert _wait(lambda: good["lock_acquisition_id"] in db.rows)
        upserts = db.upserts
        time.sleep(0.3)
        assert db.upserts == upserts
    finally:
        service.stop()
    status = service.get_state()["status"]
    assert status["failed_count"] == 1 and status["persisted_count"] == 1


def test_an_outage_never_blocks_the_caller(tmp_path, monkeypatch):
    db = FakeDatabase()
    db.down = True
    db.connect_delay_s = 1.0  # a slow, failing connect
    service = _service(tmp_path, db, monkeypatch)
    service.start()
    try:
        time.sleep(0.05)  # let the writer get stuck in its connect
        started = time.perf_counter()
        for _ in range(5):
            assert service.enqueue_lock_result(_row(trace_y=[0.1] * 2048))
        elapsed = time.perf_counter() - started
    finally:
        service.stop()
    assert elapsed < 0.5, f"5 enqueues took {elapsed:.3f} s during an outage"


def test_unsent_records_survive_a_restart_and_are_written_after(tmp_path, monkeypatch):
    db = FakeDatabase()
    db.down = True
    first = _service(tmp_path, db, monkeypatch)
    first.start()
    rows = [_row(), _row()]
    for row in rows:
        first.enqueue_lock_result(row)
    first.stop()
    assert db.rows == {}

    db.down = False
    second = LockResultPostgresService(
        config_path=tmp_path / "cfg.json", spool_path=tmp_path / "spool.sqlite3"
    )
    assert second.get_state()["status"]["queue_size"] == 2
    second.start()
    try:
        assert _wait(lambda: second.get_state()["status"]["persisted_count"] == 2)
    finally:
        second.stop()
    assert set(db.rows) == {row["lock_acquisition_id"] for row in rows}


def test_disabled_logging_spools_nothing(tmp_path, monkeypatch):
    db = FakeDatabase()
    service = _service(tmp_path, db, monkeypatch)
    service.update_config({"enabled": False})

    assert service.enqueue_lock_result(_row()) is False
    assert service.get_state()["status"]["queue_size"] == 0


def test_backoff_grows_and_is_capped():
    assert mlp.retry_delay_s(0) == mlp.RETRY_BASE_S
    assert mlp.retry_delay_s(3) == 8 * mlp.RETRY_BASE_S
    assert mlp.retry_delay_s(50) == mlp.RETRY_MAX_S


# ---------------------------------------------------------------- spool


def test_an_update_during_a_send_is_not_marked_persisted(tmp_path):
    spool = LockRecordSpool(tmp_path / "s.sqlite3")
    row = _row()
    spool.put(row)
    (in_flight,) = spool.due()
    spool.put({**row, "verification_status": "verified"})  # arrives meanwhile

    assert spool.mark_persisted(row["lock_acquisition_id"], in_flight["revision"]) is False
    (resend,) = spool.due()
    assert resend["row"]["verification_status"] == "verified"
    assert spool.mark_persisted(row["lock_acquisition_id"], resend["revision"])
    assert spool.counts()["persisted"] == 1


def test_a_full_spool_evicts_persisted_records_before_unsent_ones(tmp_path):
    spool = LockRecordSpool(tmp_path / "s.sqlite3", max_records=3)
    sent = [_row() for _ in range(2)]
    for row in sent:
        spool.put(row)
        spool.mark_persisted(row["lock_acquisition_id"], 1)
    unsent = [_row() for _ in range(3)]
    for row in unsent:
        spool.put(row)

    ids = {item["lock_acquisition_id"] for item in spool.due(100)}
    assert ids == {row["lock_acquisition_id"] for row in unsent}
    assert spool.counts()["persisted"] == 0
    assert spool.dropped_unsent == 0

    spool.put(_row())  # now only unsent records are left to evict
    assert spool.dropped_unsent == 1
    assert sum(spool.counts().values()) == 3


def test_old_persisted_records_age_out_but_unsent_ones_do_not(tmp_path):
    spool = LockRecordSpool(tmp_path / "s.sqlite3", max_age_s=60.0)
    old_sent, old_unsent = _row(), _row()
    spool.put(old_sent)
    spool.put(old_unsent)
    spool.mark_persisted(old_sent["lock_acquisition_id"], 1)
    spool._conn.execute("UPDATE records SET updated_at = updated_at - 3600")

    spool.put(_row())

    assert spool.get(old_sent["lock_acquisition_id"]) is None
    assert spool.get(old_unsent["lock_acquisition_id"]) is not None


def test_spooling_a_full_size_record_is_fast(tmp_path):
    spool = LockRecordSpool(tmp_path / "s.sqlite3")
    row = _row(
        trace_x=[0.001 * i for i in range(2048)],
        trace_y=[0.5] * 2048,
        secondary_trace_y=[0.25] * 2048,
    )
    timings = []
    for _ in range(20):
        started = time.perf_counter()
        spool.put({**row, "lock_acquisition_id": str(uuid.uuid4())})
        timings.append(time.perf_counter() - started)
    timings.sort()
    median = timings[len(timings) // 2]
    print(f"spool put median {median * 1e3:.2f} ms, max {timings[-1] * 1e3:.2f} ms")
    assert median < 0.05


# ------------------------------------------------- end to end with a session


def test_a_manual_lock_during_a_database_outage_locks_and_spools(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import numpy as np

    from app.session import DeviceSession
    from tests.test_lock_acquisition_records import _Control, _Manager, _Parameters, _Stream

    db = FakeDatabase()
    db.down = True
    db.connect_delay_s = 1.0
    service = _service(tmp_path, db, monkeypatch)
    service.start()
    device = SimpleNamespace(key="dev-1", name="Laser A", host="h", port=1, parameters={})
    session = DeviceSession(device, _Manager(), lock_result_postgres_service=service)
    session.parameters = _Parameters(
        sweep_center=0.0, sweep_amplitude=1.0, sweep_pause=False, lock=False,
        target_slope_rising=True, modulation_frequency=0.0,
    )
    session.control = _Control(session)
    session.plot_state = _Stream()
    session.plot_state.show(np.linspace(-4000.0, 4000.0, 2048))
    try:
        time.sleep(0.05)
        started = time.perf_counter()
        acquisition_id = session.start_lock()
        elapsed = time.perf_counter() - started
        assert session.control.lock_commands == 1
        assert elapsed < 0.5, f"start_lock took {elapsed:.3f} s during an outage"
        record = service.spool.get(acquisition_id)
        assert record is not None and record["state"] in ("pending", "retrying")
        db.down = False
        db.connect_delay_s = 0.0
        assert _wait(lambda: acquisition_id in db.rows)
    finally:
        service.stop()
