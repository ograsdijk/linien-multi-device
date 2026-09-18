"""How close is close enough, and how far is a different feature.

The auto-lock refinement walk has to answer two questions about a re-detected
crossing: is it near enough to the target to lock on, and is it so far out that
it must be a NEIGHBOURING crossing rather than a displaced one? Correcting
towards a neighbour would walk the lock onto the wrong feature, so the second
question is a guard, not a detail.

Both answers scale with the calibrated feature width
(``AutoLockScanSettings.half_range_sweep_v``) rather than being configured as
absolute voltages: a lock succeeds whenever the DC point lands inside the
monotonic stretch between the two lobe extrema, and that stretch is what the
scan already measured. All voltages are sweep volts on the x-axis, the same
units as ``sweep_center``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, NamedTuple

# Fraction of the sideband offset beyond which a re-detected crossing is taken to
# BE a sideband rather than a displaced carrier. The PDH sidebands sit at +/-Omega
# from the carrier, so a feature more than this far out is nearer the sideband
# than any plausible excursion. Internal; not a user setting.
_SIDEBAND_GUARD_FRACTION = 0.4


@dataclass
class AcceptanceSettings:
    """Per-device acceptance geometry and settling time for the refinement walk."""

    # Acceptance window, as a fraction of the calibrated feature half-width. A
    # lock succeeds as long as the DC point lands inside the monotonic stretch
    # between the two lobe extrema, so the tolerance is derived from that
    # measured width rather than being a voltage somebody has to guess.
    capture_fraction: float = 0.5
    # Rejection bound, in the same units: a re-detection further out than this
    # is a NEIGHBOURING crossing, not a displaced one. Used when the scan cannot
    # resolve a sideband offset (dispersive mode).
    max_correction_span: float = 4.0
    # How long the mechanics take to stop creeping after a sweep-geometry write.
    # Both sweep axes are actuators, so the refinement walk waits this out
    # before believing the trace it reads back, and the final drift gate uses it
    # as the handover interval a measured drift is charged against.
    settle_ms: int = 300

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "AcceptanceSettings":
        if payload is None:
            return cls()
        defaults = cls()
        values: dict[str, Any] = {}
        for name in defaults.__dataclass_fields__.keys():
            values[name] = payload[name] if name in payload else getattr(defaults, name)
        return cls(**values)


def capture_tolerance_v(
    settings: AcceptanceSettings, half_range_sweep_v: float
) -> float:
    """Acceptance window for the pre-lock check, in sweep volts.

    The lock only needs the DC point to land inside the monotonic region between
    the two lobe extrema, so the window scales with the calibrated feature width
    rather than being configured as an absolute voltage.
    """
    width = abs(float(half_range_sweep_v))
    return max(0.0, float(settings.capture_fraction)) * width


def rejection_bound_v(
    settings: AcceptanceSettings,
    half_range_sweep_v: float,
    sideband_offset_v: float | None = None,
) -> float:
    """Offset beyond which a re-detection is a DIFFERENT crossing, not a moved one.

    Correcting towards a neighbouring feature would walk the lock onto the wrong
    one, so an offset past this bound aborts instead of being corrected. When the
    scan resolved the PDH sideband spacing, that is the physical bound;
    otherwise fall back to a multiple of the calibrated feature width.

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
    settings: AcceptanceSettings,
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
