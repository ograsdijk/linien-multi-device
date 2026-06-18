from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

import app.main as main


def test_unknown_key_status_params_404_without_allocating_lock(monkeypatch):
    # No such device anywhere.
    monkeypatch.setattr(main.device_store, "get_device", lambda _key: None)

    client = TestClient(main.app)
    assert "ghost-key" not in main.session_registry._key_locks

    assert client.get("/api/devices/ghost-key/status").status_code == 404
    assert client.get("/api/devices/ghost-key/params").status_code == 404

    # The per-key lock must NOT have been created for the unknown key (#53).
    assert "ghost-key" not in main.session_registry._key_locks


def test_unknown_key_stream_rejected_without_allocating_lock(monkeypatch):
    monkeypatch.setattr(main.device_store, "get_device", lambda _key: None)

    client = TestClient(main.app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/devices/ghost-stream/stream"):
            pass

    assert "ghost-stream" not in main.session_registry._key_locks
