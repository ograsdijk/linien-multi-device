from __future__ import annotations

from fastapi.testclient import TestClient

import app.main as main


def test_reboot_returns_accepted_operation(monkeypatch):
    class Session:
        def start_reboot(self):
            return {"operation_id": "operation-1"}

    monkeypatch.setattr(main, "_get_session", lambda _key: Session())
    client = TestClient(main.app)

    response = client.post("/api/devices/device-1/control/reboot")

    assert response.status_code == 202
    assert response.json() == {"ok": True, "operation_id": "operation-1"}
    assert response.headers["cache-control"] == "no-store"


def test_reboot_rejects_conflicting_operation(monkeypatch):
    class Session:
        def start_reboot(self):
            raise RuntimeError("A device recovery operation is already running")

    monkeypatch.setattr(main, "_get_session", lambda _key: Session())
    client = TestClient(main.app)

    response = client.post("/api/devices/device-1/control/reboot")

    assert response.status_code == 409
    assert response.json()["detail"] == "A device recovery operation is already running"
