import threading

from fastapi.testclient import TestClient

import app.main as main

TRACE = {
    "timestamp": 1.0,
    "lock": False,
    "dual_channel": False,
    "sweep_center": 0.0,
    "sweep_amplitude": 1.0,
    "n_points": 3,
    "x": [-1.0, 0.0, 1.0],
    "x_unit": "V",
    "combined_error": [0.1, 0.2, 0.3],
    "error_signal_1": [0.0, 0.0, 0.0],
    "error_signal_2": None,
    "monitor_signal": [0.0, 0.0, 0.0],
}


class FakeTraceSession:
    """Stands in for a DeviceSession in acquire-scan endpoint tests."""

    def __init__(self, connected: bool = True, capture_error: str | None = None):
        self.control = object() if connected else None
        self.capture_error = capture_error
        self.calls: list[tuple] = []
        self._lock = threading.Lock()

    def _record(self, *call) -> None:
        with self._lock:
            self.calls.append(call)

    def set_param(self, name, value, write_registers) -> None:
        self._record("set_param", name, value, write_registers)

    def start_sweep(self) -> None:
        if self.control is None:
            raise RuntimeError("Device not connected")
        self._record("start_sweep")

    def set_csr_direct(self, key, value) -> None:
        self._record("set_csr_direct", key, value)

    def wait_for_fresh_trace(self, timeout_s=None):
        if self.capture_error:
            raise RuntimeError(self.capture_error)
        return dict(TRACE)


def test_acquire_scan_single_triggers_then_returns_trace(monkeypatch):
    session = FakeTraceSession()
    monkeypatch.setattr(main, "_get_session", lambda key: session)

    client = TestClient(main.app)
    response = client.post("/api/devices/dev/control/acquire_scan")
    assert response.status_code == 200
    assert response.json()["combined_error"] == [0.1, 0.2, 0.3]

    # Stopped (run=0) then synchronized start (run=1), with a sweep trigger.
    assert ("start_sweep",) in session.calls
    assert ("set_csr_direct", "logic_sweep_run", 0) in session.calls
    assert ("set_csr_direct", "logic_sweep_run", 1) in session.calls
    run_writes = [c for c in session.calls if c[0] == "set_csr_direct"]
    assert run_writes.index(("set_csr_direct", "logic_sweep_run", 0)) < run_writes.index(
        ("set_csr_direct", "logic_sweep_run", 1)
    )


def test_acquire_scan_single_not_connected_conflict(monkeypatch):
    monkeypatch.setattr(main, "_get_session", lambda key: FakeTraceSession(connected=False))
    client = TestClient(main.app)
    response = client.post("/api/devices/dev/control/acquire_scan")
    assert response.status_code == 409


def test_acquire_scan_simultaneous_collects_and_skips(monkeypatch):
    a = FakeTraceSession(connected=True)
    b = FakeTraceSession(connected=True, capture_error="Timed out waiting for a sweep trace")
    c = FakeTraceSession(connected=False)
    registry = {"a": a, "b": b, "c": c}
    monkeypatch.setattr(main.session_registry, "get", lambda key: registry.get(key))

    client = TestClient(main.app)
    response = client.post(
        "/api/control/acquire_scan",
        json={"device_keys": ["a", "b", "c", "missing"]},
    )
    assert response.status_code == 200
    body = response.json()

    # Only the device that produced a trace appears under `traces`.
    assert set(body["traces"].keys()) == {"a"}
    assert body["traces"]["a"]["n_points"] == 3

    # The rest are reported (batch does not fail).
    assert set(body["skipped"].keys()) == {"b", "c", "missing"}
    assert body["skipped"]["c"] == "unconnected"
    assert body["skipped"]["missing"] == "unconnected"
    assert "timed out" in body["skipped"]["b"].lower()

    # The connected device was stopped then synchronously restarted.
    assert ("set_csr_direct", "logic_sweep_run", 0) in a.calls
    assert ("set_csr_direct", "logic_sweep_run", 1) in a.calls


def test_acquire_scan_validation(monkeypatch):
    monkeypatch.setattr(main.session_registry, "get", lambda key: None)
    client = TestClient(main.app)

    assert (
        client.post("/api/control/acquire_scan", json={"device_keys": []}).status_code
        == 422
    )
    assert (
        client.post(
            "/api/control/acquire_scan",
            json={"device_keys": ["a"], "timeout_s": 0},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/control/acquire_scan",
            json={"device_keys": ["a"], "timeout_s": 120},
        ).status_code
        == 422
    )
