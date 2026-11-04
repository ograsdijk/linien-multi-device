"""Diagnostic characterization of the unlocked sweep a lock was acquired from.

Purely descriptive: nothing here feeds back into a lock decision, and any
failure becomes a status/reason on the record instead of an exception. It reuses
the auto-lock detector and its helpers (auto_lock_scan) rather than a second
implementation, so the numbers mean the same as the ones the auto-lock used.

Units
-----
- Error-signal values are plot volts: ADC counts / 8192 (Linien's full scale).
- Positions and widths on the sweep axis are sweep volts, from the trace's own
  sweep geometry (center +/- amplitude across the samples).
- ``slope_error_v_per_sweep_v`` is the discriminator slope on the sweep axis.
- ``discriminator_slope_v_per_mhz`` is reported only when a frequency
  calibration exists: a PDH trace with a known modulation frequency whose
  sidebands were resolved (sideband spacing in sweep volts <-> modulation
  frequency). Otherwise it is null rather than estimated.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from .auto_lock_scan import (
    AutoLockScanResult,
    AutoLockScanSettings,
    _crossing_slope_v_per_v,
    _half_range_to_points,
    _moving_average,
    _peak_offsets,
    _sanitize_trace,
    find_auto_lock_target,
)
from .lock_acquisition import TraceSnapshot

CHARACTERIZATION_VERSION = 1
METHOD = "auto_lock_scan.find_auto_lock_target"


def _volts_per_sample(n_points: int, sweep_amplitude_v: float) -> float | None:
    if n_points < 2 or abs(sweep_amplitude_v) <= 1e-12:
        return None
    return 2.0 * abs(float(sweep_amplitude_v)) / (n_points - 1)


def _status(status: str, reason: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "version": CHARACTERIZATION_VERSION,
        "status": status,
        "reason": reason,
        "method": METHOD,
        **extra,
    }


def characterize_trace(
    trace: TraceSnapshot | None,
    *,
    settings: AutoLockScanSettings,
    result: AutoLockScanResult | None = None,
    preferred_slope_rising: bool | None = None,
    modulation_frequency_hz: float | None = None,
    dual_channel: bool | None = None,
) -> dict[str, Any]:
    """Characterize one stored acquisition trace; never raises.

    ``result`` is the detection the lock was decided on, when there was one
    (auto-lock paths); otherwise the detector is run read-only on the trace
    with the device's current auto-lock settings.
    """
    try:
        return _characterize(
            trace,
            settings=settings,
            result=result,
            preferred_slope_rising=preferred_slope_rising,
            modulation_frequency_hz=modulation_frequency_hz,
            dual_channel=dual_channel,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics must never break a record
        return _status("error", f"{type(exc).__name__}: {exc}")


def _characterize(
    trace: TraceSnapshot | None,
    *,
    settings: AutoLockScanSettings,
    result: AutoLockScanResult | None,
    preferred_slope_rising: bool | None,
    modulation_frequency_hz: float | None,
    dual_channel: bool | None,
) -> dict[str, Any]:
    if trace is None or not trace.available or trace.error_v is None:
        reason = trace.unavailable_reason if trace is not None else None
        return _status("unavailable", reason or "no_trace")
    if trace.sweep_center_v is None or trace.sweep_amplitude_v is None:
        return _status("not_applicable", "sweep_geometry_unknown")
    if trace.sweep_paused:
        return _status("not_applicable", "sweep_paused")
    raw = _sanitize_trace(np.asarray(trace.error_v, dtype=float))
    n = len(raw)
    vps = _volts_per_sample(n, trace.sweep_amplitude_v)
    if vps is None:
        return _status("not_applicable", "degenerate_sweep_axis")

    context = {
        "signal_type": settings.signal_type,
        "trace_role": "combined_error",
        "dual_channel": dual_channel,
        "result_source": "locked_detection" if result is not None else "diagnostic_detection",
    }
    if result is None:
        try:
            result = find_auto_lock_target(
                error_trace_v=raw,
                monitor_trace_v=None,
                sweep_center_v=float(trace.sweep_center_v),
                sweep_amplitude_v=float(trace.sweep_amplitude_v),
                settings=settings,
                preferred_slope_rising=preferred_slope_rising,
                modulation_frequency_hz=modulation_frequency_hz,
            )
        except ValueError as exc:
            # Not every trace is a resolvable discriminator signal; say so
            # rather than reporting numbers for a crossing that is not there.
            return _status(
                "no_feature_detected",
                str(exc),
                trace_peak_to_peak_v=float(np.ptp(raw)),
                **context,
            )

    smoothed = _moving_average(raw, settings.smooth_window_pts)
    half_range_pts = _half_range_to_points(
        settings.half_range_sweep_v, n, trace.sweep_amplitude_v
    )
    crossing_idx = float(result.crossing_index or result.target_index)
    slope_v_per_v = _crossing_slope_v_per_v(
        smoothed, crossing_idx, half_range_pts, float(trace.sweep_amplitude_v)
    )
    notes: list[str] = []
    if slope_v_per_v is None:
        notes.append("slope_not_resolvable")

    # Lobe-peak separation around the carrier crossing (the dispersive feature
    # width). Reported only when both nearest lobe peaks were found as real
    # turning points; _peak_offsets falls back to n // 8 otherwise.
    anchor = int(round(crossing_idx))
    fallback = max(2, n // 8)
    left_off, right_off = _peak_offsets(
        smoothed, anchor, max(2, n // 2), bool(result.target_slope_rising)
    )
    if 0 < left_off < fallback and 0 < right_off < fallback:
        feature_width_v: float | None = float(left_off + right_off) * vps
    else:
        feature_width_v = None
        notes.append("lobe_peaks_not_resolved")

    # Baseline: median error away from the carrier feature, when enough of
    # the trace lies outside it to make that the background level.
    exclude = max(half_range_pts, left_off + right_off) * 2
    mask = np.ones(n, dtype=bool)
    mask[max(0, anchor - exclude) : min(n, anchor + exclude + 1)] = False
    if mask.sum() >= n // 2:
        baseline_v: float | None = float(np.median(raw[mask]))
    else:
        baseline_v = None
        notes.append("baseline_not_measurable")

    calibrated = (
        result.hz_per_v is not None
        and result.hz_per_v > 0.0
        and result.discriminator_slope_v_per_mhz is not None
    )
    return {
        **_status("ok", None),
        **context,
        "zero_crossing_sweep_v": float(result.target_voltage),
        "zero_crossing_index": crossing_idx,
        "target_slope_rising": bool(result.target_slope_rising),
        "slope_error_v_per_sweep_v": slope_v_per_v,
        "discriminator_slope_v_per_mhz": (
            float(result.discriminator_slope_v_per_mhz) if calibrated else None
        ),
        "frequency_calibration": (
            {
                "source": "sideband_spacing",
                "hz_per_sweep_v": float(result.hz_per_v),
                "sideband_offset_sweep_v": result.sideband_offset_v,
                "modulation_frequency_hz": modulation_frequency_hz,
            }
            if calibrated
            else None
        ),
        "peak_to_peak_v": float(result.feature_amplitude),
        "trace_peak_to_peak_v": float(np.ptp(raw)),
        "baseline_v": baseline_v,
        "feature_width_sweep_v": feature_width_v,
        "quality": {
            "score": float(result.score),
            "symmetry": float(result.symmetry),
            "left_excursion_v": float(result.left_excursion),
            "right_excursion_v": float(result.right_excursion),
        },
        "notes": notes,
    }
