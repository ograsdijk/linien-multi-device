"""Auto-lock target detection and calibration.

Units
-----
Amplitude thresholds and measured excursions are in **raw linien units** — exactly the
values the device returns for the (combined) error and monitor signals, with no division
or normalization. Only sweep-axis quantities use volts: ``half_range_sweep_v`` is a window
width on the sweep voltage (x) axis, and ``target_voltage`` / ``sweep_*_v`` /
``sideband_offset_v`` are sweep volts. ``symmetry_min`` is a dimensionless ratio; ``hz_per_v``
is Hz per sweep volt.

Defaults for the amplitude thresholds are rough placeholders; per-device **calibration**
(``calibrate_auto_lock_settings``) measures a known-good trace and sets the real values.

The error signal is a (possibly PDH) dispersive signal: a central carrier zero-crossing with
two lobes, and — in PDH mode — two opposite-slope sideband crossings at ±Ω (the modulation
frequency). The monitor, when present, is a photodiode level (transmission peak or reflection
dip) that confirms the lock sits on the right feature.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

# Weight of the monitor level in the candidate score (a strong tie-breaker among
# crossings that already pass the gates). Internal; not a user setting.
_MONITOR_SCORE_WEIGHT = 0.5


@dataclass
class AutoLockScanSettings:
    # Qualitative — set by the user per device:
    signal_type: str = "pdh"  # "pdh" (carrier + ±Ω sidebands) | "dispersive"
    allow_single_side: bool = False
    use_monitor: bool = False
    monitor_mode: str = "locked_above"  # "locked_above" (PD peak) | "locked_below" (PD dip)
    # Quantitative — calibration learns these (defaults are rough placeholders):
    half_range_sweep_v: float = 0.08  # lobe-measurement window half-width, sweep volts (x)
    error_min: float = 600.0  # min feature peak-to-peak, raw linien
    symmetry_min: float = 0.2  # min weaker/stronger lobe ratio (dimensionless)
    single_error_min: float = 600.0  # min stronger single lobe, raw linien
    min_amplitude: float = 100.0  # whole-trace dead-signal floor, raw linien
    smooth_window_pts: int = 5
    monitor_threshold: float = 1000.0  # monitor (PD) level at the lock point, raw linien (+)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "AutoLockScanSettings":
        if payload is None:
            return cls()
        defaults = cls()
        values: dict[str, Any] = {}
        for name in defaults.__dataclass_fields__.keys():
            values[name] = payload[name] if name in payload else getattr(defaults, name)
        return cls(**values)


@dataclass
class AutoLockScanResult:
    target_index: int
    target_voltage: float
    target_slope_rising: bool
    score: float
    left_excursion: float
    right_excursion: float
    pair_excursion: float
    symmetry: float
    monitor_level: float | None
    hz_per_v: float | None
    sideband_offset_v: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_index": self.target_index,
            "target_voltage": self.target_voltage,
            "target_slope_rising": self.target_slope_rising,
            "score": self.score,
            "left_excursion": self.left_excursion,
            "right_excursion": self.right_excursion,
            "pair_excursion": self.pair_excursion,
            "symmetry": self.symmetry,
            "monitor_level": self.monitor_level,
            "hz_per_v": self.hz_per_v,
            "sideband_offset_v": self.sideband_offset_v,
        }


@dataclass
class _Candidate:
    index: int
    index_float: float
    score: float
    target_slope_rising: bool
    left_excursion: float
    right_excursion: float
    pair_excursion: float
    symmetry: float
    monitor_level: float | None


def _sanitize_trace(values: np.ndarray) -> np.ndarray:
    return np.nan_to_num(values.astype(float), nan=0.0, posinf=0.0, neginf=0.0)


def _moving_average(values: np.ndarray, window_pts: int) -> np.ndarray:
    width = int(round(window_pts))
    if width <= 1:
        return values
    if width % 2 == 0:
        width += 1
    kernel = np.ones(width, dtype=float) / float(width)
    return np.convolve(values, kernel, mode="same")


def _half_range_to_points(
    half_range_sweep_v: float,
    n_points: int,
    sweep_amplitude_v: float,
) -> int:
    if n_points < 8:
        return 2
    amplitude = abs(float(sweep_amplitude_v))
    if amplitude <= 1e-9:
        return max(2, min(24, n_points // 16))
    span_v = 2.0 * amplitude
    points_per_v = float(n_points - 1) / span_v
    points = int(round(max(0.001, float(half_range_sweep_v)) * points_per_v))
    return max(2, min(points, (n_points // 2) - 1))


def _extract_crossing_candidates(error_trace_v: np.ndarray) -> list[int]:
    candidates: list[int] = []
    n = len(error_trace_v)
    for idx in range(n - 1):
        left = float(error_trace_v[idx])
        right = float(error_trace_v[idx + 1])
        if left == 0.0 and right == 0.0:
            continue
        if left == 0.0:
            candidates.append(idx)
            continue
        if right == 0.0:
            candidates.append(idx + 1)
            continue
        if (left < 0.0 <= right) or (left > 0.0 >= right):
            candidates.append(idx if abs(left) <= abs(right) else idx + 1)

    if not candidates:
        near_zero = np.argsort(np.abs(error_trace_v))[:64]
        candidates = [int(idx) for idx in near_zero if 1 <= int(idx) < (n - 1)]

    # preserve order while deduplicating
    seen: set[int] = set()
    unique: list[int] = []
    for idx in candidates:
        if idx in seen:
            continue
        seen.add(idx)
        unique.append(idx)
    return unique


def _index_to_voltage(index: float, n_points: int, sweep_center_v: float, sweep_amplitude_v: float) -> float:
    if n_points <= 1:
        return float(sweep_center_v)
    minimum = float(sweep_center_v) - float(sweep_amplitude_v)
    maximum = float(sweep_center_v) + float(sweep_amplitude_v)
    fraction = float(index) / float(n_points - 1)
    return minimum + fraction * (maximum - minimum)


def _estimate_crossing(
    error_trace_v: np.ndarray,
    center_idx: int,
) -> float:
    """Sub-sample position of the zero crossing nearest ``center_idx``.

    Linearly interpolates within a sign-change bracket; falls back to ``center_idx``
    when there is no bracket (degenerate near-zero candidate)."""
    n_points = len(error_trace_v)
    if center_idx < 0 or center_idx >= n_points:
        return float(center_idx)

    bracket_positions: list[float] = []
    for left_idx, right_idx in ((center_idx - 1, center_idx), (center_idx, center_idx + 1)):
        if left_idx < 0 or right_idx >= n_points:
            continue
        left = float(error_trace_v[left_idx])
        right = float(error_trace_v[right_idx])
        if left == 0.0:
            bracket_positions.append(float(left_idx))
            continue
        if right == 0.0:
            bracket_positions.append(float(right_idx))
            continue
        if left * right < 0.0:
            fraction = -left / (right - left)
            fraction = max(0.0, min(1.0, float(fraction)))
            bracket_positions.append(float(left_idx) + fraction)

    if bracket_positions:
        return min(bracket_positions, key=lambda item: abs(item - float(center_idx)))
    return float(center_idx)


def _sideband_offset_pts(
    error: np.ndarray, anchor: int, carrier_rising: bool
) -> float | None:
    """Mean distance (in samples) from the carrier crossing to the nearest
    opposite-slope (sideband) crossings on each side.

    In PDH the ±Ω sideband error features cross zero exactly Ω from the carrier and
    with the opposite slope, so this distance is the sample-equivalent of Ω."""
    n = len(error)
    left_idx: int | None = None
    right_idx: int | None = None
    for i in range(n - 1):
        a = float(error[i])
        b = float(error[i + 1])
        rising = (a < 0.0 <= b) or (a <= 0.0 < b)
        falling = (a > 0.0 >= b) or (a >= 0.0 > b)
        if not (rising or falling):
            continue
        if rising == carrier_rising:
            continue  # same slope as carrier -> not a sideband
        idx = i if abs(a) <= abs(b) else i + 1
        if idx < anchor:
            if left_idx is None or idx > left_idx:
                left_idx = idx  # nearest on the left
        elif idx > anchor:
            if right_idx is None or idx < right_idx:
                right_idx = idx  # nearest on the right

    offsets = []
    if left_idx is not None and (anchor - left_idx) > 0:
        offsets.append(anchor - left_idx)
    if right_idx is not None and (right_idx - anchor) > 0:
        offsets.append(right_idx - anchor)
    if not offsets:
        return None
    return float(sum(offsets)) / float(len(offsets))


def find_auto_lock_target(
    *,
    error_trace_v: np.ndarray,
    monitor_trace_v: np.ndarray | None,
    sweep_center_v: float,
    sweep_amplitude_v: float,
    settings: AutoLockScanSettings,
    preferred_slope_rising: bool | None = None,
    modulation_frequency_hz: float | None = None,
) -> AutoLockScanResult:
    if error_trace_v is None:
        raise ValueError("No error trace available.")

    error_raw = _sanitize_trace(np.asarray(error_trace_v, dtype=float))
    n_points = len(error_raw)
    if n_points < 16:
        raise ValueError("Trace is too short for auto-lock detection.")

    monitor_raw: np.ndarray | None = None
    if monitor_trace_v is not None:
        monitor_raw = _sanitize_trace(np.asarray(monitor_trace_v, dtype=float))
        if len(monitor_raw) != n_points:
            monitor_raw = None

    error = _moving_average(error_raw, settings.smooth_window_pts)
    monitor = (
        _moving_average(monitor_raw, settings.smooth_window_pts)
        if monitor_raw is not None
        else None
    )

    # Whole-trace dead-signal floor: is there any signal at all?
    amplitude_pp = float(np.max(error) - np.min(error))
    if amplitude_pp < float(settings.min_amplitude):
        raise ValueError(
            "No lockable signal: trace peak-to-peak is below min_amplitude "
            f"({amplitude_pp:.1f} < {float(settings.min_amplitude):.1f})."
        )

    # Monitor is optional: only used when a real monitor trace exists AND it's enabled.
    # Absent/disabled -> lock on the error signal alone (no error raised).
    use_monitor = bool(settings.use_monitor) and monitor is not None
    monitor_mode = str(settings.monitor_mode)

    half_range_pts = _half_range_to_points(
        settings.half_range_sweep_v, n_points, sweep_amplitude_v
    )
    candidates = _extract_crossing_candidates(error)
    if not candidates:
        raise ValueError("No valid zero-crossing candidates found.")

    accepted: list[_Candidate] = []
    slope_hint_rejects = 0
    monitor_rejects = 0
    for center_idx in candidates:
        crossing_idx_float = _estimate_crossing(error, center_idx)
        anchor_idx = int(round(crossing_idx_float))
        if anchor_idx <= 0 or anchor_idx >= (n_points - 1):
            continue

        left_start = max(0, anchor_idx - half_range_pts)
        right_end = min(n_points, anchor_idx + 1 + half_range_pts)
        if (anchor_idx - left_start) < 2 or (right_end - (anchor_idx + 1)) < 2:
            continue

        # Shared with calibration via _excursions_for_slope so the two paths measure
        # excursions identically and cannot drift.
        rising_left, rising_right = _excursions_for_slope(
            error, anchor_idx, half_range_pts, True
        )
        falling_left, falling_right = _excursions_for_slope(
            error, anchor_idx, half_range_pts, False
        )

        if preferred_slope_rising is None:
            target_slope_rising = (rising_left + rising_right) >= (
                falling_left + falling_right
            )
        else:
            target_slope_rising = bool(preferred_slope_rising)

        if target_slope_rising:
            left_excursion = rising_left
            right_excursion = rising_right
        else:
            left_excursion = falling_left
            right_excursion = falling_right

        stronger = max(left_excursion, right_excursion)
        weaker = min(left_excursion, right_excursion)
        pair_excursion = left_excursion + right_excursion
        symmetry = weaker / stronger if stronger > 1e-12 else 0.0

        paired_ok = (
            pair_excursion >= float(settings.error_min)
            and symmetry >= float(settings.symmetry_min)
        )
        single_ok = (
            bool(settings.allow_single_side)
            and stronger >= float(settings.single_error_min)
        )
        if not (paired_ok or single_ok):
            if preferred_slope_rising is not None:
                opp_left = falling_left if target_slope_rising else rising_left
                opp_right = falling_right if target_slope_rising else rising_right
                opp_stronger = max(opp_left, opp_right)
                opp_weaker = min(opp_left, opp_right)
                opp_pair = opp_left + opp_right
                opp_symmetry = (
                    opp_weaker / opp_stronger if opp_stronger > 1e-12 else 0.0
                )
                if (
                    opp_pair >= float(settings.error_min)
                    and opp_symmetry >= float(settings.symmetry_min)
                ) or (
                    bool(settings.allow_single_side)
                    and opp_stronger >= float(settings.single_error_min)
                ):
                    slope_hint_rejects += 1
            continue

        monitor_level: float | None = None
        if monitor is not None:
            window = monitor[left_start:right_end]
            if len(window):
                monitor_level = float(np.mean(window))

        if use_monitor:
            level = monitor_level if monitor_level is not None else 0.0
            if monitor_mode == "locked_below":
                if level > float(settings.monitor_threshold):
                    monitor_rejects += 1
                    continue
            else:  # locked_above
                if level < float(settings.monitor_threshold):
                    monitor_rejects += 1
                    continue

        score = _candidate_score(pair_excursion, weaker)
        if use_monitor and monitor_level is not None:
            # Prefer the candidate whose monitor most strongly matches the mode.
            signed = -monitor_level if monitor_mode == "locked_below" else monitor_level
            score += _MONITOR_SCORE_WEIGHT * signed

        accepted.append(
            _Candidate(
                index=int(anchor_idx),
                index_float=float(crossing_idx_float),
                score=float(score),
                target_slope_rising=target_slope_rising,
                left_excursion=float(left_excursion),
                right_excursion=float(right_excursion),
                pair_excursion=float(pair_excursion),
                symmetry=float(symmetry),
                monitor_level=(
                    float(monitor_level) if monitor_level is not None else None
                ),
            )
        )

    if not accepted:
        if slope_hint_rejects > 0:
            raise ValueError(
                "No valid crossing passed thresholds for the current target slope. "
                "Try toggling Target slope (rising/falling) or shifting demodulation phase by 180 degrees."
            )
        if use_monitor and monitor_rejects > 0:
            raise ValueError(
                "No valid crossing passed thresholds because the monitor level did not meet monitor_threshold."
            )
        raise ValueError("No valid crossing passed the configured thresholds.")

    best = max(accepted, key=lambda item: item.score)

    sideband_offset_v: float | None = None
    hz_per_v: float | None = None
    if str(settings.signal_type) == "pdh" and modulation_frequency_hz:
        off_pts = _sideband_offset_pts(error, best.index, best.target_slope_rising)
        if off_pts and off_pts > 0 and n_points > 1:
            sideband_offset_v = float(off_pts) * (
                2.0 * abs(float(sweep_amplitude_v)) / (n_points - 1)
            )
            if sideband_offset_v > 1e-12:
                hz_per_v = float(modulation_frequency_hz) / sideband_offset_v
            else:
                sideband_offset_v = None

    target_voltage = _index_to_voltage(
        best.index_float,
        n_points,
        float(sweep_center_v),
        float(sweep_amplitude_v),
    )
    return AutoLockScanResult(
        target_index=best.index,
        target_voltage=float(target_voltage),
        target_slope_rising=bool(best.target_slope_rising),
        score=float(best.score),
        left_excursion=float(best.left_excursion),
        right_excursion=float(best.right_excursion),
        pair_excursion=float(best.pair_excursion),
        symmetry=float(best.symmetry),
        monitor_level=(
            float(best.monitor_level) if best.monitor_level is not None else None
        ),
        hz_per_v=(float(hz_per_v) if hz_per_v is not None else None),
        sideband_offset_v=(
            float(sideband_offset_v) if sideband_offset_v is not None else None
        ),
    )


# ---------------------------------------------------------------------------
# Calibration: derive auto-lock settings from a known-good error trace.
#
# The user manually sweeps and centers a good error signal and sets the qualitative
# settings (signal_type, use_monitor, monitor_mode, allow_single_side); calibration
# measures the dominant dispersive crossing the same way find_auto_lock_target selects
# it, then sets the quantitative thresholds as multipliers of what the reference showed,
# and finally re-runs find_auto_lock_target with the derived settings as a self-check so
# we never persist settings that would not lock.
#
# Units: raw linien values (no normalization); see the module docstring.
# ---------------------------------------------------------------------------


@dataclass
class AutoLockCalibrationFactors:
    error_min_factor: float = 0.5
    single_error_min_factor: float = 0.5
    half_range_margin: float = 1.3
    symmetry_margin: float = 0.7
    # min_amplitude (the live dead-trace floor) as a fraction of the feature's
    # measured peak-to-peak — a dead/noise trace won't have a feature this tall.
    min_amplitude_factor: float = 0.3
    # monitor_threshold placed this fraction of the way from the off-resonance
    # baseline to the on-resonance level (0.5 = midpoint).
    monitor_threshold_factor: float = 0.5


@dataclass
class AutoLockCalibration:
    settings: AutoLockScanSettings
    amplitude: float
    feature_half_width_v: float
    target_index: int
    target_voltage: float
    target_slope_rising: bool
    symmetry: float
    monitor_level: float | None
    hz_per_v: float | None
    detail: str


def _clamp(value: float, lo: float, hi: float) -> float:
    return float(min(max(float(value), lo), hi))


def _candidate_score(pair_excursion: float, weaker: float) -> float:
    """Crossing-quality score shared by detection and calibration so the two never
    rank crossings differently (the monitor term is added by the detector on top)."""
    return pair_excursion + (0.25 * weaker)


def _excursions_for_slope(
    error: np.ndarray, anchor: int, window_pts: int, slope_rising: bool
) -> tuple[float, float]:
    left_start = max(0, anchor - window_pts)
    right_end = min(len(error), anchor + 1 + window_pts)
    left = error[left_start:anchor]
    right = error[anchor + 1 : right_end]
    if slope_rising:
        left_exc = max(0.0, -float(np.min(left))) if len(left) else 0.0
        right_exc = max(0.0, float(np.max(right))) if len(right) else 0.0
    else:
        left_exc = max(0.0, float(np.max(left))) if len(left) else 0.0
        right_exc = max(0.0, -float(np.min(right))) if len(right) else 0.0
    return left_exc, right_exc


def _slope_and_excursions(
    error: np.ndarray,
    anchor: int,
    window_pts: int,
    preferred_slope_rising: bool | None,
) -> tuple[bool, float, float]:
    rising_left, rising_right = _excursions_for_slope(error, anchor, window_pts, True)
    falling_left, falling_right = _excursions_for_slope(error, anchor, window_pts, False)
    if preferred_slope_rising is None:
        rising = (rising_left + rising_right) >= (falling_left + falling_right)
    else:
        rising = bool(preferred_slope_rising)
    if rising:
        return True, rising_left, rising_right
    return False, falling_left, falling_right


def _lobe_offset(
    error: np.ndarray,
    anchor: int,
    step: int,
    max_pts: int,
    lobe_positive: bool,
    fallback: int,
) -> int:
    """Offset to the *nearest* dispersive lobe peak walking ``step`` from anchor.

    Tracks the running magnitude and stops at the first turning point (the lobe
    peak closest to the crossing) so a distant baseline/offset cannot win the
    extremum. An opposite-sign prefix right at the crossing is skipped rather
    than treated as the end of the lobe. If the walk is monotonic (e.g. a
    baseline tilt with no clear local peak), the far extremum is untrustworthy,
    so we fall back to a moderate width instead of over-reporting.
    """
    n = len(error)
    best_mag = 0.0
    best_idx = anchor
    entered = False
    found_peak = False
    idx = anchor + step
    steps = 0
    while 0 <= idx < n and steps < max_pts:
        v = float(error[idx])
        in_lobe = (v > 0.0) if lobe_positive else (v < 0.0)
        if in_lobe:
            entered = True
            mag = v if lobe_positive else -v
            if mag > best_mag:
                best_mag = mag
                best_idx = idx
            elif best_mag > 0.0 and mag < best_mag:
                found_peak = True  # magnitude fell off the lobe peak
                break
        elif entered:
            found_peak = True  # returned through zero past the lobe
            break
        idx += step
        steps += 1

    off = abs(best_idx - anchor)
    if not found_peak:
        # Monotonic walk: don't trust the far extremum.
        return min(off, fallback) if off else fallback
    return off


def _peak_offsets(
    error: np.ndarray, anchor: int, max_pts: int, slope_rising: bool
) -> tuple[int, int]:
    """Offsets from ``anchor`` to the nearest left/right dispersive lobe peaks.

    ``max_pts`` is a generous cap (so wide features aren't truncated); the real
    bound is the lobe's own turning point found by :func:`_lobe_offset`.
    """
    fallback = max(2, len(error) // 8)
    # Rising slope -> negative left lobe, positive right lobe.
    left_off = _lobe_offset(
        error, anchor, -1, max_pts, lobe_positive=not slope_rising, fallback=fallback
    )
    right_off = _lobe_offset(
        error, anchor, 1, max_pts, lobe_positive=slope_rising, fallback=fallback
    )
    return max(0, left_off), max(0, right_off)


def calibrate_auto_lock_settings(
    *,
    error_trace_v: np.ndarray,
    monitor_trace_v: np.ndarray | None,
    sweep_center_v: float,
    sweep_amplitude_v: float,
    base: AutoLockScanSettings,
    preferred_slope_rising: bool | None = None,
    include_monitor: bool = False,
    allow_single_side: bool = False,
    modulation_frequency_hz: float | None = None,
    factors: AutoLockCalibrationFactors | None = None,
) -> AutoLockCalibration:
    """Derive a full ``AutoLockScanSettings`` from a known-good error trace."""
    factors = factors or AutoLockCalibrationFactors()

    if error_trace_v is None:
        raise ValueError("No error trace available.")
    error_raw = _sanitize_trace(np.asarray(error_trace_v, dtype=float))
    n_points = len(error_raw)
    if n_points < 16:
        raise ValueError("Trace is too short for calibration.")

    monitor_raw: np.ndarray | None = None
    if monitor_trace_v is not None:
        monitor_raw = _sanitize_trace(np.asarray(monitor_trace_v, dtype=float))
        if len(monitor_raw) != n_points:
            monitor_raw = None
    if include_monitor and monitor_raw is None:
        raise ValueError("Monitor signal requested but no monitor trace available.")

    error = _moving_average(error_raw, base.smooth_window_pts)
    monitor = (
        _moving_average(monitor_raw, base.smooth_window_pts)
        if monitor_raw is not None
        else None
    )

    # Peak-to-peak amplitude of the (smoothed) trace. True min/max, not percentiles:
    # a sharp feature occupies few points, so p2/p98 would clip it to ~0.
    amplitude_pp = max(0.0, float(np.max(error) - np.min(error)))
    if amplitude_pp <= 1e-9:
        raise ValueError(
            "No signal detected on the current trace. Center a good error signal "
            "before calibrating."
        )

    candidates = _extract_crossing_candidates(error)
    if not candidates:
        raise ValueError("No zero-crossing found on the current trace.")

    coarse_pts = max(4, min(n_points // 8, (n_points // 2) - 1))

    best_score: float | None = None
    best_anchor = 0
    best_crossing_idx = 0.0
    best_slope = True
    for center_idx in candidates:
        crossing_idx_float = _estimate_crossing(error, center_idx)
        anchor = int(round(crossing_idx_float))
        if anchor <= 0 or anchor >= (n_points - 1):
            continue
        slope, left_exc, right_exc = _slope_and_excursions(
            error, anchor, coarse_pts, preferred_slope_rising
        )
        pair = left_exc + right_exc
        if pair <= 0.0:
            continue
        # Rank by the same objective find_auto_lock_target uses, so the calibrated
        # anchor matches the locked one.
        weaker = min(left_exc, right_exc)
        score = _candidate_score(pair, weaker)
        if best_score is None or score > best_score:
            best_score = score
            best_anchor = anchor
            best_crossing_idx = crossing_idx_float
            best_slope = slope

    if best_score is None:
        raise ValueError("No usable dispersive crossing found on the current trace.")

    anchor = best_anchor
    slope_rising = best_slope

    # Feature half-width -> half_range_sweep_v. Search out to half the trace (bounded
    # by the lobe's own zero crossing) so wide features are not truncated.
    left_off, right_off = _peak_offsets(
        error, anchor, (n_points // 2) - 1, slope_rising
    )
    half_width_pts = max(left_off, right_off, 2)
    pts_to_v = (
        2.0 * abs(float(sweep_amplitude_v)) / (n_points - 1) if n_points > 1 else 0.0
    )
    feature_half_width_v = (
        half_width_pts * pts_to_v if pts_to_v > 0.0 else float(base.half_range_sweep_v)
    )
    half_range_sweep_v = _clamp(
        factors.half_range_margin * feature_half_width_v, 0.001, 2.0
    )

    # Re-measure excursions over the derived window so the calibrated thresholds
    # match what find_auto_lock_target will later see.
    half_range_pts = _half_range_to_points(
        half_range_sweep_v, n_points, sweep_amplitude_v
    )
    left_exc, right_exc = _excursions_for_slope(
        error, anchor, half_range_pts, slope_rising
    )
    pair = left_exc + right_exc
    stronger = max(left_exc, right_exc)
    weaker = min(left_exc, right_exc)
    symmetry = weaker / stronger if stronger > 1e-12 else 0.0

    settings = dataclasses.replace(base)
    settings.half_range_sweep_v = half_range_sweep_v
    settings.error_min = _clamp(factors.error_min_factor * pair, 0.0, 1e12)
    settings.symmetry_min = _clamp(factors.symmetry_margin * symmetry, 0.0, 0.9)
    settings.min_amplitude = _clamp(factors.min_amplitude_factor * pair, 0.0, 1e12)

    settings.allow_single_side = bool(allow_single_side)
    if allow_single_side:
        settings.single_error_min = _clamp(
            factors.single_error_min_factor * stronger, 0.0, 1e12
        )

    monitor_level: float | None = None
    settings.use_monitor = False
    monitor_note = ""
    if include_monitor and monitor is not None:
        left_start = max(0, anchor - half_range_pts)
        right_end = min(n_points, anchor + 1 + half_range_pts)
        window = monitor[left_start:right_end]
        on_res = float(np.mean(window)) if len(window) else 0.0
        # Off-resonance baseline: monitor away from the feature window.
        mask = np.ones(n_points, dtype=bool)
        mask[left_start:right_end] = False
        baseline = float(np.mean(monitor[mask])) if bool(mask.any()) else on_res
        monitor_level = on_res
        if abs(on_res - baseline) > 1e-9:
            settings.use_monitor = True
            settings.monitor_mode = str(base.monitor_mode)
            settings.monitor_threshold = _clamp(
                baseline + factors.monitor_threshold_factor * (on_res - baseline),
                0.0,
                1e12,
            )
        else:
            monitor_note = " monitor shows no contrast at the feature, left disabled;"

    target_voltage = _index_to_voltage(
        best_crossing_idx, n_points, float(sweep_center_v), float(sweep_amplitude_v)
    )

    # Self-check: the calibrated settings must actually lock this trace. Run the
    # detector on the original (unsmoothed) trace; it smooths internally with
    # settings.smooth_window_pts, matching what we used above.
    try:
        check = find_auto_lock_target(
            error_trace_v=error_raw,
            monitor_trace_v=monitor_raw,
            sweep_center_v=float(sweep_center_v),
            sweep_amplitude_v=float(sweep_amplitude_v),
            settings=settings,
            preferred_slope_rising=preferred_slope_rising,
            modulation_frequency_hz=modulation_frequency_hz,
        )
    except ValueError as exc:
        raise ValueError(
            "Calibration could not converge on the trace's dominant feature "
            f"({exc}). Adjust the sweep so the good crossing is the strongest one, "
            "then calibrate again."
        ) from exc
    # "Same crossing" tolerance: a zero crossing is a sharp, single-point feature,
    # so the calibrated and detected anchors should differ by at most a few points
    # (smoothing jitter). It must NOT scale with half_range_pts.
    converge_tol = max(8, 2 * int(settings.smooth_window_pts))
    if abs(check.target_index - anchor) > converge_tol:
        raise ValueError(
            "Calibration could not converge on the trace's dominant feature "
            "(the detector selected a different crossing). Adjust the sweep so the "
            "good crossing is the strongest one, then calibrate again."
        )

    detail = (
        f"Calibrated from trace (raw linien units): amplitude={amplitude_pp:.1f}, "
        f"feature half-width={feature_half_width_v:.4f} V, target={target_voltage:.4f} V, "
        f"smooth={settings.smooth_window_pts} pts.{monitor_note}"
    )

    return AutoLockCalibration(
        settings=settings,
        amplitude=float(amplitude_pp),
        feature_half_width_v=float(feature_half_width_v),
        target_index=int(anchor),
        target_voltage=float(target_voltage),
        target_slope_rising=bool(slope_rising),
        symmetry=float(symmetry),
        monitor_level=(float(monitor_level) if monitor_level is not None else None),
        hz_per_v=(float(check.hz_per_v) if check.hz_per_v is not None else None),
        detail=detail,
    )
