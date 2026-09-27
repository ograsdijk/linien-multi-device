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

import threading
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


# --------------------------------------------------------------------------
# Fix #1: a `busy` guard around the in-flight `step`/`lock` I/O, so the TTL
# timer cannot restore geometry underneath it and a concurrent call cannot
# race it.
# --------------------------------------------------------------------------


def _blocking_geometry_recorder(
    session: DeviceSession,
) -> tuple[list[tuple[float, float]], threading.Event, threading.Event]:
    """Like `_geometry_recorder`, but the write blocks until released.

    Lets a test pause a `step`/`lock` call mid-I/O (after it has set `busy`
    and released `_state_lock`) so it can probe what a concurrent
    call/timer-fire sees.
    """
    writes: list[tuple[float, float]] = []
    entered = threading.Event()
    release = threading.Event()

    def _fake(center_v, amplitude_v, *, settle_s=0.0):
        entered.set()
        release.wait(timeout=5.0)
        writes.append((float(center_v), float(amplitude_v)))
        session.parameters.sweep_center.value = float(center_v)
        session.parameters.sweep_amplitude.value = float(amplitude_v)
        return time.time()

    session._set_sweep_geometry = _fake  # type: ignore[method-assign]
    return writes, entered, release


def test_ttl_expiry_during_a_slow_step_does_not_restore_mid_step_then_expires_after():
    session = _make_session()
    writes, entered, release = _blocking_geometry_recorder(session)
    restores = _restore_recorder(session)
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            [_result(1, 0.1, sideband_offset_v=0.03)], 0.0, 1.0, 2.0, _frame(1, 0.0, 1.0)
        )
    )
    # A short TTL that will elapse WHILE the step below is blocked inside its
    # geometry write.
    result = session.staged_autolock_begin(None, 0.05)
    token = result["token"]

    # The post-move detection the step's planner will use once it un-blocks.
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            [_result(1, 0.15, sideband_offset_v=0.03)], 0.05, 0.5, 5.0, _frame(2, 0.05, 0.5)
        )
    )

    errors: list[BaseException] = []
    results: list[dict[str, Any]] = []

    def _run_step():
        try:
            results.append(session.staged_autolock_step(token, 1, 1))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=_run_step)
    thread.start()
    assert entered.wait(timeout=5.0), "step never reached the geometry write"

    # Let the TTL elapse while the step is still blocked inside the write.
    time.sleep(0.15)
    # The timer must NOT have restored geometry mid-step: the run is still
    # active, at its ORIGINAL (pre-step) geometry, and busy.
    state = session.staged_autolock_state()
    assert state["active"] is True
    assert state["busy"] is True
    assert state["geometry"] == {"center_v": 0.0, "amplitude_v": 1.0}
    assert restores == []

    # Let the step finish.
    release.set()
    thread.join(timeout=5.0)
    assert not errors, errors
    assert len(results) == 1

    # The deadline had already passed while busy, so the step's own `finally`
    # expires the run right away and restores the ORIGINAL geometry (not the
    # narrowed one it was mid-write to).
    assert session.staged_autolock_state() == {"active": False}
    assert restores == [(0.0, 1.0)]


def test_a_concurrent_step_on_a_busy_run_is_refused_with_409():
    session = _make_session()
    _writes, entered, release = _blocking_geometry_recorder(session)
    _restore_recorder(session)
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            [_result(1, 0.1, sideband_offset_v=0.03)], 0.0, 1.0, 2.0, _frame(1, 0.0, 1.0)
        )
    )
    result = session.staged_autolock_begin(None, 60.0)
    token = result["token"]
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            [_result(1, 0.15, sideband_offset_v=0.03)], 0.05, 0.5, 5.0, _frame(2, 0.05, 0.5)
        )
    )

    thread = threading.Thread(target=lambda: session.staged_autolock_step(token, 1, 1))
    thread.start()
    try:
        assert entered.wait(timeout=5.0), "step never reached the geometry write"

        with pytest.raises(StagedAutolockError) as excinfo:
            session.staged_autolock_step(token, 1, 1)
        assert excinfo.value.status_code == 409

        with pytest.raises(StagedAutolockError) as lock_excinfo:
            session.staged_autolock_lock(token, 1, 1)
        assert lock_excinfo.value.status_code == 409
    finally:
        release.set()
        thread.join(timeout=5.0)


def test_abort_on_a_busy_run_is_refused_with_409():
    session = _make_session()
    _writes, entered, release = _blocking_geometry_recorder(session)
    _restore_recorder(session)
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            [_result(1, 0.1, sideband_offset_v=0.03)], 0.0, 1.0, 2.0, _frame(1, 0.0, 1.0)
        )
    )
    result = session.staged_autolock_begin(None, 60.0)
    token = result["token"]
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            [_result(1, 0.15, sideband_offset_v=0.03)], 0.05, 0.5, 5.0, _frame(2, 0.05, 0.5)
        )
    )

    thread = threading.Thread(target=lambda: session.staged_autolock_step(token, 1, 1))
    thread.start()
    try:
        assert entered.wait(timeout=5.0), "step never reached the geometry write"

        with pytest.raises(StagedAutolockError) as excinfo:
            session.staged_autolock_abort(token)
        assert excinfo.value.status_code == 409
    finally:
        release.set()
        thread.join(timeout=5.0)


def test_lock_cannot_proceed_after_the_run_has_expired():
    session = _make_session()
    _geometry_recorder(session)
    restores = _restore_recorder(session)
    locked = _lock_recorder(session)
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
        session.staged_autolock_lock(token, 1, 1)
    assert excinfo.value.status_code == 404
    assert locked == []


# --------------------------------------------------------------------------
# Fix #3: per-candidate `lockable_here`, and the width-shift measurement
# deferred to the CALLER's selected candidate on the new frame (never the
# best-score one).
# --------------------------------------------------------------------------


def test_lockable_here_is_reported_per_candidate_not_just_for_the_selection():
    session = _make_session()
    _geometry_recorder(session)
    # Two candidates at amplitude 1.0: A's sideband is too narrow for this
    # scan width (not lockable_here), B's is wide enough (lockable_here).
    # Selecting B makes the planner declare "done" immediately -- no further
    # detection call needed -- so both candidates from this one frame are
    # returned annotated together.
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            [
                _result(1, -0.2, sideband_offset_v=0.02, score=0.9),
                _result(2, 0.2, sideband_offset_v=0.5, score=0.5),
            ],
            0.0, 1.0, 2.0, _frame(1, 0.0, 1.0),
        )
    )
    result = session.staged_autolock_begin(None, 60.0)
    token = result["token"]

    step_result = session.staged_autolock_step(token, 1, 2)
    assert step_result["planner"]["action"] == "done"
    by_index = {c["target_index"]: c for c in step_result["candidates"]}
    assert by_index[1]["lockable_here"] is False
    assert by_index[2]["lockable_here"] is True
    # At least one candidate on the frame IS lockable -> no more refinement.
    assert step_result["needs_more_refinement"] is False


def test_needs_more_refinement_is_true_when_no_candidate_is_lockable_here():
    session = _make_session()
    _geometry_recorder(session)
    _restore_recorder(session)
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            [_result(1, 0.1, sideband_offset_v=0.03)], 0.0, 1.0, 2.0, _frame(1, 0.0, 1.0)
        )
    )
    result = session.staged_autolock_begin(None, 60.0)
    token = result["token"]

    # The single candidate's sideband stays too narrow for the NEW (narrower)
    # amplitude too, so the frame this step lands on has no lockable
    # candidate at all.
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            [_result(1, 0.12, sideband_offset_v=0.02)], 0.05, 0.5, 5.0, _frame(2, 0.05, 0.5)
        )
    )
    step_result = session.staged_autolock_step(token, 1, 1)
    assert step_result["candidates"][0]["lockable_here"] is False
    assert step_result["needs_more_refinement"] is True


def test_width_shift_measurement_uses_the_callers_selection_not_the_best_score():
    """Regression for fix #3: the shift-per-fraction bookkeeping must be
    measured against the candidate the CALLER selects on the new frame, not
    `outcome.target` (the best-score one) -- otherwise a decoy at a wildly
    different voltage poisons `shift_per_fraction` for every later stage.
    """
    session = _make_session()
    _geometry_recorder(session)
    _restore_recorder(session)

    begin_candidates = [
        _result(100, 0.180, score=0.5, sideband_offset_v=0.03),  # desired (weaker)
        _result(200, -0.410, score=0.95, sideband_offset_v=0.03),  # decoy (stronger)
    ]
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (begin_candidates, 0.0, 1.0, 2.0, _frame(1, 0.0, 1.0))
    )
    result = session.staged_autolock_begin(None, 60.0)
    token = result["token"]

    # After narrowing (amplitude 1.0 -> 0.3), the desired feature moved only
    # slightly (0.180 -> 0.191); the decoy is far away (-0.402). Both
    # sidebands are now wide enough that selecting either would make the
    # NEXT step's planner declare "done" with no further detection needed.
    post_move_candidates = [
        _result(50, 0.191, score=0.4, sideband_offset_v=0.2),  # desired, moved
        _result(75, -0.402, score=0.97, sideband_offset_v=0.2),  # decoy, still highest score
    ]
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (post_move_candidates, 0.05, 0.3, 9.0, _frame(2, 0.05, 0.3))
    )
    session.staged_autolock_step(token, 1, 100)  # select the DESIRED candidate

    # The width-shift measurement from the step above is still PENDING: it
    # has not been consumed yet, because no selection has been made on frame
    # 2 -- see StagedAutolockRun.pending_shift.
    assert session._staged_autolock.pending_shift is not None
    assert session._staged_autolock.shift_per_fraction is None

    # Selecting the DESIRED candidate on frame 2 consumes it.
    session.staged_autolock_step(token, 2, 50)

    run = session._staged_autolock
    assert run is not None
    assert run.pending_shift is None
    assert run.shift_per_fraction is not None
    # Expected: |0.191 - 0.180| / (1 - 0.3/1.0) ~= 0.0157 -- small, because
    # the desired feature barely moved.
    assert run.shift_per_fraction == pytest.approx(0.011 / 0.7, rel=1e-6)
    # NOT the value the pre-fix code would have measured against the decoy:
    # |-0.402 - 0.180| / 0.7 ~= 0.831 -- would poison every later stage.
    assert run.shift_per_fraction != pytest.approx(0.582 / 0.7, rel=0.01)
