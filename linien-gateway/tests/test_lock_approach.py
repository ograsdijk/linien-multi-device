import pytest

from app.lock_approach import (
    MAX_RAMP_STEPS,
    SWEEP_MAX,
    SWEEP_MIN,
    ApproachSettings,
    acceptance_window_v,
    capture_tolerance_v,
    classify_hysteresis,
    plan_approach,
    rejection_bound_v,
)


def _settings(**overrides) -> ApproachSettings:
    base = dict(
        enabled=True,
        max_direct_jump_v=0.05,
        approach_offset_v=0.05,
        ramp_step_v=0.01,
        ramp_step_delay_ms=20,
        settle_ms=300,
        approach_from_below=True,
        max_approach_iterations=2,
        capture_fraction=0.5,
        max_correction_span=4.0,
    )
    base.update(overrides)
    return ApproachSettings(**base)


def _voltages(plan) -> list[float]:
    return [step.voltage for step in plan.steps]


def test_disabled_settings_plan_a_single_direct_set():
    plan = plan_approach(0.0, 0.9, _settings(enabled=False))
    assert plan.direct is True
    assert _voltages(plan) == [0.9]
    assert plan.settle_s == 0.0


def test_a_small_jump_goes_direct_even_when_enabled():
    plan = plan_approach(0.30, 0.34, _settings())
    assert plan.direct is True
    assert _voltages(plan) == [0.34]


def test_a_jump_exactly_at_the_threshold_still_goes_direct():
    plan = plan_approach(0.0, 0.05, _settings(max_direct_jump_v=0.05))
    assert plan.direct is True


def test_a_large_jump_overshoots_below_then_ramps_up():
    plan = plan_approach(0.0, 0.5, _settings())
    assert plan.direct is False
    assert plan.from_below is True
    voltages = _voltages(plan)
    # Staging point sits one offset below the target...
    assert voltages[0] == pytest.approx(0.45)
    # ...and every subsequent set-point moves upward onto it.
    assert all(b > a for a, b in zip(voltages, voltages[1:]))
    assert voltages[-1] == pytest.approx(0.5)


def test_approaching_from_above_overshoots_the_other_way():
    plan = plan_approach(0.0, 0.5, _settings(approach_from_below=False))
    assert plan.from_below is False
    voltages = _voltages(plan)
    assert voltages[0] == pytest.approx(0.55)
    assert all(b < a for a, b in zip(voltages, voltages[1:]))
    assert voltages[-1] == pytest.approx(0.5)


def test_the_from_below_argument_overrides_the_setting():
    plan = plan_approach(0.0, 0.5, _settings(approach_from_below=True), from_below=False)
    assert plan.from_below is False
    assert _voltages(plan)[0] == pytest.approx(0.55)


def test_the_approach_direction_is_independent_of_which_way_the_jump_goes():
    # Target below the current center, but the final approach is still upward.
    plan = plan_approach(0.8, 0.2, _settings())
    assert plan.from_below is True
    voltages = _voltages(plan)
    assert voltages[0] == pytest.approx(0.15)
    assert voltages[-1] == pytest.approx(0.2)


def test_ramp_steps_are_uniform_and_no_larger_than_ramp_step_v():
    plan = plan_approach(0.0, 0.5, _settings(approach_offset_v=0.05, ramp_step_v=0.01))
    voltages = _voltages(plan)
    deltas = [b - a for a, b in zip(voltages, voltages[1:])]
    assert deltas
    assert max(deltas) <= 0.01 + 1e-9
    assert max(deltas) - min(deltas) < 1e-9


def test_a_ramp_step_larger_than_the_offset_makes_one_move():
    plan = plan_approach(0.0, 0.5, _settings(approach_offset_v=0.02, ramp_step_v=0.5))
    assert _voltages(plan) == [pytest.approx(0.48), pytest.approx(0.5)]


def test_delays_come_from_the_settings_in_seconds():
    plan = plan_approach(0.0, 0.5, _settings(ramp_step_delay_ms=25, settle_ms=300))
    assert all(step.delay_s == pytest.approx(0.025) for step in plan.steps)
    assert plan.settle_s == pytest.approx(0.3)


def test_set_points_never_leave_the_sweep_range():
    plan = plan_approach(-0.9, SWEEP_MAX, _settings())
    assert all(SWEEP_MIN <= v <= SWEEP_MAX for v in _voltages(plan))
    assert _voltages(plan)[-1] == pytest.approx(SWEEP_MAX)


def test_a_target_pinned_against_the_rail_flips_to_the_other_direction():
    # Overshooting below -1.0 is impossible, so approach from above instead of
    # silently issuing a direct set that skips the anti-backlash move.
    plan = plan_approach(0.5, SWEEP_MIN, _settings(approach_from_below=True))
    assert plan.direct is False
    assert plan.from_below is False
    assert _voltages(plan)[0] == pytest.approx(-0.95)
    assert _voltages(plan)[-1] == pytest.approx(SWEEP_MIN)


def test_a_zero_offset_degrades_to_a_direct_set_but_still_settles():
    plan = plan_approach(0.0, 0.5, _settings(approach_offset_v=0.0, settle_ms=250))
    assert plan.direct is True
    assert _voltages(plan) == [pytest.approx(0.5)]
    assert plan.settle_s == pytest.approx(0.25)


def test_a_tiny_ramp_step_is_coarsened_rather_than_planning_endless_moves():
    plan = plan_approach(0.0, 0.5, _settings(approach_offset_v=1.0, ramp_step_v=1e-6))
    assert len(plan.steps) <= MAX_RAMP_STEPS + 1
    assert _voltages(plan)[-1] == pytest.approx(0.5)


def test_the_target_is_clamped_before_anything_else():
    plan = plan_approach(0.0, 5.0, _settings())
    assert plan.target_voltage == pytest.approx(SWEEP_MAX)


def test_from_mapping_fills_gaps_with_defaults_and_ignores_extras():
    settings = ApproachSettings.from_mapping({"enabled": True, "settle_ms": 50, "junk": 1})
    assert settings.enabled is True
    assert settings.settle_ms == 50
    assert settings.max_direct_jump_v == ApproachSettings().max_direct_jump_v


def test_force_anti_backlash_ignores_the_direct_jump_threshold():
    # The retry path: the center already sits on the target, so without the
    # override the plan would collapse to a no-op direct set.
    plan = plan_approach(0.5, 0.5, _settings(), from_below=False, force_anti_backlash=True)
    assert plan.direct is False
    assert plan.from_below is False
    assert _voltages(plan)[0] == pytest.approx(0.55)
    assert _voltages(plan)[-1] == pytest.approx(0.5)


def test_force_anti_backlash_still_respects_a_disabled_setting():
    plan = plan_approach(0.5, 0.5, _settings(enabled=False), force_anti_backlash=True)
    assert plan.direct is True


def test_capture_tolerance_scales_with_the_calibrated_feature_width():
    assert capture_tolerance_v(_settings(capture_fraction=0.5), 0.08) == pytest.approx(0.04)
    assert capture_tolerance_v(_settings(capture_fraction=0.25), 0.08) == pytest.approx(0.02)


def test_capture_tolerance_is_never_negative():
    assert capture_tolerance_v(_settings(capture_fraction=0.5), -0.08) == pytest.approx(0.04)


def test_rejection_bound_falls_back_to_the_feature_width_without_a_sideband():
    assert rejection_bound_v(_settings(max_correction_span=4.0), 0.08) == pytest.approx(0.32)


def test_a_known_sideband_offset_tightens_the_rejection_bound():
    # Sidebands 0.5 V out: anything past 0.2 V is nearer the sideband than the
    # carrier, which is tighter than 4 x 0.08 = 0.32 V.
    bound = rejection_bound_v(_settings(max_correction_span=4.0), 0.08, sideband_offset_v=0.5)
    assert bound == pytest.approx(0.2)


def test_a_wide_sideband_offset_does_not_loosen_the_bound():
    bound = rejection_bound_v(_settings(max_correction_span=4.0), 0.08, sideband_offset_v=5.0)
    assert bound == pytest.approx(0.32)


def test_a_tight_signal_tightens_the_window_rather_than_loosening_the_guard():
    """Closely spaced features must still leave room to correct.

    Before, the bound was clamped up to meet the window, so the two met and
    every offset that failed acceptance was immediately called a neighbouring
    crossing -- the correction loop never ran and the message was wrong.
    """
    settings = _settings(capture_fraction=0.5, max_correction_span=4.0)
    window = acceptance_window_v(settings, 0.02, sideband_offset_v=0.015)

    assert window.bound_v == pytest.approx(0.006)  # 0.4 x the sideband spacing
    assert window.tolerance_v == pytest.approx(0.003)  # tightened to half the bound
    assert window.tolerance_v < window.bound_v
    assert window.tightened is True


def test_a_roomy_signal_keeps_the_configured_window():
    settings = _settings(capture_fraction=0.5, max_correction_span=4.0)
    window = acceptance_window_v(settings, 0.02, sideband_offset_v=0.5)

    assert window.tolerance_v == pytest.approx(0.01)  # 0.5 x the feature half-width
    assert window.bound_v == pytest.approx(0.08)
    assert window.tightened is False


def test_a_generous_capture_fraction_cannot_swallow_the_correction_window():
    settings = _settings(capture_fraction=4.0, max_correction_span=1.0)
    window = acceptance_window_v(settings, 0.02)

    assert window.tolerance_v == pytest.approx(0.5 * window.bound_v)
    assert window.tolerance_v < window.bound_v


def test_a_zero_sideband_offset_is_ignored_rather_than_collapsing_the_bound():
    bound = rejection_bound_v(_settings(max_correction_span=4.0), 0.08, sideband_offset_v=0.0)
    assert bound == pytest.approx(0.32)


def test_engine_and_schema_settings_stay_in_parity():
    """The engine dataclass and the Pydantic boundary model must declare the same
    field names and defaults, so the two definitions cannot drift."""
    import dataclasses

    from app.schemas import LockApproachSettings as SchemaLockApproachSettings

    engine_fields = {f.name: f.default for f in dataclasses.fields(ApproachSettings)}
    schema_fields = {
        name: info.default
        for name, info in SchemaLockApproachSettings.model_fields.items()
    }
    assert engine_fields.keys() == schema_fields.keys()
    assert engine_fields == schema_fields


def _sample(from_below: bool, settle_ms: int, offset_v: float | None):
    return {"from_below": from_below, "settle_ms": settle_ms, "offset_v": offset_v}


def test_an_offset_that_flips_sign_with_direction_is_backlash():
    verdict, detail = classify_hysteresis(
        [_sample(True, 300, 0.03), _sample(False, 300, -0.031)], tolerance_v=0.01
    )
    assert verdict == "backlash"
    assert "approach_offset_v" in detail


def test_a_direction_independent_offset_that_settles_out_is_creep():
    verdict, detail = classify_hysteresis(
        [
            _sample(True, 50, 0.04),
            _sample(True, 1000, 0.01),
            _sample(False, 50, 0.041),
            _sample(False, 1000, 0.011),
        ],
        tolerance_v=0.005,
    )
    assert verdict == "creep"
    assert "settle_ms" in detail


def test_a_direction_independent_offset_that_will_not_settle_is_not_called_creep():
    verdict, _detail = classify_hysteresis(
        [
            _sample(True, 50, 0.04),
            _sample(True, 1000, 0.039),
            _sample(False, 50, 0.041),
            _sample(False, 1000, 0.040),
        ],
        tolerance_v=0.005,
    )
    assert verdict == "drift_or_creep"


def test_small_offsets_both_ways_mean_no_guarded_move_is_needed():
    verdict, detail = classify_hysteresis(
        [_sample(True, 300, 0.002), _sample(False, 300, -0.001)], tolerance_v=0.01
    )
    assert verdict == "negligible"
    assert "no guarded move needed" in detail


def test_one_direction_alone_cannot_distinguish_the_two_causes():
    verdict, _detail = classify_hysteresis(
        [_sample(True, 50, 0.03), _sample(True, 1000, 0.01)], tolerance_v=0.005
    )
    assert verdict == "inconclusive"


def test_failed_measurements_are_ignored_rather_than_read_as_zero():
    verdict, _detail = classify_hysteresis(
        [_sample(True, 300, None), _sample(False, 300, None)], tolerance_v=0.01
    )
    assert verdict == "inconclusive"


def test_a_zero_correction_span_disables_the_guard_instead_of_rejecting_everything():
    """A bound of 0 would make every nonzero offset a "different feature", so a
    single innocuous-looking setting would make the device unlockable."""
    window = acceptance_window_v(_settings(max_correction_span=0.0), 0.02)

    assert window.bound_v is None
    assert window.tolerance_v == pytest.approx(0.01)


def test_an_uncalibrated_feature_width_yields_no_window_at_all():
    # half_range_sweep_v = 0 means the device was never calibrated; callers must
    # refuse rather than silently reject every landing.
    window = acceptance_window_v(_settings(), 0.0)

    assert window.tolerance_v == 0.0
    assert window.bound_v is None
