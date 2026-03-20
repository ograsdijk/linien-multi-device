from __future__ import annotations

import threading
import time
from copy import copy
from typing import Any

import rpyc
from linien_common.communication import (
    hash_username_and_password,
    pack,
    unpack,
)
from linien_common.influxdb import InfluxDBCredentials
from rpyc.core.protocol import Connection
from rpyc.utils.authenticators import AuthenticationError

from .model import SimulatorStatus, VirtualPdhModel
from .parameters import MHZ_UNIT, VPP_UNIT, SimParameters

SERVER_VERSION = "2.1.0"


class VirtualLinienControlService(rpyc.Service):
    def __init__(
        self,
        *,
        username: str,
        password: str,
        no_auth: bool,
        frame_rate_hz: float = 20.0,
        seed: int = 0,
        linewidth_hz: float | None = None,
        linewidth_v: float | None = None,
        fsr_hz: float | None = None,
        jitter_v: float | None = None,
    ) -> None:
        super().__init__()
        self.parameters = SimParameters()
        self.model = VirtualPdhModel(seed=seed)
        if linewidth_hz is not None:
            self.model.set_linewidth_hz(linewidth_hz)
        if linewidth_v is not None:
            self.model.set_linewidth_v(linewidth_v)
        if fsr_hz is not None:
            self.model.set_fsr_hz(fsr_hz)
        if jitter_v is not None:
            self.model.set_detuning_jitter(jitter_v)
        self.frame_period_s = 1.0 / max(2.0, frame_rate_hz)

        self._username = username
        self._password = password
        self._no_auth = no_auth
        self._expected_auth_hash = hash_username_and_password(username, password)

        self._uuid_mapping: dict[Connection, str] = {}
        self._sim_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._sim_thread: threading.Thread | None = None

        self._influx_credentials = InfluxDBCredentials()
        self._logging_stop_event = threading.Event()
        self._logging_stop_event.set()
        self._logging_thread: threading.Thread | None = None
        self._last_logging_fields: dict[str, Any] = {}

    def make_authenticator(self):
        expected = self._expected_auth_hash
        no_auth = self._no_auth

        def _authenticator(sock):
            client_hash = sock.recv(64).decode()
            if no_auth:
                return sock, None
            if client_hash != expected:
                raise AuthenticationError("Authentication hashes do not match.")
            return sock, None

        return _authenticator

    def start(self) -> None:
        if self._sim_thread and self._sim_thread.is_alive():
            return
        self._stop_event.clear()
        self._sim_thread = threading.Thread(
            target=self._simulation_loop,
            daemon=True,
            name="linien-sim-loop",
        )
        self._sim_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._logging_stop_event.set()
        if self._logging_thread and self._logging_thread.is_alive():
            self._logging_thread.join(timeout=1.0)
        self._logging_thread = None
        if self._sim_thread and self._sim_thread.is_alive():
            self._sim_thread.join(timeout=2.0)
        self._sim_thread = None

    def _simulation_loop(self) -> None:
        last_tick = time.monotonic()
        last_ping = last_tick
        while not self._stop_event.is_set():
            now = time.monotonic()
            dt = max(1e-4, min(0.2, now - last_tick))
            last_tick = now

            with self._sim_lock:
                self.model.advance(dt, self.parameters)
                if not bool(self.parameters.pause_acquisition.value):
                    to_plot = self.model.build_plot(self.parameters)
                    self.parameters.to_plot.value = pack(to_plot)
                    self.parameters.signal_stats.value = self.model.build_signal_stats(to_plot)
                    self.parameters.control_signal_history.value = {
                        "times": [],
                        "values": [],
                        "slow_times": [],
                        "slow_values": [],
                    }
                    self.parameters.monitor_signal_history.value = {"times": [], "values": []}

            if now - last_ping >= 1.0:
                self.parameters.ping.value = int(self.parameters.ping.value) + 1
                last_ping = now

            time.sleep(self.frame_period_s)

    def _gather_logged_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        for name, param in self.parameters:
            if not param.log:
                continue
            if name == "signal_stats" and isinstance(param.value, dict):
                for key, value in param.value.items():
                    fields[key] = value
            else:
                fields[name] = param.value
        return fields

    def _logging_loop(self, interval_s: float) -> None:
        while not self._logging_stop_event.is_set():
            with self._sim_lock:
                self._last_logging_fields = self._gather_logged_fields()
            time.sleep(max(0.05, interval_s))

    def on_connect(self, conn: Connection) -> None:
        try:
            uuid = conn.root.uuid
        except Exception:  # noqa: BLE001 - compatibility with non-Linien clients
            uuid = f"client-{id(conn)}"
        self._uuid_mapping[conn] = uuid

    def on_disconnect(self, conn: Connection) -> None:
        uuid = self._uuid_mapping.pop(conn, None)
        if uuid:
            self.parameters.unregister_remote_listeners(uuid)

    def exposed_get_server_version(self) -> str:
        return SERVER_VERSION

    def exposed_get_param(self, param_name: str) -> Any:
        return pack(getattr(self.parameters, param_name).value)

    def exposed_set_param(self, param_name: str, value: Any) -> None:
        with self._sim_lock:
            getattr(self.parameters, param_name).value = unpack(value)

    def exposed_reset_param(self, param_name: str) -> None:
        with self._sim_lock:
            getattr(self.parameters, param_name).reset()

    def exposed_init_parameter_sync(
        self, uuid: str
    ) -> list[tuple[str, Any, bool, bool, bool, bool]]:
        return list(self.parameters.init_parameter_sync(uuid))

    def exposed_register_remote_listener(self, uuid: str, param_name: str) -> None:
        self.parameters.register_remote_listener(uuid, param_name)

    def exposed_register_remote_listeners(
        self, uuid: str, param_names: list[str]
    ) -> None:
        self.parameters.register_remote_listeners(uuid, param_names)

    def exposed_get_changed_parameters_queue(self, uuid: str) -> list[tuple[str, Any]]:
        return self.parameters.get_changed_parameters_queue(uuid)

    def exposed_set_parameter_log(self, param_name: str, value: bool) -> None:
        getattr(self.parameters, param_name).log = bool(value)

    def exposed_get_parameter_log(self, param_name: str) -> bool:
        return bool(getattr(self.parameters, param_name).log)

    def exposed_update_influxdb_credentials(
        self, credentials: InfluxDBCredentials
    ) -> tuple[bool, str]:
        self._influx_credentials = copy(credentials)
        return True, "Simulator accepted credentials (no external write configured)."

    def exposed_get_influxdb_credentials(self) -> InfluxDBCredentials:
        return self._influx_credentials

    def exposed_start_logging(self, interval: float) -> None:
        if self._logging_thread and self._logging_thread.is_alive():
            return
        self._logging_stop_event.clear()
        self._logging_thread = threading.Thread(
            target=self._logging_loop,
            args=(float(interval),),
            daemon=True,
            name="linien-sim-logging",
        )
        self._logging_thread.start()

    def exposed_stop_logging(self) -> None:
        self._logging_stop_event.set()
        if self._logging_thread and self._logging_thread.is_alive():
            self._logging_thread.join(timeout=1.0)
        self._logging_thread = None

    def exposed_get_logging_status(self) -> bool:
        return not self._logging_stop_event.is_set()

    def exposed_write_registers(self) -> None:
        return

    def exposed_start_autolock(self, x0, x1, spectrum, additional_spectra=None) -> None:
        _ = (x0, x1, spectrum, additional_spectra)
        self.parameters.autolock_running.value = True
        self.parameters.autolock_running.value = False

    def exposed_start_optimization(self, x0, x1, spectrum) -> None:
        _ = (x0, x1, spectrum)
        self.parameters.optimization_running.value = True

    def exposed_start_psd_acquisition(self) -> None:
        self.parameters.psd_acquisition_running.value = True

    def exposed_start_pid_optimization(self) -> None:
        self.parameters.psd_optimization_running.value = True

    def exposed_start_sweep(self) -> None:
        with self._sim_lock:
            self.parameters.combined_offset.value = 0
            self.parameters.lock.value = False

    def exposed_start_lock(self) -> None:
        with self._sim_lock:
            self.model.initialize_control_from_sweep_center(
                float(self.parameters.sweep_center.value)
            )
            self.parameters.lock.value = True

    def exposed_shutdown(self) -> None:
        self.stop()
        raise SystemExit()

    def exposed_pause_acquisition(self) -> None:
        self.parameters.pause_acquisition.value = True

    def exposed_continue_acquisition(self) -> None:
        self.parameters.pause_acquisition.value = False

    def exposed_set_csr_direct(self, key: str, value: int) -> None:
        _ = (key, value)
        return

    # CLI helper methods
    def cli_status(self) -> SimulatorStatus:
        with self._sim_lock:
            return self.model.snapshot(self.parameters)

    def cli_set_noise(self, sigma_v: float) -> None:
        with self._sim_lock:
            self.model.set_noise_sigma(sigma_v)

    def cli_set_drift(self, drift_v_per_s: float) -> None:
        with self._sim_lock:
            self.model.set_drift(drift_v_per_s)

    def cli_set_walk(self, sigma_v_per_sqrt_s: float) -> None:
        with self._sim_lock:
            self.model.set_walk_sigma(sigma_v_per_sqrt_s)

    def cli_step_disturbance(self, delta_v: float) -> None:
        with self._sim_lock:
            self.model.step_disturbance(delta_v)

    def cli_schedule_ramp(self, delta_v: float, duration_s: float) -> None:
        with self._sim_lock:
            self.model.schedule_ramp(delta_v, duration_s)

    def cli_kick(self, delta_v: float) -> None:
        with self._sim_lock:
            self.model.kick_detuning(delta_v)

    def cli_set_monitor_mode(self, mode: str) -> None:
        with self._sim_lock:
            self.model.set_monitor_mode(mode)

    def cli_set_seed(self, seed: int) -> None:
        with self._sim_lock:
            self.model.set_seed(seed)

    def cli_set_modfreq_hz(self, modulation_hz: float) -> None:
        with self._sim_lock:
            self.parameters.modulation_frequency.value = int(
                max(0.0, modulation_hz) / 1_000_000.0 * MHZ_UNIT
            )

    def cli_set_modamp_vpp(self, amplitude_vpp: float) -> None:
        with self._sim_lock:
            self.parameters.modulation_amplitude.value = int(
                max(0.0, amplitude_vpp) * VPP_UNIT
            )

    def cli_set_phase_deg(self, phase_deg: float, channel: str = "active") -> None:
        with self._sim_lock:
            if channel == "a":
                self.parameters.demodulation_phase_a.value = phase_deg
                return
            if channel == "b":
                self.parameters.demodulation_phase_b.value = phase_deg
                return
            if int(float(self.parameters.control_channel.value)) == 1:
                self.parameters.demodulation_phase_b.value = phase_deg
            else:
                self.parameters.demodulation_phase_a.value = phase_deg

    def cli_set_pid(self, p: float, i: float, d: float) -> None:
        with self._sim_lock:
            self.parameters.p.value = p
            self.parameters.i.value = i
            self.parameters.d.value = d

    def cli_set_pid_p(self, p: float) -> None:
        with self._sim_lock:
            self.parameters.p.value = float(p)

    def cli_set_pid_i(self, i: float) -> None:
        with self._sim_lock:
            self.parameters.i.value = float(i)

    def cli_set_pid_d(self, d: float) -> None:
        with self._sim_lock:
            self.parameters.d.value = float(d)

    def cli_set_linewidth_hz(self, linewidth_hz: float) -> None:
        with self._sim_lock:
            self.model.set_linewidth_hz(linewidth_hz)

    def cli_set_linewidth_v(self, linewidth_v: float) -> None:
        with self._sim_lock:
            self.model.set_linewidth_v(linewidth_v)

    def cli_set_fsr_hz(self, fsr_hz: float) -> None:
        with self._sim_lock:
            self.model.set_fsr_hz(fsr_hz)

    def cli_set_detuning_jitter(self, sigma_v: float) -> None:
        with self._sim_lock:
            self.model.set_detuning_jitter(sigma_v)

    def cli_get_tunables(self) -> dict[str, Any]:
        with self._sim_lock:
            control_channel = int(float(self.parameters.control_channel.value))
            phase_a = float(self.parameters.demodulation_phase_a.value)
            phase_b = float(self.parameters.demodulation_phase_b.value)
            phase_active = phase_b if control_channel == 1 else phase_a
            return {
                "noise_sigma_v": float(self.model.noise_sigma_v),
                "detuning_jitter_v": float(self.model.detuning_jitter_v),
                "drift_v_per_s": float(self.model.drift_v_per_s),
                "walk_sigma_v_sqrt_s": float(self.model.walk_sigma_v_sqrt_s),
                "modulation_hz": float(self.parameters.modulation_frequency.value)
                / MHZ_UNIT
                * 1_000_000.0,
                "modulation_amp_vpp": float(self.parameters.modulation_amplitude.value)
                / VPP_UNIT,
                "phase_active_deg": phase_active,
                "phase_a_deg": phase_a,
                "phase_b_deg": phase_b,
                "pid_p": float(self.parameters.p.value),
                "pid_i": float(self.parameters.i.value),
                "pid_d": float(self.parameters.d.value),
                "linewidth_hz": float(self.model.linewidth_hz),
                "linewidth_v": float(self.model.linewidth_v),
                "fsr_hz": float(self.model.fsr_hz),
                "monitor_mode": str(self.model.monitor_mode),
                "control_channel": control_channel,
            }
