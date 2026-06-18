import threading

from fastapi.testclient import TestClient

import app.main as main


class FakeSession:
    """Records control calls so we can assert orchestration behavior."""

    def __init__(self, connected: bool):
        # `control is None` is how the gateway detects an unconnected device.
        self.control = object() if connected else None
        self.calls: list[tuple] = []
        self._lock = threading.Lock()

    def _record(self, *call) -> None:
        with self._lock:
            self.calls.append(call)

    def set_param(self, name, value, write_registers) -> None:
        self._record("set_param", name, value, write_registers)

    def start_sweep(self) -> None:
        self._record("start_sweep")

    def set_csr_direct(self, key, value) -> None:
        self._record("set_csr_direct", key, value)


def _patch_registry(monkeypatch, registry: dict) -> None:
    monkeypatch.setattr(main.session_registry, "get", lambda key: registry.get(key))


def test_simultaneous_sweep_skips_unconnected_and_triggers_connected(monkeypatch):
    a = FakeSession(connected=True)
    b = FakeSession(connected=True)
    c = FakeSession(connected=False)
    _patch_registry(monkeypatch, {"a": a, "b": b, "c": c})

    client = TestClient(main.app)
    response = client.post(
        "/api/control/start_sweep",
        json={"device_keys": ["a", "b", "c", "missing"], "sweep_speed": 6},
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body["started"]) == {"a", "b"}
    assert set(body["skipped_unconnected"]) == {"c", "missing"}
    assert body["sweep_speed"] == 6

    for session in (a, b):
        names = [call[0] for call in session.calls]
        assert names.count("start_sweep") == 1
        assert ("set_param", "sweep_speed", 6, False) in session.calls
        # Forced run edge: parked at center then re-enabled.
        assert ("set_csr_direct", "logic_sweep_run", 0) in session.calls
        assert ("set_csr_direct", "logic_sweep_run", 1) in session.calls

    # The unconnected session must not be touched at all.
    assert c.calls == []


def test_simultaneous_sweep_without_restart_skips_run_edge(monkeypatch):
    a = FakeSession(connected=True)
    _patch_registry(monkeypatch, {"a": a})

    client = TestClient(main.app)
    response = client.post(
        "/api/control/start_sweep",
        json={"device_keys": ["a"], "restart_from_center": False},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["started"] == ["a"]
    assert body["sweep_speed"] is None
    # No sweep_speed -> set_param not called; no restart -> no CSR writes.
    names = [call[0] for call in a.calls]
    assert names == ["start_sweep"]


def test_simultaneous_sweep_validation(monkeypatch):
    _patch_registry(monkeypatch, {})
    client = TestClient(main.app)

    # Empty device list is rejected.
    assert (
        client.post("/api/control/start_sweep", json={"device_keys": []}).status_code
        == 422
    )
    # sweep_speed out of range (0..15).
    assert (
        client.post(
            "/api/control/start_sweep",
            json={"device_keys": ["a"], "sweep_speed": 16},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/control/start_sweep",
            json={"device_keys": ["a"], "sweep_speed": -1},
        ).status_code
        == 422
    )
