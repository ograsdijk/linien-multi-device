"""The lock-record schema and upsert against a real PostgreSQL.

Skipped unless LINIEN_TEST_POSTGRES_DSN points at a disposable database, e.g.
with the bundled container (docker/postgres):

    LINIEN_TEST_POSTGRES_DSN="host=127.0.0.1 port=5432 dbname=experiment_db user=admin password=adminpassword"

Each test works in its own throwaway schema and drops it afterwards.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("LINIEN_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="LINIEN_TEST_POSTGRES_DSN not set")

from app.lock_acquisition import LockAcquisition, TraceSnapshot  # noqa: E402
from app.manual_lock_postgres import (  # noqa: E402
    LockResultPostgresConfig,
    LockResultPostgresService,
    SchemaMismatchError,
    prepare_row,
    render_init_sql,
    render_upsert_sql,
    save_lock_result_postgres_config,
)
from app.manual_lock_record import build_acquisition_row  # noqa: E402


@pytest.fixture
def schema():
    name = f"linien_test_{uuid.uuid4().hex[:8]}"
    yield name
    with psycopg.connect(DSN) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")


def _service(tmp_path: Path, schema: str) -> LockResultPostgresService:
    params = dict(item.split("=", 1) for item in DSN.split())
    cfg = LockResultPostgresConfig(
        enabled=True,
        host=params.get("host", "127.0.0.1"),
        port=int(params.get("port", 5432)),
        database=params.get("dbname", "postgres"),
        user=params.get("user", "postgres"),
        password=params.get("password", ""),
        sslmode=params.get("sslmode", "prefer"),
        db_schema=schema,
    )
    save_lock_result_postgres_config(cfg, tmp_path / "cfg.json")
    return LockResultPostgresService(
        config_path=tmp_path / "cfg.json", spool_path=tmp_path / "spool.sqlite3"
    )


def _full_row() -> dict:
    acquisition = LockAcquisition(
        device_key="dev-1", laser_name="Laser A", lock_source="auto_relock"
    )
    acquisition.lock_command_status = "accepted"
    acquisition.lock_command_at = time.time()
    acquisition.verification_status = "pending"
    acquisition.linien_config = {"schema_version": 1, "parameters": {"p": float("nan")}}
    import numpy as np

    acquisition.trace = TraceSnapshot(
        error_v=np.array([0.1, float("nan"), -0.1]), secondary_v=None,
        secondary_role=None, frame_id=4, frame_processed_at=time.time(),
        sweep_center_v=0.0, sweep_amplitude_v=1.0, geometry_read_at=time.time(),
        purpose="final_detection", sweep_paused=False,
    )
    return build_acquisition_row(acquisition)


def test_the_init_script_runs_and_matches_what_the_writer_expects(tmp_path, schema):
    with psycopg.connect(DSN) as conn:
        conn.execute(render_init_sql(schema))
    service = _service(tmp_path, schema)

    ok, detail = service.test_connection()

    assert ok, detail


def test_a_full_record_upserts_once_and_updates_in_place(tmp_path, schema):
    service = _service(tmp_path, schema)
    assert service.test_connection()[0]
    row = _full_row()
    upsert = render_upsert_sql(schema)
    with psycopg.connect(DSN) as conn:
        conn.execute(upsert, prepare_row(row))
        conn.execute(upsert, prepare_row({**row, "verification_status": "verified"}))
        count, status, trace, meta, cmd_at, verified_at = conn.execute(
            f"SELECT COUNT(*), MAX(verification_status), MAX(trace_y), "
            f"MAX(trace_meta::text), MAX(lock_command_at), MAX(verification_completed_at) "
            f"FROM {schema}.pdh_lock_results WHERE lock_acquisition_id = %s",
            (row["lock_acquisition_id"],),
        ).fetchone()
    assert count == 1
    assert status == "verified"
    assert len(trace) == 3
    assert '"frame_id": 4' in meta
    assert cmd_at is not None
    assert verified_at is None  # to_timestamp(NULL) stays NULL


def test_the_writer_drains_the_spool_into_postgres(tmp_path, schema):
    service = _service(tmp_path, schema)
    service.start()
    try:
        row = _full_row()
        assert service.enqueue_lock_result(row)
        deadline = time.time() + 10
        while time.time() < deadline and service.get_state()["status"]["persisted_count"] < 1:
            time.sleep(0.05)
    finally:
        service.stop()
    assert service.get_state()["status"]["persisted_count"] == 1


def test_a_check_constraint_violation_is_a_permanent_failure(tmp_path, schema):
    service = _service(tmp_path, schema)
    assert service.test_connection()[0]
    service.start()
    try:
        service.enqueue_lock_result({**_full_row(), "lock_source": "not_a_source"})
        deadline = time.time() + 10
        while time.time() < deadline and service.get_state()["status"]["failed_count"] < 1:
            time.sleep(0.05)
    finally:
        service.stop()
    assert service.get_state()["status"]["failed_count"] == 1


def test_a_foreign_table_in_the_schema_is_refused(tmp_path, schema):
    with psycopg.connect(DSN) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"CREATE TABLE {schema}.pdh_lock_results (id BIGSERIAL PRIMARY KEY)")
    service = _service(tmp_path, schema)

    ok, detail = service.test_connection()

    assert not ok and "no schema version" in detail
    assert SchemaMismatchError.__name__  # imported for readers of this test


def test_the_read_queries_return_what_was_written(tmp_path, schema):
    from app.manual_lock_postgres import RecordFilters

    service = _service(tmp_path, schema)
    assert service.test_connection()[0]
    row = _full_row()
    with psycopg.connect(DSN) as conn:
        conn.execute(render_upsert_sql(schema), prepare_row(row))

    listed = service.list_records(
        device_key="dev-1", filters=RecordFilters(lock_source="auto_relock"), limit=10
    )
    one = service.get_record(row["lock_acquisition_id"])

    assert listed["source"] == "postgres"
    assert [item["lock_acquisition_id"] for item in listed["items"]] == [
        row["lock_acquisition_id"]
    ]
    assert "trace_y" not in listed["items"][0]
    assert one["trace_y"] == [0.1, None, -0.1]
    assert one["attempt_started_at"] == pytest.approx(row["attempt_started_at"], abs=1e-3)
    assert one["linien_config"]["parameters"] == {"p": None}
