"""Read-only lock-acquisition API and the PostgreSQL config endpoints."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

import app.main as main
import app.manual_lock_postgres as mlp
from app.lock_acquisition import LockAcquisition, TraceSnapshot
from app.manual_lock_postgres import (
    LockResultPostgresConfig,
    LockResultPostgresService,
    save_lock_result_postgres_config,
)
from app.manual_lock_record import build_acquisition_row


class _DownDatabase:
    """PostgreSQL that refuses every connection, naming host and user the way
    a real driver error does."""

    OperationalError = RuntimeError

    def connect(self, **kwargs):
        raise RuntimeError(
            f'connection to server at "{kwargs["host"]}", port {kwargs["port"]} '
            f'failed: password authentication failed for user "{kwargs["user"]}"'
        )


def _record(
    *,
    device_key: str = "dev-1",
    lock_source: str = "manual_lock",
    started: float | None = None,
    accepted: bool = True,
    verification: str = "verified",
) -> dict[str, Any]:
    acquisition = LockAcquisition(
        device_key=device_key, laser_name="Laser A", lock_source=lock_source
    )
    if started is not None:
        acquisition.attempt_started_at = started
    if accepted:
        acquisition.mark_lock_command(True, at=acquisition.attempt_started_at + 0.01)
    acquisition.verification_status = verification if accepted else "not_attempted"
    acquisition.linien_config = {"schema_version": 1, "parameters": {"p": 50}}
    acquisition.trace = TraceSnapshot(
        error_v=np.array([0.1, float("nan"), -0.1]), secondary_v=None,
        secondary_role=None, frame_id=3, frame_processed_at=acquisition.attempt_started_at,
        sweep_center_v=0.0, sweep_amplitude_v=1.0,
        geometry_read_at=acquisition.attempt_started_at, purpose="pre_lock_snapshot",
        sweep_paused=False,
    )
    return build_acquisition_row(acquisition)


@pytest.fixture
def service(tmp_path: Path, monkeypatch) -> LockResultPostgresService:
    monkeypatch.setattr(mlp, "psycopg", _DownDatabase())
    save_lock_result_postgres_config(
        LockResultPostgresConfig(enabled=True, host="db.lab.internal", password="s3cret"),
        tmp_path / "cfg.json",
    )
    svc = LockResultPostgresService(
        config_path=tmp_path / "cfg.json", spool_path=tmp_path / "spool.sqlite3"
    )
    monkeypatch.setattr(main, "lock_result_postgres", svc)
    return svc


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


# ----------------------------------------------------------------- listing


def test_list_falls_back_to_the_spool_and_says_so_without_leaking_the_error(
    service, client
):
    record = _record()
    service.enqueue_lock_result(record)

    response = client.get("/api/devices/dev-1/lock-acquisitions")

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "spool"
    assert body["postgres_unavailable_reason"] == "postgres_query_failed"
    assert "db.lab.internal" not in response.text
    assert "s3cret" not in response.text
    assert body["count"] == 1
    item = body["items"][0]
    assert item["lock_acquisition_id"] == record["lock_acquisition_id"]
    assert item["persistence"] == "pending"
    # Metadata only by default: no waveforms, no config snapshot.
    for heavy in ("trace_x", "trace_y", "secondary_trace_y", "linien_config", "refinement"):
        assert heavy not in item
    assert item["trace_meta"]["n_samples"] == 3


def test_include_adds_traces_and_config_with_nan_as_null(service, client):
    service.enqueue_lock_result(_record())

    item = client.get(
        "/api/devices/dev-1/lock-acquisitions?include=traces,config"
    ).json()["items"][0]

    assert item["trace_y"] == [0.1, None, -0.1]
    assert len(item["trace_x"]) == 3
    assert item["linien_config"]["parameters"] == {"p": 50}
    assert "refinement" not in item


def test_filters_by_device_time_source_and_status(service, client):
    now = time.time()
    rows = [
        _record(started=now - 300),
        _record(started=now - 200, lock_source="auto_relock", verification="failed"),
        _record(started=now - 100, accepted=False),
        _record(device_key="dev-2", started=now - 50),
    ]
    for row in rows:
        service.enqueue_lock_result(row)

    def ids(query: str) -> list[str]:
        response = client.get(f"/api/devices/dev-1/lock-acquisitions{query}")
        assert response.status_code == 200, response.text
        return [item["lock_acquisition_id"] for item in response.json()["items"]]

    newest_first = [rows[2], rows[1], rows[0]]
    assert ids("") == [r["lock_acquisition_id"] for r in newest_first]
    assert ids("?lock_source=auto_relock") == [rows[1]["lock_acquisition_id"]]
    assert ids("?verification_status=verified") == [rows[0]["lock_acquisition_id"]]
    assert ids("?success=false") == [rows[2]["lock_acquisition_id"]]
    assert ids(f"?since={now - 250}&until={now - 150}") == [rows[1]["lock_acquisition_id"]]
    assert ids("?limit=1&offset=1") == [rows[1]["lock_acquisition_id"]]


@pytest.mark.parametrize(
    "query",
    ["?lock_source=nltl_scan", "?verification_status=maybe", "?include=everything", "?limit=0"],
)
def test_invalid_filters_are_rejected(service, client, query):
    assert client.get(f"/api/devices/dev-1/lock-acquisitions{query}").status_code == 422


def test_list_reads_postgres_when_it_answers(service, client, monkeypatch):
    persisted = {**_record(), "created_at": 1.0, "updated_at": 2.0}
    persisted.pop("trace_y")
    service.enqueue_lock_result(_record())  # one more, still unsent
    captured: dict[str, Any] = {}

    def answer(query):
        captured["sql"], captured["params"] = query
        return [persisted], None

    monkeypatch.setattr(service, "_postgres_rows", answer)

    body = client.get("/api/devices/dev-1/lock-acquisitions?lock_source=manual_lock").json()

    assert body["source"] == "postgres"
    assert body["unsent_records"] == 1
    assert body["items"][0]["persistence"] == "persisted"
    assert "linien.pdh_lock_results" in captured["sql"]
    selected = captured["sql"].split(" FROM ")[0].removeprefix("SELECT ").split(", ")
    assert "trace_y" not in selected and "linien_config" not in selected
    assert captured["params"][:2] == ["dev-1", "manual_lock"]


# ------------------------------------------------------------- one record


def test_get_one_returns_the_full_record(service, client):
    record = _record()
    service.enqueue_lock_result(record)

    response = client.get(f"/api/lock-acquisitions/{record['lock_acquisition_id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["lock_acquisition_id"] == record["lock_acquisition_id"]
    assert body["trace_y"] == [0.1, None, -0.1]
    assert body["linien_config"]["schema_version"] == 1
    assert body["source"] == "spool"


def test_get_one_unknown_or_malformed(service, client):
    assert client.get(f"/api/lock-acquisitions/{uuid.uuid4()}").status_code == 404
    assert client.get("/api/lock-acquisitions/not-a-uuid").status_code == 422


# ------------------------------------------------- config endpoints and secrets


def test_the_config_endpoint_never_returns_the_password(service, client):
    body = client.get("/api/postgres/manual-lock").json()

    assert body["config"]["password"] == ""
    assert body["config"]["password_set"] is True
    assert "s3cret" not in str(body)
    assert body["config"]["db_schema"] == "linien"
    for key in ("pending_count", "retrying_count", "persisted_count", "failed_count"):
        assert key in body["status"]


def test_saving_back_the_read_config_keeps_the_stored_password(service, client):
    config = client.get("/api/postgres/manual-lock").json()["config"]

    response = client.put(
        "/api/postgres/manual-lock", json={**config, "connect_timeout_s": 4.0}
    )

    assert response.status_code == 200
    assert service._connect_kwargs()["password"] == "s3cret"
    assert service._connect_kwargs()["connect_timeout"] == 4

    client.put("/api/postgres/manual-lock", json={**config, "password": "n3w"})
    assert service._connect_kwargs()["password"] == "n3w"


def test_connection_errors_never_carry_a_password(service, monkeypatch):
    class Leaky:
        def connect(self, **_kwargs):
            raise RuntimeError("invalid dsn: host=db password=s3cret user=admin")

    monkeypatch.setattr(mlp, "psycopg", Leaky())

    ok, detail = service.test_connection()

    assert not ok
    assert "s3cret" not in detail
    assert "s3cret" not in str(service.get_state())
