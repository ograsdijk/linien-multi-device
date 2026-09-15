"""Guarded sweep-center moves for hysteretic piezo/cavity systems.

A large jump in ``sweep_center`` can land somewhere other than the commanded
voltage: piezo stacks (and the mechanics they push) show hysteresis and
backlash, so where the actuator ends up depends on the direction it was last
moved. A direct set to a distant target therefore risks locking to the wrong
feature, or to nothing at all.

This module plans the move; it does not perform it. ``plan_approach`` turns a
(current, target) voltage pair plus :class:`ApproachSettings` into an
:class:`ApproachPlan` — an ordered list of sweep-center set-points with a delay
after each, plus a final settle. The caller applies them.

Strategy: for a jump within ``max_direct_jump_v`` the plan is a single direct
set, exactly what the code did before this module existed. For a larger jump,
the plan overshoots to ``target -/+ approach_offset_v`` and then ramps back to
the target in ``ramp_step_v`` increments, so the final approach is always from
the same direction -- the condition under which a hysteresis curve is
single-valued and repeatable.

All voltages are sweep volts on the x-axis, the same units as
``sweep_center``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, NamedTuple

# The sweep DAC range, matching SWEEP_MIN/SWEEP_MAX in the web UI. Set-points are
# clamped here so an overshoot can never command a voltage the device will just
# saturate at (which would silently break the anti-backlash guarantee).
SWEEP_MIN = -1.0
SWEEP_MAX = 1.0

# Upper bound on ramp set-points. A small ramp_step_v against a large
# approach_offset_v could otherwise plan thousands of RPyC round-trips; past a
# few hundred the extra resolution buys nothing and the move just takes minutes.
# The step is coarsened to fit rather than the ramp being truncated, so the
# approach still covers the whole offset from one direction.
MAX_RAMP_STEPS = 400

# Fraction of the sideband offset beyond which a re-detected crossing is taken to
# BE a sideband rather than a displaced carrier. The PDH sidebands sit at +/-Omega
# from the carrier, so a feature more than this far out is nearer the sideband
# than any plausible hysteresis excursion. Internal; not a user setting.
_SIDEBAND_GUARD_FRACTION = 0.4


class ApproachAborted(RuntimeError):
    """The guarded move never confirmed the target, so no lock was started.

    Carries the approach report alongside the message: an aborted attempt is
    the most informative hysteresis measurement there is -- it has the largest
    offsets -- so callers record it rather than only surfacing the text.
    """

    def __init__(self, message: str, report: dict[str, Any]) -> None:
        super().__init__(message)
        self.report = report


@dataclass
class ApproachSettings:
    """Per-device motion settings for the guarded center move.

    Disabled by default: until a device is characterised, the direct set is
    what it has always had.
    """

    enabled: bool = False
    # Acceptance window, as a fraction of the calibrated feature half-width
    # (``AutoLockScanSettings.half_range_sweep_v``). A lock succeeds as long as
    # the DC point lands inside the monotonic stretch between the two lobe
    # extrema, so the tolerance is derived from that measured width rather than
    # being a voltage somebody has to guess.
    capture_fraction: float = 0.5
    # Rejection bound, in the same units: a re-detection further out than this
    # is a NEIGHBOURING crossing, not a displaced one, and correcting towards it
    # would walk the lock onto the wrong feature. Used when the scan cannot
    # resolve a sideband offset (dispersive mode).
    max_correction_span: float = 4.0
    # Shortcut past the direct probe for jumps already known to be too big.
    # Wide open by default: the probe costs one sweep and its measured offset is
    # more trustworthy than a threshold nobody has measured yet.
    max_direct_jump_v: float = 2.0
    # How far past the target to overshoot before ramping back in. Must exceed
    # the actuator's backlash width to do any good.
    approach_offset_v: float = 0.05
    ramp_step_v: float = 0.005
    ramp_step_delay_ms: int = 20
    # Dwell after the last set-point, for the mechanics to stop creeping. This
    # is the knob that matters when the residual is creep rather than backlash.
    settle_ms: int = 300
    # Default final-approach direction. True ramps upward onto the target, which
    # matches the rising branch the crossing was detected on: the plot x-axis
    # runs from ``center - amplitude`` to ``center + amplitude``, so increasing
    # index is increasing sweep voltage.
    approach_from_below: bool = True
    # Correction attempts per approach direction, before flipping direction.
    max_approach_iterations: int = 2

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "ApproachSettings":
        if payload is None:
            return cls()
        defaults = cls()
        values: dict[str, Any] = {}
        for name in defaults.__dataclass_fields__.keys():
            values[name] = payload[name] if name in payload else getattr(defaults, name)
        return cls(**values)


@dataclass(frozen=True)
class ApproachStep:
    """One sweep-center set-point, and how long to wait after applying it."""

    voltage: float
    delay_s: float


@dataclass(frozen=True)
class ApproachPlan:
    steps: tuple[ApproachStep, ...]
    settle_s: float
    # True when this is the plain direct set (no overshoot, no ramp).
    direct: bool
    # Direction of the final approach; meaningless when ``direct``.
    from_below: bool

    @property
    def target_voltage(self) -> float:
        return self.steps[-1].voltage


def _clamp(voltage: float) -> float:
    return max(SWEEP_MIN, min(SWEEP_MAX, float(voltage)))


def _direct_plan(target_v: float, *, from_below: bool, settle_s: float = 0.0) -> ApproachPlan:
    return ApproachPlan(
        steps=(ApproachStep(voltage=_clamp(target_v), delay_s=0.0),),
        settle_s=settle_s,
        direct=True,
        from_below=from_below,
    )


def _ramp_voltages(start_v: float, target_v: float, step_v: float) -> list[float]:
    """Set-points strictly between ``start_v`` and ``target_v``, then the target.

    The span is divided into equal steps no larger than ``step_v``, so the
    ramp is uniform and always ends exactly on the target rather than on a
    rounding remainder.
    """
    span = abs(target_v - start_v)
    if span <= 0.0 or step_v <= 0.0:
        return [target_v]
    count = max(1, int(-(-span // step_v)))  # ceil
    count = min(count, MAX_RAMP_STEPS)
    direction = 1.0 if target_v > start_v else -1.0
    increment = span / float(count)
    voltages = [start_v + direction * increment * i for i in range(1, count)]
    voltages.append(target_v)
    return voltages


def plan_approach(
    current_v: float,
    target_v: float,
    settings: ApproachSettings,
    *,
    from_below: bool | None = None,
    force_anti_backlash: bool = False,
) -> ApproachPlan:
    """Plan the move from ``current_v`` to ``target_v``.

    ``from_below`` overrides ``settings.approach_from_below`` -- the retry path
    passes the opposite of whatever the first attempt used. ``force_anti_backlash``
    ignores ``max_direct_jump_v``, which the retry path also needs: after a failed
    attempt the center already sits on the target, so a threshold check would make
    the retry a no-op instead of a genuine re-approach from the other side.

    Falls back to a direct set when the anti-backlash move is disabled, when
    the jump is small enough not to need one, or when the target sits so close
    to a sweep rail that there is no room to overshoot in either direction.
    """
    target = _clamp(target_v)
    approach_from_below = (
        settings.approach_from_below if from_below is None else bool(from_below)
    )

    if not settings.enabled:
        return _direct_plan(target, from_below=approach_from_below)

    jump = abs(target - float(current_v))
    if not force_anti_backlash and jump <= max(0.0, float(settings.max_direct_jump_v)):
        return _direct_plan(target, from_below=approach_from_below)

    offset = abs(float(settings.approach_offset_v))
    settle_s = max(0.0, float(settings.settle_ms) / 1000.0)
    if offset <= 0.0:
        return _direct_plan(target, from_below=approach_from_below, settle_s=settle_s)

    # Overshoot to the far side of the target, so the final move travels in a
    # single direction. If the rail leaves no room on the requested side, try
    # the other one -- an approach from the "wrong" direction is still better
    # than no anti-backlash move at all -- and give up only if both are pinned.
    staging: float | None = None
    for candidate_from_below in (approach_from_below, not approach_from_below):
        sign = -1.0 if candidate_from_below else 1.0
        candidate = _clamp(target + sign * offset)
        if candidate != target:
            staging = candidate
            approach_from_below = candidate_from_below
            break
    if staging is None:
        return _direct_plan(target, from_below=approach_from_below, settle_s=settle_s)

    step_delay_s = max(0.0, float(settings.ramp_step_delay_ms) / 1000.0)
    steps = [ApproachStep(voltage=staging, delay_s=step_delay_s)]
    for voltage in _ramp_voltages(staging, target, abs(float(settings.ramp_step_v))):
        steps.append(ApproachStep(voltage=_clamp(voltage), delay_s=step_delay_s))

    return ApproachPlan(
        steps=tuple(steps),
        settle_s=settle_s,
        direct=False,
        from_below=approach_from_below,
    )


def capture_tolerance_v(settings: ApproachSettings, half_range_sweep_v: float) -> float:
    """Acceptance window for the pre-lock check, in sweep volts.

    The lock only needs the DC point to land inside the monotonic region between
    the two lobe extrema, so the window scales with the calibrated feature width
    rather than being configured as an absolute voltage.
    """
    width = abs(float(half_range_sweep_v))
    return max(0.0, float(settings.capture_fraction)) * width


def rejection_bound_v(
    settings: ApproachSettings,
    half_range_sweep_v: float,
    sideband_offset_v: float | None = None,
) -> float:
    """Offset beyond which a re-detection is a DIFFERENT crossing, not a moved one.

    Correcting towards a neighbouring feature would walk the lock onto the wrong
    one -- the precise failure this module exists to prevent -- so an offset past
    this bound aborts instead of being corrected. When the scan resolved the PDH
    sideband spacing, that is the physical bound; otherwise fall back to a
    multiple of the calibrated feature width.

    Returned raw; :func:`acceptance_window_v` is what reconciles it against the
    acceptance window, and is what callers should use.
    """
    width = abs(float(half_range_sweep_v))
    bound = max(0.0, float(settings.max_correction_span)) * width
    if sideband_offset_v is not None:
        sideband = abs(float(sideband_offset_v))
        if sideband > 0.0:
            bound = min(bound, _SIDEBAND_GUARD_FRACTION * sideband)
    return bound


class AcceptanceWindow(NamedTuple):
    """The pair of thresholds the verification step compares an offset against."""

    tolerance_v: float
    # None when no usable neighbour guard could be derived, meaning distance
    # alone never rejects a re-detection.
    bound_v: float | None
    # True when the configured window had to be narrowed to stay clear of a
    # neighbouring feature -- worth telling the operator, since it means the
    # signal is more closely spaced than the settings assume.
    tightened: bool


def acceptance_window_v(
    settings: ApproachSettings,
    half_range_sweep_v: float,
    sideband_offset_v: float | None = None,
) -> AcceptanceWindow:
    """The acceptance window and the rejection bound, as a consistent pair.

    Configured independently these two can meet -- a closely spaced PDH signal
    can put the neighbour guard at or below ``capture_fraction`` x the feature
    width -- and then every offset that fails acceptance is immediately called a
    neighbouring crossing, so the correction loop never runs and the operator is
    told the wrong thing.

    The fix belongs on the acceptance side: if the configured window reaches more
    than halfway to the next feature, it is too generous for this signal, so it
    is tightened to half the bound. That always leaves a real correction window
    between the two, and it never accepts a landing that is closer to a
    neighbouring feature than to the one that was asked for.

    A non-positive bound means no guard could be derived at all (``0`` disables
    ``max_correction_span``, or the device has no calibrated feature width). That
    is reported as no bound rather than as a bound of zero, which would reject
    every offset and make the device unlockable.
    """
    raw_bound = rejection_bound_v(settings, half_range_sweep_v, sideband_offset_v)
    tolerance = capture_tolerance_v(settings, half_range_sweep_v)
    if raw_bound <= 0.0 or not math.isfinite(raw_bound):
        return AcceptanceWindow(tolerance_v=tolerance, bound_v=None, tightened=False)
    if tolerance > 0.5 * raw_bound:
        return AcceptanceWindow(
            tolerance_v=0.5 * raw_bound, bound_v=raw_bound, tightened=True
        )
    return AcceptanceWindow(tolerance_v=tolerance, bound_v=raw_bound, tightened=False)


def classify_hysteresis(
    samples: list[Mapping[str, Any]], tolerance_v: float
) -> tuple[str, str]:
    """Read a diagnostic sweep of offsets and say what kind of error it is.

    ``samples`` are ``{"from_below": bool, "settle_ms": int, "offset_v": float}``
    measurements of where the crossing appeared relative to the commanded center.
    The distinction matters because the two causes want different fixes:

    * **backlash** -- the offset flips sign with the approach direction. Widen
      ``approach_offset_v`` past the backlash width; direction is what saves you.
    * **creep** -- the offset is the same whichever way you arrive, and shrinks
      the longer you wait. Raise ``settle_ms``.

    Returns ``(verdict, detail)``.
    """
    usable = [
        item
        for item in samples
        if item.get("offset_v") is not None and item.get("settle_ms") is not None
    ]
    if len(usable) < 2:
        return "inconclusive", "not enough successful measurements to tell"

    tolerance = abs(float(tolerance_v))
    longest = max(int(item["settle_ms"]) for item in usable)

    def _at(from_below: bool, settle_ms: int) -> float | None:
        for item in usable:
            if bool(item["from_below"]) is from_below and int(item["settle_ms"]) == settle_ms:
                return float(item["offset_v"])
        return None

    below = _at(True, longest)
    above = _at(False, longest)
    if below is None or above is None:
        return "inconclusive", "both approach directions are needed to tell them apart"

    if abs(below) <= tolerance and abs(above) <= tolerance:
        return (
            "negligible",
            f"both directions land within {tolerance:.4f} V — no guarded move needed",
        )

    def _decays(from_below: bool) -> bool:
        settles = sorted(
            int(item["settle_ms"])
            for item in usable
            if bool(item["from_below"]) is from_below
        )
        if len(settles) < 2:
            return False
        first = _at(from_below, settles[0])
        last = _at(from_below, settles[-1])
        if first is None or last is None or abs(first) <= tolerance:
            return False
        return abs(last) < 0.5 * abs(first)

    if (below > 0.0) != (above > 0.0):
        return (
            "backlash",
            f"the offset flips sign with direction ({below:+.4f} V from below, "
            f"{above:+.4f} V from above) — set approach_offset_v above "
            f"{max(abs(below), abs(above)):.4f} V",
        )

    if _decays(True) or _decays(False):
        return (
            "creep",
            f"the offset is the same either way ({below:+.4f} V, {above:+.4f} V) and "
            "shrinks with settling — raise settle_ms",
        )

    return (
        "drift_or_creep",
        f"the offset does not depend on direction ({below:+.4f} V, {above:+.4f} V) and "
        "did not shrink over the settle times tried — try longer settles, or look "
        "for laser/cavity drift",
    )


def probe_report(probe: Mapping[str, Any]) -> dict[str, Any]:
    """Shape a hysteresis measurement like an approach report, for storage.

    The diagnostic is the most direct hysteresis data there is -- both
    directions, several settle times, deliberately controlled -- so it belongs
    in the same table as the locks, distinguishable by ``lock_source``. Mapped
    onto the existing columns rather than new ones so a single query can trend
    locks and probes together.

    ``center_move_v`` is 0: a probe puts the center back where it found it.
    ``center_offset_v`` carries the largest excursion measured, which is the
    number that decides whether a guarded move is needed at all.
    """
    samples = list(probe.get("samples") or [])
    offsets = [
        float(item["offset_v"])
        for item in samples
        if item.get("offset_v") is not None
    ]
    worst = max(offsets, key=abs) if offsets else None
    start_voltage = probe.get("start_voltage")
    return {
        "enabled": True,
        "accepted": probe.get("verdict") not in (None, "inconclusive"),
        "target_voltage": probe.get("target_voltage"),
        "commanded_voltage": start_voltage,
        "start_voltage": start_voltage,
        "center_move_v": 0.0,
        "center_correction_v": 0.0,
        "center_offset_v": worst,
        "capture_tolerance_v": probe.get("capture_tolerance_v"),
        "rejection_bound_v": None,
        "attempts": samples,
    }
