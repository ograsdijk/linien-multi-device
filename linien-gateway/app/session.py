from __future__ import annotations

import pickle
import threading
import time
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


class DeviceSession:
    def __init__(self, device: Device, manager: WebsocketManager) -> None:
        self.device = device
        self.manager = manager
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
        self.param_cache: Dict[str, Any] = {}
        self.param_cache_serialized: Dict[str, Any] = {}
        self.plot_state = PlotState()

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
            self.last_error = str(exc)
            self.connected = False
            self.connecting = False

    def start_server(self) -> None:
        self.connect_async(autostart_server=True)

    def disconnect(self) -> None:
        with self._lock:
            self._stop_event.set()
            if self.client is not None:
                try:
                    self.client.disconnect()
                except Exception:
                    pass
            self.client = None
            self.control = None
            self.parameters = None
            self.connected = False
            self.connecting = False

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
                        except Exception:
                            pass
                time.sleep(0.05)
            except Exception as exc:
                self.last_error = str(exc)
                self.connected = False
                self.connecting = False
                self._stop_event.set()

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

    def _on_to_plot(self, value: Any) -> None:
        if self.parameters is None:
            return
        if self.parameters.pause_acquisition.value:
            return
        if value is None:
            return
        try:
            decoded = unpack(value)
            if isinstance(decoded, (bytes, bytearray)):
                to_plot = pickle.loads(decoded)
            elif isinstance(decoded, dict):
                to_plot = decoded
            else:
                try:
                    to_plot = pickle.loads(bytes(decoded))
                except Exception:
                    to_plot = decoded
        except Exception:
            return

        lock_value = bool(self.parameters.lock.value)
        if isinstance(to_plot, dict):
            if "error_signal" in to_plot and "control_signal" in to_plot:
                lock_value = True
            elif "error_signal_1" in to_plot:
                lock_value = False

        with self._rpyc_lock:
            params = {
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
        frame = build_plot_frame(to_plot, params, self.plot_state)
        if frame is None:
            return
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
        if self.connected and self.control is not None:
            try:
                with self._rpyc_lock:
                    logging_active = self.control.exposed_get_logging_status()
            except Exception:
                logging_active = None
        return {
            "connected": self.connected,
            "connecting": self.connecting,
            "last_error": self.last_error,
            "last_plot": self.last_plot_timestamp,
            "logging_active": logging_active,
        }

    def set_param(self, name: str, value: Any, write_registers: bool) -> None:
        if self.parameters is None or self.control is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            param = getattr(self.parameters, name)
            param.value = value
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

    def start_sweep(self) -> None:
        if self.control is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            self.control.exposed_start_sweep()

    def start_autolock(self, x0: int, x1: int) -> None:
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
            mean_signal, target_slope_rising, target_zoom, rolled_error_signal, line_width, peak_idxs = get_lock_point(
                combined_error,
                *sorted([x0, x1]),
            )
            self.plot_state.autolock_ref_spectrum = rolled_error_signal
        except Exception:
            self.plot_state.autolock_ref_spectrum = None

    def start_optimization(self, x0: int, x1: int) -> None:
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

    def logging_start(self, interval: float) -> None:
        if self.control is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            self.control.exposed_start_logging(interval)

    def logging_stop(self) -> None:
        if self.control is None:
            raise RuntimeError("Device not connected")
        with self._rpyc_lock:
            self.control.exposed_stop_logging()

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

