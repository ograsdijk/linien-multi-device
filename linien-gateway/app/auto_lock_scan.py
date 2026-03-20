from __future__ import annotations

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

        rising_left = max(0.0, -float(np.min(left)))
        rising_right = max(0.0, float(np.max(right)))
        falling_left = max(0.0, float(np.max(left)))
        falling_right = max(0.0, -float(np.min(right)))
        rising_pair = rising_left + rising_right
        falling_pair = falling_left + falling_right

        if preferred_slope_rising is None:
            target_slope_rising = rising_pair >= falling_pair
        else:
            target_slope_rising = bool(preferred_slope_rising)

        if target_slope_rising:
            target_slope_rising = True
            left_excursion_v = rising_left
            right_excursion_v = rising_right
        else:
            target_slope_rising = False
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

        score = pair_excursion_v + (0.25 * weaker) - (2.0 * center_abs_v)
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
