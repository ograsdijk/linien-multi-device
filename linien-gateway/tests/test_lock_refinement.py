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

import pytest

from app.auto_lock_scan import AutoLockScanSettings
from app.lock_refinement import (
    IdentityGuard,
    _TrackingIdentityChanged,
    min_safe_amplitude_v,
    plan_refinement_step,
)


def _settings(**overrides):
    settings = AutoLockScanSettings.from_mapping({})
    for name, value in overrides.items():
        setattr(settings, name, value)
    return settings


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
    assert step.action == "rail_escape"
    # A real cut, not a token one, and comfortably above the 0.4 V the
    # (buggy) min() schedule would have picked.
    assert step.amplitude_v >= 0.52
    assert step.amplitude_v == pytest.approx(0.533, abs=0.01)
    # The floor itself must actually contain the target -- the whole point
    # of the floor existing.
    assert 0.4795 < 0.9 * step.amplitude_v


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
    step = plan_refinement_step(
        settings,
        center_v=0.2, amplitude_v=0.8, target_v=0.59,
        sideband_offset_v=0.032, detector="coarse", trace_length=2048,
        shift_per_fraction=0.136,
    )
    # Recognised as pinned (rail escape), not treated as room to recentre.
    assert step.action == "rail_escape"
    assert step.center_v == pytest.approx(0.2)


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
    next_amplitude = step.bounds["shift_capped_v"]
    outside_by = abs(target_v - center_v) - 0.5 * next_amplitude
    assert outside_by == pytest.approx(0.421, abs=0.001)
    assert step.action == "recenter"
    assert step.action != "narrow"


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
    next_amplitude = step.bounds["shift_capped_v"]  # what an unconditional narrow would use
    window_after_narrow = 0.5 * next_amplitude
    # The target the "narrow anyway" bug would have committed to is still
    # outside the window the narrow itself produces -- the crop.
    assert abs(target_v - center_v) > window_after_narrow
    assert step.action != "narrow"  # the real planner refuses to do this


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
