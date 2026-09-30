"""The signed piezo-hysteresis model: prediction, pre-compensation, identity.

After a scan-geometry change a feature's apparent position shifts by ``-h * dL``,
``dL`` being the change in the LOWER scan endpoint (centre - amplitude). These
tests pin the pure functions, the planner's pre-compensation of the commanded
centre, the position check that refuses a candidate off the predicted position
(the adjacent-crossing slip), the staged API's ``hysteresis`` block, and the
settings that carry the model. The physical numbers are the measured ones:
h = 0.085, a slip is about one sideband spacing (20-60 mV).
"""

from __future__ import annotations

import json
import math
import time
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

import app.session as session_module
from app import schemas
from app.auto_lock_scan import AutoLockScanResult, AutoLockScanSettings
from app.lock_acceptance import AcceptanceSettings
from app.lock_refinement import (
    bounded_recenter_v,
    hysteresis_block,
    hysteresis_tolerance_v,
    hysteresis_window_violation,
    lower_endpoint_change_v,
    plan_refinement_step,
    precompensated_center_v,
    predicted_shift_v,
)
from app.session import DeviceSession, StagedAutolockError

H = 0.085


# ------------------------------------------------------------ pure functions


def test_the_shift_is_minus_h_times_the_lower_endpoint_change():
    # Narrowing 1.0 -> 0.5 at a fixed centre raises the lower endpoint 0.5 V.
    assert lower_endpoint_change_v(0.0, 1.0, 0.0, 0.5) == pytest.approx(0.5)
    assert predicted_shift_v(0.0, 1.0, 0.0, 0.5, H) == pytest.approx(-0.0425)
    # Widening is the exact mirror: same size, opposite sign.
    assert predicted_shift_v(0.0, 0.5, 0.0, 1.0, H) == pytest.approx(+0.0425)


def test_only_the_lower_endpoint_matters():
    # Centre up and amplitude up by the same amount: the lower endpoint (c - a)
    # is unchanged, only the upper one moved -- no shift.
    assert predicted_shift_v(0.1, 0.5, 0.3, 0.7, H) == pytest.approx(0.0)
    # Centre moved up 0.1 V at fixed width: dL = +0.1 V.
    assert predicted_shift_v(0.0, 0.5, 0.1, 0.5, H) == pytest.approx(-0.0085)


def test_the_shift_keeps_its_sign_and_is_a_plain_zero_when_nothing_moved():
    assert predicted_shift_v(0.2, 0.4, 0.3, 0.4, H) < 0.0
    assert predicted_shift_v(0.2, 0.4, 0.1, 0.4, H) > 0.0
    zero = predicted_shift_v(0.2, 0.4, 0.2, 0.4, H)
    assert zero == 0.0 and math.copysign(1.0, zero) == 1.0  # not -0.0
    assert json.dumps(zero) == "0.0"


def test_a_signed_amplitude_gives_the_same_endpoint_as_its_magnitude():
    assert lower_endpoint_change_v(0.0, -1.0, 0.0, -0.5) == pytest.approx(0.5)


def test_the_tolerance_grows_with_the_endpoint_change_on_a_floor():
    assert hysteresis_tolerance_v(0.0, 0.015, 0.005) == pytest.approx(0.005)
    assert hysteresis_tolerance_v(0.5, 0.015, 0.005) == pytest.approx(0.0125)
    assert hysteresis_tolerance_v(-0.5, 0.015, 0.005) == pytest.approx(0.0125)


def test_the_hysteresis_block_has_the_documented_shape():
    settings = AutoLockScanSettings()
    block = hysteresis_block(settings, 0.0, 1.0, 0.05, 0.3)
    # The published schema (extra="forbid") accepts exactly this shape.
    schemas.StagedAutolockHysteresis.model_validate(block)
    assert set(block) == {
        "h_per_volt", "delta_lower_v", "predicted_shift_v", "tolerance_v",
        "old_geometry", "new_geometry",
    }
    assert block["h_per_volt"] == pytest.approx(0.085)
    assert block["delta_lower_v"] == pytest.approx(0.75)
    assert block["predicted_shift_v"] == pytest.approx(-0.06375)
    assert block["tolerance_v"] == pytest.approx(0.015 * 0.75 + 0.005)
    assert block["old_geometry"] == {"center_v": 0.0, "amplitude_v": 1.0}
    assert block["new_geometry"] == {"center_v": 0.05, "amplitude_v": 0.3}


# --------------------------------------------------------- pre-compensation


@pytest.mark.parametrize(
    "c_old, a_old, a_new, target",
    [
        (0.0, 1.0, 0.5, 0.1),
        (0.3, 0.6, 0.45, 0.42),
        (-0.2, 0.8, 0.2, -0.35),
        (0.0, 0.5, 0.5, 0.05),  # a pure recentre
    ],
)
def test_the_precompensated_centre_is_the_exact_fixed_point(c_old, a_old, a_new, target):
    """The shift depends on the centre being solved for; the compensated centre
    must land the target ON the centre it puts the scan at."""
    centre = precompensated_center_v(c_old, a_old, a_new, target, H)
    landing = target + predicted_shift_v(c_old, a_old, centre, a_new, H)
    assert landing == pytest.approx(centre, abs=1e-12)


def test_with_the_model_off_the_compensation_is_the_identity():
    assert precompensated_center_v(0.0, 1.0, 0.5, 0.123, 0.0) == pytest.approx(0.123)


def _planner_settings(**overrides: Any) -> AutoLockScanSettings:
    settings = AutoLockScanSettings.from_mapping({})
    settings.half_range_sweep_v = 0.001
    settings.min_signal_scan_fraction = 0.0
    settings.max_center_step_signal_widths = 4.0
    for name, value in overrides.items():
        setattr(settings, name, value)
    return settings


def _plan(settings: AutoLockScanSettings, *, center_v, amplitude_v, target_v):
    return plan_refinement_step(
        settings, center_v=center_v, amplitude_v=amplitude_v, target_v=target_v,
        sideband_offset_v=0.10, detector="coarse", trace_length=2048,
    )


def test_a_narrowing_lands_the_target_where_the_planner_intends_under_the_shift():
    """Simulate the device: after the write the feature sits at
    target - h * dL. The planner must have chosen the centre so that this is
    within tolerance of the centre itself (target centred in the new window)."""
    settings = _planner_settings()
    center_v, amplitude_v, target_v = 0.0, 0.5, 0.03
    step = _plan(settings, center_v=center_v, amplitude_v=amplitude_v, target_v=target_v)
    assert step.action == "narrow"

    delta_lower = lower_endpoint_change_v(
        center_v, amplitude_v, step.center_v, step.amplitude_v
    )
    landed_v = target_v + predicted_shift_v(
        center_v, amplitude_v, step.center_v, step.amplitude_v, H
    )
    tolerance_v = hysteresis_tolerance_v(delta_lower, 0.015, 0.005)
    assert abs(landed_v - step.center_v) <= tolerance_v
    # ...and that is not vacuous: aiming at the target itself, as the planner
    # did before the model, would have missed the window centre by more.
    naive_centre = bounded_recenter_v(
        center_v, target_v, amplitude_v, signal_width_v=0.2, max_signal_widths=4.0,
        rail_amplitude_v=step.amplitude_v,
    )
    naive_landed = target_v + predicted_shift_v(
        center_v, amplitude_v, naive_centre, step.amplitude_v, H
    )
    assert abs(naive_landed - naive_centre) > 2.0 * tolerance_v
    # It landed the target in the new window, not merely near the old centre.
    assert step.bounds["predicted_target_v"] == pytest.approx(landed_v)


def test_the_compensated_centre_is_still_held_to_the_step_allowance():
    """Every existing bound applies to the compensated centre: a target far
    outside is approached by at most the centre-step budget."""
    settings = _planner_settings(max_center_step_signal_widths=0.25)
    step = _plan(settings, center_v=0.0, amplitude_v=0.5, target_v=0.45)
    assert step.action in {"narrow", "recenter"}
    assert abs(step.center_v - 0.0) <= step.bounds["centre_budget_v"] + 1e-9
    assert abs(step.center_v) <= 0.25 * 0.5 + 1e-9  # a quarter of the half-range


def test_with_the_model_off_the_planner_aims_at_the_target_as_before():
    settings = _planner_settings(hysteresis_per_volt_lower=0.0)
    step = _plan(settings, center_v=0.0, amplitude_v=0.5, target_v=0.03)
    assert step.center_v == pytest.approx(0.03)


def test_a_recentre_is_compensated_too():
    """A pure centre move changes the lower endpoint by the move itself."""
    centre = precompensated_center_v(0.0, 0.5, 0.5, 0.08, H)
    assert centre == pytest.approx(0.08 / (1.0 + H))
    assert 0.0 < centre < 0.08


# ------------------------------------------------------------- window check


def test_a_candidate_at_the_predicted_position_is_accepted_and_a_slip_is_not():
    block = hysteresis_block(AutoLockScanSettings(), 0.0, 1.0, 0.05, 0.3)
    predicted_v = 0.180 + block["predicted_shift_v"]  # 0.11625
    assert hysteresis_window_violation(block, 0.180, predicted_v) is None
    assert hysteresis_window_violation(block, 0.180, predicted_v + 0.010) is None
    for sideband_spacing_v in (0.020, 0.040, 0.060):
        for sign in (+1.0, -1.0):
            reason = hysteresis_window_violation(
                block, 0.180, predicted_v + sign * sideband_spacing_v
            )
            assert reason is not None and "hysteresis window" in reason


def test_a_candidate_that_did_not_move_is_a_slip_when_a_shift_was_predicted():
    """The mirror of the old behaviour: standing still after a 0.75 V endpoint
    change is not the same feature."""
    block = hysteresis_block(AutoLockScanSettings(), 0.0, 1.0, 0.05, 0.3)
    assert hysteresis_window_violation(block, 0.180, 0.180) is not None


# ------------------------------------------------------ staged API contract


class _Manager:
    def publish(self, device_key: str, message: dict[str, Any]) -> None:
        pass


class _Param:
    def __init__(self, value: Any) -> None:
        self.value = value


def _result(index: int, voltage: float, *, sideband: float = 0.03) -> AutoLockScanResult:
    return AutoLockScanResult(
        target_index=index, target_voltage=float(voltage), target_slope_rising=True,
        score=0.9, left_excursion=0.15, right_excursion=0.16, pair_excursion=0.31,
        symmetry=0.94, monitor_level=None, hz_per_v=None, sideband_offset_v=sideband,
    )


def _frame(frame_id: int, center_v: float, amplitude_v: float) -> dict[str, Any]:
    return {
        "frame_id": frame_id, "acquired_at": time.time(),
        "sweep_center_v": center_v, "sweep_amplitude_v": amplitude_v,
        "n_points": 2048, "modulation_frequency_hz": 10e6,
        "sideband_spacing_samples": 50.0, "noise_floor": 0.001,
    }


def _staged_session() -> DeviceSession:
    device = SimpleNamespace(
        key="dev-hyst", name="dev-hyst", host="127.0.0.1", port=18865, parameters={}
    )
    session = DeviceSession(device, _Manager())
    session.control = object()
    session.parameters = SimpleNamespace(
        sweep_center=_Param(0.0), sweep_amplitude=_Param(1.0),
        target_slope_rising=_Param(True), modulation_frequency=_Param(0.0),
        lock=_Param(False),
    )

    def _write(center_v, amplitude_v, *, settle_s=0.0):
        session.parameters.sweep_center.value = float(center_v)
        session.parameters.sweep_amplitude.value = float(amplitude_v)
        return time.time()

    session._set_sweep_geometry = _write  # type: ignore[method-assign]
    session._restore_sweep_geometry = lambda c, a: True  # type: ignore[method-assign]
    return session


def _detect(session: DeviceSession, candidates, center_v, amplitude_v, frame_id):
    session._capture_auto_lock_candidates_strict = (  # type: ignore[method-assign]
        lambda settings, after=None: (
            candidates, center_v, amplitude_v, 2.0,
            _frame(frame_id, center_v, amplitude_v),
        )
    )


@pytest.fixture
def staged():
    session = _staged_session()
    yield session
    run = session._staged_autolock
    if run is not None and run.timer is not None:
        run.timer.cancel()


def test_begin_carries_the_hysteresis_block_with_no_endpoint_change(staged):
    _detect(staged, [_result(1, 0.18)], 0.0, 1.0, 1)
    begun = staged.staged_autolock_begin(None, 60.0)
    block = begun["hysteresis"]
    schemas.StagedAutolockHysteresis.model_validate(block)
    assert block["delta_lower_v"] == 0.0
    assert block["predicted_shift_v"] == 0.0
    assert block["tolerance_v"] == pytest.approx(0.005)  # the floor
    assert block["h_per_volt"] == pytest.approx(0.085)
    assert block["old_geometry"] == block["new_geometry"] == {
        "center_v": 0.0, "amplitude_v": 1.0,
    }


def test_begin_reports_the_devices_own_settings(staged):
    staged.update_auto_lock_scan_settings({
        "hysteresis_per_volt_lower": 0.09, "hysteresis_tolerance_per_volt": 0.02,
        "hysteresis_floor_v": 0.004,
    })
    _detect(staged, [_result(1, 0.18)], 0.0, 1.0, 1)
    block = staged.staged_autolock_begin(None, 60.0)["hysteresis"]
    assert block["h_per_volt"] == pytest.approx(0.09)
    assert block["tolerance_v"] == pytest.approx(0.004)


def test_a_narrowing_step_reports_its_own_endpoint_change(staged):
    _detect(staged, [_result(1, 0.18)], 0.0, 1.0, 1)
    token = staged.staged_autolock_begin(None, 60.0)["token"]
    _detect(staged, [_result(2, 0.1163)], 0.05, 0.3, 2)
    step = staged.staged_autolock_step(token, 1, 1)
    block = step["hysteresis"]
    schemas.StagedAutolockHysteresis.model_validate(block)
    assert block["old_geometry"] == {"center_v": 0.0, "amplitude_v": 1.0}
    assert block["new_geometry"] == {"center_v": 0.05, "amplitude_v": 0.3}
    assert block["delta_lower_v"] == pytest.approx(0.75)
    assert block["predicted_shift_v"] == pytest.approx(-H * 0.75)
    assert block["tolerance_v"] == pytest.approx(0.015 * 0.75 + 0.005)
    assert block["h_per_volt"] == pytest.approx(H)


def test_a_done_step_reports_no_endpoint_change(staged):
    """Nothing moved: dL = 0, predicted 0, tolerance = the floor."""
    _detect(staged, [_result(1, 0.2, sideband=0.5)], 0.0, 1.0, 1)
    token = staged.staged_autolock_begin(None, 60.0)["token"]
    step = staged.staged_autolock_step(token, 1, 1)
    assert step["planner"]["action"] == "done"
    block = step["hysteresis"]
    assert block["delta_lower_v"] == 0.0
    assert block["predicted_shift_v"] == 0.0
    assert block["tolerance_v"] == pytest.approx(0.005)
    assert block["old_geometry"] == block["new_geometry"]


def test_an_adjacent_crossing_slip_is_refused_on_the_next_step(staged):
    """One sideband spacing (40 mV) off the predicted position: the candidate is
    annotated identity_ok=false before it is picked, and picking it is a 422."""
    _detect(staged, [_result(1, 0.180)], 0.0, 1.0, 1)
    token = staged.staged_autolock_begin(None, 60.0)["token"]
    predicted_v = 0.180 - H * 0.75  # 0.11625
    frame_2 = [
        _result(10, predicted_v + 0.003),  # the tracked feature, 3 mV off
        _result(11, predicted_v + 0.040),  # one sideband spacing up
        _result(12, predicted_v - 0.040),  # one sideband spacing down
    ]
    _detect(staged, frame_2, 0.05, 0.3, 2)
    step = staged.staged_autolock_step(token, 1, 1)
    ok = {c["target_index"]: c["identity_ok"] for c in step["candidates"]}
    assert ok == {10: True, 11: False, 12: False}

    for slipped in (11, 12):
        with pytest.raises(StagedAutolockError) as excinfo:
            staged.staged_autolock_step(token, 2, slipped)
        assert excinfo.value.status_code == 422
        assert "hysteresis window" in str(excinfo.value)
    # The run is still active and the correct selection is still accepted.
    _detect(staged, [_result(20, predicted_v + 0.003, sideband=0.5)], 0.05, 0.3, 3)
    staged.staged_autolock_step(token, 2, 10)


def test_lock_straight_after_a_step_refuses_a_slipped_selection(staged):
    _detect(staged, [_result(1, 0.180, sideband=0.5)], 0.0, 1.0, 1)
    token = staged.staged_autolock_begin(None, 60.0)["token"]
    predicted_v = 0.180 - H * 0.75
    _detect(staged, [_result(10, predicted_v + 0.040, sideband=0.5)], 0.05, 0.3, 2)
    # Force a geometry-changing step (a narrow candidate on the first frame).
    staged._staged_autolock.latest_candidates = [_result(1, 0.180, sideband=0.03)]
    staged.staged_autolock_step(token, 1, 1)
    locked: list[Any] = []
    staged._move_and_lock = lambda *a, **k: locked.append(a)  # type: ignore[method-assign]
    with pytest.raises(StagedAutolockError, match="hysteresis window"):
        staged.staged_autolock_lock(token, 2, 10)
    assert locked == []


# ------------------------------------------------------------ one-shot walk


def _walk_session(slip_v: float):
    """A bare session whose detections follow -h * dL, plus ``slip_v``."""
    device = SimpleNamespace(
        key="dev-walk", name="dev-walk", host="127.0.0.1", port=18866, parameters={}
    )
    session = DeviceSession(device, _Manager())
    session.parameters = SimpleNamespace(
        sweep_center=_Param(0.0), sweep_amplitude=_Param(1.0),
        target_slope_rising=_Param(True), modulation_frequency=_Param(0.0),
        lock=_Param(False),
    )
    session.control = SimpleNamespace(
        exposed_write_registers=lambda: None, exposed_start_lock=lambda: None
    )
    session.auto_lock_scan_settings["half_range_sweep_v"] = 0.02

    def _write(center_v, amplitude_v, *, settle_s=0.0):
        session.parameters.sweep_center.value = float(center_v)
        session.parameters.sweep_amplitude.value = float(amplitude_v)
        return time.time()

    def _where():
        c = float(session.parameters.sweep_center.value)
        a = float(session.parameters.sweep_amplitude.value)
        moved = a != 1.0 or c != 0.0
        return 0.1 - H * ((c - a) - (0.0 - 1.0)) + (slip_v if moved else 0.0), c, a

    def _coarse(settings, after=None):
        v, c, a = _where()
        return _result(1, v, sideband=0.028), c, a, 3.0, {}

    def _strict(settings, traces=None, after=None):
        raise ValueError("scan still too wide for the strict detector")

    session._set_sweep_geometry = _write  # type: ignore[method-assign]
    session._restore_sweep_geometry = lambda c, a: True  # type: ignore[method-assign]
    session._coarse_auto_lock_target = _coarse  # type: ignore[method-assign]
    session._capture_auto_lock_target = _strict  # type: ignore[method-assign]
    return session


def _walk(session):
    return session._trajectory_refine_auto_lock(
        AutoLockScanSettings.from_mapping(session.auto_lock_scan_settings),
        AcceptanceSettings.from_mapping(session.lock_acceptance_settings),
        0.0, 1.0,
        initial_target=_result(1, 0.1, sideband=0.028),
        initial_center_v=0.0, initial_amplitude_v=1.0,
        initial_resolution=1.5, initial_detector="coarse", trace_length=2048,
    )


def test_the_walk_refuses_a_target_one_sideband_off_the_prediction():
    session = _walk_session(slip_v=0.028)
    with pytest.raises(session_module.TrajectoryRefinementAborted) as excinfo:
        _walk(session)
    assert excinfo.value.failure_kind == "identity"
    assert "hysteresis window" in excinfo.value.refinement["failure"]
    # The refused stage is on the record, with what was measured.
    record = excinfo.value.refinement["stages"][-1]["hysteresis"]
    assert record["residual_v"] == pytest.approx(0.028)


def test_the_walk_records_measured_against_predicted_on_every_stage():
    session = _walk_session(slip_v=0.0)
    with pytest.raises(session_module.TrajectoryRefinementAborted) as excinfo:
        _walk(session)  # never reaches the strict detector: budget runs out
    stages = [
        s for s in excinfo.value.refinement["stages"] if s["kind"] == "narrow"
    ]
    assert stages, "the walk should have narrowed"
    for stage in stages:
        record = stage["hysteresis"]
        assert abs(record["residual_v"]) < 1e-9  # simulated device follows the model
        assert record["predicted_shift_v"] == pytest.approx(
            record["measured_shift_v"]
        )
        assert record["tolerance_v"] >= 0.005
        assert "shift_per_fraction_v" not in stage


# ------------------------------------------------------------------ settings


def test_defaults_match_the_measured_model():
    settings = AutoLockScanSettings()
    assert settings.hysteresis_per_volt_lower == pytest.approx(0.085)
    assert settings.hysteresis_tolerance_per_volt == pytest.approx(0.015)
    assert settings.hysteresis_floor_v == pytest.approx(0.005)


@pytest.mark.parametrize(
    "field, bad",
    [
        ("hysteresis_per_volt_lower", -0.001),
        ("hysteresis_per_volt_lower", 0.501),
        ("hysteresis_per_volt_lower", float("nan")),
        ("hysteresis_tolerance_per_volt", -0.001),
        ("hysteresis_floor_v", -0.001),
        ("hysteresis_floor_v", float("inf")),
    ],
)
def test_the_engine_refuses_out_of_range_hysteresis_settings(field, bad):
    with pytest.raises(ValueError, match=field):
        AutoLockScanSettings.from_mapping({field: bad})


@pytest.mark.parametrize("h", [0.0, 0.5])
def test_the_bounds_are_inclusive(h):
    assert AutoLockScanSettings.from_mapping(
        {"hysteresis_per_volt_lower": h}
    ).hysteresis_per_volt_lower == h
    assert schemas.AutoLockScanSettings(hysteresis_per_volt_lower=h)


@pytest.mark.parametrize("h", [-0.001, 0.501])
def test_the_schema_refuses_out_of_range_h(h):
    with pytest.raises(ValidationError):
        schemas.AutoLockScanSettings(hysteresis_per_volt_lower=h)


def _session_with_stored(stored: dict[str, Any] | None) -> DeviceSession:
    parameters = {} if stored is None else {"auto_lock_scan_settings": stored}
    device = SimpleNamespace(
        key="dev-store", name="dev-store", host="127.0.0.1", port=18867,
        parameters=parameters,
    )
    return DeviceSession(device, _Manager())


def test_stored_hysteresis_settings_load_with_the_device():
    stored = schemas.AutoLockScanSettings(
        hysteresis_per_volt_lower=0.083, hysteresis_tolerance_per_volt=0.02,
        hysteresis_floor_v=0.003,
    ).model_dump()
    session = _session_with_stored(stored)
    assert session.auto_lock_scan_settings["hysteresis_per_volt_lower"] == 0.083
    assert session.auto_lock_scan_settings["hysteresis_tolerance_per_volt"] == 0.02
    assert session.auto_lock_scan_settings["hysteresis_floor_v"] == 0.003


def test_a_block_stored_before_the_fields_existed_gets_the_defaults():
    stored = schemas.AutoLockScanSettings().model_dump()
    for name in (
        "hysteresis_per_volt_lower", "hysteresis_tolerance_per_volt",
        "hysteresis_floor_v",
    ):
        del stored[name]
    session = _session_with_stored(stored)
    assert session.auto_lock_scan_settings["hysteresis_per_volt_lower"] == 0.085


def test_an_invalid_stored_h_falls_back_to_defaults_rather_than_crashing():
    stored = schemas.AutoLockScanSettings().model_dump()
    stored["hysteresis_per_volt_lower"] = 0.9  # past the sanity bound
    session = _session_with_stored(stored)
    assert session.auto_lock_scan_settings["hysteresis_per_volt_lower"] == 0.085


def test_updating_settings_round_trips_the_hysteresis_fields():
    session = _session_with_stored(None)
    updated = session.update_auto_lock_scan_settings(
        schemas.AutoLockScanSettings(hysteresis_per_volt_lower=0.09).model_dump()
    )
    assert updated["hysteresis_per_volt_lower"] == 0.09
    assert session.get_auto_lock_scan_settings()["hysteresis_per_volt_lower"] == 0.09
