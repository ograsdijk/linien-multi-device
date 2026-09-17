"""The guarded center move, driven against a toy hysteretic actuator.

No board is involved. A fake device stands in for the hardware and a small
model decides where the crossing APPEARS for a given commanded sweep center,
which is the only thing the approach loop can observe. That is enough to
exercise the real control logic -- planning, applying set-points, waiting for a
fresh sweep, measuring the offset, correcting, flipping direction, aborting --
against the failure modes it exists to handle.

It does not, and cannot, verify the physical constants. Those need the
diagnostic and a real board.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Callable

import numpy as np
import pytest

import app.session as session_module
from app.auto_lock_scan import AutoLockScanResult, AutoLockScanSettings
from app.lock_approach import ApproachSettings
from app.session import DeviceSession

FEATURE_V = 0.20
START_CENTER_V = -0.30
HALF_RANGE_V = 0.02  # -> capture tolerance 0.01 V, neighbour guard 0.08 V


class RecordingManager:
    def publish(self, device_key: str, message: dict[str, Any]) -> None:
        pass


class FlowingPlotState:
    """Sweeps keep landing on the poll thread, independent of our writes.

    ``last_unlocked_trace_at`` is therefore always current -- until ``flowing``
    is cleared, which is what a stopped or locked sweep looks like to the
    verification step.
    """

    def __init__(self, trace: np.ndarray) -> None:
        self.last_plot_data = [trace, trace, trace]
        self.last_monitor_signal = None
        self.flowing = True
        self._frozen_at = time.time()

    @property
    def last_unlocked_trace_at(self) -> float:
        return time.time() if self.flowing else self._frozen_at


class FakeParam:
    def __init__(self, value: Any) -> None:
        self.value = value


class FakeParameters:
    def __init__(self, **values: Any) -> None:
        for name, value in values.items():
            setattr(self, name, FakeParam(value))


class FakeBoard:
    """Records every sweep-center set-point and says where the feature appears.

    ``error_model`` receives the ordered history of commanded centers and
    returns the displacement between the true feature and where a sweep would
    show it -- the hysteresis excursion.
    """

    def __init__(self, error_model: Callable[[list[float]], float]) -> None:
        self.error_model = error_model
        self.centers: list[float] = []
        self.lock_started = False
        self.detections = 0
        self.plot_state: Any = None
        self.parameters: Any = None

    def on_write_registers(self) -> None:  # noqa: D401 - records a set-point
        self.centers.append(float(self.parameters.sweep_center.value))

    def apparent_feature_v(self) -> float:
        return FEATURE_V + float(self.error_model(self.centers))


class FakeControl:
    def __init__(self, board: FakeBoard) -> None:
        self.board = board

    def exposed_write_registers(self) -> None:
        self.board.on_write_registers()

    def exposed_start_lock(self) -> None:
        self.board.lock_started = True


def _result(target_voltage: float, sideband_offset_v: float | None = None):
    return AutoLockScanResult(
        target_index=1024,
        target_voltage=float(target_voltage),
        target_slope_rising=True,
        score=0.9,
        left_excursion=0.15,
        right_excursion=0.16,
        pair_excursion=0.31,
        symmetry=0.94,
        monitor_level=None,
        hz_per_v=None,
        sideband_offset_v=sideband_offset_v,
    )


def _make_session(
    monkeypatch,
    error_model: Callable[[list[float]], float],
    *,
    approach: dict[str, Any] | None = None,
    sideband_offset_v: float | None = None,
) -> tuple[DeviceSession, FakeBoard]:
    board = FakeBoard(error_model)
    device = SimpleNamespace(
        key="dev-1", name="dev-1", host="127.0.0.1", port=18862, parameters={}
    )
    session = DeviceSession(device, RecordingManager())
    session.parameters = FakeParameters(
        sweep_center=START_CENTER_V,
        sweep_amplitude=1.0,
        target_slope_rising=True,
        modulation_frequency=0.0,
        lock=False,
    )
    session.control = FakeControl(board)
    board.parameters = session.parameters

    session.plot_state = FlowingPlotState(np.zeros(256))
    board.plot_state = session.plot_state

    session.auto_lock_scan_settings["half_range_sweep_v"] = HALF_RANGE_V
    settings = {
        "enabled": True,
        "capture_fraction": 0.5,
        "max_correction_span": 4.0,
        "max_direct_jump_v": 2.0,
        "approach_offset_v": 0.05,
        "ramp_step_v": 0.02,
        "ramp_step_delay_ms": 0,
        "settle_ms": 0,
        "approach_from_below": True,
        "max_approach_iterations": 2,
    }
    settings.update(approach or {})
    session.update_lock_approach_settings(settings)

    def fake_find(**kwargs):
        board.detections += 1
        if board.detections == 1:
            # The initial scan: the detector picks the right crossing.
            return _result(FEATURE_V, sideband_offset_v)
        return _result(board.apparent_feature_v(), sideband_offset_v)

    monkeypatch.setattr(session_module, "find_auto_lock_target", fake_find)
    return session, board


# ---------------------------------------------------------------- error models


def _no_error(_centers: list[float]) -> float:
    return 0.0


def _approached_from_below(centers: list[float]) -> bool:
    if len(centers) < 2:
        return True
    return centers[-1] >= centers[-2]


def _backlash(width: float) -> Callable[[list[float]], float]:
    """Sign flips with the direction of the final move -- classic backlash."""

    def model(centers: list[float]) -> float:
        return width if _approached_from_below(centers) else -width

    return model


def _creep(magnitude: float) -> Callable[[list[float]], float]:
    """Same displacement whichever way you arrive -- creep, not backlash."""

    def model(_centers: list[float]) -> float:
        return magnitude

    return model


def _unrepeatable(width: float) -> Callable[[list[float]], float]:
    """Displacement that never settles; correction cannot converge on it."""

    state = {"n": 0}

    def model(_centers: list[float]) -> float:
        state["n"] += 1
        return width if state["n"] % 2 else -width

    return model


# --------------------------------------------------------------------- tests


def test_a_clean_board_locks_on_the_direct_probe_with_no_correction(monkeypatch):
    session, board = _make_session(monkeypatch, _no_error)

    payload = session.auto_lock_from_scan(None)

    approach = payload["approach"]
    assert approach["accepted"] is True
    assert approach["attempts"][0]["direct"] is True
    assert len(approach["attempts"]) == 1
    assert approach["center_correction_v"] == pytest.approx(0.0)
    assert approach["center_move_v"] == pytest.approx(FEATURE_V - START_CENTER_V)
    assert board.lock_started is True


def test_backlash_is_corrected_and_the_lock_starts(monkeypatch):
    session, board = _make_session(monkeypatch, _backlash(0.03))

    payload = session.auto_lock_from_scan(None)

    approach = payload["approach"]
    assert approach["accepted"] is True
    # First attempt measured the displacement, second landed on it.
    assert len(approach["attempts"]) == 2
    assert approach["attempts"][0]["offset_v"] == pytest.approx(0.03)
    assert approach["attempts"][0]["accepted"] is False
    assert approach["attempts"][1]["accepted"] is True
    assert approach["center_correction_v"] == pytest.approx(0.03)
    assert approach["commanded_voltage"] == pytest.approx(FEATURE_V + 0.03)
    assert board.lock_started is True


def test_creep_is_corrected_the_same_way_backlash_is(monkeypatch):
    # The whole point of correcting rather than only flipping direction: a
    # displacement that does NOT flip sign would defeat a direction-only retry.
    session, board = _make_session(monkeypatch, _creep(0.025))

    payload = session.auto_lock_from_scan(None)

    approach = payload["approach"]
    assert approach["accepted"] is True
    assert approach["center_correction_v"] == pytest.approx(0.025)
    assert board.lock_started is True


def test_the_retry_flips_the_approach_direction(monkeypatch):
    """Exhausting the corrections on one side must re-approach from the other.

    Driven through _approach_and_verify directly, because the report is what
    records the direction and auto_lock_from_scan only raises a message.
    """
    session, board = _make_session(
        monkeypatch, _unrepeatable(0.03), approach={"max_approach_iterations": 1}
    )
    # Stand in for the initial scan, which auto_lock_from_scan would have run.
    board.detections = 1
    scan_settings = AutoLockScanSettings.from_mapping(session.auto_lock_scan_settings)
    approach = ApproachSettings.from_mapping(session.lock_approach_settings)

    report, failure = session._approach_and_verify(
        target_v=FEATURE_V,
        start_center_v=START_CENTER_V,
        approach=approach,
        scan_settings=scan_settings,
        sideband_offset_v=None,
    )

    assert failure is not None
    assert report["accepted"] is False
    # The direct probe sits outside the correction budget, so one correction
    # per direction means probe, one from below, one from above.
    attempts = report["attempts"]
    assert [item["direct"] for item in attempts] == [True, False, False]
    assert [item["from_below"] for item in attempts] == [True, True, False]


def test_both_directions_get_the_same_correction_budget(monkeypatch):
    """The direct probe must not eat one direction's attempts."""
    session, board = _make_session(
        monkeypatch, _unrepeatable(0.03), approach={"max_approach_iterations": 2}
    )
    board.detections = 1
    scan_settings = AutoLockScanSettings.from_mapping(session.auto_lock_scan_settings)
    approach = ApproachSettings.from_mapping(session.lock_approach_settings)

    report, _failure = session._approach_and_verify(
        target_v=FEATURE_V,
        start_center_v=START_CENTER_V,
        approach=approach,
        scan_settings=scan_settings,
        sideband_offset_v=None,
    )

    corrections = [item for item in report["attempts"] if not item["direct"]]
    assert sum(1 for item in corrections if item["from_below"]) == 2
    assert sum(1 for item in corrections if not item["from_below"]) == 2


def test_an_unconvergeable_board_aborts_without_locking(monkeypatch):
    session, board = _make_session(monkeypatch, _unrepeatable(0.03))

    with pytest.raises(RuntimeError) as excinfo:
        session.auto_lock_from_scan(None)

    assert board.lock_started is False
    message = str(excinfo.value)
    assert "Auto-lock aborted" in message
    assert "from below" in message and "from above" in message
    assert "never confirmed the target" in message


def test_an_aborted_approach_puts_the_sweep_center_back(monkeypatch):
    session, board = _make_session(monkeypatch, _unrepeatable(0.03))

    with pytest.raises(RuntimeError) as excinfo:
        session.auto_lock_from_scan(None)

    assert session.parameters.sweep_center.value == pytest.approx(START_CENTER_V)
    assert board.centers[-1] == pytest.approx(START_CENTER_V)
    assert "restored" in str(excinfo.value)


def test_a_neighbouring_crossing_is_rejected_not_chased(monkeypatch):
    """Correcting towards a feature 0.5 V away would walk the lock onto the
    wrong crossing -- exactly the failure this path exists to prevent."""
    session, board = _make_session(monkeypatch, _creep(0.5))

    with pytest.raises(RuntimeError) as excinfo:
        session.auto_lock_from_scan(None)

    assert board.lock_started is False
    assert "different feature" in str(excinfo.value)
    # It stopped on the first measurement rather than trying the other direction.
    assert board.centers[-1] == pytest.approx(START_CENTER_V)


def test_a_known_sideband_offset_tightens_the_neighbour_guard(monkeypatch):
    # Sidebands 0.1 V out put the guard at 0.04 V, so a 0.05 V displacement is
    # a sideband -- even though the feature-width fallback would have allowed it.
    session, _board = _make_session(
        monkeypatch, _creep(0.05), sideband_offset_v=0.1
    )

    with pytest.raises(RuntimeError) as excinfo:
        session.auto_lock_from_scan(None)

    assert "different feature" in str(excinfo.value)


def test_a_large_jump_ramps_in_from_below_rather_than_stepping(monkeypatch):
    session, board = _make_session(
        monkeypatch, _backlash(0.03), approach={"max_direct_jump_v": 0.1}
    )

    session.auto_lock_from_scan(None)

    # The jump from -0.30 to 0.20 exceeds the shortcut, so the very first move
    # overshoots below the target and ramps up onto it.
    assert board.centers[0] == pytest.approx(FEATURE_V - 0.05)
    ramp = board.centers[: board.centers.index(pytest.approx(FEATURE_V)) + 1]
    assert all(b > a for a, b in zip(ramp, ramp[1:]))


def test_the_approach_is_skipped_entirely_when_disabled(monkeypatch):
    session, board = _make_session(monkeypatch, _backlash(0.5), approach={"enabled": False})

    payload = session.auto_lock_from_scan(None)

    assert "approach" not in payload
    assert payload["detail"] == "Auto-lock started from scan."
    # One write of the detected target, straight to the lock -- unchanged behaviour.
    assert board.centers == [pytest.approx(FEATURE_V)]
    assert board.lock_started is True


def test_a_verification_sweep_that_detects_nothing_is_not_a_correction(monkeypatch):
    def blind(_centers: list[float]) -> float:
        raise ValueError("No signal detected on the current trace.")

    session, board = _make_session(monkeypatch, blind)

    with pytest.raises(RuntimeError) as excinfo:
        session.auto_lock_from_scan(None)

    assert board.lock_started is False
    assert "no detection" in str(excinfo.value)


def test_a_sweep_that_stopped_is_reported_as_such_rather_than_locked_blind(monkeypatch):
    session, board = _make_session(monkeypatch, _no_error)
    session.plot_state.flowing = False
    monkeypatch.setattr(session, "_unlocked_trace_timeout_s", lambda: 0.1)

    with pytest.raises(RuntimeError) as excinfo:
        session.auto_lock_from_scan(None)

    assert board.lock_started is False
    assert "no fresh sweep" in str(excinfo.value)


def test_the_diagnostic_identifies_backlash_from_the_sign_flip(monkeypatch):
    session, board = _make_session(monkeypatch, _backlash(0.03))

    report = session.measure_lock_approach([0, 10])

    assert report["verdict"] == "backlash"
    assert len(report["samples"]) == 4
    below = [s["offset_v"] for s in report["samples"] if s["from_below"]]
    above = [s["offset_v"] for s in report["samples"] if not s["from_below"]]
    assert all(value > 0 for value in below)
    assert all(value < 0 for value in above)
    assert board.lock_started is False


def test_the_diagnostic_identifies_creep_when_the_sign_does_not_flip(monkeypatch):
    session, _board = _make_session(monkeypatch, _creep(0.03))

    report = session.measure_lock_approach([0, 10])

    # Same offset either way and it does not shrink over these settle times.
    assert report["verdict"] == "drift_or_creep"
    assert "does not depend on direction" in report["detail"]


def test_the_diagnostic_puts_the_sweep_center_back(monkeypatch):
    session, board = _make_session(monkeypatch, _backlash(0.03))

    session.measure_lock_approach([0])

    assert session.parameters.sweep_center.value == pytest.approx(START_CENTER_V)
    assert board.centers[-1] == pytest.approx(START_CENTER_V)


def test_the_diagnostic_restores_the_center_even_when_it_fails(monkeypatch):
    def blind(_centers: list[float]) -> float:
        raise ValueError("No signal detected on the current trace.")

    session, board = _make_session(monkeypatch, blind)

    report = session.measure_lock_approach([0])

    assert report["verdict"] == "inconclusive"
    assert board.centers[-1] == pytest.approx(START_CENTER_V)


def test_measuring_without_an_overshoot_is_refused_not_guessed(monkeypatch):
    """With approach_offset_v = 0 both directions issue the same set-point, so
    any verdict would compare a measurement against itself."""
    session, board = _make_session(
        monkeypatch, _backlash(0.03), approach={"approach_offset_v": 0.0}
    )

    with pytest.raises(ValueError) as excinfo:
        session.measure_lock_approach([0])

    assert "approach_offset_v" in str(excinfo.value)
    assert board.centers == []


def test_a_target_against_the_rail_is_reported_as_a_gap_not_a_reading(monkeypatch):
    """One side has no room to overshoot, so that sample must not silently
    carry a reading taken from the other direction."""
    session, _board = _make_session(
        monkeypatch, _no_error, approach={"approach_offset_v": 0.05}
    )
    # Every plan comes back direct, as it does for a target pinned to a rail.
    monkeypatch.setattr(
        session_module,
        "plan_approach",
        lambda *_args, **kwargs: _rail_plan(kwargs.get("from_below", True)),
    )

    report = session.measure_lock_approach([0])

    assert report["verdict"] == "inconclusive"
    assert any(sample["offset_v"] is None for sample in report["samples"])


def _rail_plan(from_below: bool):
    from app.lock_approach import ApproachPlan, ApproachStep

    return ApproachPlan(
        steps=(ApproachStep(voltage=1.0, delay_s=0.0),),
        settle_s=0.0,
        direct=True,
        from_below=from_below,
    )


def test_an_uncalibrated_device_is_told_to_calibrate_not_blamed_on_the_signal(monkeypatch):
    """With no calibrated feature width there is no capture region to derive,
    and a zero window would reject every landing as a neighbouring crossing."""
    session, board = _make_session(monkeypatch, _no_error)
    session.auto_lock_scan_settings["half_range_sweep_v"] = 0.0

    # A configuration problem, not a failed lock attempt: ValueError maps to 422.
    with pytest.raises(ValueError) as excinfo:
        session.auto_lock_from_scan(None)

    assert "Calibrate" in str(excinfo.value)
    assert board.lock_started is False
    # It refuses before touching the device at all -- no move, and so no
    # restore write either.
    assert board.centers == []
    assert session.parameters.sweep_center.value == pytest.approx(START_CENTER_V)


def test_a_disabled_neighbour_guard_lets_the_correction_loop_run(monkeypatch):
    """max_correction_span = 0 used to reject every nonzero offset, making the
    device unlockable through one innocuous-looking setting."""
    session, board = _make_session(
        monkeypatch, _creep(0.025), approach={"max_correction_span": 0.0}
    )

    payload = session.auto_lock_from_scan(None)

    assert payload["approach"]["accepted"] is True
    assert payload["approach"]["rejection_bound_v"] is None
    assert board.lock_started is True


def test_a_failed_approach_keeps_its_last_measured_offset(monkeypatch):
    """These rows carry the largest offsets, so blanking the column would empty
    the very field the failure rows exist to fill."""
    session, board = _make_session(monkeypatch, _unrepeatable(0.03))
    board.detections = 1
    scan_settings = AutoLockScanSettings.from_mapping(session.auto_lock_scan_settings)
    approach = ApproachSettings.from_mapping(session.lock_approach_settings)

    report, failure = session._approach_and_verify(
        target_v=FEATURE_V,
        start_center_v=START_CENTER_V,
        approach=approach,
        scan_settings=scan_settings,
        sideband_offset_v=None,
    )

    assert failure is not None
    assert report["accepted"] is False
    assert report["center_offset_v"] is not None
    assert report["center_offset_v"] == report["attempts"][-1]["offset_v"]


def test_the_diagnostic_judges_by_the_window_the_lock_actually_applies(monkeypatch):
    """A tightly spaced signal narrows the lock's window; the diagnostic must not
    call an offset negligible that the lock would then reject."""
    session, _board = _make_session(
        monkeypatch, _creep(0.005), sideband_offset_v=0.015
    )

    report = session.measure_lock_approach([0])

    # 0.4 x 0.015 = 0.006 bound -> 0.003 window, so a 0.005 V offset is NOT
    # negligible, even though capture_fraction x half_range alone would allow it.
    assert report["capture_tolerance_v"] == pytest.approx(0.003)
    assert report["verdict"] != "negligible"


def test_the_diagnostic_refuses_an_uncalibrated_device_too(monkeypatch):
    session, board = _make_session(monkeypatch, _no_error)
    session.auto_lock_scan_settings["half_range_sweep_v"] = 0.0

    with pytest.raises(ValueError) as excinfo:
        session.measure_lock_approach([0])

    assert "Calibrate" in str(excinfo.value)
    assert board.centers == []


def test_a_second_center_move_is_refused_rather_than_interleaved(monkeypatch):
    """Two paths drive the actuator for seconds. Interleaved, each would measure
    offsets the other caused."""
    session, board = _make_session(monkeypatch, _no_error)
    assert session._center_move_lock.acquire(blocking=False)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            session.auto_lock_from_scan(None)
        assert "already running" in str(excinfo.value)
        assert board.lock_started is False

        with pytest.raises(RuntimeError) as measure_error:
            session.measure_lock_approach([0])
        assert "already running" in str(measure_error.value)
    finally:
        session._center_move_lock.release()


def test_the_lock_is_released_after_a_failed_move(monkeypatch):
    session, _board = _make_session(monkeypatch, _unrepeatable(0.03))

    with pytest.raises(RuntimeError):
        session.auto_lock_from_scan(None)

    assert session._center_move_lock.acquire(blocking=False)
    session._center_move_lock.release()


def test_the_guard_covers_the_plain_direct_path_too(monkeypatch):
    session, board = _make_session(monkeypatch, _no_error, approach={"enabled": False})
    assert session._center_move_lock.acquire(blocking=False)
    try:
        with pytest.raises(RuntimeError):
            session.auto_lock_from_scan(None)
        assert board.lock_started is False
    finally:
        session._center_move_lock.release()


def test_a_relock_action_runs_off_the_calling_thread(monkeypatch):
    """_on_to_plot runs on the poll thread, which is also the only thread that
    advances last_unlocked_trace_at. A guarded move waits for that stamp, so
    running the relock action inline would block waiting for output it is itself
    preventing -- timing out every attempt and stalling the plot pipeline."""
    import threading

    session, _board = _make_session(monkeypatch, _no_error)
    completed: list[tuple] = []
    session.auto_relock = SimpleNamespace(
        complete_action=lambda *args: completed.append(args)
    )
    ran_on: list[int] = []
    done = threading.Event()

    def _action() -> None:
        ran_on.append(threading.get_ident())
        done.set()

    session._relock_action_lock.acquire()
    session._dispatch_relock_action(_action)

    assert done.wait(timeout=2.0)
    assert ran_on and ran_on[0] != threading.get_ident()
    for _ in range(200):
        if completed:
            break
        time.sleep(0.01)
    assert completed == [("relock", True, None)]


def test_a_failed_relock_action_is_still_reported_back(monkeypatch):
    session, _board = _make_session(monkeypatch, _no_error)
    completed: list[tuple] = []
    session.auto_relock = SimpleNamespace(
        complete_action=lambda *args: completed.append(args)
    )

    session._relock_action_lock.acquire()
    session._dispatch_relock_action(
        lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    for _ in range(200):
        if completed:
            break
        time.sleep(0.01)
    assert completed == [("relock", False, "boom")]


def _relock_session(monkeypatch):
    """A session wired so _on_to_plot will hand out a "relock" action."""
    session, board = _make_session(monkeypatch, _no_error)
    completed: list[tuple] = []
    dispatched: list[object] = []
    session.auto_relock = SimpleNamespace(
        complete_action=lambda *args: completed.append(args),
        get_status=lambda: {},
    )
    return session, board, completed, dispatched


def test_a_duplicate_relock_action_is_dropped_not_completed(monkeypatch):
    """tick() hands out "relock" on every frame until complete_action lands,
    which was only safe while the action ran synchronously. A duplicate must be
    skipped outright -- completing it would record a spurious failure and
    discard the result of the attempt actually in flight."""
    import threading

    session, _board, completed, _ = _relock_session(monkeypatch)
    release = threading.Event()
    started = threading.Event()

    def _slow_action() -> None:
        started.set()
        release.wait(timeout=2.0)

    assert session._maybe_dispatch_relock_action(_slow_action) is True
    assert started.wait(timeout=2.0)

    # What the next plot frame does while the first is still running.
    assert session._maybe_dispatch_relock_action(_slow_action) is False
    assert completed == []  # nothing was reported on the pending action

    release.set()
    for _ in range(200):
        if completed:
            break
        time.sleep(0.01)
    assert completed == [("relock", True, None)]


def test_the_action_lock_is_held_until_the_result_is_applied(monkeypatch):
    """Releasing before complete_action would let the next frame dispatch again
    while the controller still reports the action as pending."""
    import threading

    session, _board, completed, _ = _relock_session(monkeypatch)
    held_at_completion: list[bool] = []
    session.auto_relock = SimpleNamespace(
        complete_action=lambda *args: (
            held_at_completion.append(
                session._relock_action_lock.acquire(blocking=False) is False
            ),
            completed.append(args),
        ),
        get_status=lambda: {},
    )

    session._relock_action_lock.acquire()
    session._dispatch_relock_action(lambda: None)

    for _ in range(200):
        if completed:
            break
        time.sleep(0.01)
    assert held_at_completion == [True]
    # And released afterwards, so the next attempt can run.
    assert session._relock_action_lock.acquire(blocking=False)
    session._relock_action_lock.release()


def test_disconnect_waits_for_an_in_flight_relock(monkeypatch):
    """A guarded move takes seconds; tearing the connection down underneath one
    strands the sweep center part-way along a ramp."""
    import threading

    session, _board, _completed, _ = _relock_session(monkeypatch)
    finished = threading.Event()

    def _slow_action() -> None:
        time.sleep(0.2)
        finished.set()

    session._relock_action_lock.acquire()
    session._dispatch_relock_action(_slow_action)

    session._await_relock_action(timeout_s=2.0)

    assert finished.is_set()


def test_disconnect_gives_up_on_a_stuck_relock_rather_than_hanging(monkeypatch):
    import threading

    session, _board, _completed, _ = _relock_session(monkeypatch)
    release = threading.Event()

    session._relock_action_lock.acquire()
    session._dispatch_relock_action(lambda: release.wait(timeout=5.0))

    started = time.time()
    session._await_relock_action(timeout_s=0.1)
    assert (time.time() - started) < 1.0

    release.set()


def test_an_auto_relock_row_carries_the_guarded_move_it_actually_ran(monkeypatch):
    """Auto-relock runs unattended and repeatedly, so it is the richest source
    of the offsets these columns exist to trend. Recording approach_enabled as
    false there is not just missing data -- it is a wrong value in a column you
    would filter on."""
    session, _board = _make_session(monkeypatch, _creep(0.025))
    rows: list[dict] = []
    session._lock_result_postgres = SimpleNamespace(
        enqueue_lock_result=lambda row: rows.append(row) or True
    )

    payload = session.auto_lock_from_scan(None)
    session._write_lock_result_to_postgres(
        lock_source="auto_relock",
        event_source="auto_relock",
        approach=payload["approach"],
    )

    assert rows and rows[0]["approach_enabled"] is True
    assert rows[0]["center_correction_v"] == pytest.approx(0.025)


def test_a_lock_without_a_guarded_move_still_records_it_as_disabled(monkeypatch):
    session, _board = _make_session(monkeypatch, _no_error, approach={"enabled": False})
    rows: list[dict] = []
    session._lock_result_postgres = SimpleNamespace(
        enqueue_lock_result=lambda row: rows.append(row) or True
    )

    session.auto_lock_from_scan(None)
    session._write_lock_result_to_postgres(
        lock_source="auto_relock", event_source="auto_relock"
    )

    assert rows and rows[0]["approach_enabled"] is False


class SteppedPlotState:
    """A plot state whose frames land only when the test says so.

    last_unlocked_trace_at records when a frame was PROCESSED, not when the
    board acquired it, so a single frame after the move proves nothing about the
    trace's contents.
    """

    def __init__(self) -> None:
        self.last_plot_data = [np.zeros(256)] * 3
        self.last_monitor_signal = None
        self.last_unlocked_trace_at = time.time()

    def deliver(self) -> None:
        self.last_unlocked_trace_at = time.time()


def test_one_processed_frame_is_not_accepted_as_proof_of_a_fresh_sweep(monkeypatch):
    """A trace pulled off the device mid-ramp, or before the settle finished,
    can be processed after the move and would pass a single-frame check -- which
    is exactly the un-settled state settle_ms exists to let decay."""
    session, _board = _make_session(monkeypatch, _no_error)
    stepped = SteppedPlotState()
    session.plot_state = stepped
    moved_at = time.time()
    time.sleep(0.01)  # a real move takes time; two time.time() calls may not
    stepped.deliver()  # one frame only

    assert session._wait_for_fresh_unlocked_trace(moved_at, 0.3) is False
    # The same single frame satisfies the old one-frame rule.
    assert session._wait_for_fresh_unlocked_trace(moved_at, 0.3, frames=1) is True


def test_a_second_frame_confirms_the_sweep_post_dates_the_move(monkeypatch):
    import threading

    session, _board = _make_session(monkeypatch, _no_error)
    stepped = SteppedPlotState()
    session.plot_state = stepped
    moved_at = time.time()
    time.sleep(0.01)

    def _deliver_two() -> None:
        time.sleep(0.05)
        stepped.deliver()
        time.sleep(0.05)
        stepped.deliver()

    threading.Thread(target=_deliver_two, daemon=True).start()

    assert session._wait_for_fresh_unlocked_trace(moved_at, 2.0) is True


def test_the_wait_budget_is_per_frame(monkeypatch):
    session, _board = _make_session(monkeypatch, _no_error)
    stepped = SteppedPlotState()
    session.plot_state = stepped
    started = time.time()

    assert session._wait_for_fresh_unlocked_trace(time.time(), 0.2, frames=2) is False

    # Two frames' worth of budget, not one.
    assert (time.time() - started) >= 0.4


def test_the_timeout_message_reports_the_wait_that_actually_happened(monkeypatch):
    """The budget is per frame, so quoting the per-frame figure would tell an
    operator three seconds after waiting six."""
    session, _board = _make_session(monkeypatch, _no_error)
    session.plot_state = SteppedPlotState()  # no frames will be delivered
    monkeypatch.setattr(session, "_unlocked_trace_timeout_s", lambda: 0.1)
    scan_settings = AutoLockScanSettings.from_mapping(session.auto_lock_scan_settings)

    started = time.time()
    _center, _detected, offset_v, detail = session._redetect_after_move(
        scan_settings, time.time()
    )
    elapsed = time.time() - started

    assert offset_v is None
    assert "0.2 s" in detail  # 0.1 s per frame, two frames
    assert elapsed >= 0.2


def test_the_diagnostic_is_recorded_not_just_returned(monkeypatch):
    """Both directions at several settle times is the most direct hysteresis
    data there is; returning it only in the HTTP response would mean running it
    on ten lasers and keeping none of it."""
    session, _board = _make_session(monkeypatch, _backlash(0.03))
    rows: list[dict] = []
    session._lock_result_postgres = SimpleNamespace(
        enqueue_lock_result=lambda row: rows.append(row) or True
    )

    report = session.measure_lock_approach([0, 10])

    assert rows and rows[0]["lock_source"] == "lock_approach_probe"
    assert rows[0]["success"] is True
    assert rows[0]["approach_enabled"] is True
    # The probe restores the center, so no net move -- but the excursion stands.
    assert rows[0]["center_move_v"] == pytest.approx(0.0)
    assert abs(rows[0]["center_offset_v"]) == pytest.approx(0.03)
    assert rows[0]["capture_tolerance_v"] == pytest.approx(report["capture_tolerance_v"])


def test_an_inconclusive_measurement_is_recorded_as_unsuccessful(monkeypatch):
    def blind(_centers: list[float]) -> float:
        raise ValueError("No signal detected on the current trace.")

    session, _board = _make_session(monkeypatch, blind)
    rows: list[dict] = []
    session._lock_result_postgres = SimpleNamespace(
        enqueue_lock_result=lambda row: rows.append(row) or True
    )

    session.measure_lock_approach([0])

    assert rows and rows[0]["success"] is False
    assert rows[0]["center_offset_v"] is None


def test_the_stored_samples_survive_as_json(monkeypatch):
    import json

    session, _board = _make_session(monkeypatch, _backlash(0.03))
    rows: list[dict] = []
    session._lock_result_postgres = SimpleNamespace(
        enqueue_lock_result=lambda row: rows.append(row) or True
    )

    session.measure_lock_approach([0, 10])

    stored = json.loads(rows[0]["approach_detail"])
    assert len(stored) == 4
    assert {item["from_below"] for item in stored} == {True, False}


# ------------------------------------------------ refined-lock geometry restore


REFINED_AMPLITUDE_V = 0.05


def _refined_session(monkeypatch):
    """A session whose auto-lock goes through trajectory refinement.

    The refinement is stubbed to do what the real one leaves behind on
    success: a scan narrowed around the feature. Sweep starts are recorded in
    the same event list as register writes, so ordering can be asserted.
    """
    session, board = _make_session(monkeypatch, _no_error)
    events: list[tuple[str, float, float]] = []
    board.on_write_registers = lambda: events.append((
        "write",
        float(session.parameters.sweep_center.value),
        float(session.parameters.sweep_amplitude.value),
    ))
    session.parameters.fetch_additional_signals = FakeParam(False)
    session.parameters.task = FakeParam(None)

    def _start_sweep() -> None:
        session.parameters.lock.value = False
        events.append(("sweep", float("nan"), float("nan")))

    def _start_lock() -> None:
        board.lock_started = True
        session.parameters.lock.value = True
        events.append(("lock", float(session.parameters.sweep_center.value), float("nan")))

    session.control.exposed_start_sweep = _start_sweep
    session.control.exposed_start_lock = _start_lock
    # A live-looking trace, so the dead-signal shortcut does not skip refinement.
    session.plot_state.last_plot_data = [np.linspace(-1e4, 1e4, 256)] * 3
    monkeypatch.setattr(
        session,
        "_capture_auto_lock_target",
        lambda settings, traces=None, after=None: (
            _result(FEATURE_V), START_CENTER_V, 1.0, 2.0  # under-resolved
        ),
    )

    def _refine(settings, center, amplitude, **_kwargs):
        session.parameters.sweep_center.value = FEATURE_V
        session.parameters.sweep_amplitude.value = REFINED_AMPLITUDE_V
        return _result(FEATURE_V), {"attempted": True, "stages": []}

    monkeypatch.setattr(session, "_trajectory_refine_auto_lock", _refine)
    return session, board, events


def test_a_refined_lock_keeps_its_operating_point_while_locked(monkeypatch):
    """Restoring the old center after start_lock moved the lock's hold point
    by (target - original center) and dropped the lock."""
    session, board, events = _refined_session(monkeypatch)

    payload = session.auto_lock_from_scan(None)

    assert board.lock_started
    assert "refinement" in payload
    lock_at = [e for e in events if e[0] == "lock"][-1][1]
    assert lock_at == pytest.approx(FEATURE_V, abs=0.02)
    after_lock = events[events.index(next(e for e in events if e[0] == "lock")) + 1 :]
    assert after_lock == []  # nothing written while locked
    assert session.parameters.sweep_center.value == pytest.approx(lock_at)


def test_a_refined_lock_restores_the_scan_when_the_sweep_restarts(monkeypatch):
    session, _board, events = _refined_session(monkeypatch)
    session.auto_lock_from_scan(None)
    events.clear()

    session.stop_lock()

    assert events[0][0] == "sweep"  # unlocked first ...
    assert events[1] == ("write", START_CENTER_V, 1.0)  # ... then restored
    assert session.parameters.sweep_center.value == START_CENTER_V
    assert session.parameters.sweep_amplitude.value == 1.0

    # One-shot: a later sweep start does not write it again.
    events.clear()
    session.start_sweep()
    assert [event[0] for event in events] == ["sweep"]


def test_the_operators_own_geometry_cancels_a_pending_restore(monkeypatch):
    session, _board, events = _refined_session(monkeypatch)
    monkeypatch.setattr(session, "_update_persistent_setting", lambda *_a: None)
    session.auto_lock_from_scan(None)

    session.set_param("sweep_amplitude", 0.3, write_registers=False)
    session.start_sweep()

    assert session.parameters.sweep_amplitude.value == 0.3
    assert session.parameters.sweep_center.value != START_CENTER_V


# ----------------------------------------- auto-relock lock-result rows


def _recorded_rows(monkeypatch, session):
    rows: list[dict] = []
    monkeypatch.setattr(
        session,
        "_write_lock_result_to_postgres",
        lambda **kwargs: rows.append(kwargs),
    )
    monkeypatch.setattr(session, "_emit_log_event", lambda **_kwargs: None)
    return rows


def test_a_relock_that_moved_nothing_writes_no_failure_row(monkeypatch):
    """E.g. a laser far off resonance fails detection on every relock tick;
    a row per tick with no approach data would skew the failure statistics."""
    session, _board = _make_session(monkeypatch, _no_error)
    rows = _recorded_rows(monkeypatch, session)
    session.parameters.lock.value = True  # "already locked": nothing moves

    with pytest.raises(RuntimeError, match="already locked"):
        session._start_auto_relock()

    assert rows == []


def test_a_relock_whose_guarded_move_aborted_writes_a_failure_row(monkeypatch):
    session, board = _make_session(monkeypatch, _unrepeatable(0.03))
    rows = _recorded_rows(monkeypatch, session)

    with pytest.raises(RuntimeError, match="Auto-lock aborted"):
        session._start_auto_relock()

    assert board.lock_started is False
    assert len(rows) == 1
    assert rows[0]["success"] is False
    assert rows[0]["lock_source"] == "auto_relock"
    assert isinstance(rows[0]["approach"], dict)
    assert rows[0]["approach"]["attempts"]


def test_a_successful_relock_writes_its_row(monkeypatch):
    session, board = _make_session(monkeypatch, _no_error)
    rows = _recorded_rows(monkeypatch, session)

    session._start_auto_relock()

    assert board.lock_started
    assert len(rows) == 1
    assert rows[0].get("success", True) is True
