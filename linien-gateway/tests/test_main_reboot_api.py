from __future__ import annotations

from fastapi.testclient import TestClient

import app.main as main


def test_reboot_returns_accepted_operation(monkeypatch):
    class Session:
        def start_reboot(self):
            return {"operation_id": "operation-1"}

    monkeypatch.setattr(main, "_get_session", lambda _key: Session())
    monkeypatch.setattr(main, "get_reboot_admin_token", lambda: "admin-secret")
    client = TestClient(main.app)

    response = client.post(
        "/api/devices/device-1/control/reboot",
        headers={"X-Linien-Admin-Token": "admin-secret"},
    )

    assert response.status_code == 202
    assert response.json() == {"ok": True, "operation_id": "operation-1"}
    assert response.headers["cache-control"] == "no-store"


def test_reboot_rejects_conflicting_operation(monkeypatch):
    class Session:
        def start_reboot(self):
            raise RuntimeError("A device recovery operation is already running")

    monkeypatch.setattr(main, "_get_session", lambda _key: Session())
    monkeypatch.setattr(main, "get_reboot_admin_token", lambda: "admin-secret")
    client = TestClient(main.app)

    response = client.post(
        "/api/devices/device-1/control/reboot",
        headers={"X-Linien-Admin-Token": "admin-secret"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "A device recovery operation is already running"


def test_reboot_is_disabled_without_configured_token(monkeypatch):
    called = False

    def get_session(_key):
        nonlocal called
        called = True

    monkeypatch.setattr(main, "_get_session", get_session)
    monkeypatch.setattr(main, "get_reboot_admin_token", lambda: None)

    response = TestClient(main.app).post("/api/devices/device-1/control/reboot")

    assert response.status_code == 503
    assert called is False


def test_reboot_requires_valid_admin_token(monkeypatch):
    monkeypatch.setattr(main, "get_reboot_admin_token", lambda: "admin-secret")
    client = TestClient(main.app)

    missing = client.post("/api/devices/device-1/control/reboot")
    wrong = client.post(
        "/api/devices/device-1/control/reboot",
        headers={"X-Linien-Admin-Token": "wrong"},
    )

    assert missing.status_code == 401
    assert wrong.status_code == 403
