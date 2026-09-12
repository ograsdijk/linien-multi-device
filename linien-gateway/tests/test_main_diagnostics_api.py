from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.board_event_store import KIND_DISCONNECTED, KIND_PERSISTENT_LOG_ENABLED


def make_device(key="dev-1"):
    return SimpleNamespace(
        key=key,
        name=key,
        host="10.0.0.1",
        port=18862,
        username="root",
        password="root",
        parameters={},
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main.device_store, "get_device", lambda key: make_device(key))
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def clean_timeline():
    yield
    for key in ("dev-1", "a", "b"):
        main.board_event_store.forget(key)


# --- timeline ------------------------------------------------------------


def test_the_events_endpoint_does_no_io(client, monkeypatch):
    """It is a cache read, like the telemetry status endpoint."""

    def boom(*_args, **_kwargs):
        raise AssertionError("the events endpoint must not open an SSH session")

    monkeypatch.setattr(main.board_diagnostics, "collect_diagnostics", boom)
    main.board_event_store.record("dev-1", KIND_DISCONNECTED, detail="poll failed")

    payload = client.get("/api/devices/dev-1/events").json()

    assert [event["detail"] for event in payload["events"]] == ["poll failed"]


def test_an_unknown_device_is_a_404(client, monkeypatch):
    monkeypatch.setattr(main.device_store, "get_device", lambda key: None)

    assert client.get("/api/devices/nope/events").status_code == 404


def test_transition_log_events_land_in_the_timeline(client):
    """The hook is `_emit_log`, so no poll path had to change to feed this."""
    main._emit_log(
        logging.WARNING,
        "session",
        "poll_failure",
        "RPyC poll failed: connection reset",
        "dev-1",
    )

    payload = client.get("/api/devices/dev-1/events").json()

    assert payload["events"][0]["kind"] == KIND_DISCONNECTED
    assert "connection reset" in payload["events"][0]["detail"]


def test_ordinary_log_events_do_not_flood_the_timeline(client):
    """Only once-per-transition codes are mirrored; the rest stay in the log."""
    main._emit_log(logging.INFO, "session", "lock_acquired", "Locked.", "dev-1")

    assert client.get("/api/devices/dev-1/events").json()["events"] == []


# --- collection ----------------------------------------------------------


def test_collect_returns_the_bundle(client, monkeypatch):
    bundle = {
        "ok": True,
        "error": None,
        "collected_at": 1.0,
        "sections": [{"name": "kernel", "title": "K", "command": "c", "output": "o", "error": None}],
        "persistent_journal": False,
    }
    monkeypatch.setattr(
        main.board_diagnostics, "collect_diagnostics", lambda device: bundle
    )

    payload = client.post("/api/devices/dev-1/diagnostics/collect").json()

    assert payload == bundle


def test_a_board_that_stopped_answering_is_reported_not_a_500(client, monkeypatch):
    monkeypatch.setattr(
        main.board_diagnostics,
        "collect_diagnostics",
        lambda device: {
            "ok": False,
            "error": "no route to host",
            "collected_at": 1.0,
            "sections": [],
            "persistent_journal": None,
        },
    )

    response = client.post("/api/devices/dev-1/diagnostics/collect")

    assert response.status_code == 200
    assert response.json()["ok"] is False


# --- enabling persistence ------------------------------------------------


def test_enabling_persistence_records_it_on_the_timeline(client, monkeypatch):
    monkeypatch.setattr(
        main.board_diagnostics,
        "enable_persistent_journal",
        lambda device: {"ok": True, "persistent_journal": True},
    )

    response = client.post("/api/devices/dev-1/diagnostics/enable-persistent-log")

    assert response.status_code == 200
    assert response.json()["persistent_journal"] is True
    events = client.get("/api/devices/dev-1/events").json()["events"]
    assert events[0]["kind"] == KIND_PERSISTENT_LOG_ENABLED


def test_a_failure_to_enable_is_a_409_with_the_reason(client, monkeypatch):
    def boom(device):
        raise RuntimeError("journald would not restart")

    monkeypatch.setattr(main.board_diagnostics, "enable_persistent_journal", boom)

    response = client.post("/api/devices/dev-1/diagnostics/enable-persistent-log")

    assert response.status_code == 409
    assert "journald would not restart" in response.json()["detail"]
    # Nothing claimed on the timeline for an action that did not happen.
    assert client.get("/api/devices/dev-1/events").json()["events"] == []


def test_bulk_enable_reports_per_device_outcomes(monkeypatch):
    devices = {key: make_device(key) for key in ("a", "b")}
    monkeypatch.setattr(main.device_store, "get_device", lambda key: devices.get(key))

    def enable(device):
        if device.key == "b":
            raise RuntimeError("ssh timed out")
        return {"ok": True, "persistent_journal": True}

    monkeypatch.setattr(main.board_diagnostics, "enable_persistent_journal", enable)
    client = TestClient(main.app)

    payload = client.post(
        "/api/diagnostics/enable-persistent-log",
        json={"device_keys": ["a", "b", "missing"]},
    ).json()

    assert payload["enabled"] == ["a"]
    assert payload["failed"]["b"] == "ssh timed out"
    assert payload["failed"]["missing"] == "Device not found"
