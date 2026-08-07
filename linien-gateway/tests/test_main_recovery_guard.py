from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.main as main


def test_device_mutation_is_blocked_during_recovery(monkeypatch):
    session = SimpleNamespace(recovery_active=lambda: True)
    monkeypatch.setattr(main.session_registry, "get", lambda _key: session)

    response = TestClient(main.app).post("/api/devices/device-1/control/start_lock")

    assert response.status_code == 409
    assert response.json()["detail"] == "Device recovery is running for device-1"


def test_read_and_candidate_routes_are_not_blocked(monkeypatch):
    session = SimpleNamespace(recovery_active=lambda: True)
    monkeypatch.setattr(main.session_registry, "get", lambda _key: session)
    monkeypatch.setattr(main, "_get_device_or_404", lambda _key: object())
    monkeypatch.setattr(
        main,
        "_get_session",
        lambda _key: SimpleNamespace(status=lambda: {"connected": False, "connecting": False}),
    )

    response = TestClient(main.app).get("/api/devices/device-1/status")

    assert response.status_code == 200


def test_batch_mutation_is_atomic_when_one_device_is_recovering(monkeypatch):
    sessions = {
        "device-1": SimpleNamespace(recovery_active=lambda: False),
        "device-2": SimpleNamespace(recovery_active=lambda: True),
    }
    monkeypatch.setattr(main.session_registry, "get", sessions.get)

    response = TestClient(main.app).post(
        "/api/control/start_sweep",
        json={"device_keys": ["device-1", "device-2"]},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Device recovery is running for device-2"
