from __future__ import annotations

import logging
import math
import pickle
import threading
import time
from collections.abc import Callable
from typing import Any, Dict, List, Optional

import numpy as np
from linien_client.connection import LinienClient
from linien_client.device import Device
from linien_client.exceptions import (
    GeneralConnectionError,
    InvalidServerVersionException,
    RPYCAuthenticationException,
    ServerNotRunningException,
)
from linien_common.common import get_lock_point
from linien_common.communication import unpack
from linien_common.influxdb import InfluxDBCredentials

from .auto_lock_scan import AutoLockScanSettings, find_auto_lock_target
from .auto_relock import AutoRelockConfig, AutoRelockController
from .lock_indicator import LockIndicatorConfig, LockIndicatorEvaluator
from .manual_lock_record import ADC_SCALE, build_manual_lock_row
from .plot_processing import PlotState, build_plot_frame
from .serializers import UNSERIALIZABLE, to_jsonable
from .stream import WebsocketManager

IGNORED_PARAMS = {
    "to_plot",
    "signal_stats",
    "acquisition_raw_data",
    "psd_data_partial",
    "psd_data_complete",
    "control_signal_history",
    "monitor_signal_history",
    "task",
    "ping",
}

FILTER_AUTOMATIC_PARAMS = {
    "filter_automatic_a",
    "filter_automatic_b",
}

NORMALIZED_PARAMS_ON_CONNECT = (
    "filter_automatic_a",
    "filter_automatic_b",
    "channel_mixing",
    "modulation_frequency",
)

# Temporary compatibility switch.
# Set to False to re-enable the original autolock/optimization implementations below.
AUTOMATION_TEMP_DISABLED = True
AUTOMATION_TEMP_DISABLED_REASON = (
    "temporarily disabled due to NumPy pickle compatibility between gateway and server."
)
DEFAULT_INFLUX_LOGGING_INTERVAL_S = 1.0
logger = logging.getLogger(__name__)


class DeviceSession:
    def __init__(
        self,
        device: Device,
        manager: WebsocketManager,
        lock_result_postgres_service: Any | None = None,
        log_event_callback: Callable[
            [int, str, str, str, str | None, dict[str, Any] | None], None
        ]
        | None = None,
    ) -> None:
        self.device = device
        self.manager = manager
        self._lock_result_postgres = lock_result_postgres_service
        self._log_event_callback = log_event_callback
        self.client: LinienClient | None = None
        self.control = None
        self.parameters = None
        self.connected = False
        self.connecting = False
        self.last_error: str | None = None
        self.last_plot_frame: Dict[str, Any] | None = None
        self.last_plot_timestamp: float | None = None
        self._poll_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._rpyc_lock = threading.RLock()
        self._relock_action_lock = threading.Lock()
        self.param_cache: Dict[str, Any] = {}
        self.param_cache_serialized: Dict[str, Any] = {}
        self.plot_state = PlotState()
        self.auto_lock_scan_settings = self._initial_auto_lock_scan_settings()
        self.lock_indicator = LockIndicatorEvaluator(self._initial_lock_indicator_config())
        self.auto_relock = AutoRelockController(
            self._initial_auto_relock_config(),
            event_hook=self._on_auto_relock_event,
        )
        self._last_lock_indicator_state: str | None = None
        self._last_auto_relock_state: str | None = None
        self.influx_logging_state = self._initial_influx_logging_state()

    def _emit_log_event(
        self,
        *,
        level: int,
        source: str,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        callback = self._log_event_callback
        if callback is None:
            return
        try:
            callback(level, source, code, message, self.device.key, details)
        except Exception:
            logger.debug("Session log callback failed", exc_info=True)

    def set_log_event_callback(
        self,
        callback: Callable[
            [int, str, str, str, str | None, dict[str, Any] | None], None
        ]
        | None,
    ) -> None:
        self._log_event_callback = callback
        if hasattr(self.auto_relock, "set_event_hook"):
            self.auto_relock.set_event_hook(self._on_auto_relock_event)

    def _on_auto_relock_event(self, event: str, payload: dict[str, Any]) -> None:
        if event == "attempt":
            self._emit_log_event(
                level=logging.INFO,
                source="auto_relock",
                code="auto_relock_attempt",
                message="Auto-relock attempt started.",
                details=payload,
            )
            return
        if event == "success":
            self._emit_log_event(
                level=logging.INFO,
                source="auto_relock",
                code="auto_relock_success",
                message="Auto-relock verified successfully.",
                details=payload,
            )
            return
        if event == "failure":
            self._emit_log_event(
                level=logging.ERROR,
                source="auto_relock",
                code="auto_relock_failure",
                message="Auto-relock attempt failed.",
                details=payload,
            )

    @staticmethod
    def _compact_indicator_metrics(indicator_snapshot: dict[str, Any]) -> dict[str, Any]:
        metrics = indicator_snapshot.get("metrics")
        if not isinstance(metrics, dict):
            return {}
        keys = (
            "error_std_v",
            "error_mean_abs_v",
            "control_mean_v",
            "control_std_v",
            "monitor_mean_v",
        )
        return {key: metrics.get(key) for key in keys if key in metrics}

    def _emit_lock_transition_log(
        self,
        *,
        lock_enabled: bool,
        indicator_state: str | None,
        indicator_snapshot: dict[str, Any],
    ) -> None:
        previous_state = self._last_lock_indicator_state
        self._last_lock_indicator_state = indicator_state
        if not lock_enabled or indicator_state is None or indicator_state == previous_state:
            return
        details = {
            "from_state": previous_state,
            "to_state": indicator_state,
            "reasons": (
                indicator_snapshot.get("reasons")
                if isinstance(indicator_snapshot.get("reasons"), list)
                else []
            ),
            "metrics": self._compact_indicator_metrics(indicator_snapshot),
        }
        if indicator_state == "lost":
            self._emit_log_event(
                level=logging.ERROR,
                source="lock_indicator",
                code="lock_lost",
                message="Lock indicator reports lock lost.",
                details=details,
            )
            return
        if indicator_state == "locked" and previous_state in {"lost", "marginal"}:
            self._emit_log_event(
                level=logging.INFO,
                source="lock_indicator",
                code="lock_acquired",
                message="Lock indicator reports lock acquired.",
                details=details,
            )

    def _emit_auto_relock_state_transition_log(
        self,
        auto_relock_status: dict[str, Any] | None,
    ) -> None:
        if not isinstance(auto_relock_status, dict):
            self._last_auto_relock_state = None
            return
        enabled = bool(auto_relock_status.get("enabled"))
        state = auto_relock_status.get("state")
        if not isinstance(state, str):
            self._last_auto_relock_state = None
            return
        previous_state = self._last_auto_relock_state
        self._last_auto_relock_state = state
        if not enabled or state == previous_state:
            return
        details = {
            "from_state": previous_state,
            "to_state": state,
            "attempts": auto_relock_status.get("attempts"),
            "max_attempts": auto_relock_status.get("max_attempts"),
            "last_error": auto_relock_status.get("last_error"),
        }
        if state == "lost_pending":
            self._emit_log_event(
                level=logging.WARNING,
                source="auto_relock",
                code="auto_relock_lost_pending",
                message="Auto-relock detected lost-lock condition.",
                details=details,
            )
            return
        if state == "waiting_unlocked_trace":
            self._emit_log_event(
                level=logging.INFO,
                source="auto_relock",
                code="auto_relock_waiting_unlocked_trace",
                message="Auto-relock waiting for unlocked sweep trace.",
                details=details,
            )

    def _write_lock_result_to_postgres(
        self,
        *,
        lock_source: str,
        event_source: str,
    ) -> None:
        if self._lock_result_postgres is None:
            self._emit_log_event(
                level=logging.WARNING,
                source="postgres",
                code="lock_result_postgres_unavailable",
                message="Lock-result postgres service unavailable.",
                details={"lock_source": lock_source, "event_source": event_source},
            )
            return

        service = self._lock_result_postgres
        try:
            device_name = self.device.name if getattr(self.device, "name", None) else self.device.key
            row = self.build_manual_lock_row(
                device_name=device_name,
                device_key=self.device.key,
                lock_source=lock_source,
            )
            enqueued = service.enqueue_lock_result(row)
            get_state = getattr(service, "get_state", None)
            state = get_state() if callable(get_state) else {}
            config = state.get("config", {}) if isinstance(state, dict) else {}
            status = state.get("status", {}) if isinstance(state, dict) else {}
            config_enabled = bool(config.get("enabled")) if isinstance(config, dict) else False
            details: dict[str, Any] = {
                "lock_source": lock_source,
                "event_source": event_source,
                "config_enabled": config_enabled,
            }
            if isinstance(status, dict):
                details["active"] = status.get("active")
                details["last_error"] = status.get("last_error")
            if enqueued:
                self._emit_log_event(
                    level=logging.INFO,
                    source="postgres",
                    code="lock_result_postgres_enqueued",
                    message="Lock-result row enqueued for Postgres writer.",
                    details=details,
                )
                return
            if not config_enabled:
                self._emit_log_event(
                    level=logging.INFO,
                    source="postgres",
                    code="lock_result_postgres_skipped_disabled",
                    message="Lock-result postgres disabled; skipping enqueue.",
                    details=details,
                )
                return
            details["error"] = "enqueue_rejected"
            self._emit_log_event(
                level=logging.ERROR,
                source="postgres",
                code="lock_result_postgres_enqueue_rejected",
                message="Lock-result postgres enqueue rejected.",
                details=details,
            )
        except Exception as exc:  # noqa: BLE001 - optional logging hook
            logger.warning(
                "Lock-result postgres enqueue failed device=%s source=%s",
                self.device.key,
                lock_source,
                exc_info=True,
            )
            self._emit_log_event(
                level=logging.ERROR,
                source="postgres",
                code="lock_result_postgres_enqueue_failed",
                message="Lock-result postgres enqueue failed.",
                details={
                    "lock_source": lock_source,
                    "event_source": event_source,
                    "error": str(exc),
                },
            )

    def _initial_lock_indicator_config(self) -> dict[str, Any]:
        parameters = getattr(self.device, "parameters", None)
        if not isinstance(parameters, dict):
            return {}
        payload = parameters.get("lock_indicator_config")
        return payload if isinstance(payload, dict) else {}

    def _initial_auto_lock_scan_settings(self) -> dict[str, Any]:
        parameters = getattr(self.device, "parameters", None)
        payload = parameters.get("auto_lock_scan_settings") if isinstance(parameters, dict) else None
        settings = AutoLockScanSettings.from_mapping(
            payload if isinstance(payload, dict) else None
        )
        return settings.__dict__.copy()

    def _initial_auto_relock_config(self) -> dict[str, Any]:
        parameters = getattr(self.device, "parameters", None)
        if not isinstance(parameters, dict):
            return {}
        payload = parameters.get("auto_relock_config")
        return payload if isinstance(payload, dict) else {}

    def _normalize_influx_logging_state(self, payload: Any) -> dict[str, Any]:
        interval = DEFAULT_INFLUX_LOGGING_INTERVAL_S
        enabled = False
        if isinstance(payload, bool):
            enabled = bool(payload)
            return {"enabled": enabled, "interval_s": interval}
        if isinstance(payload, dict):
            enabled = bool(payload.get("enabled", False))
            interval_raw = self._coerce_float(payload.get("interval_s"))
            if interval_raw is not None and interval_raw > 0:
                interval = max(0.1, float(interval_raw))
        return {"enabled": enabled, "interval_s": interval}

    def _initial_influx_logging_state(self) -> dict[str, Any]:
        parameters = getattr(self.device, "parameters", None)
        if not isinstance(parameters, dict):
            return self._normalize_influx_logging_state(None)
        return self._normalize_influx_logging_state(parameters.get("influx_logging_state"))

    def sync_auto_lock_scan_settings_from_device(self) -> None:
        next_auto_lock_scan_settings = self._initial_auto_lock_scan_settings()
        if next_auto_lock_scan_settings != self.auto_lock_scan_settings:
            self.auto_lock_scan_settings = next_auto_lock_scan_settings

    def sync_lock_indicator_settings_from_device(self) -> None:
        next_lock_indicator_config = LockIndicatorConfig.from_mapping(
            self._initial_lock_indicator_config()
        ).to_dict()
        if next_lock_indicator_config != self.lock_indicator.get_config():
            self.lock_indicator.set_config(next_lock_indicator_config)

    def sync_auto_relock_config_from_device(self) -> None:
        next_auto_relock_config = AutoRelockConfig.from_mapping(
            self._initial_auto_relock_config()
        ).to_dict()
        if next_auto_relock_config != self.auto_relock.get_config():
            self.auto_relock.set_config(next_auto_relock_config)
        if hasattr(self.auto_relock, "set_event_hook"):
            self.auto_relock.set_event_hook(self._on_auto_relock_event)

    def sync_influx_logging_state_from_device(self) -> None:
        next_influx_logging_state = self._initial_influx_logging_state()
        if next_influx_logging_state != self.influx_logging_state:
            self.influx_logging_state = next_influx_logging_state

    def sync_configs_from_device(self) -> None:
        self.sync_auto_lock_scan_settings_from_device()
        self.sync_lock_indicator_settings_from_device()
        self.sync_auto_relock_config_from_device()
        self.sync_influx_logging_state_from_device()

    # Backwards-compatible alias retained for existing tests/callers.
    def sync_lock_indicator_config_from_device(self) -> None:
        self.sync_configs_from_device()

    def get_lock_indicator_config(self) -> dict[str, Any]:
        return self.lock_indicator.get_config()

    def update_lock_indicator_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.lock_indicator.set_config(payload)

    def get_auto_lock_scan_settings(self) -> dict[str, Any]:
        return dict(self.auto_lock_scan_settings)

    def update_auto_lock_scan_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = AutoLockScanSettings.from_mapping(payload)
        self.auto_lock_scan_settings = settings.__dict__.copy()
        return self.get_auto_lock_scan_settings()

    def get_auto_relock_state(self) -> dict[str, Any]:
        return self.auto_relock.get_state()

    def update_auto_relock_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.auto_relock.set_config(payload)
        if hasattr(self.auto_relock, "set_event_hook"):
            self.auto_relock.set_event_hook(self._on_auto_relock_event)
        return self.get_auto_relock_state()

    def set_auto_relock_enabled(self, enabled: bool) -> dict[str, Any]:
        self.auto_relock.set_enabled(enabled)
        if hasattr(self.auto_relock, "set_event_hook"):
            self.auto_relock.set_event_hook(self._on_auto_relock_event)
        return self.get_auto_relock_state()

    def get_influx_logging_state(self) -> dict[str, Any]:
        return dict(self.influx_logging_state)

    def set_influx_logging_state(
        self,
        *,
        enabled: bool | None = None,
        interval_s: float | None = None,
    ) -> dict[str, Any]:
        current = dict(self.influx_logging_state)
        if enabled is not None:
            current["enabled"] = bool(enabled)
        if interval_s is not None and math.isfinite(float(interval_s)) and float(interval_s) > 0:
            current["interval_s"] = max(0.1, float(interval_s))
        self.influx_logging_state = self._normalize_influx_logging_state(current)
        return self.get_influx_logging_state()

    @staticmethod
    def _coerce_float(value: Any) -> float | None:
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(numeric):
            return None
        return numeric

    def _normalize_param_value(self, name: str, value: Any) -> Any:
        if name in FILTER_AUTOMATIC_PARAMS:
            numeric = self._coerce_float(value)
            return 2 if numeric is not None and numeric > 0 else 0

        if name == "channel_mixing":
            numeric = self._coerce_float(value)
            if numeric is None:
                return 0
            clamped = int(round(numeric))
            return max(-128, min(127, clamped))

        if name == "modulation_frequency":
            numeric = self._coerce_float(value)
            if numeric is None:
                return 0
            if numeric < 0:
                numeric = 0
            return int(round(numeric))

        return value

    @staticmethod
    def _value_needs_normalization(current: Any, normalized: Any) -> bool:
        return current != normalized or type(current) is not type(normalized)

    def _sanitize_parameters_on_connect(self) -> None:
        if self.parameters is None:
            return
        changed = False
        for name in NORMALIZED_PARAMS_ON_CONNECT:
            try:
                param = getattr(self.parameters, name)
                current = param.value
            except Exception:
                continue
            normalized = self._normalize_param_value(name, current)
            if self._value_needs_normalization(current, normalized):
                param.value = normalized
                changed = True
        if changed and self.control is not None:
            self.control.exposed_write_registers()

    @staticmethod
    def _disconnect_client_safely(client: LinienClient | None) -> None:
        if client is None:
            return
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001 - best effort cleanup
            logger.warning("Failed to disconnect Linien client cleanly", exc_info=True)

    def _reset_connection_state(self, *, last_error: str | None = None) -> None:
        client_to_close: LinienClient | None = None
        with self._lock:
            self._stop_event.set()
            client_to_close = self.client
            self.client = None
            self.control = None
            self.parameters = None
            self.connected = False
            self.connecting = False
            self.last_error = last_error
            self._last_lock_indicator_state = None
            self._last_auto_relock_state = None
        self._disconnect_client_safely(client_to_close)

    def _handle_poll_failure(self, exc: Exception) -> None:
        logger.warning(
            "Device poll loop failed for key=%s",
            self.device.key,
            exc_info=True,
        )
        self._emit_log_event(
            level=logging.ERROR,
            source="session",
            code="poll_failure",
            message="Device poll loop failed.",
            details={"error": str(exc)},
        )
        self._reset_connection_state(last_error=str(exc))

    def connect_async(self, autostart_server: bool = False) -> None:
        if self.connected or self.connecting:
            return
        thread = threading.Thread(
            target=self.connect, args=(autostart_server,), daemon=True
        )
        thread.start()

    def connect(self, autostart_server: bool = False) -> None:
        with self._lock:
            if self.connected or self.connecting:
                return
            self.connecting = True
        try:
            client = LinienClient(self.device)
            client.connect(
                autostart_server=autostart_server,
                use_parameter_cache=True,
            )
            self.client = client
            self.control = client.control
            self.parameters = client.parameters
            self.param_cache = {}
            self.param_cache_serialized = {}
            self.plot_state = PlotState()
            with self._rpyc_lock:
                self._sanitize_parameters_on_connect()
                should_resume_logging = bool(self.influx_logging_state.get("enabled", False))
                resume_interval = max(
                    0.1,
                    float(
                        self.influx_logging_state.get(
                            "interval_s", DEFAULT_INFLUX_LOGGING_INTERVAL_S
                        )
                    ),
                )
                if should_resume_logging:
                    try:
                        self.control.exposed_start_logging(resume_interval)
                    except Exception:  # noqa: BLE001 - optional resume path
                        logger.warning(
                            "Failed to resume influx logging for device=%s",
                            self.device.key,
                            exc_info=True,
                        )
            self.connected = True
            self.connecting = False
            self.last_error = None
            self._register_callbacks()
            self._stop_event.clear()
            self._poll_thread = threading.Thread(
                target=self._poll_loop, daemon=True
            )
            self._poll_thread.start()
        except (
            ServerNotRunningException,
            GeneralConnectionError,
            InvalidServerVersionException,
            RPYCAuthenticationException,
            Exception,
        ) as exc:
            self._reset_connection_state(last_error=str(exc))

    def start_server(self) -> None:
        self.connect_async(autostart_server=True)

    def disconnect(self) -> None:
        self._reset_connection_state(last_error=self.last_error)

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self.parameters is not None:
                    with self._rpyc_lock:
                        self.parameters.check_for_changed_parameters()
                    if (
                        self.last_plot_timestamp is None
                        or (time.time() - self.last_plot_timestamp) > 1.0
                    ):
                        try:
                            with self._rpyc_lock:
                                raw = self.parameters.to_plot.value
                            self._on_to_plot(raw)
                        except Exception:  # noqa: BLE001 - keep polling on transient frame errors
                            logger.debug(
                                "Transient to_plot processing failure device=%s",
                                self.device.key,
                                exc_info=True,
                            )
                time.sleep(0.05)
            except Exception as exc:
                self._handle_poll_failure(exc)

    def _register_callbacks(self) -> None:
        if self.parameters is None:
            return
        for name, param in self.parameters:
            if name == "to_plot":
                param.add_callback(self._on_to_plot)
            else:
                param.add_callback(
                    lambda value, n=name: self._on_param_changed(n, value),
                    call_immediately=True,
                )

    def _on_param_changed(self, name: str, value: Any) -> None:
        self.param_cache[name] = value
        if name in IGNORED_PARAMS:
            return
        encoded = to_jsonable(value)
        if encoded is UNSERIALIZABLE:
            return
        self.param_cache_serialized[name] = encoded
        self.manager.publish(
            self.device.key,
            {"type": "param_update", "name": name, "value": encoded},
        )

    def _decode_to_plot_payload(self, value: Any) -> dict[str, Any] | None:
        try:
            decoded = unpack(value)
            if isinstance(decoded, (bytes, bytearray)):
                maybe_plot = pickle.loads(decoded)
            elif isinstance(decoded, dict):
                maybe_plot = decoded
            else:
                maybe_plot = pickle.loads(bytes(decoded))
        except Exception:
            logger.debug(
                "Failed to decode to_plot payload device=%s",
                self.device.key,
                exc_info=True,
            )
            return None
        if isinstance(maybe_plot, dict):
            return maybe_plot
        logger.debug(
            "Decoded to_plot payload was not a mapping device=%s type=%s",
            self.device.key,
            type(maybe_plot).__name__,
        )
        return None

    def _derive_lock_value(self, to_plot: dict[str, Any]) -> bool:
        with self._rpyc_lock:
            if self.parameters is None:
                return False
            lock_value = bool(self.parameters.lock.value)
        if "error_signal" in to_plot and "control_signal" in to_plot:
            return True
        if "error_signal_1" in to_plot:
            return False
        return lock_value

    def _plot_params(self, lock_value: bool) -> dict[str, Any]:
        with self._rpyc_lock:
            if self.parameters is None:
                return {"lock": lock_value}
            return {
                "lock": lock_value,
                "dual_channel": self.parameters.dual_channel.value,
                "channel_mixing": self.parameters.channel_mixing.value,
                "combined_offset": self.parameters.combined_offset.value,
                "modulation_frequency": self.parameters.modulation_frequency.value,
                "pid_only_mode": self.parameters.pid_only_mode.value,
                "offset_a": self.parameters.offset_a.value,
                "offset_b": self.parameters.offset_b.value,
                "pid_on_slow_enabled": self.parameters.pid_on_slow_enabled.value,
                "autolock_preparing": self.parameters.autolock_preparing.value,
                "sweep_amplitude": self.parameters.sweep_amplitude.value,
                "autolock_initial_sweep_amplitude": self.parameters.autolock_initial_sweep_amplitude.value,
                "control_signal_history_length": self.parameters.control_signal_history_length.value,
            }

    def _on_to_plot(self, value: Any) -> None:
        if self.parameters is None:
            return
        if self.parameters.pause_acquisition.value:
            return
        if value is None:
            return
        to_plot = self._decode_to_plot_payload(value)
        if to_plot is None:
            return

        lock_value = self._derive_lock_value(to_plot)
        params = self._plot_params(lock_value)
        frame = build_plot_frame(to_plot, params, self.plot_state)
        if frame is None:
            return
        frame_lock_indicator = self.lock_indicator.update(
            lock=lock_value,
            to_plot=to_plot,
        )
        frame["lock_indicator"] = frame_lock_indicator
        indicator_state = (
            frame_lock_indicator.get("state")
            if isinstance(frame_lock_indicator, dict)
            else None
        )
        self._emit_lock_transition_log(
            lock_enabled=bool(lock_value),
            indicator_state=indicator_state if isinstance(indicator_state, str) else None,
            indicator_snapshot=(
                frame_lock_indicator if isinstance(frame_lock_indicator, dict) else {}
            ),
        )

        def _start_auto_relock() -> None:
            acquired = self._relock_action_lock.acquire(blocking=False)
            if not acquired:
                raise RuntimeError("relock_action_in_progress")
            try:
                self._emit_log_event(
                    level=logging.INFO,
                    source="auto_relock",
                    code="auto_relock_action_start",
                    message="Auto-relock action started.",
                )
                self.auto_lock_from_scan(None)
                self._emit_log_event(
                    level=logging.INFO,
                    source="auto_relock",
                    code="auto_relock_action_success",
                    message="Auto-relock action completed.",
                )
                self._write_lock_result_to_postgres(
                    lock_source="auto_relock",
                    event_source="auto_relock",
                )
            except Exception as exc:
                self._emit_log_event(
                    level=logging.ERROR,
                    source="auto_relock",
                    code="auto_relock_action_failed",
                    message="Auto-relock action failed.",
                    details={"error": str(exc)},
                )
                raise
            finally:
                self._relock_action_lock.release()

        self.auto_relock.tick(
            lock=lock_value,
            indicator_state=indicator_state if isinstance(indicator_state, str) else None,
            unlocked_trace_at=self.plot_state.last_unlocked_trace_at,
            start_sweep=self.stop_lock,
            start_relock=_start_auto_relock,
        )
        auto_relock_status = self.auto_relock.get_status()
        frame["auto_relock"] = auto_relock_status
        self._emit_auto_relock_state_transition_log(auto_relock_status)
        self.last_plot_frame = frame
        self.last_plot_timestamp = time.time()
        self.manager.publish(self.device.key, frame)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "params": self.param_cache_serialized,
            "plot_frame": self.last_plot_frame,
            "status": self.status(),
        }

    def status(self) -> Dict[str, Any]:
        logging_active = None
        lock_value = None
        if self.connected and self.control is not None:
            try:
                with self._rpyc_lock:
                    logging_active = self.control.exposed_get_logging_status()
                    if self.parameters is not None:
                        lock_value = bool(self.parameters.lock.value)
            except Exception:  # noqa: BLE001 - status endpoint should remain available
                logger.debug(
                    "Failed to query live status from control for device=%s",
                    self.device.key,
                    exc_info=True,
                )
                logging_active = None
                lock_value = None
        if lock_value is None and isinstance(self.last_plot_frame, dict):
            frame_lock = self.last_plot_frame.get("lock")
            if isinstance(frame_lock, bool):
                lock_value = frame_lock
        return {
            "connected": self.connected,
            "connecting": self.connecting,
            "last_error": self.last_error,
            "last_plot": self.last_plot_timestamp,
            "logging_active": logging_active,
            "lock": lock_value,
            "auto_relock": self.auto_relock.get_status(),
        }

    def set_param(self, name: str, value: Any, write_registers: bool) -> None:
        if self.parameters is None or self.control is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            param = getattr(self.parameters, name)
            param.value = self._normalize_param_value(name, value)
            if write_registers:
                self.control.exposed_write_registers()

    def write_registers(self) -> None:
        if self.control is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            self.control.exposed_write_registers()

    def start_lock(self) -> None:
        if self.control is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            self.control.exposed_start_lock()

    def auto_lock_from_scan(self, settings_payload: dict[str, Any] | None) -> dict[str, Any]:
        if self.control is None or self.parameters is None:
            raise RuntimeError("Device not connected")
        if self.plot_state.last_plot_data is None or len(self.plot_state.last_plot_data) < 3:
            raise RuntimeError("No unlocked trace available")

        error_trace = self.plot_state.last_plot_data[2]
        monitor_trace = self.plot_state.last_plot_data[1]
        if error_trace is None:
            raise RuntimeError("No error trace available")

        if settings_payload is None:
            settings = AutoLockScanSettings.from_mapping(self.auto_lock_scan_settings)
        else:
            settings = AutoLockScanSettings.from_mapping(settings_payload)
            self.auto_lock_scan_settings = settings.__dict__.copy()
        with self._rpyc_lock:
            if bool(self.parameters.lock.value):
                raise RuntimeError("Device is already locked. Start sweep first.")
            sweep_center = float(self.parameters.sweep_center.value)
            sweep_amplitude = float(self.parameters.sweep_amplitude.value)
            preferred_slope_rising = bool(self.parameters.target_slope_rising.value)

        error_trace_v = np.asarray(error_trace, dtype=float) / ADC_SCALE
        monitor_trace_v = (
            np.asarray(monitor_trace, dtype=float) / ADC_SCALE
            if monitor_trace is not None
            else None
        )
        result = find_auto_lock_target(
            error_trace_v=error_trace_v,
            monitor_trace_v=monitor_trace_v,
            sweep_center_v=sweep_center,
            sweep_amplitude_v=sweep_amplitude,
            settings=settings,
            preferred_slope_rising=preferred_slope_rising,
        )

        with self._rpyc_lock:
            self.parameters.sweep_center.value = float(result.target_voltage)
            self.control.exposed_write_registers()
            self.control.exposed_start_lock()

        payload = result.to_dict()
        payload["detail"] = "Auto-lock started from scan."
        return payload

    def build_manual_lock_row(
        self,
        *,
        device_name: str | None = None,
        device_key: str,
        lock_source: str = "manual_lock",
    ) -> dict[str, Any]:
        param_names = (
            "modulation_frequency",
            "modulation_amplitude",
            "demodulation_phase_a",
            "demodulation_phase_b",
            "offset_a",
            "offset_b",
            "control_channel",
            "p",
            "i",
            "d",
            "sweep_center",
            "sweep_amplitude",
        )
        params = self._collect_manual_lock_params(param_names)
        trace_values, monitor_trace_values = self._extract_manual_lock_traces()

        return build_manual_lock_row(
            device_name=device_name,
            device_key=device_key,
            lock_source=lock_source,
            params=params,
            trace_y=trace_values,
            monitor_trace_y=monitor_trace_values,
        )

    def _collect_manual_lock_params(self, names: tuple[str, ...]) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.parameters is not None:
            with self._rpyc_lock:
                for name in names:
                    try:
                        params[name] = getattr(self.parameters, name).value
                    except Exception:
                        params[name] = self.param_cache.get(name)
            return params
        for name in names:
            params[name] = self.param_cache.get(name)
        return params

    def _extract_manual_lock_traces(
        self,
    ) -> tuple[list[float] | None, list[float] | None]:
        trace_values: list[float] | None = None
        monitor_trace_values: list[float] | None = None
        plot_data = self.plot_state.last_plot_data
        if plot_data is not None and len(plot_data) >= 3:
            try:
                if plot_data[2] is not None:
                    combined_error = np.asarray(plot_data[2], dtype=float)
                    trace_values = (combined_error / ADC_SCALE).tolist()
                if plot_data[1] is not None:
                    monitor_trace = np.asarray(plot_data[1], dtype=float)
                    monitor_trace_values = (monitor_trace / ADC_SCALE).tolist()
            except Exception:
                logger.debug(
                    "Failed extracting lock traces from raw plot_data device=%s",
                    self.device.key,
                    exc_info=True,
                )
                trace_values = None
                monitor_trace_values = None
            return trace_values, monitor_trace_values

        if self.last_plot_frame is None:
            return None, None

        series = self.last_plot_frame.get("series", {})
        combined_series = series.get("combined_error")
        monitor_series = series.get("monitor_signal")
        if monitor_series is None:
            monitor_series = series.get("error_signal_2")
        if isinstance(combined_series, list):
            trace_values = [
                float(value) if value is not None else float("nan")
                for value in combined_series
            ]
        if isinstance(monitor_series, list):
            monitor_trace_values = [
                float(value) if value is not None else float("nan")
                for value in monitor_series
            ]
        return trace_values, monitor_trace_values

    def start_sweep(self) -> None:
        if self.control is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            self.control.exposed_start_sweep()

    def start_autolock(self, x0: int, x1: int) -> None:
        if AUTOMATION_TEMP_DISABLED:
            raise RuntimeError(
                f"Autolock is {AUTOMATION_TEMP_DISABLED_REASON}"
            )
        if self.control is None:
            raise RuntimeError("Device not connected")
        if self.plot_state.last_plot_data is None:
            raise RuntimeError("No plot data available")
        combined_error = self.plot_state.last_plot_data[2]
        additional = self.plot_state.combined_error_cache
        with self._rpyc_lock:
            self.control.exposed_start_autolock(
                x0,
                x1,
                pickle.dumps(combined_error),
                additional_spectra=pickle.dumps(additional),
            )
        try:
            (
                mean_signal,
                target_slope_rising,
                target_zoom,
                rolled_error_signal,
                line_width,
                peak_idxs,
            ) = get_lock_point(
                combined_error,
                *sorted([x0, x1]),
            )
            self.plot_state.autolock_ref_spectrum = rolled_error_signal
        except Exception:  # noqa: BLE001 - optional helper for lock target overlay
            logger.debug(
                "Failed computing autolock reference spectrum device=%s",
                self.device.key,
                exc_info=True,
            )
            self.plot_state.autolock_ref_spectrum = None

    def start_optimization(self, x0: int, x1: int) -> None:
        if AUTOMATION_TEMP_DISABLED:
            raise RuntimeError(
                f"Optimization is {AUTOMATION_TEMP_DISABLED_REASON}"
            )
        if self.control is None or self.parameters is None:
            raise RuntimeError("Device not connected")
        if self.plot_state.last_plot_data is None:
            raise RuntimeError("No plot data available")
        x0, x1 = sorted([int(x0), int(x1)])
        with self._rpyc_lock:
            dual_channel = bool(self.parameters.dual_channel.value)
            channel = int(self.parameters.optimization_channel.value)
        if not dual_channel:
            spectrum = self.plot_state.last_plot_data[0]
        else:
            spectrum = self.plot_state.last_plot_data[0 if channel == 0 else 1]
        cropped = np.array(spectrum[x0:x1], dtype=float)
        cropped = cropped[np.isfinite(cropped)]
        if cropped.size < 2:
            raise RuntimeError("Selected range is too small")
        if int(np.argmin(cropped)) == int(np.argmax(cropped)):
            raise RuntimeError("Selected range does not contain a slope")
        with self._rpyc_lock:
            self.control.exposed_start_optimization(
                x0, x1, pickle.dumps(spectrum)
            )

    def start_pid_optimization(self) -> None:
        if self.control is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            self.control.exposed_start_pid_optimization()

    def stop_lock(self) -> None:
        if self.parameters is None or self.control is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            self.parameters.fetch_additional_signals.value = True
            task = self.parameters.task.value
            if task is not None:
                if hasattr(task, "stop"):
                    try:
                        task.stop()
                    except TypeError:
                        task.stop(False)
                elif hasattr(task, "exposed_stop"):
                    try:
                        task.exposed_stop()
                    except TypeError:
                        task.exposed_stop(False)
                self.parameters.task.value = None
            self.control.exposed_start_sweep()

    def stop_task(self, use_new_parameters: bool = False) -> None:
        if self.parameters is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            task = self.parameters.task.value
        if task is None:
            return
        if hasattr(task, "stop"):
            try:
                with self._rpyc_lock:
                    task.stop(use_new_parameters)
                return
            except TypeError:
                with self._rpyc_lock:
                    task.stop()
                return
        if hasattr(task, "exposed_stop"):
            try:
                with self._rpyc_lock:
                    task.exposed_stop(use_new_parameters)
            except TypeError:
                with self._rpyc_lock:
                    task.exposed_stop()

    def shutdown_server(self) -> None:
        if self.control is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            self.control.exposed_shutdown()

    def logging_start(self, interval: float) -> dict[str, Any]:
        if self.control is None:
            raise RuntimeError("Device not connected")
        safe_interval = max(0.1, float(interval))
        with self._rpyc_lock:
            self.control.exposed_start_logging(safe_interval)
        return self.set_influx_logging_state(enabled=True, interval_s=safe_interval)

    def logging_stop(self) -> dict[str, Any]:
        if self.control is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            self.control.exposed_stop_logging()
        return self.set_influx_logging_state(enabled=False)

    def logging_set_param(self, name: str, enabled: bool) -> None:
        if self.control is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            self.control.exposed_set_parameter_log(name, enabled)

    def logging_get_credentials(self) -> InfluxDBCredentials:
        if self.control is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            return self.control.exposed_get_influxdb_credentials()

    def logging_update_credentials(
        self, credentials: InfluxDBCredentials
    ) -> tuple[bool, str]:
        if self.control is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            return self.control.exposed_update_influxdb_credentials(credentials)

    def param_metadata(self) -> List[Dict[str, Any]]:
        if self.parameters is None:
            raise RuntimeError("Device not connected")
        data = []
        with self._rpyc_lock:
            for name, param in self.parameters:
                data.append(
                    {
                        "name": name,
                        "restorable": bool(param.restorable),
                        "loggable": bool(param.loggable),
                        "log": bool(param.log),
                    }
                )
        return data

