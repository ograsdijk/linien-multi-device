from __future__ import annotations

from dataclasses import dataclass, field
from math import log10
from typing import Any, Dict, List, Optional, Tuple
import time

import numpy as np
from linien_common.common import (
    N_POINTS,
    check_plot_data,
    combine_error_signal,
    determine_shift_by_correlation,
    get_signal_strength_from_i_q,
    update_signal_history,
)

V = 8192


@dataclass
class PlotState:
    control_history: Dict[str, List[float]] = field(
        default_factory=lambda: {
            "times": [],
            "values": [],
            "slow_times": [],
            "slow_values": [],
        }
    )
    monitor_history: Dict[str, List[float]] = field(
        default_factory=lambda: {"times": [], "values": []}
    )
    error_std_history: List[float] = field(default_factory=list)
    control_std_history: List[float] = field(default_factory=list)
    combined_error_cache: List[np.ndarray] = field(default_factory=list)
    last_plot_data: Optional[List[np.ndarray]] = None
    last_unlocked_trace_at: float | None = None
    autolock_ref_spectrum: Optional[np.ndarray] = None
    last_lock_state: Optional[bool] = None


def peak_voltage_to_dbm(voltage: float) -> Optional[float]:
    if voltage <= 0:
        return None
    return 10 + 20 * log10(voltage)


def scale_history_times(arr: List[float], timescale: float) -> np.ndarray:
    if not arr:
        return np.array([])
    values = np.array(arr, dtype=float)
    values -= values[0]
    values *= 1 / timescale * N_POINTS
    return values


def history_to_series(times: np.ndarray, values: List[float]) -> List[float | None]:
    series: List[float | None] = [None] * N_POINTS
    for t, v in zip(times, values):
        idx = int(round(float(t)))
        if 0 <= idx < N_POINTS:
            series[idx] = float(v)
    return series


def signal_strength_band(
    i: np.ndarray, q: np.ndarray, channel_offset: float
) -> Tuple[np.ndarray, np.ndarray, float]:
    i = i.astype(np.int64) - int(round(channel_offset))
    q = q.astype(np.int64) - int(round(channel_offset))
    signal_strength = get_signal_strength_from_i_q(i, q)
    signal_strength_scaled = signal_strength / V
    offset_scaled = channel_offset / V
    upper = offset_scaled + signal_strength_scaled
    lower = offset_scaled - signal_strength_scaled
    max_strength = float(
        np.max([np.max(upper), -1 * np.min(lower)]) * V
    )
    return upper, lower, max_strength


def update_histories(
    state: PlotState,
    to_plot: Dict[str, np.ndarray],
    is_locked: bool,
    timescale: float,
) -> None:
    control_history, monitor_history = update_signal_history(
        state.control_history,
        state.monitor_history,
        to_plot,
        is_locked,
        timescale,
    )
    state.control_history = control_history
    state.monitor_history = monitor_history


def calculate_lock_target(
    state: PlotState,
    combined_error_signal: np.ndarray,
    sweep_amplitude: float,
    autolock_initial_sweep_amplitude: float,
) -> Optional[float]:
    if state.autolock_ref_spectrum is None:
        return None
    zoom_factor = 1 / sweep_amplitude
    initial_zoom_factor = 1 / autolock_initial_sweep_amplitude
    try:
        shift, _, _ = determine_shift_by_correlation(
            zoom_factor / initial_zoom_factor,
            state.autolock_ref_spectrum.copy(),
            combined_error_signal.copy(),
        )
        shift *= zoom_factor / initial_zoom_factor
        length = len(combined_error_signal)
        return (length / 2) - (shift / 2 * length)
    except Exception:
        return None


def build_plot_frame(
    to_plot: Dict[str, np.ndarray],
    params: Dict[str, Any],
    state: PlotState,
) -> Optional[Dict[str, Any]]:
    if to_plot is None:
        return None
    lock = bool(params.get("lock"))
    if state.last_lock_state is None or state.last_lock_state != lock:
        state.error_std_history = []
        state.control_std_history = []
        state.last_lock_state = lock
    if not check_plot_data(lock, to_plot):
        return None

    series: Dict[str, List[float]] = {}
    signal_power1 = None
    signal_power2 = None
    error_std_mean = None
    control_std_mean = None
    lock_target = None

    timescale = float(params.get("control_signal_history_length", 600))
    update_histories(state, to_plot, lock, timescale)

    if lock:
        error_signal = to_plot.get("error_signal")
        control_signal = to_plot.get("control_signal")
        if error_signal is not None and control_signal is not None:
            # Store lock stats in volts to match UI thresholds and other scaled traces.
            state.error_std_history.append(float(np.std(error_signal) / V))
            state.control_std_history.append(float(np.std(control_signal) / V))
            state.error_std_history = state.error_std_history[-10:]
            state.control_std_history = state.control_std_history[-10:]
            error_std_mean = float(np.mean(state.error_std_history))
            control_std_mean = float(np.mean(state.control_std_history))

        if error_signal is not None:
            series["combined_error"] = (error_signal / V).tolist()
        if control_signal is not None:
            series["control_signal"] = (control_signal / V).tolist()

        control_times = scale_history_times(
            state.control_history["times"], timescale
        )
        control_series = history_to_series(
            control_times, state.control_history["values"]
        )
        series["control_signal_history"] = [
            (v / V) if v is not None else None for v in control_series
        ]

        if params.get("pid_on_slow_enabled"):
            slow_series = history_to_series(
                scale_history_times(state.control_history["slow_times"], timescale),
                state.control_history["slow_values"],
            )
            series["slow_history"] = [
                (v / V) if v is not None else None for v in slow_series
            ]

        if not params.get("dual_channel"):
            monitor_series = history_to_series(
                scale_history_times(state.monitor_history["times"], timescale),
                state.monitor_history["values"],
            )
            series["monitor_signal_history"] = [
                (v / V) if v is not None else None for v in monitor_series
            ]
    else:
        dual_channel = bool(params.get("dual_channel"))
        monitor_signal = to_plot.get("monitor_signal")
        error_signal_1 = to_plot.get("error_signal_1")
        error_signal_2 = to_plot.get("error_signal_2")
        monitor_or_error_signal_2 = (
            error_signal_2 if error_signal_2 is not None else monitor_signal
        )
        if error_signal_1 is None or monitor_or_error_signal_2 is None:
            return None

        combined_error = combine_error_signal(
            (error_signal_1, monitor_or_error_signal_2),
            dual_channel,
            int(params.get("channel_mixing", 0)),
            int(params.get("combined_offset", 0)) if dual_channel else 0,
        )

        state.last_plot_data = [
            error_signal_1,
            monitor_or_error_signal_2,
            combined_error,
        ]
        state.last_unlocked_trace_at = time.time()

        state.combined_error_cache.append(combined_error)
        state.combined_error_cache = state.combined_error_cache[-20:]

        series["combined_error"] = (combined_error / V).tolist()

        if error_signal_1 is not None:
            series["error_signal_1"] = (error_signal_1 / V).tolist()
        if error_signal_2 is not None:
            series["error_signal_2"] = (error_signal_2 / V).tolist()
        if monitor_signal is not None:
            series["monitor_signal"] = (monitor_signal / V).tolist()

        modulation_frequency = float(params.get("modulation_frequency", 0))
        pid_only_mode = bool(params.get("pid_only_mode"))
        if modulation_frequency != 0 and not pid_only_mode:
            error_1_quadrature = to_plot.get("error_signal_1_quadrature")
            error_2_quadrature = to_plot.get("error_signal_2_quadrature")

            if error_1_quadrature is not None and error_signal_1 is not None:
                upper, lower, max_strength = signal_strength_band(
                    error_signal_1,
                    error_1_quadrature,
                    float(params.get("offset_a", 0)),
                )
                series["signal_strength_a_upper"] = upper.tolist()
                series["signal_strength_a_lower"] = lower.tolist()
                signal_power1 = peak_voltage_to_dbm(max_strength / V)

            if error_2_quadrature is not None and monitor_or_error_signal_2 is not None:
                upper, lower, max_strength = signal_strength_band(
                    monitor_or_error_signal_2,
                    error_2_quadrature,
                    float(params.get("offset_b", 0)),
                )
                series["signal_strength_b_upper"] = upper.tolist()
                series["signal_strength_b_lower"] = lower.tolist()
                signal_power2 = peak_voltage_to_dbm(max_strength / V)

        if params.get("autolock_preparing"):
            lock_target = calculate_lock_target(
                state,
                combined_error,
                float(params.get("sweep_amplitude", 1)),
                float(params.get("autolock_initial_sweep_amplitude", 1)),
            )

    frame = {
        "type": "plot_frame",
        "lock": lock,
        "dual_channel": bool(params.get("dual_channel")),
        "series": series,
        "signal_power": {
            "channel1": signal_power1,
            "channel2": signal_power2,
        },
        "stats": {
            "error_std": error_std_mean,
            "control_std": control_std_mean,
        },
        "lock_target": lock_target,
        "x_label": "time" if lock else "sweep voltage",
        "x_unit": "us" if lock else "V",
    }
    return frame


