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

# Source of truth for the series keys that `build_plot_frame` populates when
# called with detail="summary". `stream.filter_plot_frame` MUST use this same
# set when stripping a full-detail broadcast down to summary form; the two
# code paths must agree on which series a summary subscriber receives.
SUMMARY_SERIES_KEYS: frozenset[str] = frozenset(
    {
        "combined_error",
        "control_signal",
        "error_signal_1",
        "error_signal_2",
        "monitor_signal",
        # Locked-state history traces surfaced in the overview and group-tab
        # plots. build_plot_frame emits these on the summary path too, so they
        # must survive filter_plot_frame when a full frame is stripped for a
        # summary subscriber (the two paths must agree -- see note above).
        "control_signal_history",
        "monitor_signal_history",
    }
)


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
    # Cached scaled history series for the full-detail path. Rebuilding
    # these per frame is expensive and the underlying history values
    # only mutate via append, so we gate the rebuild on a cheap
    # signature.
    #
    # IMPORTANT: the cached arrays are returned by reference inside
    # the plot frame's `series` dict and may be shared across many
    # frames while the underlying history is unchanged. Consumers MUST
    # NOT mutate them in place; doing so would corrupt subsequent
    # frames. All current consumers (`filter_plot_frame`,
    # `encode_plot_frame_json`/`encode_plot_frame_binary`, REST
    # snapshot readers) treat them as read-only.
    _control_history_signature: Optional[Tuple[Any, ...]] = None
    _control_history_scaled: Optional[np.ndarray] = None
    _slow_history_signature: Optional[Tuple[Any, ...]] = None
    _slow_history_scaled: Optional[np.ndarray] = None
    _monitor_history_signature: Optional[Tuple[Any, ...]] = None
    _monitor_history_scaled: Optional[np.ndarray] = None


def _history_signature(
    times: List[float], values: List[float], timescale: float
) -> Tuple[Any, ...]:
    """Cheap fingerprint of an append-only history series + scaling."""
    n = len(values)
    return (
        n,
        values[-1] if n else None,
        times[0] if times else None,
        times[-1] if times else None,
        timescale,
    )


def _scale_history(
    times: List[float], values: List[float], timescale: float
) -> np.ndarray:
    """Scaled history as a Float64 ndarray with NaN for empty slots.

    The result lives directly in plot frames (no Python list step) so
    binary encoders can `.astype(np.float32).tobytes()` it without
    re-walking. JSON encoders convert via `_array_to_json_safe` at
    encode time, which produces `null` for NaN entries to match the
    previous list-of-Optional[float] shape.

    Vectorized with numpy fancy-indexing: ~15x faster than the prior
    Python for-loop at ~500 samples (bench/optimizations.py #7).
    Duplicate target indices fall back to "last write wins" same as
    the loop did.
    """
    scaled_times = scale_history_times(times, timescale)
    out = np.full(N_POINTS, np.nan, dtype=np.float64)
    if scaled_times.size == 0 or not values:
        return out
    idxs = np.round(scaled_times).astype(np.int64)
    mask = (idxs >= 0) & (idxs < N_POINTS)
    if not mask.any():
        return out
    vals = np.asarray(values, dtype=np.float64)
    # `values` may be a longer list than `scaled_times` in edge cases
    # (linien-common's update_signal_history can briefly desync). Trim
    # to the safer of the two lengths so the mask + assignment stay
    # consistent.
    pair_len = min(idxs.size, vals.size)
    if pair_len < idxs.size:
        idxs = idxs[:pair_len]
        mask = mask[:pair_len]
    elif pair_len < vals.size:
        vals = vals[:pair_len]
    out[idxs[mask]] = vals[mask] / V
    return out


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
    detail: str = "full",
    build_series: bool = True,
) -> Optional[Dict[str, Any]]:
    """Build a plot frame from a raw ``to_plot`` payload.

    ``build_series=False`` skips the expensive per-series
    ``(arr / V).tolist()`` conversions and the signal-strength band
    work. Use it when there are no websocket subscribers AND no
    auto-relock active — the resulting frame has an empty ``series``
    dict but state-mutating side effects (history updates,
    ``last_plot_data`` for auto-lock, std stats, autolock target)
    still run, so the next subscriber that connects starts with
    coherent backing state.
    """
    if to_plot is None:
        return None
    lock = bool(params.get("lock"))
    full_detail = detail != "summary"
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

        if build_series:
            # Series values stored as numpy float64 arrays; the
            # encoders (encode_plot_frame_json /
            # encode_plot_frame_binary) handle the per-protocol
            # conversion. Skipping the .tolist() step here saves the
            # per-series Python-list build on the gateway hot path.
            if error_signal is not None:
                series["combined_error"] = (error_signal / V).astype(np.float64)
            if control_signal is not None:
                series["control_signal"] = (control_signal / V).astype(np.float64)

        if build_series:
            # control_signal_history and monitor_signal_history are emitted on
            # every frame (summary AND full detail) so the overview and
            # group-tab plots show the locked history. These are the gateway's
            # already-accumulated rolling buffers (see update_histories) -- the
            # same client-side accumulation the desktop GUI performs -- so this
            # only serializes existing data; the _scale_history call is
            # signature-cached and re-runs only when the buffer changes.
            control_times = state.control_history["times"]
            control_values = state.control_history["values"]
            control_sig = _history_signature(
                control_times, control_values, timescale
            )
            if (
                state._control_history_signature != control_sig
                or state._control_history_scaled is None
            ):
                state._control_history_scaled = _scale_history(
                    control_times, control_values, timescale
                )
                state._control_history_signature = control_sig
            series["control_signal_history"] = state._control_history_scaled

            if not params.get("dual_channel"):
                monitor_times = state.monitor_history["times"]
                monitor_values = state.monitor_history["values"]
                monitor_sig = _history_signature(
                    monitor_times, monitor_values, timescale
                )
                if (
                    state._monitor_history_signature != monitor_sig
                    or state._monitor_history_scaled is None
                ):
                    state._monitor_history_scaled = _scale_history(
                        monitor_times, monitor_values, timescale
                    )
                    state._monitor_history_signature = monitor_sig
                series["monitor_signal_history"] = state._monitor_history_scaled

        if build_series and full_detail:
            # slow_history stays full-detail only (not surfaced in summary views).
            if params.get("pid_on_slow_enabled"):
                slow_times = state.control_history["slow_times"]
                slow_values = state.control_history["slow_values"]
                slow_sig = _history_signature(slow_times, slow_values, timescale)
                if (
                    state._slow_history_signature != slow_sig
                    or state._slow_history_scaled is None
                ):
                    state._slow_history_scaled = _scale_history(
                        slow_times, slow_values, timescale
                    )
                    state._slow_history_signature = slow_sig
                series["slow_history"] = state._slow_history_scaled
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

        if build_series:
            series["combined_error"] = (combined_error / V).astype(np.float64)
            if error_signal_1 is not None:
                series["error_signal_1"] = (error_signal_1 / V).astype(np.float64)
            if error_signal_2 is not None:
                series["error_signal_2"] = (error_signal_2 / V).astype(np.float64)
            if monitor_signal is not None:
                series["monitor_signal"] = (monitor_signal / V).astype(np.float64)

        modulation_frequency = float(params.get("modulation_frequency", 0))
        pid_only_mode = bool(params.get("pid_only_mode"))
        if build_series and full_detail and modulation_frequency != 0 and not pid_only_mode:
            error_1_quadrature = to_plot.get("error_signal_1_quadrature")
            error_2_quadrature = to_plot.get("error_signal_2_quadrature")

            if error_1_quadrature is not None and error_signal_1 is not None:
                upper, lower, max_strength = signal_strength_band(
                    error_signal_1,
                    error_1_quadrature,
                    float(params.get("offset_a", 0)),
                )
                series["signal_strength_a_upper"] = upper.astype(np.float64)
                series["signal_strength_a_lower"] = lower.astype(np.float64)
                signal_power1 = peak_voltage_to_dbm(max_strength / V)

            if error_2_quadrature is not None and monitor_or_error_signal_2 is not None:
                upper, lower, max_strength = signal_strength_band(
                    monitor_or_error_signal_2,
                    error_2_quadrature,
                    float(params.get("offset_b", 0)),
                )
                series["signal_strength_b_upper"] = upper.astype(np.float64)
                series["signal_strength_b_lower"] = lower.astype(np.float64)
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


