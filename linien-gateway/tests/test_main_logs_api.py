import logging

from fastapi.testclient import TestClient

import app.main as main
from app.log_store import LogStore


def test_logs_tail_and_clear(monkeypatch):
    store = LogStore(max_entries=100, max_age_s=3600.0)
    monkeypatch.setattr(main, "log_store", store)
    client = TestClient(main.app)

    store.emit(
        level=logging.ERROR,
        source="lock",
        code="manual_lock_failed",
        message="Manual lock failed.",
        device_key="dev-a",
        details={"error": "boom"},
    )
    store.emit(
        level=logging.INFO,
        source="lock",
        code="manual_lock_attempt",
        message="Manual lock command accepted.",
        device_key="dev-a",
    )

    res = client.get("/api/logs/tail?limit=10")
    assert res.status_code == 200
    payload = res.json()
    assert "entries" in payload
    assert len(payload["entries"]) == 2
    assert payload["entries"][0]["level_name"] == "error"
    assert payload["entries"][1]["level_name"] == "info"

    clear_res = client.delete("/api/logs")
    assert clear_res.status_code == 200
    assert clear_res.json()["ok"] is True
    assert clear_res.json()["cleared"] == 2

    empty_res = client.get("/api/logs/tail?limit=10")
    assert empty_res.status_code == 200
    assert empty_res.json()["entries"] == []


def test_logs_stream_pushes_entries(monkeypatch):
    store = LogStore(max_entries=100, max_age_s=3600.0)
    monkeypatch.setattr(main, "log_store", store)

    with TestClient(main.app) as client:
        with client.websocket_connect("/api/logs/stream") as ws:
            store.emit(
                level=logging.WARNING,
                source="auto_relock",
                code="auto_relock_attempt",
                message="Auto-relock attempt started.",
                device_key="dev-b",
            )
            msg = ws.receive_json()
            assert msg["type"] == "log"
            assert msg["entry"]["code"] == "auto_relock_attempt"
            assert msg["entry"]["level"] == logging.WARNING
