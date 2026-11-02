from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Mapping, Sequence

import numpy as np

from .auto_lock_scan import _index_to_voltage

if TYPE_CHECKING:
    from .lock_acquisition import LockAcquisition

ADC_SCALE = 8192.0
MOD_HZ_UNIT = 0x10000000 / 8
MOD_AMP_SCALE = ((1 << 14) - 1) / 4
OFFSET_SCALE = 8191.0

# How trace_x is derived when it is not measured. The board does not report a
# per-sample sweep voltage; Linien sweeps linearly from center - amplitude to
# center + amplitude across the plotted samples. This is the same mapping
# auto_lock_scan uses to turn a detected sample index into the sweep_center it
# commands for the lock, so a wrong mapping would already show up as auto-lock
# landing off target.
X_AXIS_MODEL = (
    "linear: sample k of n at sweep_center - sweep_amplitude "
    "+ 2 * sweep_amplitude * k / (n - 1)"
)


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


def _clean_trace(values: Sequence[Any] | np.ndarray | None) -> list[float] | None:
    """A trace as a list of floats, or None when there is no trace.

    Unreadable or non-finite samples become NaN so sample positions are kept;
    a missing or empty trace stays None rather than being replaced by a
    placeholder waveform.
    """
    if values is None:
        return None
    cleaned: list[float] = []
    for value in values:
        numeric = _to_float(value)
        cleaned.append(float("nan") if numeric is None else numeric)
    return cleaned or None


def _build_trace_x(
    count: int,
    sweep_center: float | None,
    sweep_amplitude: float | None,
    *,
    sweep_paused: bool | None = None,
) -> tuple[list[float] | None, str | None, str | None]:
    """(trace_x, units, source) for a trace of ``count`` samples.

    Reconstructed in sweep volts when the geometry is known and the sweep is
    running; otherwise the sample index, labelled as such -- never volts it
    cannot vouch for.
    """
    if count <= 0:
        return None, None, None
    if sweep_center is None or sweep_amplitude is None or sweep_paused:
        return [float(idx) for idx in range(count)], "sample", "sample_index"
    return (
        [
            _index_to_voltage(idx, count, sweep_center, sweep_amplitude)
            for idx in range(count)
        ],
        "V",
        "reconstructed_from_sweep",
    )


def _json_safe(value: Any) -> Any:
    """Strip non-finite floats, which json.dumps writes as bare NaN/Infinity.

    That is not valid JSON, so Postgres rejects it when parsing the jsonb column
    -- and the write error is swallowed by the best-effort writer, losing the
    row silently.
    """
    if isinstance(value, (float, np.floating)):
        value = float(value)
        return value if math.isfinite(value) else None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.ndarray):
        return [_json_safe(item) for item in value.tolist()]
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def build_manual_lock_row(
    *,
    device_name: str | None,
    device_key: str,
    lock_source: str = "manual_lock",
    success: bool = True,
    params: Mapping[str, Any],
    trace_y: Sequence[Any] | np.ndarray | None,
    secondary_trace_y: Sequence[Any] | np.ndarray | None = None,
    secondary_trace_role: str | None = None,
    sweep_center_v: float | None = None,
    sweep_amplitude_v: float | None = None,
    sweep_paused: bool | None = None,
) -> dict[str, Any]:
    """The converted columns of one lock record.

    ``sweep_center_v``/``sweep_amplitude_v`` are the geometry the trace was
    measured at; they fall back to ``params`` only when the caller has none.
    """
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

    sweep_center = _to_float(sweep_center_v)
    sweep_amplitude = _to_float(sweep_amplitude_v)
    if sweep_center is None and sweep_amplitude is None:
        sweep_center = _to_float(params.get("sweep_center"))
        sweep_amplitude = _to_float(params.get("sweep_amplitude"))
    if sweep_paused is None and "sweep_pause" in params:
        sweep_paused = bool(params.get("sweep_pause"))

    y_values = _clean_trace(trace_y)
    secondary_values = _clean_trace(secondary_trace_y)
    x_values, x_units, x_source = _build_trace_x(
        len(y_values) if y_values else 0,
        sweep_center,
        sweep_amplitude,
        sweep_paused=sweep_paused,
    )

    return {
        "laser_name": laser_name,
        "device_key": device_key,
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
        "secondary_trace_y": secondary_values,
        "trace_x_units": x_units,
        "trace_y_units": "V" if y_values is not None else None,
        "secondary_trace_y_units": "V" if secondary_values is not None else None,
        "trace_x_source": x_source,
        "secondary_trace_role": secondary_trace_role if secondary_values else None,
        "sweep_center_v": sweep_center,
        "sweep_amplitude_v": sweep_amplitude,
    }


def build_acquisition_row(acquisition: "LockAcquisition") -> dict[str, Any]:
    """The full PostgreSQL row for one lock-acquisition attempt."""
    from .lock_acquisition import (
        ACQUISITION_RECORD_VERSION,
        TRACE_ROLE_COMBINED_ERROR,
    )

    params = acquisition.params
    trace = acquisition.trace
    available = trace is not None and trace.available
    row = build_manual_lock_row(
        device_name=acquisition.laser_name,
        device_key=acquisition.device_key,
        lock_source=acquisition.lock_source,
        success=acquisition.success,
        params=params,
        trace_y=trace.error_v if available else None,
        secondary_trace_y=trace.secondary_v if available else None,
        secondary_trace_role=trace.secondary_role if available else None,
        sweep_center_v=trace.sweep_center_v if available else None,
        sweep_amplitude_v=trace.sweep_amplitude_v if available else None,
        sweep_paused=trace.sweep_paused if available else None,
    )
    if trace is None:
        unavailable_reason: str | None = "no_trace_captured"
    elif not available:
        unavailable_reason = trace.unavailable_reason or "no_trace_captured"
    else:
        unavailable_reason = None

    trace_meta: dict[str, Any] | None = None
    if trace is not None:
        n_samples = len(row["trace_y"]) if row["trace_y"] is not None else 0
        secondary_n = (
            len(row["secondary_trace_y"]) if row["secondary_trace_y"] is not None else 0
        )
        trace_meta = {
            "trace_type": "unlocked_sweep",
            "purpose": trace.purpose,
            "detector": trace.detector,
            "trace_role": TRACE_ROLE_COMBINED_ERROR if available else None,
            "secondary_role": row["secondary_trace_role"],
            "dual_channel": params.get("dual_channel"),
            "channel_mixing": params.get("channel_mixing"),
            "frame_id": trace.frame_id,
            "frame_processed_at": trace.frame_processed_at,
            "trace_age_at_attempt_s": (
                acquisition.attempt_started_at - trace.frame_processed_at
                if trace.frame_processed_at is not None
                else None
            ),
            "geometry_read_at": trace.geometry_read_at,
            "geometry_source": trace.geometry_source,
            "sweep_center_v": trace.sweep_center_v,
            "sweep_amplitude_v": trace.sweep_amplitude_v,
            "sweep_paused": trace.sweep_paused,
            "n_samples": n_samples,
            "secondary_n_samples": secondary_n,
            "secondary_length_mismatch": bool(secondary_n and secondary_n != n_samples),
            "x_axis_source": row["trace_x_source"],
            "x_axis_model": (
                X_AXIS_MODEL
                if row["trace_x_source"] == "reconstructed_from_sweep"
                else None
            ),
            "y_scale": "adc_counts / 8192",
            "unavailable_reason": unavailable_reason,
            **acquisition.trace_context,
        }

    row.update(
        {
            "lock_acquisition_id": acquisition.lock_acquisition_id,
            "record_version": ACQUISITION_RECORD_VERSION,
            "attempt_started_at": acquisition.attempt_started_at,
            "lock_command_at": acquisition.lock_command_at,
            "lock_command_status": acquisition.lock_command_status,
            "failure_stage": acquisition.failure_stage,
            "failure_reason": acquisition.failure_reason,
            "trace_y_role": TRACE_ROLE_COMBINED_ERROR if row["trace_y"] else None,
            "trace_unavailable_reason": unavailable_reason,
            "trace_meta": _json_safe(trace_meta) if trace_meta is not None else None,
            "refinement": (
                _json_safe(acquisition.refinement)
                if acquisition.refinement is not None
                else None
            ),
        }
    )
    return row
