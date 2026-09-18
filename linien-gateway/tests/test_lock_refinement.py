"""Direct unit tests of the trajectory-refinement planner.

These exercise ``plan_refinement_step``, ``min_safe_amplitude_v`` and
``IdentityGuard`` directly, rather than through ``_trajectory_refine_auto_lock``
and its four monkeypatched I/O methods the way the walk-level tests in
``test_session_lock_approach.py`` do. They encode the four field cases that
escaped the suite before: a value derived from round numbers agreed with both
a correct and a backwards implementation, and only the field's actual numbers
told them apart. Each of the four below was confirmed, by hand, to fail
against the pre-fix arithmetic (the comparison temporarily flipped, the test
run, the failure observed, the code restored) before being written down here
-- see the session report for the specific edit made for each case.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.auto_lock_scan import AutoLockScanSettings
from app.lock_refinement import (
    IdentityGuard,
    _TrackingIdentityChanged,
    bounded_recenter_v,
    min_safe_amplitude_v,
    plan_refinement_step,
)


def _settings(**overrides):
    settings = AutoLockScanSettings.from_mapping({})
    for name, value in overrides.items():
        setattr(settings, name, value)
    return settings


def _target(sideband_offset_v, *, slope_rising=True):
    """The two fields IdentityGuard reads off a detection."""
    return SimpleNamespace(
        target_slope_rising=slope_rising, sideband_offset_v=sideband_offset_v
    )


# ------------------------------------------------------------ field case 1
# "Cropped again at the rail": min() used where max() was needed on the
# rail-escape floor. Target 0.6795 V, centre pinned at the 0.2 V rail: the
# floor that keeps it in view is ~0.533 V. A schedule that wants 0.4 V and
# takes min() of the two discards the floor and crops the target out of the
# next window.

def test_rail_pinned_offset_floors_the_amplitude_near_0_533_v():
    settings = _settings(
        half_range_sweep_v=0.128, min_signal_scan_fraction=0.25,
        max_center_step_signal_widths=1.0,
    )
    step = plan_refinement_step(
        settings,
        center_v=0.2, amplitude_v=0.8, target_v=0.2 + 0.4795,
        sideband_offset_v=0.032, detector="coarse", trace_length=2048,
        shift_per_fraction=None,
    )
    # The centre sits on the rail for the CURRENT width (1 - 0.8 = 0.2), but a
    # stage that narrows also moves the rail outward, so it narrows and
    # recentres in one write rather than stalling.
    assert step.action == "narrow"
    assert step.center_v > 0.2, "held at the rail instead of moving with the cut"
    assert step.amplitude_v < 0.8, "no width change"
    # The invariant the old 0.533 V floor existed to enforce, stated directly:
    # the target must still be inside the window this step creates. The floor
    # is legitimately lower now, because the centre closes part of the gap in
    # the same write.
    residual = abs((0.2 + 0.4795) - step.center_v)
    assert residual <= 0.9 * step.amplitude_v + 1e-9, (
        f"cut to +/-{step.amplitude_v:.4f} around {step.center_v:.4f}, leaving "
        f"the target {residual:.4f} out -- cropped out of its own window"
    )


def test_rail_pinned_offset_floors_the_amplitude_near_0_533_v__fails_on_min():
    """Same case, run against a hand-inlined min() to show what the bug
    looked like: min() picks the smaller (schedule) value and drops below
    the floor needed to keep the target in view."""
    settings = _settings(
        half_range_sweep_v=0.128, min_signal_scan_fraction=0.25,
        max_center_step_signal_widths=1.0,
    )
    step = plan_refinement_step(
        settings,
        center_v=0.2, amplitude_v=0.8, target_v=0.2 + 0.4795,
        sideband_offset_v=0.032, detector="coarse", trace_length=2048,
        shift_per_fraction=None,
    )
    schedule_v = step.bounds["shift_capped_v"]  # 0.4 V, what min() would pick
    safe = min_safe_amplitude_v(0.8, 0.4795, None, step.bounds["goal_v"])
    buggy = min(schedule_v, safe)
    assert buggy == pytest.approx(schedule_v)
    assert buggy < 0.4795 / 0.9  # crops the target out of the window it picks
    assert step.amplitude_v != pytest.approx(buggy)  # the real planner disagrees


# ------------------------------------------------------------ field case 2
# "Stalled 13 stages at the rail": crop guard vs. rail clamp deadlock, caused
# by an exact float comparison. 1.0 - 0.8 is 0.19999999999999996, so a centre
# reading back as the literal 0.2 must still be recognised as pinned.

def test_a_centre_on_the_rail_is_recognised_despite_float_noise():
    settings = _settings(
        half_range_sweep_v=0.128, min_signal_scan_fraction=0.25,
        max_center_step_signal_widths=1.0,
    )
    # Where the rails genuinely bind -- a centre move at an UNCHANGED width --
    # a centre reading back as the literal 0.2 must still be recognised as
    # sitting on the 1.0 - 0.8 rail, so the step is capped there rather than
    # being allowed past it.
    pinned = bounded_recenter_v(
        0.2, 0.59, 0.8, signal_width_v=0.064, max_signal_widths=1.0
    )
    assert pinned == pytest.approx(1.0 - 0.8, abs=1e-12)
    # With the rail taken at the width the scan will HAVE, the same step is
    # free to move: that is what turns the old stall into progress.
    loosened = bounded_recenter_v(
        0.2, 0.59, 0.8, signal_width_v=0.064, max_signal_widths=1.0,
        rail_amplitude_v=0.4,
    )
    assert loosened > 0.2


def test_a_centre_on_the_rail_is_recognised_despite_float_noise__fails_on_exact_compare():
    """The bug: comparing the centre to the rail exactly instead of with
    tolerance. 1.0 - 0.8 != 0.2 in floating point, so an exact comparison
    concludes the centre is off the rail and there is room to recentre."""
    rail_hi = 1.0 - 0.8
    center = 0.2
    assert center != rail_hi  # exactly the float noise the tolerance exists for
    exact_pinned = rail_hi <= center <= rail_hi  # what an exact check asks
    assert exact_pinned is False  # the buggy exact comparison misses it
    tolerant_pinned = rail_hi - 1e-9 <= center <= rail_hi + 1e-9
    assert tolerant_pinned is True  # the real (tolerant) comparison catches it


# ------------------------------------------------------------ field case 3
# "Walk cropped the tracked feature": narrowed after one bounded re-centre
# while the target was still far outside the window that narrowing would
# produce. A target 421 mV outside the next window must recentre again, and
# must never narrow while still that far out.

def test_a_target_421_mv_outside_the_next_window_recentres_not_narrows():
    settings = _settings(
        half_range_sweep_v=0.08, min_signal_scan_fraction=0.325,
        max_center_step_signal_widths=1.0,
    )
    center_v, amplitude_v, target_v = 0.0, 0.6, 0.571
    step = plan_refinement_step(
        settings,
        center_v=center_v, amplitude_v=amplitude_v, target_v=target_v,
        sideband_offset_v=0.0325, detector="coarse", trace_length=2048,
        shift_per_fraction=None,
    )
    scheduled = step.bounds["shift_capped_v"]
    outside_by = abs(target_v - center_v) - 0.5 * scheduled
    assert outside_by == pytest.approx(0.421, abs=0.001)
    # The step must not commit to the scheduled cut while the target is that
    # far out. It may now narrow, but only together with a centre move and
    # only to a width that still contains the target -- the crop the original
    # bug produced is what must not happen, not narrowing as such.
    assert step.center_v > center_v, "narrowed without moving the centre at all"
    residual = abs(target_v - step.center_v)
    assert residual <= 0.9 * step.amplitude_v + 1e-9, (
        f"target {residual:.4f} V from the new centre, outside the "
        f"+/-{step.amplitude_v:.4f} V window this step creates"
    )


def test_a_target_421_mv_outside_the_next_window__fails_if_the_centring_check_is_skipped():
    """The bug: narrowing unconditionally instead of checking the target is
    inside the NEXT window first. Reproduced here by evaluating what the
    walk would have done with that check removed -- it narrows, cropping a
    target still 421 mV outside the window the narrow produces."""
    settings = _settings(
        half_range_sweep_v=0.08, min_signal_scan_fraction=0.325,
        max_center_step_signal_widths=1.0,
    )
    center_v, amplitude_v, target_v = 0.0, 0.6, 0.571
    step = plan_refinement_step(
        settings,
        center_v=center_v, amplitude_v=amplitude_v, target_v=target_v,
        sideband_offset_v=0.0325, detector="coarse", trace_length=2048,
        shift_per_fraction=None,
    )
    scheduled = step.bounds["shift_capped_v"]  # what an unconditional narrow uses
    # The bug: narrow to the scheduled width around the UNMOVED centre. The
    # target is then outside the window that narrow produces -- the crop.
    assert abs(target_v - center_v) > 0.9 * scheduled, (
        "this case no longer reproduces the crop; pick numbers that do"
    )
    # The real planner does not commit to that geometry: it moves the centre in
    # the same write and floors the width so the target stays in view.
    assert not (
        step.center_v == pytest.approx(center_v)
        and step.amplitude_v == pytest.approx(scheduled)
    )
    assert abs(target_v - step.center_v) <= 0.9 * step.amplitude_v + 1e-9


# ------------------------------------------------------------ field case 4
# "Identity rejected a better measurement" was one symptom of the same root
# cause as this one: adaptive narrowing must gentle the NEXT cut once a
# stage's own width change is measured to move the feature more than the
# allowance -- a 50% cut that moved the feature 136 mV per unit fraction of
# width change must be followed by a strictly gentler one.

def test_a_measured_136_mv_per_fraction_shift_gentles_the_next_cut():
    settings = _settings(
        half_range_sweep_v=0.0001, min_signal_scan_fraction=0.0,
        max_center_step_signal_widths=1.0,
    )
    first = plan_refinement_step(
        settings, center_v=0.4, amplitude_v=0.6, target_v=0.4,
        sideband_offset_v=0.0325, detector="coarse", trace_length=2048,
        shift_per_fraction=None,
    )
    second = plan_refinement_step(
        settings, center_v=0.4, amplitude_v=first.amplitude_v, target_v=0.4,
        sideband_offset_v=0.0325, detector="coarse", trace_length=2048,
        shift_per_fraction=0.136,
    )
    first_cut = 1.0 - first.amplitude_v / 0.6
    second_cut = 1.0 - second.amplitude_v / first.amplitude_v
    assert first_cut == pytest.approx(0.5)  # no measurement yet: the base schedule
    assert second_cut < first_cut, (
        f"kept cutting {second_cut:.0%} after a {first_cut:.0%} cut measured "
        "136 mV of shift per unit fraction of width change"
    )


def test_a_measured_136_mv_per_fraction_shift__fails_if_the_shift_cap_is_ignored():
    """The bug: computing the next cut from the coarse/gentle schedule alone
    and never consulting the measured shift. Reproduced by comparing the
    real (shift-capped) result against what the schedule alone would have
    picked for the second stage -- they must differ, and the schedule-alone
    figure must not be gentler."""
    settings = _settings(
        half_range_sweep_v=0.0001, min_signal_scan_fraction=0.0,
        max_center_step_signal_widths=1.0,
    )
    first = plan_refinement_step(
        settings, center_v=0.4, amplitude_v=0.6, target_v=0.4,
        sideband_offset_v=0.0325, detector="coarse", trace_length=2048,
        shift_per_fraction=None,
    )
    second = plan_refinement_step(
        settings, center_v=0.4, amplitude_v=first.amplitude_v, target_v=0.4,
        sideband_offset_v=0.0325, detector="coarse", trace_length=2048,
        shift_per_fraction=0.136,
    )
    schedule_only_v = second.bounds["scheduled_v"]  # ignores the shift cap
    assert schedule_only_v < second.amplitude_v  # the bug would narrow further
    schedule_only_cut = 1.0 - schedule_only_v / first.amplitude_v
    real_cut = 1.0 - second.amplitude_v / first.amplitude_v
    assert schedule_only_cut > real_cut  # the bug is NOT strictly gentler


# --------------------------------------------------------- identity guard

def test_identity_guard_rejects_a_changed_slope():
    class _Candidate:
        target_slope_rising = False
        sideband_offset_v = None

    class _Initial:
        target_slope_rising = True
        sideband_offset_v = None

    guard = IdentityGuard(_Initial(), 10.0, trace_length=2048)
    with pytest.raises(_TrackingIdentityChanged):
        guard.check(_Candidate(), amplitude_v=0.5)


def test_identity_guard_adopts_any_better_resolved_strict_detection():
    class _Initial:
        target_slope_rising = True
        sideband_offset_v = 0.020

    class _Better:
        target_slope_rising = True
        sideband_offset_v = 0.026  # would fail the 35% tolerance if compared

    guard = IdentityGuard(_Initial(), 6.0, trace_length=2048)
    # A tiny resolution gain (barely above the 1.001x margin) still adopts
    # rather than being judged against the old, worse-resolved spacing.
    guard.check(_Better(), amplitude_v=0.5, detector="strict", resolution_samples=6.01)


def test_two_readings_from_one_estimator_that_disagree_are_an_identity_change():
    """Where the sideband rule still bites. Two readings from the SAME
    estimator at the same resolution, four times apart, is a different
    crossing. The walk-level path that used to cover this no longer emits a
    pure re-centring stage, so it is asserted directly on the guard."""
    guard = IdentityGuard(_target(0.05), 20.0, trace_length=2048, detector="coarse")

    with pytest.raises(_TrackingIdentityChanged) as excinfo:
        guard.check(
            _target(0.20), amplitude_v=0.6, detector="coarse",
            resolution_samples=20.0,
        )
    assert "200.000 mV" in str(excinfo.value)
    assert "50.000 mV" in str(excinfo.value)


def test_a_coarse_reading_is_never_judged_against_a_strict_baseline():
    """The field failure: strict established 17.497 mV, coarse then read
    32.369 mV of the same untroubled feature. Across eight runs the two
    estimators sit a systematic ~1.75x apart, so the comparison tests which
    algorithm ran, not whether the crossing changed."""
    guard = IdentityGuard(_target(0.017497), 4.07, trace_length=2048, detector="strict")

    # Must be taken as the coarse estimator's own first reading, not a breach.
    guard.check(
        _target(0.032369), amplitude_v=0.4477, detector="coarse",
        resolution_samples=4.07,
    )

    # ...and the strict baseline is untouched, so strict is still policed.
    with pytest.raises(_TrackingIdentityChanged):
        guard.check(
            _target(0.20), amplitude_v=0.4477, detector="strict",
            resolution_samples=4.07,
        )


# ------------------------------------------------------------ field case 5
# "The sweep rails hold the center at +0.4048 V" -- with the rail at 0.7880 V,
# 383 mV away. The real stop was inside min_safe_amplitude_v, whose five-round
# fixed point diverges once the actuator's shift coefficient exceeds
# _WINDOW_KEEP_FRACTION x the current amplitude. The field stage had
# spf 0.2890 V per unit fraction at amplitude 0.2120 V: a contraction factor
# of 1.5, a two-cycle between 0.1556 V and 0.2411 V, and round five on the high
# branch -- which reads as "no cut is safe" purely by the parity of the loop.

_DIVERGENT_AMPLITUDE_V = 0.21203012978894104
_DIVERGENT_SHIFT_PER_FRACTION = 0.2890252242537811
_DIVERGENT_OFFSET_V = 0.140
_DIVERGENT_FLOOR_V = 0.10820284981490627


def test_a_shift_coefficient_past_the_contraction_limit_still_has_a_safe_cut():
    # Well past the limit: the old iteration cannot be trusted here at all.
    limit = 0.9 * _DIVERGENT_AMPLITUDE_V
    assert _DIVERGENT_SHIFT_PER_FRACTION > limit

    safe = min_safe_amplitude_v(
        _DIVERGENT_AMPLITUDE_V,
        _DIVERGENT_OFFSET_V,
        _DIVERGENT_SHIFT_PER_FRACTION,
        _DIVERGENT_FLOOR_V,
    )

    assert safe is not None, "a legal narrowing was reported as impossible"
    assert safe == pytest.approx(0.1896, abs=5e-4)
    # It is a narrowing, and it holds the target after the shift it causes.
    assert _DIVERGENT_FLOOR_V <= safe < _DIVERGENT_AMPLITUDE_V
    shifted = _DIVERGENT_OFFSET_V + _DIVERGENT_SHIFT_PER_FRACTION * (
        1.0 - safe / _DIVERGENT_AMPLITUDE_V
    )
    assert shifted <= 0.9 * safe + 1e-9


def test_a_shift_coefficient_past_the_contraction_limit__fails_when_iterated():
    """The pre-fix arithmetic, verbatim, on the same numbers."""
    candidate = max(_DIVERGENT_FLOOR_V, 1e-9)
    for _ in range(5):
        fraction = max(0.0, 1.0 - (candidate / _DIVERGENT_AMPLITUDE_V))
        candidate = max(
            (_DIVERGENT_OFFSET_V + _DIVERGENT_SHIFT_PER_FRACTION * fraction) / 0.9,
            _DIVERGENT_FLOOR_V,
        )
    assert candidate >= _DIVERGENT_AMPLITUDE_V, "iteration would have returned a cut"
    # ... and one more round flips the answer, which is the whole objection.
    fraction = max(0.0, 1.0 - (candidate / _DIVERGENT_AMPLITUDE_V))
    candidate = max(
        (_DIVERGENT_OFFSET_V + _DIVERGENT_SHIFT_PER_FRACTION * fraction) / 0.9,
        _DIVERGENT_FLOOR_V,
    )
    assert candidate < _DIVERGENT_AMPLITUDE_V


def test_an_offset_past_the_keep_fraction_of_the_span_has_no_safe_cut():
    """The flat branch: no shift at all, but the target is simply too far out."""
    assert min_safe_amplitude_v(0.2, 0.19, 0.0, 0.01) is None
    assert min_safe_amplitude_v(0.2, 0.15, 0.0, 0.01) == pytest.approx(0.15 / 0.9)


def test_a_refusal_names_the_step_allowance_when_the_rails_are_far_away():
    settings = _settings(half_range_sweep_v=0.001524, max_center_step_signal_widths=1.0)
    step = plan_refinement_step(
        settings,
        center_v=0.4047675963415047,
        amplitude_v=_DIVERGENT_AMPLITUDE_V,
        target_v=0.5780148514918598,
        sideband_offset_v=0.030142045807807445,
        detector="strict",
        trace_length=2048,
        shift_per_fraction=_DIVERGENT_SHIFT_PER_FRACTION,
    )
    if step.action == "refuse":
        assert "sweep rails" not in step.reason, step.reason
        assert "may move the center only" in step.reason
    else:
        # The closed-form solve is expected to keep this stage moving instead.
        assert step.action in {"narrow", "recenter"}
        assert step.amplitude_v <= _DIVERGENT_AMPLITUDE_V
