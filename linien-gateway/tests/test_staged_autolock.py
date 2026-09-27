"""Tests for the staged (step-by-step) identity-aware auto-lock API (spec C2).

Exercises `DeviceSession.staged_autolock_begin/step/renew/lock/abort/state`
directly (not through HTTP) for tight control over the detection sequence,
following the same mocking style as
``tests/test_auto_lock_refinement_characterization.py``: the low-level
detection methods (`_capture_auto_lock_candidates_strict`/
`_coarse_auto_lock_candidates`) and `_set_sweep_geometry`/
`_restore_sweep_geometry`/`_move_and_lock` are mocked directly, so the real
planner (`plan_refinement_step`), `IdentityGuard`, and the staged run's own
bookkeeping are what's under test.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

import app.session as session_module
from app.auto_lock_scan import AutoLockScanResult, AutoLockScanSettings
from app.session import DeviceSession, StagedAutolockError


class _RecordingManager:
    def publish(self, device_key: str, message: dict[str, Any]) -> None:
        pass


class _FakeParam:
    def __init__(self, value: Any) -> None:
        self.value = value


class _FakeParameters:
    def __init__(self, **values: Any) -> None:
        for name, value in values.items():
            setattr(self, name, _FakeParam(value))


def _result(
    target_index: int,
    target_voltage: float,
    *,
    sideband_offset_v: float | None = 0.03,
    slope_rising: bool = True,
    score: float = 0.9,
) -> AutoLockScanResult:
    return AutoLockScanResult(
        target_index=target_index,
        target_voltage=float(target_voltage),
        target_slope_rising=slope_rising,
        score=score,
        left_excursion=0.15,
        right_excursion=0.16,
        pair_excursion=0.31,
        symmetry=0.94,
        monitor_level=None,
        hz_per_v=None,
        sideband_offset_v=sideband_offset_v,
    )


def _frame(frame_id: int, center_v: float, amplitude_v: float) -> dict[str, Any]:
    return {
        "frame_id": frame_id,
        "acquired_at": time.time(),
        "sweep_center_v": center_v,
        "sweep_amplitude_v": amplitude_v,
        "n_points": 2048,
        "modulation_frequency_hz": 10e6,
        "sideband_spacing_samples": 50.0,
        "noise_floor": 0.001,
    }


def _make_session() -> DeviceSession:
    device = SimpleNamespace(
        key="dev-staged", name="dev-staged", host="127.0.0.1", port=18864, parameters={}
    )
    session = DeviceSession(device, _RecordingManager())
    session.control = object()  # truthy; never touched (start_lock etc mocked)
    session.parameters = _FakeParameters(
        sweep_center=0.0,
        sweep_amplitude=1.0,
        target_slope_rising=True,
        modulation_frequency=0.0,
        lock=False,
    )
    return session


def _geometry_recorder(session: DeviceSession) -> list[tuple[float, float]]:
    writes: list[tuple[float, float]] = []

    def _fake(center_v, amplitude_v, *, settle_s=0.0):
        writes.append((float(center_v), float(amplitude_v)))
        session.parameters.sweep_center.value = float(center_v)
        session.parameters.sweep_amplitude.value = float(amplitude_v)
        return time.time()

    session._set_sweep_geometry = _fake  # type: ignore[method-assign]
    return writes


def _restore_recorder(session: DeviceSession) -> list[tuple[float, float]]:
    calls: list[tuple[float, float]] = []

    def _fake(center_v, amplitude_v):
        calls.append((float(center_v), float(amplitude_v)))
        return True

    session._restore_sweep_geometry = _fake  # type: ignore[method-assign]
    return calls


def _lock_recorder(session: DeviceSession) -> list[Any]:
    calls: list[Any] = []

    def _fake(result, sweep_center):
        calls.append(result)

    session._move_and_lock = _fake  # type: ignore[method-assign]
    return calls


# --------------------------------------------------------------------------
# Happy path + the hysteresis requirement: after a staged step changes
# geometry, the desired feature appears at an unpredictable position while a
# stronger identical-morphology decoy exists; the caller selects the desired
# one by target_index, and staged step/lock must follow that selection.
# --------------------------------------------------------------------------


def test_staged_step_follows_the_callers_selection_not_the_higher_score(monkeypatch):
    session = _make_session()
    writes = _geometry_recorder(session)
    _restore_recorder(session)
    locked = _lock_recorder(session)

    begin_candidates = [
        _result(100, 0.180, score=0.5),  # desired (weaker)
        _result(200, -0.410, score=0.95),  # decoy (stronger, same morphology)
    ]

    def _begin_detect(settings, after=None):
        return begin_candidates, 0.0, 1.0, 2.0, _frame(1, 0.0, 1.0)

    monkeypatch.setattr(session, "_capture_auto_lock_candidates_strict", _begin_detect)

    result = session.staged_autolock_begin(None, 60.0)
    assert result["candidates"][0]["target_index"] == 100
    assert result["candidates"][1]["target_index"] == 200
    token = result["token"]

    # After narrowing, a fresh frame shows the desired feature at a NEW
    # (unpredictable) position, distinguished only by target_index -- the
    # decoy remains higher-scoring.
    post_move_candidates = [
        _result(50, 0.191, score=0.4),  # desired, moved, weaker score
        _result(75, -0.402, score=0.97),  # decoy, still highest score
    ]

    def _narrow_detect(settings, after=None):
        return post_move_candidates, 0.05, 0.3, 9.0, _frame(2, 0.05, 0.3)

    monkeypatch.setattr(session, "_capture_auto_lock_candidates_strict", _narrow_detect)

    step_result = session.staged_autolock_step(token, 1, 100)
    assert step_result["stage_index"] == 1
    # Both candidates on the new frame are reported -- never only the winner.
    returned_indices = {c["target_index"] for c in step_result["candidates"]}
    assert returned_indices == {50, 75}
    assert step_result["frame"]["frame_id"] == 2

    # The caller selects the DESIRED (lower-score) candidate by target_index.
    def _final_detect(settings, after=None):
        return (
            [_result(50, 0.191, score=0.4)],
            0.05, 0.3, 9.0, _frame(3, 0.05, 0.3),
        )

    monkeypatch.setattr(session, "_capture_auto_lock_candidates_strict", _final_detect)

    lock_result = session.staged_autolock_lock(token, 2, 50)
    assert lock_result["target_voltage"] == pytest.approx(0.191)
    assert len(locked) == 1
    assert locked[0].target_voltage == pytest.approx(0.191)
    # The run has ended.
    assert session.staged_autolock_state() == {"active": False}
    # Never wrote geometry anywhere near the decoy's voltage.
    assert all(abs(c - (-0.4)) > 0.1 for c, _a in writes)


# --------------------------------------------------------------------------
# Conflict / validation paths
# --------------------------------------------------------------------------


def test_a_second_begin_is_refused_while_a_run_is_active():
    session = _make_session()
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: ([], 0.0, 1.0, 1.0, _frame(1, 0.0, 1.0))
    )
    session.staged_autolock_begin(None, 60.0)

    with pytest.raises(RuntimeError, match="staged auto-lock run is active"):
        session.staged_autolock_begin(None, 60.0)


def test_one_shot_and_start_lock_are_blocked_during_a_staged_run():
    session = _make_session()
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: ([], 0.0, 1.0, 1.0, _frame(1, 0.0, 1.0))
    )
    session.staged_autolock_begin(None, 60.0)

    with pytest.raises(RuntimeError, match="staged auto-lock run is active"):
        session.auto_lock_from_scan(None)
    with pytest.raises(RuntimeError, match="staged auto-lock run is active"):
        session.start_lock()
    # start_autolock's own AUTOMATION_TEMP_DISABLED guard runs first (a
    # pre-existing, unrelated feature flag) and reports that instead when
    # both apply; the staged-run guard is still reached and enforced whenever
    # automation is enabled (see the guard order in start_autolock).


def test_step_with_a_stale_frame_id_is_refused(monkeypatch):
    session = _make_session()
    _geometry_recorder(session)
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            [_result(1, 0.1)], 0.0, 1.0, 2.0, _frame(1, 0.0, 1.0)
        )
    )
    result = session.staged_autolock_begin(None, 60.0)
    token = result["token"]

    with pytest.raises(StagedAutolockError) as excinfo:
        session.staged_autolock_step(token, 999, 1)
    assert excinfo.value.status_code == 409


def test_step_with_a_missing_target_index_is_refused_without_locking(monkeypatch):
    session = _make_session()
    _geometry_recorder(session)
    locked = _lock_recorder(session)
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            [_result(1, 0.1)], 0.0, 1.0, 2.0, _frame(1, 0.0, 1.0)
        )
    )
    result = session.staged_autolock_begin(None, 60.0)
    token = result["token"]

    with pytest.raises(StagedAutolockError) as excinfo:
        session.staged_autolock_step(token, 1, 42)
    assert excinfo.value.status_code == 422
    assert locked == []
    # The run stays active.
    assert session.staged_autolock_state()["active"] is True


def test_step_fails_identity_guard_when_the_slope_changes(monkeypatch):
    session = _make_session()
    _geometry_recorder(session)
    _restore_recorder(session)
    locked = _lock_recorder(session)
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            [_result(1, 0.1, slope_rising=True)], 0.0, 1.0, 2.0, _frame(1, 0.0, 1.0)
        )
    )
    result = session.staged_autolock_begin(None, 60.0)
    token = result["token"]
    # First selection establishes identity with rising slope. The stage's own
    # detection lands a (falling-slope) candidate on the new frame -- not yet
    # checked against identity, since nothing has selected it.
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            [_result(2, 0.15, slope_rising=False)], 0.1, 0.5, 5.0, _frame(2, 0.1, 0.5)
        )
    )
    session.staged_autolock_step(token, 1, 1)

    # Second call selects a candidate with the OPPOSITE slope on the same
    # (now latest) frame -- IdentityGuard must refuse, not silently pick it.
    with pytest.raises(StagedAutolockError) as excinfo:
        session.staged_autolock_step(token, 2, 2)
    assert excinfo.value.status_code == 422
    assert locked == []


def test_lock_verification_ambiguous_two_candidates_refuses_to_lock(monkeypatch):
    session = _make_session()
    _geometry_recorder(session)
    locked = _lock_recorder(session)
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            [_result(1, 0.1)], 0.0, 1.0, 2.0, _frame(1, 0.0, 1.0)
        )
    )
    result = session.staged_autolock_begin(None, 60.0)
    token = result["token"]

    # Two fresh candidates within tolerance of the selected voltage -> ambiguous.
    def _ambiguous(settings, after=None):
        return (
            [
                _result(1, 0.1000001),
                _result(2, 0.1000002),
            ],
            0.0, 1.0, 2.0, _frame(2, 0.0, 1.0),
        )

    monkeypatch.setattr(session, "_capture_auto_lock_candidates_strict", _ambiguous)

    with pytest.raises(StagedAutolockError) as excinfo:
        session.staged_autolock_lock(token, 1, 1)
    assert excinfo.value.status_code == 422
    assert "ambiguous" in str(excinfo.value)
    assert locked == []
    assert session.staged_autolock_state()["active"] is True


def test_abort_restores_geometry_and_ends_the_run():
    session = _make_session()
    _geometry_recorder(session)
    restores = _restore_recorder(session)
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            [_result(1, 0.1)], 0.0, 1.0, 2.0, _frame(1, 0.0, 1.0)
        )
    )
    result = session.staged_autolock_begin(None, 60.0)
    token = result["token"]

    response = session.staged_autolock_abort(token)
    assert response == {"restored": True}
    assert restores == [(0.0, 1.0)]
    assert session.staged_autolock_state() == {"active": False}


def test_ttl_expiry_restores_geometry_with_no_further_calls(monkeypatch):
    session = _make_session()
    _geometry_recorder(session)
    restores = _restore_recorder(session)
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            [_result(1, 0.1)], 0.0, 1.0, 2.0, _frame(1, 0.0, 1.0)
        )
    )
    result = session.staged_autolock_begin(None, 0.05)
    token = result["token"]

    time.sleep(0.3)

    assert session.staged_autolock_state() == {"active": False}
    assert restores == [(0.0, 1.0)]

    with pytest.raises(StagedAutolockError) as excinfo:
        session.staged_autolock_step(token, 1, 1)
    assert excinfo.value.status_code == 404


def test_renew_extends_the_ttl_and_prevents_expiry(monkeypatch):
    session = _make_session()
    _geometry_recorder(session)
    restores = _restore_recorder(session)
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            [_result(1, 0.1)], 0.0, 1.0, 2.0, _frame(1, 0.0, 1.0)
        )
    )
    result = session.staged_autolock_begin(None, 0.15)
    token = result["token"]

    time.sleep(0.08)
    renewed = session.staged_autolock_renew(token, 5.0)
    assert renewed["expires_at"] > time.time()

    time.sleep(0.2)  # past the ORIGINAL ttl, well within the renewed one
    assert session.staged_autolock_state()["active"] is True
    assert restores == []
