from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Mapping

import numpy as np

ADC_SCALE = 8192.0


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_float(value: Any, default: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if not np.isfinite(numeric):
        return default
    return numeric


def _as_int(value: Any, default: int) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default

def _plot_array(to_plot: Mapping[str, Any] | None, name: str) -> np.ndarray | None:
    if not isinstance(to_plot, Mapping):
        return None
    values = to_plot.get(name)
    if values is None:
        return None
    try:
        arr = np.asarray(values, dtype=float)
    except Exception:
        return None
    if arr.ndim != 1 or arr.size == 0:
        return None
    return np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)


@dataclass
class LockIndicatorConfig:
    enabled: bool = True
    bad_hold_s: float = 1.0
    good_hold_s: float = 2.0
    use_control: bool = True
    control_stuck_delta_counts: int = 0
    control_stuck_time_s: float = 1.5
    control_rail_threshold_v: float = 0.9
    control_rail_hold_s: float = 1.0
    use_error: bool = True
    error_mean_abs_max_v: float = 0.2
    error_std_min_v: float = 0.001
    error_std_max_v: float = 0.8
    use_monitor: bool = False
    monitor_mode: str = "locked_above"  # locked_above | locked_below
    monitor_threshold_v: float = 0.0

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "LockIndicatorConfig":
        defaults = cls()
        if payload is None:
            return defaults
        mode_raw = str(payload.get("monitor_mode", defaults.monitor_mode)).strip().lower()
        monitor_mode = mode_raw if mode_raw in {"locked_above", "locked_below"} else defaults.monitor_mode
        return cls(
            enabled=_as_bool(payload.get("enabled"), defaults.enabled),
            bad_hold_s=max(0.05, _as_float(payload.get("bad_hold_s"), defaults.bad_hold_s)),
            good_hold_s=max(0.05, _as_float(payload.get("good_hold_s"), defaults.good_hold_s)),
            use_control=_as_bool(payload.get("use_control"), defaults.use_control),
            control_stuck_delta_counts=max(
                0,
                _as_int(
                    payload.get("control_stuck_delta_counts"),
                    defaults.control_stuck_delta_counts,
                ),
            ),
            control_stuck_time_s=max(
                0.05,
                _as_float(payload.get("control_stuck_time_s"), defaults.control_stuck_time_s),
            ),
            control_rail_threshold_v=max(
                0.0,
                _as_float(payload.get("control_rail_threshold_v"), defaults.control_rail_threshold_v),
            ),
            control_rail_hold_s=max(
                0.05,
                _as_float(payload.get("control_rail_hold_s"), defaults.control_rail_hold_s),
            ),
            use_error=_as_bool(payload.get("use_error"), defaults.use_error),
            error_mean_abs_max_v=max(
                0.0,
                _as_float(payload.get("error_mean_abs_max_v"), defaults.error_mean_abs_max_v),
            ),
            error_std_min_v=max(
                0.0,
                _as_float(payload.get("error_std_min_v"), defaults.error_std_min_v),
            ),
            error_std_max_v=max(
                0.0,
                _as_float(payload.get("error_std_max_v"), defaults.error_std_max_v),
            ),
            use_monitor=_as_bool(payload.get("use_monitor"), defaults.use_monitor),
            monitor_mode=monitor_mode,
            monitor_threshold_v=_as_float(
                payload.get("monitor_threshold_v"),
                defaults.monitor_threshold_v,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LockIndicatorMetrics:
    error_std_v: float | None = None
    error_mean_abs_v: float | None = None
    control_std_v: float | None = None
    control_mean_v: float | None = None
    control_range_counts: float | None = None
    monitor_mean_v: float | None = None
    control_stuck_s: float = 0.0
    control_rail_s: float = 0.0


class LockIndicatorEvaluator:
    def __init__(
        self,
        config: LockIndicatorConfig | Mapping[str, Any] | None = None,
    ) -> None:
        if isinstance(config, LockIndicatorConfig):
            self._config = config
        else:
            self._config = LockIndicatorConfig.from_mapping(config)
        self._state = "unknown"
        self._reasons: list[str] = []
        self._metrics = LockIndicatorMetrics()
        self._last_update_at: float | None = None
        self._last_transition_at = time.time()
        self._bad_duration_s = 0.0
        self._good_duration_s = 0.0
        self._control_stuck_duration_s = 0.0
        self._control_rail_duration_s = 0.0

    def get_config(self) -> dict[str, Any]:
        return self._config.to_dict()

    def set_config(
        self,
        payload: LockIndicatorConfig | Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if isinstance(payload, LockIndicatorConfig):
            self._config = payload
        else:
            self._config = LockIndicatorConfig.from_mapping(payload)
        self._bad_duration_s = 0.0
        self._good_duration_s = 0.0
        self._control_stuck_duration_s = 0.0
        self._control_rail_duration_s = 0.0
        return self.get_config()

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self._last_transition_at = time.time()

    def _reset_durations(self) -> None:
        self._bad_duration_s = 0.0
        self._good_duration_s = 0.0
        self._control_stuck_duration_s = 0.0
        self._control_rail_duration_s = 0.0

    def _time_delta(self, now: float) -> float:
        if self._last_update_at is None:
            self._last_update_at = now
            return 0.1
        dt = now - self._last_update_at
        self._last_update_at = now
        return min(2.0, max(0.02, float(dt)))

    def _snapshot(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "reasons": list(self._reasons),
            "metrics": asdict(self._metrics),
            "last_transition_at": float(self._last_transition_at),
        }

    def update(
        self,
        *,
        lock: bool,
        to_plot: Mapping[str, Any] | None,
        now: float | None = None,
    ) -> dict[str, Any]:
        ts = float(now) if now is not None else time.time()
        dt = self._time_delta(ts)
        cfg = self._config

        if not cfg.enabled:
            self._reset_durations()
            self._metrics = LockIndicatorMetrics()
            self._reasons = ["disabled"]
            self._set_state("unknown")
            return self._snapshot()

        if not lock:
            self._reset_durations()
            self._metrics = LockIndicatorMetrics()
            self._reasons = ["not_locked"]
            self._set_state("unknown")
            return self._snapshot()

        error = _plot_array(to_plot, "error_signal")
        control = _plot_array(to_plot, "control_signal")
        monitor = _plot_array(to_plot, "monitor_signal")

        bad_reasons: list[str] = []
        hard_fail = False
        metrics = LockIndicatorMetrics()

        if error is not None:
            metrics.error_std_v = float(np.std(error) / ADC_SCALE)
            metrics.error_mean_abs_v = abs(float(np.mean(error) / ADC_SCALE))
        elif cfg.use_error:
            bad_reasons.append("missing_error_signal")

        if control is not None:
            control_mean_v = float(np.mean(control) / ADC_SCALE)
            control_std_v = float(np.std(control) / ADC_SCALE)
            control_range_counts = float(np.max(control) - np.min(control))
            metrics.control_mean_v = control_mean_v
            metrics.control_std_v = control_std_v
            metrics.control_range_counts = control_range_counts
            if cfg.use_control:
                if control_range_counts <= float(cfg.control_stuck_delta_counts):
                    self._control_stuck_duration_s += dt
                else:
                    self._control_stuck_duration_s = 0.0
                if abs(control_mean_v) >= float(cfg.control_rail_threshold_v):
                    self._control_rail_duration_s += dt
                else:
                    self._control_rail_duration_s = 0.0
                if self._control_stuck_duration_s >= float(cfg.control_stuck_time_s):
                    hard_fail = True
                    bad_reasons.append("control_stuck")
                if self._control_rail_duration_s >= float(cfg.control_rail_hold_s):
                    bad_reasons.append("control_near_rail")
        elif cfg.use_control:
            bad_reasons.append("missing_control_signal")
            self._control_stuck_duration_s = 0.0
            self._control_rail_duration_s = 0.0
        else:
            self._control_stuck_duration_s = 0.0
            self._control_rail_duration_s = 0.0

        metrics.control_stuck_s = float(self._control_stuck_duration_s)
        metrics.control_rail_s = float(self._control_rail_duration_s)

        if cfg.use_error and error is not None:
            if metrics.error_mean_abs_v is not None and metrics.error_mean_abs_v > float(cfg.error_mean_abs_max_v):
                bad_reasons.append("error_mean_out_of_range")
            if float(cfg.error_std_min_v) > 0 and metrics.error_std_v is not None:
                if metrics.error_std_v < float(cfg.error_std_min_v):
                    bad_reasons.append("error_std_too_low")
            if float(cfg.error_std_max_v) > 0 and metrics.error_std_v is not None:
                if metrics.error_std_v > float(cfg.error_std_max_v):
                    bad_reasons.append("error_std_too_high")

        if cfg.use_monitor:
            if monitor is None:
                bad_reasons.append("missing_monitor_signal")
            else:
                metrics.monitor_mean_v = float(np.mean(monitor) / ADC_SCALE)
                if cfg.monitor_mode == "locked_above":
                    if metrics.monitor_mean_v < float(cfg.monitor_threshold_v):
                        bad_reasons.append("monitor_below_threshold")
                else:
                    if metrics.monitor_mean_v > float(cfg.monitor_threshold_v):
                        bad_reasons.append("monitor_above_threshold")
        elif monitor is not None:
            metrics.monitor_mean_v = float(np.mean(monitor) / ADC_SCALE)

        self._metrics = metrics
        self._reasons = bad_reasons

        if hard_fail:
            self._bad_duration_s = float(cfg.bad_hold_s)
            self._good_duration_s = 0.0
            self._set_state("lost")
            return self._snapshot()

        if bad_reasons:
            self._bad_duration_s += dt
            self._good_duration_s = 0.0
            if self._bad_duration_s >= float(cfg.bad_hold_s):
                self._set_state("lost")
            else:
                self._set_state("marginal")
            return self._snapshot()

        self._bad_duration_s = 0.0
        self._good_duration_s += dt
        if self._state in {"lost", "marginal"} and self._good_duration_s < float(cfg.good_hold_s):
            self._set_state("marginal")
            return self._snapshot()

        self._set_state("locked")
        self._reasons = []
        return self._snapshot()
