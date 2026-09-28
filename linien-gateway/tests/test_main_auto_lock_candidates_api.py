"""Endpoint tests for POST /api/devices/{key}/control/auto_lock_candidates.

Covers the serrodyne spec C1 contract: the response is a superset of the old
{"found", "candidate", "reason"} shape (adds "candidates" + "frame"), the new
`acquire`/`timeout_s` query params, and that the endpoint stays read-only.
"""

from fastapi.testclient import TestClient

import app.main as main


class FakeCandidatesSession:
    """Stands in for a DeviceSession in auto_lock_candidates endpoint tests.

    Mirrors the acquire_scan fakes (see test_acquire_scan_api.py): `control`
    truthy, plus the trigger/wait mechanics `_trigger_and_acquire` drives.
    `auto_lock_detect` reports whichever frame is "current" -- incremented by
    `wait_for_fresh_trace` -- so a test can tell whether the endpoint analysed
    the cached frame or a freshly acquired one.
    """

    def __init__(self, connected: bool = True, capture_error: str | None = None):
        self.control = object() if connected else None
        self.capture_error = capture_error
        self.calls: list[tuple] = []
        self._frame_id = 1
        self._acquired_at = 100.0
        self.detect_error: Exception | None = None

    def set_param(self, name, value, write_registers) -> None:
        self.calls.append(("set_param", name, value, write_registers))

    def start_sweep(self) -> None:
        self.calls.append(("start_sweep",))

    def set_csr_direct(self, key, value) -> None:
        self.calls.append(("set_csr_direct", key, value))

    def staged_autolock_observe_frame(self, frame, candidates) -> None:
        # No staged run in these tests; the endpoint calls this
        # unconditionally after every detection (see C2's "read-only calls
        # still advance the run's latest-seen frame"). Recorded so a test can
        # assert it was called if it ever needs to.
        self.calls.append(("staged_autolock_observe_frame", frame, candidates))

    def wait_for_fresh_trace(self, timeout_s=None):
        self.calls.append(("wait_for_fresh_trace", timeout_s))
        if self.capture_error:
            raise RuntimeError(self.capture_error)
        self._frame_id += 1
        self._acquired_at += 1.0
        return {"frame_id": self._frame_id, "acquired_at": self._acquired_at}

    def auto_lock_candidates_acquire_precheck(self) -> None:
        # Fix #1: the route runs this BEFORE triggering a sweep restart when
        # acquire=true. No-op here (these tests have no staged run/lock
        # state to refuse on); recorded so ordering/coverage can be asserted.
        self.calls.append(("auto_lock_candidates_acquire_precheck",))

    def auto_lock_candidates_detect(self, settings_payload, detector="strict", **coarse_opts):
        # The route calls this (not `auto_lock_detect` directly) since fix
        # #2b; with no staged run active it is exactly `auto_lock_detect`.
        self.calls.append(("auto_lock_candidates_detect", settings_payload))
        self.detector = detector
        self.coarse_opts = coarse_opts
        return self.auto_lock_detect(settings_payload)

    def auto_lock_detect(self, settings_payload, detector="strict"):
        self.calls.append(("auto_lock_detect", settings_payload))
        if self.detect_error is not None:
            raise self.detect_error
        return {
            "found": True,
            "candidate": {"target_index": 1, "score": 1.0},
            "candidates": [{"target_index": 1, "score": 1.0}],
            "reason": None,
            "frame": {
                "frame_id": self._frame_id,
                "acquired_at": self._acquired_at,
                "sweep_center_v": 0.0,
                "sweep_amplitude_v": 1.0,
                "n_points": 2048,
                "modulation_frequency_hz": 25.0e6,
                "sideband_spacing_samples": 50.0,
                "noise_floor": 0.001,
            },
        }


def _patch(monkeypatch, session):
    device = type("Device", (), {"key": "dev", "name": "dev", "parameters": {}})()
    monkeypatch.setattr(main.device_store, "get_device", lambda _key: device)
    monkeypatch.setattr(main, "_session_for_device", lambda _device: session)


def test_default_acquire_false_uses_the_cached_frame_without_triggering(monkeypatch):
    session = FakeCandidatesSession()
    _patch(monkeypatch, session)
    client = TestClient(main.app)

    response = client.post("/api/devices/dev/control/auto_lock_candidates", json=None)
    assert response.status_code == 200
    body = response.json()
    assert body["found"] is True
    assert body["candidate"] == {"target_index": 1, "score": 1.0}
    assert body["candidates"] == [{"target_index": 1, "score": 1.0}]
    assert body["reason"] is None
    assert body["frame"]["frame_id"] == 1  # the initial cached frame, never bumped

    # No sweep restart/acquire mechanism touched.
    assert ("wait_for_fresh_trace", None) not in session.calls
    assert not any(call[0] == "start_sweep" for call in session.calls)
    assert ("auto_lock_detect", None) in session.calls


def test_acquire_true_triggers_a_new_frame_and_analyses_exactly_that_frame(monkeypatch):
    session = FakeCandidatesSession()
    _patch(monkeypatch, session)
    client = TestClient(main.app)

    response = client.post(
        "/api/devices/dev/control/auto_lock_candidates?acquire=true", json=None
    )
    assert response.status_code == 200
    body = response.json()

    # The trigger/restart/capture sequence ran (same mechanism as acquire_scan).
    assert ("start_sweep",) in session.calls
    assert ("set_csr_direct", "logic_sweep_run", 0) in session.calls
    assert ("set_csr_direct", "logic_sweep_run", 1) in session.calls
    assert any(call[0] == "wait_for_fresh_trace" for call in session.calls)

    # The reported frame is the FRESH one (frame_id bumped by wait_for_fresh_trace),
    # not the stale frame_id=1 that acquire=false would have reported.
    assert body["frame"]["frame_id"] == 2
    assert body["frame"]["acquired_at"] == 101.0

    # auto_lock_detect ran after the acquire, so it necessarily saw the new frame.
    wait_index = next(
        i for i, call in enumerate(session.calls) if call[0] == "wait_for_fresh_trace"
    )
    detect_index = next(
        i for i, call in enumerate(session.calls) if call[0] == "auto_lock_detect"
    )
    assert wait_index < detect_index


def test_acquire_true_propagates_timeout_s(monkeypatch):
    session = FakeCandidatesSession()
    _patch(monkeypatch, session)
    client = TestClient(main.app)

    response = client.post(
        "/api/devices/dev/control/auto_lock_candidates?acquire=true&timeout_s=5.0",
        json=None,
    )
    assert response.status_code == 200
    assert ("wait_for_fresh_trace", 5.0) in session.calls


def test_acquire_true_capture_failure_returns_409(monkeypatch):
    session = FakeCandidatesSession(capture_error="Timed out waiting for a sweep trace")
    _patch(monkeypatch, session)
    client = TestClient(main.app)

    response = client.post(
        "/api/devices/dev/control/auto_lock_candidates?acquire=true", json=None
    )
    assert response.status_code == 409
    assert "timed out" in response.text.lower()
    # Detection never ran on a capture failure.
    assert not any(call[0] == "auto_lock_detect" for call in session.calls)


def test_device_not_connected_maps_to_409(monkeypatch):
    session = FakeCandidatesSession()
    session.detect_error = RuntimeError("Device not connected")
    _patch(monkeypatch, session)
    client = TestClient(main.app)

    response = client.post("/api/devices/dev/control/auto_lock_candidates", json=None)
    assert response.status_code == 409
    assert "Device not connected" in response.text


def test_no_candidates_reports_found_false_with_frame_still_present(monkeypatch):
    class NoCandidateSession(FakeCandidatesSession):
        def auto_lock_detect(self, settings_payload):
            self.calls.append(("auto_lock_detect", settings_payload))
            return {
                "found": False,
                "candidate": None,
                "candidates": [],
                "reason": "No valid crossing passed the configured thresholds.",
                "frame": {
                    "frame_id": self._frame_id,
                    "acquired_at": self._acquired_at,
                    "sweep_center_v": 0.0,
                    "sweep_amplitude_v": 1.0,
                    "n_points": 2048,
                    "modulation_frequency_hz": None,
                    "sideband_spacing_samples": None,
                    "noise_floor": 0.001,
                },
            }

    session = NoCandidateSession()
    _patch(monkeypatch, session)
    client = TestClient(main.app)

    response = client.post("/api/devices/dev/control/auto_lock_candidates", json=None)
    assert response.status_code == 200
    body = response.json()
    assert body["found"] is False
    assert body["candidate"] is None
    assert body["candidates"] == []
    assert body["reason"]
    assert body["frame"] is not None


def test_endpoint_never_calls_a_locking_or_settings_persisting_method(monkeypatch):
    """Read-only contract: the fake session exposes no lock/persist methods at
    all, so any attempt to call one would raise AttributeError -- proving the
    endpoint only calls the acquire/detect surface it's supposed to."""
    session = FakeCandidatesSession()
    _patch(monkeypatch, session)
    client = TestClient(main.app)

    response = client.post(
        "/api/devices/dev/control/auto_lock_candidates?acquire=true",
        json={"signal_type": "pdh"},
    )
    assert response.status_code == 200
    # Only the expected calls happened; no set_param beyond what _prepare_sweep
    # itself performs (none here, since sweep_speed is not part of this path).
    call_names = {call[0] for call in session.calls}
    assert call_names <= {
        "auto_lock_candidates_acquire_precheck",
        "start_sweep",
        "set_csr_direct",
        "wait_for_fresh_trace",
        "auto_lock_candidates_detect",
        "auto_lock_detect",
        # Advancing a staged run's latest-seen frame (see C2) is itself
        # read-only: it never locks or persists settings.
        "staged_autolock_observe_frame",
    }


def test_acquire_true_rejects_an_analysed_frame_older_than_the_acquired_one(monkeypatch):
    # Guards the contract that acquire=true never detects on a frame from before
    # the trigger: if the cached frame somehow predates the acquired frame, 409.
    session = FakeCandidatesSession()
    real_detect = session.auto_lock_detect

    def stale_detect(settings_payload):
        result = real_detect(settings_payload)
        result["frame"]["frame_id"] = 1  # older than the acquired frame (2)
        return result

    session.auto_lock_detect = stale_detect
    _patch(monkeypatch, session)
    client = TestClient(main.app)

    response = client.post(
        "/api/devices/dev/control/auto_lock_candidates?acquire=true", json=None
    )
    assert response.status_code == 409
    assert "predates" in response.json()["detail"]


def test_detector_defaults_to_strict_and_is_passed_through(monkeypatch):
    session = FakeCandidatesSession()
    _patch(monkeypatch, session)
    client = TestClient(main.app)

    assert client.post("/api/devices/dev/control/auto_lock_candidates").status_code == 200
    assert session.detector == "strict"
    response = client.post(
        "/api/devices/dev/control/auto_lock_candidates", params={"detector": "coarse"}
    )
    assert response.status_code == 200
    assert session.detector == "coarse"


def test_unknown_detector_is_rejected(monkeypatch):
    session = FakeCandidatesSession()
    _patch(monkeypatch, session)
    client = TestClient(main.app)
    response = client.post(
        "/api/devices/dev/control/auto_lock_candidates", params={"detector": "bogus"}
    )
    assert response.status_code == 422


def test_include_coarse_and_its_overrides_are_passed_through(monkeypatch):
    session = FakeCandidatesSession()
    _patch(monkeypatch, session)
    client = TestClient(main.app)
    client.post("/api/devices/dev/control/auto_lock_candidates")
    assert session.coarse_opts == {
        "include_coarse": False, "coarse_min_relative_score": None, "coarse_max_candidates": None,
    }
    response = client.post(
        "/api/devices/dev/control/auto_lock_candidates",
        params={"include_coarse": "true", "coarse_min_relative_score": 0.1,
                "coarse_max_candidates": 16},
    )
    assert response.status_code == 200
    assert session.coarse_opts == {
        "include_coarse": True, "coarse_min_relative_score": 0.1, "coarse_max_candidates": 16,
    }


def test_coarse_overrides_are_validated(monkeypatch):
    session = FakeCandidatesSession()
    _patch(monkeypatch, session)
    client = TestClient(main.app)
    for params in ({"coarse_min_relative_score": 0}, {"coarse_max_candidates": 0}):
        r = client.post("/api/devices/dev/control/auto_lock_candidates", params=params)
        assert r.status_code == 422
