"""Pure planning for the auto-lock trajectory-refinement walk.

``_trajectory_refine_auto_lock`` in ``session.py`` narrows an over-wide scan
around a detected feature, one bounded stage at a time, so the lock can be
handed over safely. This module states the constraint order once and does
the arithmetic; ``session.py`` performs the I/O (writing scan geometry,
capturing a fresh trace, re-detecting) and drives its loop from what this
module decides: it plans the walk, it does not perform it.

Across eight rounds of field testing on a DFB whose apparent feature position
moves with scan geometry, the next amplitude came to depend on four
interacting constraints -- the detector sample floor, the signal-fraction
goal, the adaptive width-change (shift) allowance, and the rail-escape floor
-- combined by plain ``min()``/``max()`` calls. Scattered through a loop body
that also did RPyC I/O, a backwards ``min``/``max`` was invisible until it
cropped a tracked feature out of its own window in the field.
``plan_refinement_step`` states the order those constraints apply in exactly
once, and records every one of them in ``RefinementStep.bounds``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .auto_lock_scan import (
    AutoLockScanSettings,
    max_lockable_amplitude_v,
    scan_too_wide_to_lock,
)

# Samples per calibrated feature half-width that refinement aims for when it
# has to narrow a scan. NOT a detector requirement: find_auto_lock_target needs
# only two points either side of the crossing (see _half_range_to_points and the
# candidate loop in auto_lock_scan). This is a target for the narrowing walk to
# stop at, and nothing may use it to reject a detection the detector accepted.
_STRICT_DETECTOR_SAMPLES = 10.0
# Scan-width reduction per refinement stage for the last approach. Gradual on
# purpose: this actuator's apparent feature position moves with scan geometry
# (a 9% width change was measured to move it 5.4 mV), so a large step near the
# goal can drop the feature out of the window it was supposed to land in.
_REFINEMENT_NARROW_FACTOR = 0.75
# Reduction while still far from the goal. The window is many signal widths wide
# there, so a half-step is safe and each one saved is a stage of drift avoided:
# 0.75x alone needs ~18 stages to cross the full range, 0.5x first needs ~11.
_REFINEMENT_COARSE_NARROW_FACTOR = 0.5
# "Far from the goal" -- more than this multiple of the target amplitude.
_REFINEMENT_GENTLE_APPROACH = 4.0
# Stages before refinement gives up. Each costs several sweeps, and the walk has
# to be able to cross the full sweep range: from 1 V to a few mV takes ~11
# stages at the factors above, so the budget is that with headroom.
_MAX_REFINEMENT_STAGES = 16
# Gentlest narrowing the adaptive rule may fall back to. A stage that barely
# changes the width measures nothing and burns the budget, so a feature whose
# position is this sensitive to width is one the walk should give up on rather
# than creep after.
_REFINEMENT_MAX_NARROW_FACTOR = 0.9
# Intermediate factor used by the edge guard before the adaptive model
# uncertainty proves that preserving still more width is necessary.
_REFINEMENT_EDGE_NARROW_FACTOR = 0.85
# Width changes below this fraction cannot measure the shift they cause: the
# detector's own scatter swamps it and the ratio explodes.
_REFINEMENT_MIN_MEASURABLE_FRACTION = 0.05
# Share of the half-span the target may sit at and still count as "inside the
# window" when the rails force a narrowing that has not been centred first.
# Below 1 so the edge is not the acceptance criterion.
_WINDOW_KEEP_FRACTION = 0.9
# The edge guard leaves extra crop margin for the measured ~12.5% edge gain
# change in the simulated detector response.
_EDGE_WINDOW_KEEP_FRACTION = 0.86
# Below this, a commanded centre move is no move at all -- the rails have pinned
# it and stepping again would spin.
_CENTER_MOVE_EPSILON_V = 1e-6
# Slack on the sweep-rail comparison, comfortably above double-precision noise
# on 1.0 - amplitude and far below anything the hardware resolves.
_RAIL_EPSILON_V = 1e-9
# Below this, the narrowing SCHEDULE has computed no meaningful cut -- the walk
# is done, not stalled. Distinct from _CENTER_MOVE_EPSILON_V: this compares
# amplitudes, not sweep-centre voltages.
_NARROW_SCHEDULE_EPSILON_V = 1e-9

# Share of the (next) half-range the target may drift from centre before a
# stage recentres rather than narrows. Applied against the PROSPECTIVE next
# amplitude before narrowing, and against the CURRENT amplitude right after.
_INNER_WINDOW_FRACTION = 0.5
# The central 60% of the full scan spans +/- 0.6 of its half-range.
_EDGE_GUARD_FRACTION = 0.6
# Tolerance on the sideband spacing across a geometry change, as a fraction of
# the standing identity. Generous while the wide trace is under-resolved --
# the spacing itself is biased by resolution, not just noisy.
_SIDEBAND_IDENTITY_TOLERANCE_FRACTION = 0.35
# A later strict detection must clear this margin over the resolution the
# standing sideband identity was measured at before it REPLACES that identity
# rather than being judged against it. Kept barely above 1.0: a stage that
# improves resolution at all is the improvement narrowing exists to get, and a
# larger margin let an adaptively-gentled stage slip under it and reject the
# very improvement it produced.
_SIDEBAND_ADOPTION_MARGIN = 1.001


class _TrackingIdentityChanged(ValueError):
    """The tracked candidate is no longer the feature refinement started on."""


@dataclass(frozen=True)
class RefinementStep:
    """One decision for the refinement walk to act on.

    ``action`` is one of ``"narrow"``, ``"recenter"``, ``"rail_escape"``,
    ``"done"``, or ``"refuse"``. ``center_v``/``amplitude_v`` are the geometry
    the caller should command next (for ``"done"``/``"refuse"`` they echo the
    input, unchanged). ``bounds`` records every constraint considered, keyed
    by name (``goal_v``, ``scheduled_v``, ``shift_capped_v``, and -- only when
    the rails forced a decision -- ``crop_floor_v``/``rail_v``), so a stage
    record can show which one decided the outcome.
    """

    action: str
    center_v: float
    amplitude_v: float
    reason: str
    bounds: dict[str, float] = field(default_factory=dict)


def lower_endpoint_change_v(
    old_center_v: float,
    old_amplitude_v: float,
    new_center_v: float,
    new_amplitude_v: float,
) -> float:
    """``dL``: the change in the LOWER scan endpoint, ``(c - a)``, in sweep volts."""
    return (float(new_center_v) - abs(float(new_amplitude_v))) - (
        float(old_center_v) - abs(float(old_amplitude_v))
    )


def predicted_shift_v(
    old_center: float,
    old_amplitude: float,
    new_center: float,
    new_amplitude: float,
    h: float,
) -> float:
    """Signed apparent-position shift of a feature across a geometry change.

    ``-h * dL`` with ``dL`` the change in the LOWER scan endpoint (see
    ``AutoLockScanSettings.hysteresis_per_volt_lower``). The sign is kept: a
    rising lower endpoint (narrowing from below, or moving the centre up) moves
    the feature DOWN, and the identity check compares against a signed
    position, so an ``abs()`` here would accept the mirror-image slip.
    """
    # "+ 0.0" turns a -0.0 (h * 0) into 0.0 so it serialises as a plain zero.
    return 0.0 - float(h) * lower_endpoint_change_v(
        old_center, old_amplitude, new_center, new_amplitude
    )


def hysteresis_tolerance_v(
    delta_lower_v: float, tol_per_volt: float, floor_v: float
) -> float:
    """Half-width of the window around the predicted position that still counts
    as the same feature: ``tol * |dL| + floor``."""
    return float(tol_per_volt) * abs(float(delta_lower_v)) + float(floor_v)


def hysteresis_settings_block(settings: AutoLockScanSettings) -> dict[str, float]:
    """The device's effective hysteresis settings, in the read-only
    ``hysteresis_settings`` shape of an ``auto_lock_candidates`` response: what
    a caller needs to predict ``-h * dL`` itself for a geometry change made
    outside a staged run."""
    return {
        "h_per_volt": float(settings.hysteresis_per_volt_lower),
        "tolerance_per_volt": float(settings.hysteresis_tolerance_per_volt),
        "floor_v": float(settings.hysteresis_floor_v),
        "max_extra_tolerance_v": float(settings.hysteresis_max_extra_tolerance_v),
    }


def hysteresis_block(
    settings: AutoLockScanSettings,
    old_center_v: float,
    old_amplitude_v: float,
    new_center_v: float,
    new_amplitude_v: float,
) -> dict[str, Any]:
    """The model's prediction for one geometry change, in the staged API's
    ``hysteresis`` shape (see docs/staged_autolock in the gateway README).

    ``old == new`` (a ``begin`` or a ``done`` step: nothing moved) gives
    ``delta_lower_v == 0``, a zero shift and a tolerance of just the floor.
    """
    h = float(settings.hysteresis_per_volt_lower)
    delta_lower = lower_endpoint_change_v(
        old_center_v, old_amplitude_v, new_center_v, new_amplitude_v
    )
    return {
        "h_per_volt": h,
        "delta_lower_v": delta_lower,
        "predicted_shift_v": predicted_shift_v(
            old_center_v, old_amplitude_v, new_center_v, new_amplitude_v, h
        ),
        "tolerance_v": hysteresis_tolerance_v(
            delta_lower,
            settings.hysteresis_tolerance_per_volt,
            settings.hysteresis_floor_v,
        ),
        "old_geometry": {
            "center_v": float(old_center_v), "amplitude_v": float(old_amplitude_v),
        },
        "new_geometry": {
            "center_v": float(new_center_v), "amplitude_v": float(new_amplitude_v),
        },
    }


# Slip safety: however much extra a caller asks for, the whole window stays
# under this fraction of the sideband spacing, so a slip onto the adjacent
# crossing (one spacing away) can never fall inside it.
HYSTERESIS_WINDOW_MAX_SPACING_FRACTION = 0.8


def hysteresis_selection_window(
    base_tolerance_v: float,
    extra_requested_v: float,
    sideband_offset_v: float | None,
    max_extra_v: float,
) -> dict[str, Any]:
    """The window enforced on a caller's selection: base tolerance plus the
    caller's requested extra, capped.

    With a known sideband spacing the TOTAL is capped at ``0.8 * spacing``;
    without one the extra is capped at ``max_extra_v``
    (``hysteresis_max_extra_tolerance_v``). The window never drops below
    ``base_tolerance_v``, so ``extra == 0`` is exactly the base behaviour.
    """
    base = float(base_tolerance_v)
    requested = max(0.0, float(extra_requested_v))
    spacing = (
        float(sideband_offset_v)
        if sideband_offset_v is not None and math.isfinite(float(sideband_offset_v))
        and float(sideband_offset_v) > 0.0
        else None
    )
    if spacing is not None:
        cap_window = HYSTERESIS_WINDOW_MAX_SPACING_FRACTION * spacing
        cap_source = "sideband_spacing"
    else:
        cap_window = base + max(0.0, float(max_extra_v))
        cap_source = "max_extra_setting"
    window = max(base, min(base + requested, cap_window))
    applied = window - base
    return {
        "applied_window_v": window,
        "base_tolerance_v": base,
        "extra_tolerance_requested_v": requested,
        "extra_tolerance_applied_v": applied,
        "extra_tolerance_capped": applied < requested - 1e-12,
        "cap_window_v": cap_window,
        "cap_source": cap_source,
        "sideband_offset_v": spacing,
    }


def hysteresis_window_violation(
    block: dict[str, Any],
    before_v: float,
    candidate_v: float,
    tolerance_v: float | None = None,
) -> str | None:
    """Why ``candidate_v`` is not the feature that was at ``before_v``, or None.

    The continuing target must sit within ``tolerance_v`` of ``before_v +
    predicted_shift_v``. A candidate outside is refused however well its slope
    and sideband spacing match: on a PDH scan every adjacent crossing looks
    like the tracked one, and a slip of one sideband spacing (20-60 mV) is
    precisely the failure the position is the only witness to.

    ``tolerance_v`` overrides the block's base tolerance (the staged API's
    caller-widened window, see ``hysteresis_selection_window``).
    """
    predicted_v = float(before_v) + float(block["predicted_shift_v"])
    tolerance_v = float(block["tolerance_v"] if tolerance_v is None else tolerance_v)
    miss_v = float(candidate_v) - predicted_v
    if abs(miss_v) <= tolerance_v:
        return None
    return (
        "Tracking candidate is outside the hysteresis window: it is at "
        f"{float(candidate_v):+.4f} V but the feature that was at "
        f"{float(before_v):+.4f} V is predicted at {predicted_v:+.4f} V "
        f"(shift {float(block['predicted_shift_v']) * 1e3:+.1f} mV for a "
        f"{float(block['delta_lower_v']) * 1e3:+.1f} mV lower-endpoint change), "
        f"{miss_v * 1e3:+.1f} mV away against a tolerance of "
        f"{tolerance_v * 1e3:.1f} mV."
    )


def precompensated_center_v(
    old_center_v: float,
    old_amplitude_v: float,
    new_amplitude_v: float,
    target_v: float,
    h: float,
) -> float:
    """The centre that, after the predicted shift, lands the target ON it.

    The shift depends on the new centre through ``dL``, so this is the fixed
    point of ``c = target - h * ((c - a_new) - (c_old - a_old))``, which is
    linear and solved exactly:

        c = (target + h * (a_new + c_old - a_old)) / (1 + h)

    Unbounded: the caller still clamps it with ``bounded_recenter_v``, so every
    step, signal-width and rail limit applies to the compensated centre exactly
    as it did to the uncompensated one.
    """
    h = float(h)
    lower_old = float(old_center_v) - abs(float(old_amplitude_v))
    return (float(target_v) + h * (abs(float(new_amplitude_v)) + lower_old)) / (
        1.0 + h
    )


def bounded_recenter_v(
    center_v: float,
    target_v: float,
    amplitude_v: float,
    *,
    signal_width_v: float | None = None,
    max_signal_widths: float = 0.0,
    rail_amplitude_v: float | None = None,
    step_budget_v: float | None = None,
) -> float:
    """Where to put the sweep centre for one bounded step toward the target.

    Capped at a quarter of the present half-range, because a centre move
    itself shifts this actuator's apparent feature position: small steps
    keep that shift measurable instead of compounding into a jump onto a
    neighbouring feature.

    The FPGA sweep is bounded, but the rail clamp must never turn that small
    step into a large one. An operator scanning at a centre the rails would
    not permit -- 0.653 V at amplitude 0.6, which runs to 1.25 V -- had the
    clamp yank the centre 253 mV in a single write, four times the intended
    bound and precisely the uncontrolled move this stage exists to avoid.
    The rails are therefore honoured only when the centre already respects
    them, and never at the cost of exceeding the step bound.
    """
    amplitude = abs(float(amplitude_v))
    bound = 0.25 * amplitude
    # The scan width is an operator setting and says nothing about whether a
    # step is safe. What does is the distance to the NEXT feature: a step
    # longer than that can vault over a neighbour and land the walk on the
    # wrong one. On a device with 32.5 mV sidebands, a quarter of a 0.6 V
    # half-range is 150 mV -- 4.6 sideband spacings in a single write.
    # Bound by the measured signal width where one is known; the scan-width
    # rule remains the fallback when it is not.
    if signal_width_v and max_signal_widths > 0.0:
        bound = min(bound, float(max_signal_widths) * abs(float(signal_width_v)))
    if step_budget_v is not None:
        # A combined narrow-and-recentre stage moves the feature by BOTH
        # effects, so the caller hands down what is left of the stage's single
        # allowance after the width change has claimed its share. One event,
        # one budget -- otherwise a combined stage perturbs twice as much as
        # the single-axis one it replaces.
        bound = min(bound, max(0.0, float(step_budget_v)))
    lo, hi = center_v - bound, center_v + bound
    # Rails come from the amplitude the scan will HAVE, which is not the one it
    # has now when this write also narrows: the rail moves outward as the width
    # comes down (1 - 0.8 = 0.20, but 1 - 0.4477 = 0.55). Evaluating them at the
    # old width is what pinned the centre and produced the rail deadlock.
    rail_amplitude = (
        amplitude if rail_amplitude_v is None else abs(float(rail_amplitude_v))
    )
    rail_lo, rail_hi = -1.0 + rail_amplitude, 1.0 - rail_amplitude
    # Tolerant comparison: 1.0 - 0.8 is 0.19999999999999996, and a centre
    # sitting exactly on that rail reads back as 0.2 often enough that an
    # exact test let a 4e-17 difference decide between honouring the rails
    # and ignoring them entirely.
    if rail_lo <= rail_hi and (
        rail_lo - _RAIL_EPSILON_V <= center_v <= rail_hi + _RAIL_EPSILON_V
    ):
        lo, hi = max(lo, rail_lo), min(hi, rail_hi)
    return min(hi, max(lo, float(target_v)))


def center_step_allowance_v(
    settings: AutoLockScanSettings,
    amplitude_v: float,
    sideband_offset_v: float | None,
) -> float:
    """How far one stage may move the apparent feature, in sweep volts.

    One allowance for both ways a stage can move it -- commanding the centre
    and changing the width -- because the risk is the same either way:
    travelling further than the distance to a neighbouring feature in a
    single step can leave the walk tracking the wrong one.
    """
    allowance = 0.25 * abs(float(amplitude_v))
    widths = float(settings.max_center_step_signal_widths)
    if sideband_offset_v is not None and widths > 0.0:
        allowance = min(allowance, widths * 2.0 * abs(float(sideband_offset_v)))
    return allowance


def min_safe_amplitude_v(
    amplitude_v: float,
    offset_v: float,
    uncertainty_per_fraction: float | None,
    floor_v: float,
    keep_fraction: float = _WINDOW_KEEP_FRACTION,
) -> float | None:
    """Smallest amplitude that still leaves the target inside the window.

    A FLOOR on the next amplitude, not a ceiling: narrowing below it crops
    the target out of view. Named for what it returns, because reading it as
    a ceiling and taking min() of the two produced exactly the cropping it
    exists to prevent -- a 0.533 V floor cut to 0.400 V, putting a target at
    0.6795 V outside a window ending at 0.600 V.

    Used when the sweep rails block further re-centring: the centre cannot
    exceed +/-(1 - amplitude), so a target beyond that is unreachable until
    the amplitude comes down and takes the rail outward with it. Narrowing
    is then the only way forward, even though the target is not yet centred.

    Two effects race. A smaller window brings its own edge closer to the
    target, and the width change moves the target as well (the planner passes
    ``uncertainty_per_fraction = tol * amplitude``: the hysteresis model's
    uncertainty for a full-width cut at a fixed centre). Solve for the SMALLEST
    amplitude that still contains the target after the shift reaching it
    causes -- the floor a caller may narrow down to, not a ceiling it may
    narrow past.
    ``None`` when no cut is safe.
    """
    amplitude = abs(float(amplitude_v))
    offset = abs(float(offset_v))
    spf = max(0.0, float(uncertainty_per_fraction or 0.0))
    keep = min(1.0, max(0.0, float(keep_fraction)))
    if amplitude <= 1e-12:
        return None
    # Solved, not iterated. The requirement is
    #
    #     c >= (offset + spf * (1 - c / amplitude)) / keep
    #
    # whose right side falls as c rises, so the smallest admissible c is the
    # single crossing point -- available in closed form:
    #
    #     c* = (offset + spf) / (keep + spf / amplitude)
    #
    # This was a five-round fixed-point iteration, which is a contraction only
    # while spf < keep * amplitude. On an actuator whose
    # apparent feature position is more sensitive to width than that -- the
    # field case was spf 0.289 V per unit fraction at amplitude 0.212 V, a
    # contraction factor of 1.5 -- the iteration does not converge. It settles
    # into a two-cycle (0.1556 <-> 0.2411 V), and round five happened to land
    # on the high branch, which reads as "above the current amplitude" and so
    # as "no cut is safe". The walk refused at a stage where 0.1896 V was a
    # perfectly legal narrowing, and the refusal was decided by the parity of
    # the iteration count.
    #
    # Past the crossing the shift term is gone and the requirement is the flat
    # c >= offset / _WINDOW_KEEP_FRACTION, so when that alone exceeds the
    # current amplitude nothing narrower can hold the target either.
    if keep <= 0.0 or offset >= keep * amplitude:
        return None
    candidate = max(
        float(floor_v), (offset + spf) / (keep + spf / amplitude)
    )
    if candidate >= amplitude:
        return None
    return candidate


def plan_refinement_step(
    settings: AutoLockScanSettings,
    *,
    center_v: float,
    amplitude_v: float,
    target_v: float,
    sideband_offset_v: float | None,
    detector: str,
    trace_length: int,
) -> RefinementStep:
    """Decide the next thing the refinement walk should do, and nothing else.

    Applies the constraints in a fixed, documented order, each exactly once:

    1. Edge guard -- when refinement is needed and the target is outside the
       central 60% of the scan, preserve more width near either edge and
       recenter alone first when one bounded step reaches the central region.
       A strict-lockable scan is already done; its selected feature is handed
       directly to the normal lock-centering path.
    2. Goal -- the widest amplitude satisfying both the detector sample floor
       and ``max_lockable_amplitude_v``. Met, and the detector already
       strict, means no further narrowing is needed.
    3. Schedule -- this stage's desired cut (coarse vs. gentle factor).
    4. Shift allowance -- gentle the cut so the hysteresis model's UNCERTAINTY
       for it (``tol * |dL|``; the predicted shift itself is compensated, not
       charged) stays within ``center_step_allowance_v``.
    5. Centring -- target outside half the *next* window -> recenter via
       ``bounded_recenter_v``, aimed at the centre that puts the target on
       the centre AFTER the predicted hysteresis shift
       (``precompensated_center_v``), so every bound applies to the compensated
       centre and the crop guard judges where the target will LAND.
    6. Rail escape -- a bounded recentre cannot progress -> the amplitude is
       floored by ``min_safe_amplitude_v`` so narrowing moves the rail
       outward instead of stalling.
    7. Crop guard -- never return an amplitude below that floor.

    Pure: makes no I/O and mutates nothing. The caller (``session.py``) acts
    on the returned step, re-detects, and calls this again for the next one.
    """
    edge_fraction = abs(float(target_v) - float(center_v)) / max(
        abs(float(amplitude_v)), 1e-12
    )
    edge_guard_active = edge_fraction > _EDGE_GUARD_FRACTION
    if detector == "strict" and not scan_too_wide_to_lock(
        settings, amplitude_v, sideband_offset_v, trace_points=trace_length
    ):
        return RefinementStep(
            "done", center_v, amplitude_v,
            "goal met: strict detector accepts this scan",
            {"edge_fraction": edge_fraction, "edge_guard_active": float(edge_guard_active)},
        )

    # Two floors, whichever is wider: the amplitude that gives the detector
    # the samples it needs, and the one that puts the error signal over
    # min_signal_scan_fraction of the span.
    target_amplitude = (
        float(settings.half_range_sweep_v)
        * (trace_length - 1)
        / (2.0 * _STRICT_DETECTOR_SAMPLES)
    )
    width_amplitude = max_lockable_amplitude_v(settings, sideband_offset_v)
    if width_amplitude is not None:
        target_amplitude = min(target_amplitude, width_amplitude)
    # Sideband to sideband: the full width of the error signal on the plot.
    signal_width = (
        None if sideband_offset_v is None else 2.0 * abs(float(sideband_offset_v))
    )

    # Step size adapts to the margin. While the signal is a speck on the scan,
    # the next window is still many signal widths wide and a half-step is
    # safe; close to the goal, narrow gently, because a width change of a few
    # percent visibly moves this actuator's apparent feature position and a
    # big jump can drop it out of the new window entirely.
    factor = (
        _REFINEMENT_COARSE_NARROW_FACTOR
        if amplitude_v > _REFINEMENT_GENTLE_APPROACH * target_amplitude
        else _REFINEMENT_NARROW_FACTOR
    )
    # Preserve more of the current field of view when the target is in either
    # outer 20% of the scan. The centre is still pre-compensated and moved in
    # the same register write; the gentler cut gives that bounded centre move
    # room to pull the feature inward before another narrowing. This is based
    # on the target's position in the live scan, not a preferred voltage.
    if edge_guard_active:
        factor = max(factor, _REFINEMENT_EDGE_NARROW_FACTOR)
    scheduled_v = amplitude_v * factor

    # Hold the width-induced shift to the same allowance a centre step gets:
    # never move the feature further than the distance to a neighbour in one
    # go, whichever way it is moved.
    step_allowance = center_step_allowance_v(settings, amplitude_v, sideband_offset_v)
    # What can surprise the planner is not the predicted shift -- the centre is
    # aimed at where the feature will LAND -- but the model's error, which grows
    # with the lower-endpoint change: tol * |dL|. That, per unit width fraction
    # of a cut at a fixed centre (dL = a_old * fraction), is what the allowance
    # is charged. The constant floor is detector noise that no smaller move
    # avoids, so it is not charged: charging it made the allowance impossible
    # to meet on a narrow-signal device and pinned every cut at the gentlest
    # factor. (Charging the full predicted shift h * dL, as an earlier version
    # of this planner did, double-counts what the pre-compensation already
    # removes and shrank the very first centre step by three quarters.)
    h = float(settings.hysteresis_per_volt_lower)
    tol_per_volt = float(settings.hysteresis_tolerance_per_volt)
    uncertainty_per_fraction = tol_per_volt * abs(float(amplitude_v))
    if uncertainty_per_fraction > 1e-9:
        gentlest = 1.0 - (step_allowance / uncertainty_per_fraction)
        factor = min(_REFINEMENT_MAX_NARROW_FACTOR, max(factor, gentlest))
    shift_capped_v = amplitude_v * factor

    # Stop precisely at the goal rather than overshooting past it.
    next_amplitude = max(target_amplitude, shift_capped_v)
    next_amplitude = min(amplitude_v, next_amplitude)

    bounds = {
        "goal_v": target_amplitude,
        "scheduled_v": scheduled_v,
        "shift_capped_v": shift_capped_v,
        "edge_fraction": edge_fraction,
        "edge_guard_active": float(edge_guard_active),
    }

    if edge_guard_active:
        # If one existing bounded move can bring the target into the central
        # 60% without changing scan width, spend this stage on recentring first.
        # More distant edge targets still use the shared combined move below,
        # with the gentler cut preserving room for bounded centring.
        edge_aim = precompensated_center_v(
            center_v, amplitude_v, amplitude_v, target_v, h
        )
        edge_center = bounded_recenter_v(
            center_v,
            edge_aim,
            amplitude_v,
            signal_width_v=signal_width,
            max_signal_widths=settings.max_center_step_signal_widths,
            rail_amplitude_v=amplitude_v,
            step_budget_v=step_allowance,
        )
        if (
            abs(edge_center - center_v) > _CENTER_MOVE_EPSILON_V
            and abs(float(target_v) - edge_center)
            <= _EDGE_GUARD_FRACTION * abs(float(amplitude_v))
        ):
            return RefinementStep(
                "recenter",
                edge_center,
                amplitude_v,
                "one bounded move can bring the target inside the central 60%; recenter first",
                {
                    **bounds,
                    "centre_budget_v": step_allowance,
                    "predicted_target_v": target_v + predicted_shift_v(
                        center_v, amplitude_v, edge_center, amplitude_v, h
                    ),
                    "rail_v": 1.0 - abs(amplitude_v),
                },
            )

    if next_amplitude >= amplitude_v - _NARROW_SCHEDULE_EPSILON_V:
        return RefinementStep(
            "done", center_v, amplitude_v, "narrowing schedule reached its floor", bounds
        )

    # Narrow and recentre in the SAME register write.
    #
    # Held apart, a narrowing stage shrinks the window around a centre the
    # feature is not at, so the feature ends up proportionally further off
    # centre and the next stage has to spend itself putting it back. Nine such
    # centre-only stages in one field run accumulated 111 mV of wander and made
    # no width progress at all, and each one is its own hysteretic event -- on
    # this actuator the count of geometry changes is itself the cost, so
    # alternating pays it twice.
    #
    # Two things make the combination safe. The rails are evaluated at the
    # amplitude the scan will have, where they are looser. And the centre gets
    # only what is left of the stage's single movement allowance after the
    # predicted width-induced shift has claimed its share, so a combined stage
    # perturbs the feature no more than the single-axis stage it replaces.
    #
    # The centre is aimed at where the target will be AFTER the hysteresis
    # shift, not where it is now. The shift depends on the new centre and width
    # through the lower endpoint, so the aim is the exact fixed point
    # (`precompensated_center_v`); the clamp below is unchanged and still
    # decides how much of it one stage may take.
    def _combine(next_amp: float) -> tuple[float, float, float, float]:
        aim = precompensated_center_v(center_v, amplitude_v, next_amp, target_v, h)
        # The prediction error of the write being planned, evaluated at the
        # unclamped aim (the centre actually commanded can only be closer to
        # the old one, so this is an upper bound on |dL|).
        width_shift = tol_per_volt * abs(
            lower_endpoint_change_v(center_v, amplitude_v, aim, next_amp)
        )
        # The model's error may claim at most half the allowance: a stage that
        # could never move the centre because the uncertainty exceeds a tiny
        # allowance (a narrow-signal device) would stall where the plain
        # bounded step creeps.
        budget = step_allowance - min(width_shift, 0.5 * step_allowance)
        centre = bounded_recenter_v(
            center_v,
            aim,
            amplitude_v,
            signal_width_v=signal_width,
            max_signal_widths=settings.max_center_step_signal_widths,
            rail_amplitude_v=next_amp,
            step_budget_v=budget,
        )
        # Where the target is predicted to sit in the new window.
        landing = target_v + predicted_shift_v(
            center_v, amplitude_v, centre, next_amp, h
        )
        return centre, landing, width_shift, budget

    combined_center, landing_v, predicted_width_shift, centre_budget = _combine(
        next_amplitude
    )
    bounds = {
        **bounds,
        "centre_budget_v": centre_budget,
        "hysteresis_uncertainty_v": predicted_width_shift,
        "predicted_target_v": landing_v,
        "rail_v": 1.0 - abs(next_amplitude),
    }

    # Crop guard, on the RESIDUAL offset -- what is left after the centre has
    # moved, not the gap before it. Measuring it before the move is what forced
    # the old rail-escape path to floor the amplitude so hard that the feature
    # was parked at 90% of the new half-range, tripping the next stage's
    # centring rule immediately. "Left" includes the hysteresis shift the move
    # itself causes: the target is judged where it will land.
    residual_offset = landing_v - combined_center
    if abs(residual_offset) > _INNER_WINDOW_FRACTION * next_amplitude:
        # The gap is already the predicted landing's; min_safe_amplitude_v adds
        # only the model's uncertainty for the deeper cut it is solving for.
        safe = min_safe_amplitude_v(
            amplitude_v, residual_offset, uncertainty_per_fraction,
            target_amplitude,
            keep_fraction=(
                _EDGE_WINDOW_KEEP_FRACTION if edge_guard_active
                else _WINDOW_KEEP_FRACTION
            ),
        )
        if safe is None:
            # Nothing this stage can do keeps the feature in view: the centre is
            # as close as one bounded step and the rails allow, and no width
            # that still narrows leaves the target inside.
            # Name the bound that actually bit. The message used to blame the
            # rails unconditionally; in the field it did so at a centre of
            # +0.4048 V with the rail at 0.7880 V, 383 mV away, which sent the
            # diagnosis in the wrong direction for a whole round of testing.
            rail_v = 1.0 - abs(next_amplitude)
            if abs(combined_center) >= rail_v - _RAIL_EPSILON_V:
                held_by = f"the sweep rails hold the center at {combined_center:+.4f} V"
            elif abs(combined_center - center_v) >= centre_budget - _CENTER_MOVE_EPSILON_V:
                held_by = (
                    f"one stage may move the center only {centre_budget * 1e3:.1f} mV "
                    f"(from {center_v:+.4f} V to {combined_center:+.4f} V)"
                )
            else:
                held_by = f"the center reached {combined_center:+.4f} V"
            reason = (
                f"{held_by} and the target is {target_v:+.4f} V, "
                f"{abs(residual_offset) * 1e3:.1f} mV away, which no narrowing can "
                "bring into view without losing it. Move the laser closer to "
                "the feature, or start from a narrower scan."
            )
            return RefinementStep("refuse", center_v, amplitude_v, reason, bounds)
        if safe > next_amplitude:
            # Narrow less, and recompute the centre against the looser rail and
            # the smaller width-induced shift that a gentler cut implies.
            next_amplitude = min(amplitude_v, safe)
            combined_center, landing_v, predicted_width_shift, centre_budget = (
                _combine(next_amplitude)
            )
            bounds = {
                **bounds,
                "crop_floor_v": safe,
                "centre_budget_v": centre_budget,
                "hysteresis_uncertainty_v": predicted_width_shift,
                "predicted_target_v": landing_v,
                "rail_v": 1.0 - abs(next_amplitude),
            }
        if next_amplitude >= amplitude_v - _NARROW_SCHEDULE_EPSILON_V:
            # The crop floor has eaten the whole cut. Move the centre alone and
            # let the next stage narrow from closer in.
            if abs(combined_center - center_v) > _CENTER_MOVE_EPSILON_V:
                return RefinementStep(
                    "recenter",
                    combined_center,
                    amplitude_v,
                    "no width change is safe yet; bounded recentre first",
                    bounds,
                )
            return RefinementStep(
                "refuse",
                center_v,
                amplitude_v,
                (
                    f"The center is pinned at {center_v:+.4f} V and the target "
                    f"is {target_v:+.4f} V, which neither a bounded center step "
                    "nor any safe narrowing can reach."
                ),
                bounds,
            )

    return RefinementStep(
        "narrow", combined_center, next_amplitude, "scheduled narrowing", bounds
    )


class IdentityGuard:
    """Guards that the walk is still tracking the feature it started on.

    A changed discriminator slope, or (once one is known) a sideband spacing
    that has drifted past tolerance, means a later detection is a different
    crossing -- the one failure trajectory refinement exists to catch, as
    distinct from the same feature simply not holding still (see
    ``TrajectoryRefinementAborted.failure_kind`` in ``session.py``).
    """

    def __init__(
        self,
        target: Any,
        resolution_samples: float,
        *,
        trace_length: int,
        detector: str = "strict",
    ) -> None:
        self._slope = target.target_slope_rising
        # One baseline PER DETECTOR: {detector: (sideband_v, resolution)}.
        #
        # The two detectors do not measure the same number. Over eight field
        # runs the coarse tracker reported 32.22 mV mean with 1.86 mV of spread
        # across scan amplitudes from 0.8 down to 0.45 V, while the strict
        # detector reported 18.44 mV mean with 7.76 mV of spread, converging
        # upward as resolution improved (15.8 -> 16.9 -> 17.5 -> 23.6). They sit
        # a systematic ~1.75x apart, so comparing one against the other tests
        # which algorithm ran, not whether the crossing changed -- a walk was
        # aborted for "measuring 32.369 mV against an identity of 17.497 mV"
        # when both readings were of the same untroubled feature.
        #
        # Keeping the baselines apart is the same principle already applied to
        # resolution: only compare like with like, and let a better measurement
        # from the SAME estimator supersede a worse one.
        self._baselines: dict[str, tuple[float, float]] = {}
        self._trace_length = trace_length
        if target.sideband_offset_v is not None:
            self._baselines[str(detector)] = (
                target.sideband_offset_v,
                resolution_samples,
            )

    def check(
        self,
        candidate: Any,
        *,
        amplitude_v: float,
        detector: str = "strict",
        resolution_samples: float = 0.0,
        check_sideband: bool = True,
    ) -> None:
        if candidate.target_slope_rising != self._slope:
            raise _TrackingIdentityChanged(
                "Tracking candidate changed discriminator slope identity."
            )
        if not check_sideband:
            return
        if candidate.sideband_offset_v is None:
            return
        baseline = self._baselines.get(str(detector))
        if baseline is None:
            # First reading from this estimator: it becomes that estimator's
            # own reference. It is never compared against another's.
            self._baselines[str(detector)] = (
                candidate.sideband_offset_v,
                resolution_samples,
            )
            return
        known_sideband, known_resolution = baseline

        # A better resolved reading REPLACES its estimator's baseline rather
        # than being judged against it. The spacing is biased by resolution --
        # this estimator read 16.9 mV at 6.1 samples per half-width and 23.6 mV
        # at 8.8 on the same feature -- so comparing across a resolution change
        # tests the sweep width, not the identity of the crossing. Narrowing
        # exists to measure better; rejecting the better measurement for
        # disagreeing with the worse one rejects the improvement it was sent
        # to get. Equal or worse resolution still compares, which is what a
        # re-centring stage does.
        if resolution_samples > known_resolution * _SIDEBAND_ADOPTION_MARGIN:
            self._baselines[str(detector)] = (
                candidate.sideband_offset_v,
                resolution_samples,
            )
            return

        # Sideband spacing should survive geometry changes. Allow a generous
        # 35% while the wide trace is under-resolved.
        tolerance = max(
            _SIDEBAND_IDENTITY_TOLERANCE_FRACTION * known_sideband,
            2.0 * abs(amplitude_v) / self._trace_length,
        )
        if abs(candidate.sideband_offset_v - known_sideband) > tolerance:
            raise _TrackingIdentityChanged(
                "Tracking candidate changed PDH sideband identity: "
                f"measured {candidate.sideband_offset_v * 1e3:.3f} mV by the "
                f"{detector} detector at {resolution_samples:.2f} samples per "
                f"half-width, against an identity of "
                f"{known_sideband * 1e3:.3f} mV established at "
                f"{known_resolution:.2f} samples "
                f"(tolerance {tolerance * 1e3:.3f} mV)."
            )

    def evaluate(
        self,
        candidate: Any,
        *,
        amplitude_v: float,
        detector: str = "strict",
        resolution_samples: float = 0.0,
        check_sideband: bool = True,
    ) -> tuple[bool, str | None]:
        """Same test as `check`, without mutating the baseline.

        The staged auto-lock API has to annotate EVERY candidate on a frame
        with identity_ok/identity_reason before the caller picks one, and it
        must never let a candidate the caller does not select silently become
        (or replace) the tracked identity -- that would make the annotation
        itself an unrequested selection. Runs `check` against a scratch clone
        that shares this guard's slope/baselines by value; only `check`,
        called on the one candidate actually selected next, may mutate
        `self`.
        """
        clone = IdentityGuard.__new__(IdentityGuard)
        clone._slope = self._slope
        clone._baselines = dict(self._baselines)
        clone._trace_length = self._trace_length
        try:
            clone.check(
                candidate,
                amplitude_v=amplitude_v,
                detector=detector,
                resolution_samples=resolution_samples,
                check_sideband=check_sideband,
            )
        except _TrackingIdentityChanged as exc:
            return False, str(exc)
        return True, None
