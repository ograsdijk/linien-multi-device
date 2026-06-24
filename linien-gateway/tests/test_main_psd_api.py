import time

from fastapi.testclient import TestClient

import app.main as main
from app.psd_store import PsdStore


def _entry(uuid: str, *, complete: bool = True) -> dict:
    return {
        "device_key": "dev-a",
        "uuid": uuid,
        "time": time.time(),
        "p": 2500,
        "i": 1800,
        "d": 0,
        "fitness": 1.25,
        "complete": complete,
        "curve": [{"f": 1.0, "psd": 1e-6}, {"f": 2.0, "psd": 5e-7}],
    }


def test_psd_tail_and_clear(monkeypatch):
    store = PsdStore(max_entries=100, max_age_s=3600.0)
    monkeypatch.setattr(main, "psd_store", store)
    client = TestClient(main.app)

    store.emit(_entry("aaaa"))
    store.emit(_entry("bbbb"))

    res = client.get("/api/psd/tail?limit=10")
    assert res.status_code == 200
    payload = res.json()
    assert "entries" in payload
    assert len(payload["entries"]) == 2
    assert {e["uuid"] for e in payload["entries"]} == {"aaaa", "bbbb"}

    clear_res = client.delete("/api/psd")
    assert clear_res.status_code == 200
    assert clear_res.json()["ok"] is True
    assert clear_res.json()["cleared"] == 2

    empty_res = client.get("/api/psd/tail?limit=10")
    assert empty_res.status_code == 200
    assert empty_res.json()["entries"] == []


def test_psd_stream_pushes_entries(monkeypatch):
    store = PsdStore(max_entries=100, max_age_s=3600.0)
    monkeypatch.setattr(main, "psd_store", store)

    with TestClient(main.app) as client:
        with client.websocket_connect("/api/psd/stream") as ws:
            # Partial then complete: both should reach a live subscriber.
            store.emit(_entry("cccc", complete=False))
            partial = ws.receive_json()
            assert partial["type"] == "psd"
            assert partial["entry"]["uuid"] == "cccc"
            assert partial["entry"]["complete"] is False

            store.emit(_entry("cccc", complete=True))
            done = ws.receive_json()
            assert done["entry"]["complete"] is True
            assert done["entry"]["curve"][0]["f"] == 1.0


def test_start_psd_unconnected_device_is_skipped(monkeypatch):
    # No session registered for "ghost" -> reported under skipped, not a 500.
    client = TestClient(main.app)
    res = client.post(
        "/api/control/start_psd_acquisition",
        json={"device_keys": ["ghost"], "max_decimation": 16},
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload["started"] == []
    assert payload["skipped"].get("ghost") == "unconnected"
