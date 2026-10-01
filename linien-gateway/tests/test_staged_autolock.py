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
from app.lock_refinement import predicted_shift_v
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


# Every session `_make_session()` hands out, so an autouse fixture can cancel
# any TTL timer thread a test leaves running (a test that leaves a run active
# with a long TTL rather than aborting/locking it would otherwise leak a real
# `threading.Timer` thread for the rest of the test session -- enough of
# those alive at once made the precise-timing TTL-expiry tests intermittently
# flaky under load).
_sessions_for_cleanup: list[DeviceSession] = []


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
    _sessions_for_cleanup.append(session)
    return session


@pytest.fixture(autouse=True)
def _cancel_leaked_run_timers():
    yield
    while _sessions_for_cleanup:
        session = _sessions_for_cleanup.pop()
        run = session._staged_autolock
        if run is not None and run.timer is not None:
            run.timer.cancel()


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

    # The desired candidate's sideband (0.1) is wide enough that, after
    # narrowing to amplitude 0.3, it clears the fix #8 lockability gate
    # (scan_too_wide_to_lock) -- unrelated to what this test is about
    # (selection identity/hysteresis), but needed for the final `lock()`
    # call below to be reachable at all now that the gate exists. The decoy
    # keeps the default (narrower) sideband; it is never selected.
    begin_candidates = [
        _result(100, 0.180, score=0.5, sideband_offset_v=0.1),  # desired (weaker)
        _result(200, -0.410, score=0.95),  # decoy (stronger, same morphology)
    ]

    def _begin_detect(settings, after=None):
        return begin_candidates, 0.0, 1.0, 2.0, _frame(1, 0.0, 1.0)

    monkeypatch.setattr(session, "_capture_auto_lock_candidates_strict", _begin_detect)

    result = session.staged_autolock_begin(None, 60.0)
    assert result["candidates"][0]["target_index"] == 100
    assert result["candidates"][1]["target_index"] == 200
    token = result["token"]

    # After narrowing, a fresh frame shows the desired feature where the
    # hysteresis model puts it -- (0, 1.0) -> (0.05, 0.3) raises the lower
    # endpoint by 0.75 V, so -0.085 * 0.75 = -63.75 mV: 0.180 -> 0.1163 --
    # distinguished from the decoy only by target_index; the decoy remains
    # higher-scoring.
    post_move_candidates = [
        _result(50, 0.1163, score=0.4, sideband_offset_v=0.1),  # desired, moved, weaker score
        _result(75, -0.4737, score=0.97),  # decoy, still highest score
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
            [_result(50, 0.1163, score=0.4)],
            0.05, 0.3, 9.0, _frame(3, 0.05, 0.3),
        )

    monkeypatch.setattr(session, "_capture_auto_lock_candidates_strict", _final_detect)

    lock_result = session.staged_autolock_lock(token, 2, 50)
    assert lock_result["target_voltage"] == pytest.approx(0.1163)
    assert len(locked) == 1
    assert locked[0].target_voltage == pytest.approx(0.1163)
    # The run has ended.
    assert session.staged_autolock_state() == {"active": False}
    # Never wrote geometry anywhere near the decoy's voltage.
    assert all(abs(c - (-0.4)) > 0.1 for c, _a in writes)


@pytest.mark.parametrize("target_v", [-0.6, 0.6])
def test_staged_step_applies_edge_guard_symmetrically(monkeypatch, target_v):
    session = _make_session()
    session.parameters.sweep_amplitude.value = 0.8
    settings = {
        "half_range_sweep_v": 0.02,
        "hysteresis_per_volt_lower": 0.0,
        "hysteresis_tolerance_per_volt": 0.0,
        "hysteresis_floor_v": 0.0,
    }
    selected = _result(10, target_v, sideband_offset_v=0.04)
    frame = _frame(1, 0.0, 0.8)
    monkeypatch.setattr(
        session, "_capture_auto_lock_candidates_strict",
        lambda _settings, after=None: (_ for _ in ()).throw(ValueError("coarse seed")),
    )
    monkeypatch.setattr(
        session, "_coarse_auto_lock_candidates",
        lambda _settings, after=None: ([selected], 0.0, 0.8, 1.0, {}, frame),
    )
    result = session.staged_autolock_begin(settings, 60.0)
    writes = _geometry_recorder(session)

    def _strict(_settings, after=None):
        center = float(session.parameters.sweep_center.value)
        amplitude = float(session.parameters.sweep_amplitude.value)
        return [selected], center, amplitude, 20.0, _frame(2, center, amplitude)

    monkeypatch.setattr(session, "_capture_auto_lock_candidates_strict", _strict)
    step = session.staged_autolock_step(result["token"], 1, 10)
    assert step["planner"]["bounds"]["edge_guard_active"] == 1.0
    assert step["planner"]["bounds"]["scheduled_v"] == pytest.approx(0.68)
    assert len(writes) == 1
    assert target_v * writes[0][0] > 0.0
    assert writes[0][1] == pytest.approx(0.68)


@pytest.mark.parametrize("direction", [-1.0, 1.0])
def test_staged_edge_degraded_run_converges_and_locks_without_cropping(
    monkeypatch, direction
):
    session = _make_session()
    session.parameters.sweep_amplitude.value = 0.8
    selected = _result(10, direction * 0.65, sideband_offset_v=0.04)
    first_frame = _frame(1, 0.0, 0.8)
    previous = {"center": 0.0, "amplitude": 0.8, "target": direction * 0.65}
    frame_id = {"value": 1}
    observed: list[tuple[float, float, float]] = []
    locked = _lock_recorder(session)

    monkeypatch.setattr(
        session,
        "_capture_auto_lock_candidates_strict",
        lambda _settings, after=None: (_ for _ in ()).throw(ValueError("coarse seed")),
    )
    monkeypatch.setattr(
        session,
        "_coarse_auto_lock_candidates",
        lambda _settings, after=None: ([selected], 0.0, 0.8, 2.0, {}, first_frame),
    )
    run = session.staged_autolock_begin(
        {"half_range_sweep_v": 0.02}, 60.0
    )
    writes = _geometry_recorder(session)
    settings = session._staged_autolock.settings

    def _detect():
        center = float(session.parameters.sweep_center.value)
        amplitude = float(session.parameters.sweep_amplitude.value)
        predicted = predicted_shift_v(
            previous["center"], previous["amplitude"], center, amplitude,
            settings.hysteresis_per_volt_lower,
        )
        edge_gain = (
            1.125
            if abs(previous["target"] - previous["center"]) / previous["amplitude"] > 0.6
            else 1.0
        )
        target_v = previous["target"] + edge_gain * predicted
        previous.update(center=center, amplitude=amplitude, target=target_v)
        observed.append((target_v, center, amplitude))
        frame_id["value"] += 1
        result = _result(10, target_v, sideband_offset_v=0.04)
        return result, center, amplitude, 12.0, _frame(frame_id["value"], center, amplitude)

    def _strict(_settings, after=None):
        candidate, center, amplitude, resolution, frame = _detect()
        return [candidate], center, amplitude, resolution, frame

    def _coarse(_settings, after=None):
        candidate, center, amplitude, resolution, frame = _detect()
        return [candidate], center, amplitude, resolution, {}, frame

    monkeypatch.setattr(session, "_capture_auto_lock_candidates_strict", _strict)
    monkeypatch.setattr(session, "_coarse_auto_lock_candidates", _coarse)
    selected_frame_id = 1
    for _ in range(16):
        step = session.staged_autolock_step(run["token"], selected_frame_id, 10)
        assert step["frame"]["frame_id"] > selected_frame_id
        selected_frame_id = step["frame"]["frame_id"]
        if not step["needs_more_refinement"]:
            break
    else:
        pytest.fail("edge-degraded staged planner did not converge within its stage budget")

    lock_result = session.staged_autolock_lock(run["token"], selected_frame_id, 10)
    assert lock_result["target_voltage"] == pytest.approx(previous["target"])
    assert len(locked) == 1
    assert len(writes) <= 9
    assert all(abs(target - center) <= 0.9 * amplitude for target, center, amplitude in observed)


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
    # Ambiguity, not scan geometry, is under test here -- disable the fix #8
    # lockability gate's width check.
    session.auto_lock_scan_settings["min_signal_scan_fraction"] = 0.0
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

    # Generous margin over the 0.05 s TTL: under a loaded full-suite run
    # (many other tests' own real threads/timers contending for the GIL), a
    # tight margin here made this test intermittently flaky even though the
    # timer itself always fires no earlier than requested.
    time.sleep(1.0)

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

    # See the sleep-margin note in test_ttl_expiry_restores_geometry_with_no_further_calls.
    time.sleep(1.0)
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


def test_hysteresis_diagnostic_uses_the_callers_selection_not_the_best_score():
    """Regression for fix #3, for the hysteresis diagnostic: measured-vs-
    predicted must be taken against the candidate the CALLER selects on the new
    frame, not `outcome.target` (the best-score one) -- a decoy at a wildly
    different voltage must not be charged as the tracked feature's shift.
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

    # After narrowing (0, 1.0) -> (0.05, 0.3) the model predicts -63.75 mV:
    # the desired feature lands 3 mV off that (0.180 -> 0.119); the decoy is
    # far away (-0.402). Both sidebands are now wide enough that selecting
    # either would make the NEXT step's planner declare "done" with no
    # further detection needed.
    post_move_candidates = [
        _result(50, 0.119, score=0.4, sideband_offset_v=0.2),  # desired, moved
        _result(75, -0.402, score=0.97, sideband_offset_v=0.2),  # decoy, still highest score
    ]
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (post_move_candidates, 0.05, 0.3, 9.0, _frame(2, 0.05, 0.3))
    )
    step = session.staged_autolock_step(token, 1, 100)  # select the DESIRED candidate
    # The decoy is annotated as outside the window before anyone picks it.
    by_index = {c["target_index"]: c for c in step["candidates"]}
    assert by_index[50]["identity_ok"] is True
    assert by_index[75]["identity_ok"] is False
    assert "hysteresis window" in by_index[75]["identity_reason"]

    # The prediction for the step above is still PENDING: no selection has
    # been made on frame 2 -- see StagedAutolockRun.pending_shift.
    assert session._staged_autolock.pending_shift is not None
    assert session._staged_autolock.stages == []

    # Selecting the DESIRED candidate on frame 2 consumes it.
    session.staged_autolock_step(token, 2, 50)

    run = session._staged_autolock
    assert run is not None
    assert run.pending_shift is None
    (record,) = run.stages
    hysteresis = record["hysteresis"]
    assert hysteresis["measured_shift_v"] == pytest.approx(0.119 - 0.180)
    assert hysteresis["predicted_shift_v"] == pytest.approx(-0.085 * 0.75)
    assert hysteresis["residual_v"] == pytest.approx(-0.061 + 0.06375)
    # NOT what it would have been against the decoy: -0.402 - 0.180.
    assert abs(hysteresis["residual_v"]) < 0.01


# --------------------------------------------------------------------------
# Fix #2 / #6: `begin` falls back to the coarse detector when strict finds
# nothing (instead of storing `[]`/"strict"), and a staged run's candidates
# always come from the run's OWN settings/detector.
# --------------------------------------------------------------------------


def test_begin_falls_back_to_coarse_when_strict_finds_nothing(monkeypatch):
    session = _make_session()

    def _strict_rejects(settings, after=None):
        raise ValueError("scan too wide for the strict detector")

    coarse_candidates = [_result(1, 0.25, sideband_offset_v=0.3, score=0.6)]

    def _coarse_accepts(settings, after=None):
        return coarse_candidates, 0.0, 1.0, 3.0, {"best": True}, _frame(1, 0.0, 1.0)

    monkeypatch.setattr(session, "_capture_auto_lock_candidates_strict", _strict_rejects)
    monkeypatch.setattr(session, "_coarse_auto_lock_candidates", _coarse_accepts)

    result = session.staged_autolock_begin(None, 60.0)

    # Pre-fix: strict's ValueError made `begin` store `[]`/"strict" outright,
    # never trying the coarse detector at all -- exactly the wide-scan case
    # trajectory refinement exists for, left with nothing to step from.
    assert result["candidates"] != []
    assert result["candidates"][0]["target_index"] == 1
    assert result["detector"] == "coarse"
    assert result["detail"] is None
    run = session._staged_autolock
    assert run is not None
    assert run.latest_detector == "coarse"
    assert len(run.latest_candidates) == 1


def test_begin_reports_when_both_detectors_find_nothing(monkeypatch):
    import numpy as np

    session = _make_session()

    def _strict_rejects(settings, after=None):
        raise ValueError("no strict crossing")

    def _coarse_rejects(settings, after=None):
        raise ValueError("no coarse crossing either")

    monkeypatch.setattr(session, "_capture_auto_lock_candidates_strict", _strict_rejects)
    monkeypatch.setattr(session, "_coarse_auto_lock_candidates", _coarse_rejects)
    monkeypatch.setattr(
        session,
        "_snapshot_auto_lock_traces_with_frame",
        lambda: (np.zeros(2048), None, 1, time.time()),
    )

    session._unlocked_geometry_by_frame[1] = (0.0, 1.0)
    result = session.staged_autolock_begin(None, 60.0)
    assert result["candidates"] == []
    assert result["detail"] is not None
    assert "no strict crossing" in result["detail"]
    assert "no coarse crossing either" in result["detail"]


def test_run_aware_detect_uses_run_settings_and_detector_not_endpoint_settings(
    monkeypatch,
):
    """Regression for fix #2b: while a staged run is active and the current
    geometry matches it, `auto_lock_candidates_detect` must detect with
    `run.settings`/the run's own detector mode (never the endpoint's own
    settings), and fold that SAME result into `run.latest_*` -- not the
    strict-only, endpoint-settings result `auto_lock_detect` would have
    produced, which (pre-fix) silently replaced a coarse-stage run's
    candidates and left `latest_detector` pointing at the wrong baseline.
    """
    session = _make_session()

    # begin() lands the run on a coarse-only frame (the strict detector
    # rejects this wide scan).
    def _strict_rejects(settings, after=None):
        raise ValueError("scan too wide for the strict detector")

    coarse_candidates = [_result(1, 0.25, sideband_offset_v=0.3, score=0.6)]

    def _coarse_accepts(settings, after=None):
        return coarse_candidates, 0.0, 1.0, 3.0, {"best": True}, _frame(1, 0.0, 1.0)

    monkeypatch.setattr(session, "_capture_auto_lock_candidates_strict", _strict_rejects)
    monkeypatch.setattr(session, "_coarse_auto_lock_candidates", _coarse_accepts)
    session.staged_autolock_begin(None, 60.0)
    run = session._staged_autolock
    assert run is not None
    assert run.latest_detector == "coarse"

    # An `auto_lock_candidates` call arrives at the SAME geometry the run is
    # sitting at (e.g. acquire=false, or acquire=true landing on the same
    # frame), passing its OWN (irrelevant) settings payload. The strict
    # detector still rejects; only the coarse path should ever be tried, and
    # it must use `run.settings`, never `settings_payload`.
    strict_calls: list[Any] = []

    def _strict_rejects_again(settings, after=None):
        strict_calls.append(settings)
        raise ValueError("still too wide")

    fresh_coarse_candidates = [_result(2, 0.26, sideband_offset_v=0.31, score=0.7)]

    def _coarse_accepts_again(settings, after=None):
        assert settings is run.settings, "must detect with the RUN's settings"
        return (
            fresh_coarse_candidates, 0.0, 1.0, 3.0, {"best": True},
            _frame(2, 0.0, 1.0),
        )

    monkeypatch.setattr(session, "_capture_auto_lock_candidates_strict", _strict_rejects_again)
    monkeypatch.setattr(session, "_coarse_auto_lock_candidates", _coarse_accepts_again)

    endpoint_settings_payload = {"signal_type": "pdh", "half_range_sweep_v": 0.5}
    result = session.auto_lock_candidates_detect(endpoint_settings_payload)

    assert result["detector"] == "coarse"
    assert result["candidates"][0]["target_index"] == 2
    # Folded directly into the run -- a later `step`/`lock` on target_index 2
    # from THIS response must find it in run.latest_candidates.
    assert run.latest_detector == "coarse"
    assert [c.target_index for c in run.latest_candidates] == [2]
    assert run.latest_frame["frame_id"] == 2


# --------------------------------------------------------------------------
# Fix #1 / #3: refusing before any device I/O, and serialising the
# geometry-writing parts of `step`/`lock` against a one-shot walk.
# --------------------------------------------------------------------------


def test_begin_precheck_refuses_while_locked_without_any_side_effect():
    session = _make_session()
    session.parameters.lock.value = True

    with pytest.raises(RuntimeError, match="locked"):
        session.staged_autolock_begin_precheck()
    # No run was created and the device was never told to start sweeping.
    assert session._staged_autolock is None


def test_begin_precheck_refuses_while_the_center_move_lock_is_held():
    session = _make_session()
    assert session._center_move_lock.acquire(blocking=False)
    try:
        with pytest.raises(RuntimeError, match="sweep-center move"):
            session.staged_autolock_begin_precheck()
    finally:
        session._center_move_lock.release()


def test_step_refuses_with_409_while_a_one_shot_walk_holds_the_center_lock():
    session = _make_session()
    _geometry_recorder(session)
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

    assert session._center_move_lock.acquire(blocking=False)
    try:
        with pytest.raises(StagedAutolockError) as excinfo:
            session.staged_autolock_step(token, 1, 1)
        assert excinfo.value.status_code == 409
        # Refused before any geometry write: the run is untouched.
        assert session.staged_autolock_state()["stage_index"] == 0
        assert session.staged_autolock_state()["geometry"] == {
            "center_v": 0.0, "amplitude_v": 1.0,
        }
    finally:
        session._center_move_lock.release()


# --------------------------------------------------------------------------
# Fix #4: a stage whose geometry write lands but whose detection then fails
# must recover the run to the device's ACTUAL geometry, stay active, and
# report 422 -- not leave the run pointing at stale geometry forever.
# --------------------------------------------------------------------------


def test_step_recovers_run_geometry_after_a_failed_stage_detection(monkeypatch):
    session = _make_session()
    _geometry_recorder(session)
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            [_result(1, 0.1, sideband_offset_v=0.03)], 0.0, 1.0, 2.0, _frame(1, 0.0, 1.0)
        )
    )
    result = session.staged_autolock_begin(None, 60.0)
    token = result["token"]

    # The stage's geometry write succeeds (the fake `_set_sweep_geometry`
    # from `_geometry_recorder` updates session.parameters), but BOTH
    # detectors then reject the new trace.
    def _strict_rejects(settings, after=None):
        raise ValueError("strict rejects the new trace")

    def _coarse_rejects(settings, after=None):
        raise ValueError("coarse rejects it too")

    monkeypatch.setattr(session, "_capture_auto_lock_candidates_strict", _strict_rejects)
    monkeypatch.setattr(session, "_coarse_auto_lock_candidates", _coarse_rejects)

    with pytest.raises(StagedAutolockError) as excinfo:
        session.staged_autolock_step(token, 1, 1)
    assert excinfo.value.status_code == 422
    assert "abort or retry" in str(excinfo.value)

    # The run stays active, but its bookkeeping now reflects the device's
    # ACTUAL (new, post-write) geometry -- not the OLD one from before this
    # stage. Pre-fix, `run.center_v`/`amplitude_v` stayed at (0.0, 1.0) while
    # the device had already moved, so `lock`'s geometry-unchanged check
    # would then fail forever.
    state = session.staged_autolock_state()
    assert state["active"] is True
    assert state["stage_index"] == 1
    assert state["geometry"] != {"center_v": 0.0, "amplitude_v": 1.0}
    assert state["geometry"] == {
        "center_v": session.parameters.sweep_center.value,
        "amplitude_v": session.parameters.sweep_amplitude.value,
    }
    run = session._staged_autolock
    assert run.latest_candidates == []
    assert run.pending_shift is None


# --------------------------------------------------------------------------
# Fix #5: the TTL-expiry timer must not abort a run a `renew` call already
# extended, even if the OLD timer's callback fires after the renew.
# --------------------------------------------------------------------------


def test_expire_racing_a_renew_does_not_abort_the_extended_run():
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

    # Simulate the race directly: `renew` extends `expires_at` and arms a new
    # timer, but the OLD timer's callback -- already past its busy check --
    # lands afterward anyway (Timer.cancel() cannot stop a thread already
    # running). Calling the STALE callback (bound to the old expiry) must be
    # a no-op now that `expires_at` has moved into the future.
    session.staged_autolock_renew(token, 5.0)
    session._staged_autolock_expire(token)

    assert session.staged_autolock_state()["active"] is True
    assert restores == []


# --------------------------------------------------------------------------
# Fix #7: the deferred width-shift measurement must only be consumed AFTER
# the selection passes the identity check, not before.
# --------------------------------------------------------------------------


def test_a_rejected_selection_does_not_consume_the_pending_hysteresis_prediction():
    session = _make_session()
    _geometry_recorder(session)
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            [_result(1, 0.1, sideband_offset_v=0.03, slope_rising=True)],
            0.0, 1.0, 2.0, _frame(1, 0.0, 1.0),
        )
    )
    result = session.staged_autolock_begin(None, 60.0)
    token = result["token"]
    session.staged_autolock_step(token, 1, 1)  # establishes identity (rising slope)

    # The stage that follows produces a frame with a pending shift
    # measurement (see StagedAutolockRun.pending_shift) and a candidate with
    # the OPPOSITE slope -- selecting it must fail identity.
    # (0, 1.0) -> (0.1, 0.5) raises the lower endpoint 0.6 V: the model puts the
    # 0.1 V feature at 0.049 V, so the opposite-slope candidate sits inside the
    # window and it is the slope, not the position, that must refuse it.
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            [_result(2, 0.049, sideband_offset_v=0.03, slope_rising=False)],
            0.1, 0.5, 5.0, _frame(2, 0.1, 0.5),
        )
    )
    session.staged_autolock_step(token, 1, 1)
    run = session._staged_autolock
    assert run.pending_shift is not None
    recorded = len(run.stages)  # the earlier, accepted stage's diagnostic

    with pytest.raises(StagedAutolockError):
        session.staged_autolock_step(token, 2, 2)

    # Pre-fix, the pending measurement was consumed BEFORE the identity check
    # and had already recorded a (wrong) value by the time the rejection was
    # raised. The prediction must stay pending, and nothing be recorded, so a
    # corrected selection on the same frame is still judged against it.
    assert run.pending_shift is not None
    assert len(run.stages) == recorded


# --------------------------------------------------------------------------
# Fix #8: `lock` must refuse from a wide/coarse frame -- the same
# `scan_too_wide_to_lock` rule the one-shot loop and `lockable_here` apply.
# --------------------------------------------------------------------------


def test_lock_refuses_from_a_frame_that_is_not_lockable_here():
    session = _make_session()
    _geometry_recorder(session)
    locked = _lock_recorder(session)
    # Default settings + a narrow sideband at amplitude 1.0 is "too wide to
    # lock" (min_signal_scan_fraction requires the sideband to span a
    # sixth of the half-range or more).
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            [_result(1, 0.1, sideband_offset_v=0.03)], 0.0, 1.0, 2.0, _frame(1, 0.0, 1.0)
        )
    )
    result = session.staged_autolock_begin(None, 60.0)
    token = result["token"]

    with pytest.raises(StagedAutolockError) as excinfo:
        session.staged_autolock_lock(token, 1, 1)
    assert excinfo.value.status_code == 422
    assert "lockable" in str(excinfo.value).lower()
    assert locked == []
    # The run stays active so the caller can `step` first, per the message.
    assert session.staged_autolock_state()["active"] is True


# --------------------------------------------------------------------------
# Fix #9: lock-time verification must align with the one-shot's final
# verification at unchanged geometry -- check_sideband=False, and require
# confirmation on TWO consecutive fresh frames before ever moving/locking.
# --------------------------------------------------------------------------


def test_lock_refuses_when_the_second_confirmation_frame_disagrees():
    session = _make_session()
    _geometry_recorder(session)
    locked = _lock_recorder(session)
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            [_result(1, 0.1, sideband_offset_v=0.2)], 0.0, 1.0, 2.0, _frame(1, 0.0, 1.0)
        )
    )
    result = session.staged_autolock_begin(None, 60.0)
    token = result["token"]

    calls = {"n": 0}

    def _flaky(settings, after=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                [_result(1, 0.1, sideband_offset_v=0.2)], 0.0, 1.0, 2.0, _frame(2, 0.0, 1.0),
            )
        # Second confirmation frame: the candidate has moved far away --
        # not a consistent re-detection of the same crossing. Pre-fix, only
        # ONE frame was ever taken, so this second (disagreeing) frame was
        # never even looked at and the lock would have already succeeded.
        return (
            [_result(1, 0.5, sideband_offset_v=0.2)], 0.0, 1.0, 2.0, _frame(3, 0.0, 1.0),
        )

    session._capture_auto_lock_candidates_strict = _flaky  # type: ignore[method-assign]

    with pytest.raises(StagedAutolockError) as excinfo:
        session.staged_autolock_lock(token, 1, 1)
    assert excinfo.value.status_code == 422
    assert calls["n"] == 2, "both confirmation frames must be taken"
    assert locked == []
    assert session.staged_autolock_state()["active"] is True


def test_lock_ignores_sideband_drift_during_verification_like_one_shot():
    """`check_sideband=False` at lock-verification time, mirroring the
    one-shot's final verification at unchanged geometry: the sideband
    estimate a freshly-narrowed run just measured is the thing under test,
    not a gate that can itself refuse the confirmation.
    """
    session = _make_session()
    _geometry_recorder(session)
    locked = _lock_recorder(session)
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            [_result(1, 0.1, sideband_offset_v=0.2)], 0.0, 1.0, 2.0, _frame(1, 0.0, 1.0)
        )
    )
    result = session.staged_autolock_begin(None, 60.0)
    token = result["token"]

    # Both confirmation frames report the SAME position but a wildly
    # different sideband spacing from the established identity baseline
    # (0.2) -- pre-fix (check_sideband defaulting True) this would be
    # refused as an identity change even though the position is exact.
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            [_result(1, 0.1, sideband_offset_v=0.03)], 0.0, 1.0, 2.0, _frame(2, 0.0, 1.0),
        )
    )

    lock_result = session.staged_autolock_lock(token, 1, 1)
    assert lock_result["target_voltage"] == pytest.approx(0.1)
    assert len(locked) == 1
    assert session.staged_autolock_state() == {"active": False}


def test_lock_handover_refuses_while_a_one_shot_walk_holds_the_center_lock():
    # The final centre move + lock engage must be serialized with a one-shot
    # walk driving the same actuator, like `step`: refuse, never interleave.
    session = _make_session()
    _geometry_recorder(session)
    locked = _lock_recorder(session)
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            [_result(1, 0.1, sideband_offset_v=0.2)], 0.0, 1.0, 2.0, _frame(1, 0.0, 1.0)
        )
    )
    token = session.staged_autolock_begin(None, 60.0)["token"]
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            [_result(1, 0.1, sideband_offset_v=0.2)], 0.0, 1.0, 2.0, _frame(2, 0.0, 1.0),
        )
    )

    assert session._center_move_lock.acquire(blocking=False)
    try:
        with pytest.raises(StagedAutolockError) as excinfo:
            session.staged_autolock_lock(token, 1, 1)
        assert excinfo.value.status_code == 409
        assert locked == []
    finally:
        session._center_move_lock.release()


@pytest.mark.parametrize("drift_v,accepted", [(0.000889475, True), (0.004, False)])
def test_final_handover_follows_det2_motion_and_keeps_the_rate_gate(monkeypatch, drift_v, accepted):
    """DET2's selected crossing can move beyond three samples before handover."""
    from app.lock_acceptance import AcceptanceSettings

    session = _make_session()
    locked = _lock_recorder(session)
    center, amplitude = 0.6980547959947125, 0.1434375
    selected_v, first_v = 0.682829139110073, 0.6845431
    selected = _result(915, selected_v, sideband_offset_v=0.037979)
    session._capture_auto_lock_candidates_strict = lambda settings, after=None: (
        [selected], center, amplitude, 9.0, _frame(1, center, amplitude)
    )
    token = session.staged_autolock_begin({"half_range_sweep_v": 0.001270151441133366}, 60.0)["token"]
    session._staged_autolock.acceptance = AcceptanceSettings(capture_fraction=0.5, settle_ms=300)
    clock = [1000.0]
    monkeypatch.setattr(session_module.time, "time", lambda: clock[0])
    calls = []

    def capture(settings, after=None):
        calls.append(after)
        clock[0] += 0.922280073
        v = first_v if len(calls) == 1 else first_v - drift_v
        return ([_result(927-len(calls), v, sideband_offset_v=0.037979)],
                center, amplitude, 9.0, _frame(len(calls)+1, center, amplitude))

    session._capture_auto_lock_candidates_strict = capture
    if accepted:
        result = session.staged_autolock_lock(token, 1, 915)
        assert result["target_voltage"] == pytest.approx(first_v-drift_v)
        assert locked[0].target_voltage == pytest.approx(first_v-drift_v)
        verification = result["refinement"]["stages"][-1]
        assert verification["acceptance_policy"] == "one_shot_configured_handover_rate_projection"
        assert verification["acceptance_passed"] is True
        assert verification["pair_displacement_within_capture_window"] is False
    else:
        with pytest.raises(StagedAutolockError, match="drifting") as exc:
            session.staged_autolock_lock(token, 1, 915)
        assert exc.value.details["verification"]["acceptance_passed"] is False
        assert not locked
    assert len(calls) == 2


def test_final_handover_refuses_two_same_slope_candidates_inside_identity_bound():
    session = _make_session()
    locked = _lock_recorder(session)
    selected = _result(1, 0.1, sideband_offset_v=0.2)
    session._capture_auto_lock_candidates_strict = lambda settings, after=None: (
        [selected], 0.0, 1.0, 2.0, _frame(1, 0.0, 1.0)
    )
    token = session.staged_autolock_begin(None, 60.0)["token"]
    session._capture_auto_lock_candidates_strict = lambda settings, after=None: (
        [_result(2, 0.102, sideband_offset_v=0.2),
         _result(3, 0.104, sideband_offset_v=0.2)], 0.0, 1.0, 2.0, _frame(2, 0.0, 1.0)
    )
    with pytest.raises(StagedAutolockError, match="ambiguous"):
        session.staged_autolock_lock(token, 1, 1)
    assert not locked
