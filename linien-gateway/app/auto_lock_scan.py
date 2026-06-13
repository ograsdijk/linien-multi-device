from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np


@dataclass
class AutoLockScanSettings:
    half_range_v: float = 0.08
    crossing_max_v: float = 0.03
    error_min: float = 0.08
    symmetry_min: float = 0.2
    allow_single_side: bool = False
    single_error_min: float = 0.1
    smooth_window_pts: int = 5
    use_monitor: bool = False
    monitor_contrast_min_v: float = 0.03

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "AutoLockScanSettings":
        if payload is None:
            return cls()
        defaults = cls()
        values: dict[str, Any] = {}
        for name in defaults.__dataclass_fields__.keys():
            if name not in payload:
                values[name] = getattr(defaults, name)
                continue
            values[name] = payload[name]
        return cls(**values)


@dataclass
class AutoLockScanResult:
    target_index: int
    target_voltage: float
    target_slope_rising: bool
    score: float
    center_abs_v: float
    left_excursion_v: float
    right_excursion_v: float
    pair_excursion_v: float
    symmetry: float
    monitor_contrast_v: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_index": self.target_index,
            "target_voltage": self.target_voltage,
            "target_slope_rising": self.target_slope_rising,
            "score": self.score,
            "center_abs_v": self.center_abs_v,
            "left_excursion_v": self.left_excursion_v,
            "right_excursion_v": self.right_excursion_v,
            "pair_excursion_v": self.pair_excursion_v,
            "symmetry": self.symmetry,
            "monitor_contrast_v": self.monitor_contrast_v,
        }


@dataclass
class _Candidate:
    index: int
    index_float: float
    score: float
    target_slope_rising: bool
    center_abs_v: float
    left_excursion_v: float
    right_excursion_v: float
    pair_excursion_v: float
    symmetry: float
    monitor_contrast_v: float | None


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
    half_range_v: float,
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
    points = int(round(max(0.001, float(half_range_v)) * points_per_v))
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
) -> tuple[float, float]:
    n_points = len(error_trace_v)
    if center_idx < 0 or center_idx >= n_points:
        return 0.0, float(center_idx)

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
        best = min(bracket_positions, key=lambda item: abs(item - float(center_idx)))
        # A valid sign-change bracket implies an interpolated zero crossing.
        return 0.0, float(best)

    return abs(float(error_trace_v[center_idx])), float(center_idx)


def find_auto_lock_target(
    *,
    error_trace_v: np.ndarray,
    monitor_trace_v: np.ndarray | None,
    sweep_center_v: float,
    sweep_amplitude_v: float,
    settings: AutoLockScanSettings,
    preferred_slope_rising: bool | None = None,
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
    if settings.use_monitor and monitor_raw is None:
        raise ValueError("Monitor trace unavailable while use_monitor is enabled.")

    error = _moving_average(error_raw, settings.smooth_window_pts)
    monitor = _moving_average(monitor_raw, settings.smooth_window_pts) if monitor_raw is not None else None
    half_range_pts = _half_range_to_points(
        settings.half_range_v, n_points, sweep_amplitude_v
    )
    candidates = _extract_crossing_candidates(error)
    if not candidates:
        raise ValueError("No valid zero-crossing candidates found.")

    accepted: list[_Candidate] = []
    slope_hint_rejects = 0
    center_rejects = 0
    monitor_rejects = 0
    evaluated = 0
    for center_idx in candidates:
        center_abs_v, crossing_idx_float = _estimate_crossing(error, center_idx)
        anchor_idx = int(round(crossing_idx_float))
        if anchor_idx <= 0 or anchor_idx >= (n_points - 1):
            continue
        evaluated += 1

        left_start = max(0, anchor_idx - half_range_pts)
        right_end = min(n_points, anchor_idx + 1 + half_range_pts)
        left = error[left_start:anchor_idx]
        right = error[anchor_idx + 1 : right_end]
        if len(left) < 2 or len(right) < 2:
            continue

        if center_abs_v > float(settings.crossing_max_v):
            center_rejects += 1
            continue

        # Shared with calibration via _excursions_for_slope so the two paths
        # measure excursions identically and cannot drift.
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
            left_excursion_v = rising_left
            right_excursion_v = rising_right
        else:
            left_excursion_v = falling_left
            right_excursion_v = falling_right

        stronger = max(left_excursion_v, right_excursion_v)
        weaker = min(left_excursion_v, right_excursion_v)
        pair_excursion_v = left_excursion_v + right_excursion_v
        symmetry = weaker / stronger if stronger > 1e-12 else 0.0

        paired_ok = (
            pair_excursion_v >= float(settings.error_min)
            and symmetry >= float(settings.symmetry_min)
        )
        single_ok = (
            bool(settings.allow_single_side)
            and stronger >= float(settings.single_error_min)
        )
        if not (paired_ok or single_ok):
            if preferred_slope_rising is not None:
                opposite_left = falling_left if target_slope_rising else rising_left
                opposite_right = falling_right if target_slope_rising else rising_right
                opposite_pair = opposite_left + opposite_right
                opposite_stronger = max(opposite_left, opposite_right)
                opposite_weaker = min(opposite_left, opposite_right)
                opposite_symmetry = (
                    opposite_weaker / opposite_stronger
                    if opposite_stronger > 1e-12
                    else 0.0
                )
                opposite_paired_ok = (
                    opposite_pair >= float(settings.error_min)
                    and opposite_symmetry >= float(settings.symmetry_min)
                )
                opposite_single_ok = (
                    bool(settings.allow_single_side)
                    and opposite_stronger >= float(settings.single_error_min)
                )
                if opposite_paired_ok or opposite_single_ok:
                    slope_hint_rejects += 1
            continue

        monitor_contrast_v: float | None = None
        if monitor is not None:
            left_monitor = monitor[left_start:anchor_idx]
            right_monitor = monitor[anchor_idx + 1 : right_end]
            if len(left_monitor) > 0 and len(right_monitor) > 0:
                monitor_contrast_v = abs(
                    float(np.mean(right_monitor) - np.mean(left_monitor))
                )

        if settings.use_monitor:
            contrast = monitor_contrast_v or 0.0
            if contrast < float(settings.monitor_contrast_min_v):
                monitor_rejects += 1
                continue

        score = _candidate_score(pair_excursion_v, weaker, center_abs_v)
        if settings.use_monitor and monitor_contrast_v is not None:
            score += 0.2 * monitor_contrast_v

        accepted.append(
            _Candidate(
                index=int(anchor_idx),
                index_float=float(crossing_idx_float),
                score=float(score),
                target_slope_rising=target_slope_rising,
                center_abs_v=float(center_abs_v),
                left_excursion_v=float(left_excursion_v),
                right_excursion_v=float(right_excursion_v),
                pair_excursion_v=float(pair_excursion_v),
                symmetry=float(symmetry),
                monitor_contrast_v=(
                    float(monitor_contrast_v)
                    if monitor_contrast_v is not None
                    else None
                ),
            )
        )

    if not accepted:
        if slope_hint_rejects > 0:
            raise ValueError(
                "No valid PDH-like crossing passed thresholds for the current target slope. "
                "Try toggling Target slope (rising/falling) or shifting demodulation phase by 180 degrees."
            )
        if evaluated > 0 and center_rejects >= max(1, evaluated // 2):
            raise ValueError(
                "No valid PDH-like crossing passed thresholds because crossing_max_v is too strict for this scan sampling. "
                "Increase crossing_max_v or reduce sweep span."
            )
        if settings.use_monitor and monitor_rejects > 0:
            raise ValueError(
                "No valid PDH-like crossing passed thresholds because monitor contrast was below monitor_contrast_min_v."
            )
        raise ValueError("No valid PDH-like crossing passed the configured thresholds.")

    best = max(accepted, key=lambda item: item.score)
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
        center_abs_v=float(best.center_abs_v),
        left_excursion_v=float(best.left_excursion_v),
        right_excursion_v=float(best.right_excursion_v),
        pair_excursion_v=float(best.pair_excursion_v),
        symmetry=float(best.symmetry),
        monitor_contrast_v=(
            float(best.monitor_contrast_v)
            if best.monitor_contrast_v is not None
            else None
        ),
    )


# ---------------------------------------------------------------------------
# Calibration: derive auto-lock settings from a known-good PDH error trace.
#
# The user manually sweeps and centers a good PDH error signal, then asks the
# gateway to learn from that exact trace. We measure the dominant dispersive
# crossing the same way ``find_auto_lock_target`` selects it, then back out
# every threshold as a fraction of what the reference actually showed, and
# finally re-run ``find_auto_lock_target`` with the derived settings as a
# self-check so we never persist settings that would not lock. Optional
# features (monitor contrast, single-side acceptance) are only populated when
# the user opts into them.
#
# Units: the error signal here is the demodulated PDH signal normalised to
# full scale (-1..+1), not volts. The ``_v`` suffix on these fields is a
# pre-existing misnomer kept for API/config compatibility.
# ---------------------------------------------------------------------------


@dataclass
class AutoLockCalibrationFactors:
    error_min_frac: float = 0.5
    single_error_min_frac: float = 0.5
    crossing_max_frac: float = 0.1
    half_range_margin: float = 1.3
    symmetry_margin: float = 0.7
    monitor_contrast_frac: float = 0.5
    # Enable the monitor gate only when left/right contrast exceeds this
    # fraction of the monitor's own peak-to-peak amplitude (a symmetric monitor
    # has ~zero contrast and must be left disabled).
    monitor_floor_frac: float = 0.1
    # Dead-trace guard in normalised full-scale units (~noise level). The
    # self-check is the real "is this a lockable feature?" gate.
    min_amplitude_v: float = 0.01


@dataclass
class AutoLockCalibration:
    settings: AutoLockScanSettings
    amplitude_v: float
    feature_half_width_v: float
    target_index: int
    target_voltage: float
    target_slope_rising: bool
    symmetry: float
    monitor_contrast_v: float | None
    detail: str


def _clamp(value: float, lo: float, hi: float) -> float:
    return float(min(max(float(value), lo), hi))


def _candidate_score(pair_excursion_v: float, weaker: float, center_abs_v: float) -> float:
    """Base crossing-quality score shared by detection and calibration so the
    two never rank crossings differently (the monitor bonus is added by the
    detector on top of this)."""
    return pair_excursion_v + (0.25 * weaker) - (2.0 * center_abs_v)


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

    # Peak-to-peak amplitude of the (smoothed) trace. Use true min/max, not
    # percentiles: a sharp feature occupies few points, so p2/p98 would clip it
    # to ~0 and wrongly report "no signal". Smoothing already tames spikes.
    amplitude_pp = max(0.0, float(np.max(error) - np.min(error)))
    if amplitude_pp < float(factors.min_amplitude_v):
        raise ValueError(
            "No PDH-like signal detected on the current trace (amplitude below "
            "noise floor). Center a good error signal before calibrating."
        )

    candidates = _extract_crossing_candidates(error)
    if not candidates:
        raise ValueError("No zero-crossing found on the current trace.")

    coarse_pts = max(4, min(n_points // 8, (n_points // 2) - 1))

    best_score: float | None = None
    best_anchor = 0
    best_crossing_idx = 0.0
    best_slope = True
    best_center_abs_v = 0.0
    for center_idx in candidates:
        center_abs_v, crossing_idx_float = _estimate_crossing(error, center_idx)
        anchor = int(round(crossing_idx_float))
        if anchor <= 0 or anchor >= (n_points - 1):
            continue
        slope, left_exc, right_exc = _slope_and_excursions(
            error, anchor, coarse_pts, preferred_slope_rising
        )
        pair = left_exc + right_exc
        if pair <= 0.0:
            continue
        # Select the same crossing find_auto_lock_target would: rank by its
        # score objective (pair + 0.25*weaker - 2*center_abs_v), not by a
        # different key, so the calibrated anchor matches the locked one.
        weaker = min(left_exc, right_exc)
        score = _candidate_score(pair, weaker, center_abs_v)
        if best_score is None or score > best_score:
            best_score = score
            best_anchor = anchor
            best_crossing_idx = crossing_idx_float
            best_slope = slope
            best_center_abs_v = center_abs_v

    if best_score is None:
        raise ValueError("No usable dispersive crossing found on the current trace.")

    anchor = best_anchor
    slope_rising = best_slope

    # Feature half-width -> half_range_v. Search out to half the trace (bounded
    # by the lobe's own zero crossing) so wide features are not truncated.
    left_off, right_off = _peak_offsets(
        error, anchor, (n_points // 2) - 1, slope_rising
    )
    half_width_pts = max(left_off, right_off, 2)
    pts_to_v = (
        2.0 * abs(float(sweep_amplitude_v)) / (n_points - 1) if n_points > 1 else 0.0
    )
    feature_half_width_v = (
        half_width_pts * pts_to_v if pts_to_v > 0.0 else float(base.half_range_v)
    )
    half_range_v = _clamp(factors.half_range_margin * feature_half_width_v, 0.001, 2.0)

    # Re-measure excursions over the derived window so the calibrated
    # thresholds match what find_auto_lock_target will later see.
    half_range_pts = _half_range_to_points(half_range_v, n_points, sweep_amplitude_v)
    left_exc, right_exc = _excursions_for_slope(
        error, anchor, half_range_pts, slope_rising
    )
    pair = left_exc + right_exc
    stronger = max(left_exc, right_exc)
    weaker = min(left_exc, right_exc)
    symmetry = weaker / stronger if stronger > 1e-12 else 0.0

    settings = dataclasses.replace(base)
    settings.half_range_v = half_range_v
    settings.error_min = _clamp(factors.error_min_frac * pair, 0.0001, 4.0)
    # Derive from amplitude, but never below the chosen anchor's own residual,
    # so find_auto_lock_target cannot reject the very crossing we calibrated to.
    settings.crossing_max_v = _clamp(
        max(factors.crossing_max_frac * amplitude_pp, best_center_abs_v * 1.5),
        0.0001,
        2.0,
    )
    settings.symmetry_min = _clamp(factors.symmetry_margin * symmetry, 0.0, 0.9)

    settings.allow_single_side = bool(allow_single_side)
    if allow_single_side:
        settings.single_error_min = _clamp(
            factors.single_error_min_frac * stronger, 0.0001, 4.0
        )

    monitor_contrast_v: float | None = None
    settings.use_monitor = False
    monitor_note = ""
    if include_monitor and monitor is not None:
        left_start = max(0, anchor - half_range_pts)
        right_end = min(n_points, anchor + 1 + half_range_pts)
        left_monitor = monitor[left_start:anchor]
        right_monitor = monitor[anchor + 1 : right_end]
        if len(left_monitor) and len(right_monitor):
            monitor_contrast_v = abs(
                float(np.mean(right_monitor) - np.mean(left_monitor))
            )
        # Only enable the monitor gate when contrast is real relative to the
        # monitor's amplitude *in the same local window*; a symmetric monitor
        # (≈zero contrast), or a globally-large-but-locally-flat one, would
        # otherwise get a misleading threshold. Measuring locally keeps the
        # gate decision consistent with where the lock actually sits.
        window_monitor = monitor[left_start:right_end]
        monitor_pp = (
            max(0.0, float(np.max(window_monitor) - np.min(window_monitor)))
            if len(window_monitor)
            else 0.0
        )
        if (
            monitor_contrast_v is not None
            and monitor_contrast_v > factors.monitor_floor_frac * monitor_pp
            and monitor_contrast_v > 0.0
        ):
            settings.use_monitor = True
            settings.monitor_contrast_min_v = _clamp(
                factors.monitor_contrast_frac * monitor_contrast_v, 0.0001, 4.0
            )
        else:
            monitor_note = " monitor contrast too low to use, left disabled;"

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
        )
    except ValueError as exc:
        raise ValueError(
            "Calibration could not converge on the trace's dominant feature "
            f"({exc}). Adjust the sweep so the good PDH crossing is the "
            "strongest one, then calibrate again."
        ) from exc
    # "Same crossing" tolerance: a zero crossing is a sharp, single-point
    # feature, so the calibrated and detected anchors should differ by at most
    # a few points (smoothing jitter). It must NOT scale with half_range_pts —
    # a wide feature would otherwise make the guard accept a different crossing.
    converge_tol = max(8, 2 * int(settings.smooth_window_pts))
    if abs(check.target_index - anchor) > converge_tol:
        raise ValueError(
            "Calibration could not converge on the trace's dominant feature "
            "(the detector selected a different crossing). Adjust the sweep so "
            "the good PDH crossing is the strongest one, then calibrate again."
        )

    detail = (
        f"Calibrated from trace (normalised full-scale): "
        f"amplitude={amplitude_pp:.4f}, "
        f"feature half-width={feature_half_width_v:.4f}, "
        f"target={target_voltage:.4f}, smooth={settings.smooth_window_pts} pts."
        f"{monitor_note}"
    )

    return AutoLockCalibration(
        settings=settings,
        amplitude_v=float(amplitude_pp),
        feature_half_width_v=float(feature_half_width_v),
        target_index=int(anchor),
        target_voltage=float(target_voltage),
        target_slope_rising=bool(slope_rising),
        symmetry=float(symmetry),
        monitor_contrast_v=(
            float(monitor_contrast_v) if monitor_contrast_v is not None else None
        ),
        detail=detail,
    )
