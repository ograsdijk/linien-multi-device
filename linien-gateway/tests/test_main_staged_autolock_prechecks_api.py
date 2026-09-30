"""Route-level tests for fix #1: `staged_autolock/begin` and
`auto_lock_candidates?acquire=true` must refuse BEFORE triggering the
restart-and-capture sweep mechanism (which switches the lock OFF via
`start_sweep()`), not after.

Pre-fix, both routes called `_trigger_and_acquire` unconditionally first and
only asked whether the request should be allowed once `session.<verb>` ran --
so `begin` on a locked laser unlocked it, and a second `begin` during an
active run restarted the sweep underneath that run.
"""

from fastapi.testclient import TestClient

import app.main as main
from app.session import StagedAutolockError


class FakePrecheckSession:
    """A DeviceSession stand-in exposing only the begin/acquire surface.

    `start_sweep` is the tell-tale: if the route ever gets that far before
    checking whether the request is allowed, the fix has regressed.
    """

    def __init__(self, *, begin_precheck_error=None, acquire_precheck_error=None):
        self.control = object()
        self.calls: list[tuple] = []
        self.begin_precheck_error = begin_precheck_error
        self.acquire_precheck_error = acquire_precheck_error

    def staged_autolock_begin_precheck(self) -> None:
        self.calls.append(("staged_autolock_begin_precheck",))
        if self.begin_precheck_error is not None:
            raise self.begin_precheck_error

    def auto_lock_candidates_acquire_precheck(self) -> None:
        self.calls.append(("auto_lock_candidates_acquire_precheck",))
        if self.acquire_precheck_error is not None:
            raise self.acquire_precheck_error

    def set_param(self, *a, **k) -> None:
        self.calls.append(("set_param", a, k))

    def start_sweep(self) -> None:
        self.calls.append(("start_sweep",))

    def set_csr_direct(self, *a) -> None:
        self.calls.append(("set_csr_direct", *a))

    def wait_for_fresh_trace(self, timeout_s=None, skip_frames=1):
        self.calls.append(("wait_for_fresh_trace", timeout_s))
        return {"frame_id": 1, "acquired_at": 1.0}

    def staged_autolock_begin(self, settings_payload, ttl_s):
        self.calls.append(("staged_autolock_begin", settings_payload, ttl_s))
        return {"token": "tok", "candidates": []}

    def auto_lock_candidates_detect(self, settings_payload):
        self.calls.append(("auto_lock_candidates_detect", settings_payload))
        return {
            "found": False, "candidate": None, "candidates": [],
            "reason": None, "frame": None, "detector": "strict",
        }

    def staged_autolock_observe_frame(self, frame, candidates) -> None:
        self.calls.append(("staged_autolock_observe_frame", frame, candidates))


def _patch(monkeypatch, session):
    device = type("Device", (), {"key": "dev", "name": "dev", "parameters": {}})()
    monkeypatch.setattr(main.device_store, "get_device", lambda _key: device)
    monkeypatch.setattr(main, "_session_for_device", lambda _device: session)


def test_begin_refuses_locked_device_before_ever_triggering_a_sweep(monkeypatch):
    session = FakePrecheckSession(
        begin_precheck_error=RuntimeError("Device is already locked. Start sweep first.")
    )
    _patch(monkeypatch, session)
    client = TestClient(main.app)

    response = client.post("/api/devices/dev/control/staged_autolock/begin", json=None)
    assert response.status_code == 409
    assert "locked" in response.text.lower()
    assert not any(c[0] == "start_sweep" for c in session.calls)
    assert not any(c[0] == "staged_autolock_begin" for c in session.calls)


def test_begin_succeeds_when_the_precheck_allows_it(monkeypatch):
    session = FakePrecheckSession()
    _patch(monkeypatch, session)
    client = TestClient(main.app)

    response = client.post("/api/devices/dev/control/staged_autolock/begin", json=None)
    assert response.status_code == 200
    assert ("staged_autolock_begin_precheck",) in session.calls
    assert any(c[0] == "start_sweep" for c in session.calls)
    precheck_index = session.calls.index(("staged_autolock_begin_precheck",))
    trigger_index = next(i for i, c in enumerate(session.calls) if c[0] == "start_sweep")
    assert precheck_index < trigger_index


def test_staged_lock_failure_log_keeps_verification_diagnostics(monkeypatch):
    verification = {
        "kind": "final_verify",
        "signed_drift_v": -0.002,
        "time_since_last_geometry_change_s": 1.25,
        "acceptance_passed": False,
    }

    class FailedLockSession:
        def staged_autolock_lock(self, *_args):
            raise StagedAutolockError(
                "final pair outside capture window",
                status_code=422,
                details={"verification": verification},
            )

    log_records = []
    monkeypatch.setattr(main, "_get_session", lambda _key: FailedLockSession())
    monkeypatch.setattr(
        main, "_emit_log", lambda **kwargs: log_records.append(kwargs)
    )
    monkeypatch.setattr(main, "_enqueue_auto_lock_row", lambda *_args, **_kwargs: None)
    client = TestClient(main.app)
    response = client.post(
        "/api/devices/dev/control/staged_autolock/token/lock",
        json={"selected": {"frame_id": 1, "target_index": 2}},
    )
    assert response.status_code == 422
    assert log_records[0]["details"]["verification"] == verification


def test_acquire_true_refuses_locked_device_before_ever_triggering_a_sweep(monkeypatch):
    session = FakePrecheckSession(
        acquire_precheck_error=RuntimeError("Device is already locked. Start sweep first.")
    )
    _patch(monkeypatch, session)
    client = TestClient(main.app)

    response = client.post(
        "/api/devices/dev/control/auto_lock_candidates?acquire=true", json=None
    )
    assert response.status_code == 409
    assert "locked" in response.text.lower()
    assert not any(c[0] == "start_sweep" for c in session.calls)
    assert not any(c[0] == "auto_lock_candidates_detect" for c in session.calls)


def test_step_route_maps_a_raw_value_error_to_422_not_500(monkeypatch):
    """Fix #4 (route-level defensive mapping): `session.staged_autolock_step`
    converts every detection failure into `StagedAutolockError(422)` itself,
    but the route must never let a bare `ValueError` surface as the generic
    500 it used to when nothing caught it.
    """

    class RawValueErrorSession:
        control = object()

        def staged_autolock_step(self, token, frame_id, target_index, extra_tolerance_v=0.0):
            raise ValueError("no candidate at the new geometry")

    session = RawValueErrorSession()
    _patch(monkeypatch, session)
    client = TestClient(main.app)

    response = client.post(
        "/api/devices/dev/control/staged_autolock/tok/step",
        json={"selected": {"frame_id": 1, "target_index": 1}},
    )
    assert response.status_code == 422
    assert "no candidate at the new geometry" in response.json()["detail"]
