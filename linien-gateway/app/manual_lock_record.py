from __future__ import annotations

import json
import math
from typing import Any, Mapping, Sequence

import numpy as np

ADC_SCALE = 8192.0
MOD_HZ_UNIT = 0x10000000 / 8
MOD_AMP_SCALE = ((1 << 14) - 1) / 4
OFFSET_SCALE = 8191.0


def modulation_raw_to_hz(modulation_raw: float) -> float:
    """Convert a raw ``modulation_frequency`` register value to Hz.

    Single source of truth for the conversion (also used by the auto-lock Hz/V
    derivation in session.py)."""
    return (float(modulation_raw) / MOD_HZ_UNIT) * 1_000_000.0


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return numeric


def _clean_trace(values: Sequence[Any] | None) -> list[float]:
    if values is None:
        return [0.0]
    cleaned: list[float] = []
    for value in values:
        numeric = _to_float(value)
        if numeric is None:
            cleaned.append(float("nan"))
        else:
            cleaned.append(numeric)
    return cleaned if cleaned else [0.0]


def _build_trace_x(
    count: int, sweep_center: float | None, sweep_amplitude: float | None
) -> list[float]:
    if count <= 1:
        return [0.0]
    if sweep_center is None or sweep_amplitude is None:
        return [float(idx) for idx in range(count)]
    minimum = sweep_center - sweep_amplitude
    maximum = sweep_center + sweep_amplitude
    step = (maximum - minimum) / float(count - 1)
    return [minimum + step * idx for idx in range(count)]


def _json_safe(value: Any) -> Any:
    """Strip non-finite floats, which json.dumps writes as bare NaN/Infinity.

    That is not valid JSON, so Postgres rejects it when parsing the jsonb column
    -- and the write error is swallowed by the best-effort writer, losing the
    row silently.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _approach_columns(approach: Mapping[str, Any] | None) -> dict[str, Any]:
    """Flatten a guarded-move report into the pdh_lock_results columns.

    These are stored as scalars rather than buried in the JSON blob because the
    interesting questions are trends per laser -- is this piezo's backlash
    growing? -- which want plain SQL. ``approach_detail`` keeps the per-attempt
    breakdown for when one device needs a closer look.
    """
    if not isinstance(approach, Mapping):
        return {
            "target_voltage_v": None,
            "sweep_center_start_v": None,
            "center_move_v": None,
            "center_correction_v": None,
            "center_offset_v": None,
            "capture_tolerance_v": None,
            "approach_enabled": False,
            "approach_direct": None,
            "approach_from_below": None,
            "approach_attempts": None,
            "approach_detail": None,
        }
    attempts = list(approach.get("attempts") or [])
    last = attempts[-1] if attempts else {}
    return {
        "target_voltage_v": _to_float(approach.get("target_voltage")),
        "sweep_center_start_v": _to_float(approach.get("start_voltage")),
        "center_move_v": _to_float(approach.get("center_move_v")),
        "center_correction_v": _to_float(approach.get("center_correction_v")),
        "center_offset_v": _to_float(approach.get("center_offset_v")),
        "capture_tolerance_v": _to_float(approach.get("capture_tolerance_v")),
        "approach_enabled": bool(approach.get("enabled", False)),
        "approach_direct": last.get("direct"),
        "approach_from_below": last.get("from_below"),
        "approach_attempts": len(attempts),
        "approach_detail": (
            json.dumps(_json_safe(attempts), allow_nan=False) if attempts else None
        ),
    }


def build_manual_lock_row(
    *,
    device_name: str | None,
    device_key: str,
    lock_source: str = "manual_lock",
    success: bool = True,
    params: Mapping[str, Any],
    trace_y: Sequence[Any] | np.ndarray | None,
    monitor_trace_y: Sequence[Any] | np.ndarray | None,
    approach: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    laser_name = (device_name or "").strip() or device_key
    control_channel_raw = _to_float(params.get("control_channel"))
    control_channel = int(control_channel_raw) if control_channel_raw is not None else 0
    suffix = "_b" if control_channel == 1 else "_a"

    modulation_raw = _to_float(params.get("modulation_frequency"))
    modulation_hz = (
        None if modulation_raw is None else modulation_raw_to_hz(modulation_raw)
    )

    modulation_amplitude_raw = _to_float(params.get("modulation_amplitude"))
    modulation_amplitude = (
        None
        if modulation_amplitude_raw is None
        else modulation_amplitude_raw / MOD_AMP_SCALE
    )

    offset_raw = _to_float(params.get(f"offset{suffix}"))
    offset_volts = None if offset_raw is None else offset_raw / OFFSET_SCALE
    demod_phase = _to_float(params.get(f"demodulation_phase{suffix}"))

    pid_p = _to_float(params.get("p"))
    pid_i = _to_float(params.get("i"))
    pid_d = _to_float(params.get("d"))

    sweep_center = _to_float(params.get("sweep_center"))
    sweep_amplitude = _to_float(params.get("sweep_amplitude"))
    y_values = _clean_trace(trace_y)
    monitor_y_values = _clean_trace(monitor_trace_y)
    if len(monitor_y_values) < len(y_values):
        monitor_y_values = monitor_y_values + [float("nan")] * (
            len(y_values) - len(monitor_y_values)
        )
    elif len(monitor_y_values) > len(y_values):
        monitor_y_values = monitor_y_values[: len(y_values)]
    x_values = _build_trace_x(len(y_values), sweep_center, sweep_amplitude)

    return {
        "laser_name": laser_name,
        "lock_source": (lock_source or "").strip() or "manual_lock",
        "success": bool(success),
        "modulation_frequency_hz": modulation_hz,
        "demod_phase_deg": demod_phase,
        "signal_offset_volts": offset_volts,
        "modulation_amplitude": modulation_amplitude,
        "pid_p": pid_p,
        "pid_i": pid_i,
        "pid_d": pid_d,
        "trace_x": x_values,
        "trace_y": y_values,
        "monitor_trace_y": monitor_y_values,
        "trace_x_units": "V",
        "trace_y_units": "V",
        "monitor_trace_y_units": "V",
        "sweep_center_v": sweep_center,
        "sweep_amplitude_v": sweep_amplitude,
        **_approach_columns(approach),
    }
