from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from linien_common.common import N_POINTS

from .parameters import MHZ_UNIT, VPP_UNIT

ADC_SCALE = 8192.0
OFFSET_SCALE = 8191.0


def _clip(value: float, low: float, high: float) -> float:
    return float(min(high, max(low, value)))


def _to_adc(values: np.ndarray | float) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    return np.clip(np.rint(arr * ADC_SCALE), -8191, 8191).astype(np.int32)


def _convert_channel_mixing_value(value: int) -> tuple[int, int]:
    if value <= 0:
        a_value = 128
        b_value = 128 + value
    else:
        a_value = 127 - value
        b_value = 128
    return a_value, b_value


@dataclass
class SimulatorStatus:
    lock: bool
    laser_detuning_v: float
    disturbance_offset_v: float
    control_output_v: float
    effective_detuning_v: float
    noise_sigma_v: float
    drift_v_per_s: float
    walk_sigma_v_sqrt_s: float
    monitor_mode: str
    modulation_hz: float
    modulation_amp_vpp: float
    linewidth_hz: float
    linewidth_v: float
    fsr_hz: float
    scan_hz_per_v: float
    detuning_jitter_v: float


class VirtualPdhModel:
    def __init__(self, seed: int = 0) -> None:
        self.rng = np.random.default_rng(seed)
        self.noise_sigma_v = 0.01
        self.drift_v_per_s = 0.0
        self.walk_sigma_v_sqrt_s = 0.003
        self.detuning_jitter_v = 0.0002

        self.monitor_mode = "reflection"
        self.laser_detuning_v = 0.0
        self.disturbance_offset_v = 0.0
        self.control_output_v = 0.0
        self.control_bias_v = 0.0
        self.pid_integrator = 0.0
        self.prev_error_v = 0.0
        self.last_effective_detuning_v = 0.0
        self.last_error_v = 0.0

        self.ramp_remaining_v = 0.0
        self.ramp_rate_v_per_s = 0.0

        self.linewidth_v = 0.01
        self.linewidth_hz = 200_000.0
        self.fsr_hz = 1_000_000_000.0
        self.scan_hz_per_v = 1.0
        self.input_mirror_r = 0.99
        self.end_mirror_r = 0.99
        self._recalculate_cavity_from_linewidth()
        self.mod_index_per_vpp = 0.9
        self.control_limit_v = 1.0
        self.actuator_gain = 0.9

    def _electronics_noise(
        self,
        *,
        size: int | tuple[int, ...] | None = None,
    ) -> np.ndarray | float:
        # Keep this as a comparatively small floor so detuning jitter dominates
        # error fluctuations near steep PDH slopes.
        scale = self.noise_sigma_v * 0.08
        if size is None:
            return float(scale * self.rng.normal())
        return scale * self.rng.normal(size=size)

    @property
    def _laser_with_disturbance(self) -> float:
        return self.laser_detuning_v + self.disturbance_offset_v

    def set_seed(self, seed: int) -> None:
        self.rng = np.random.default_rng(seed)

    def set_noise_sigma(self, sigma_v: float) -> None:
        self.noise_sigma_v = max(0.0, float(sigma_v))

    def set_drift(self, drift_v_per_s: float) -> None:
        self.drift_v_per_s = float(drift_v_per_s)

    def set_walk_sigma(self, sigma_v_per_sqrt_s: float) -> None:
        self.walk_sigma_v_sqrt_s = max(0.0, float(sigma_v_per_sqrt_s))

    def set_detuning_jitter(self, sigma_v: float) -> None:
        self.detuning_jitter_v = max(0.0, float(sigma_v))

    def _recalculate_cavity_from_linewidth(self) -> None:
        self.scan_hz_per_v = self.linewidth_hz / max(self.linewidth_v, 1e-6)
        finesse = self.fsr_hz / max(self.linewidth_hz, 1.0)
        y = (-math.pi + math.sqrt((math.pi * math.pi) + (4.0 * finesse * finesse))) / (
            2.0 * finesse
        )
        self.input_mirror_r = _clip(y, 0.0, 0.99999)
        self.end_mirror_r = self.input_mirror_r

    def set_linewidth_hz(self, linewidth_hz: float) -> None:
        linewidth = float(linewidth_hz)
        if not math.isfinite(linewidth) or linewidth <= 0:
            raise ValueError("linewidth_hz must be > 0")
        self.linewidth_hz = max(100.0, linewidth)
        self._recalculate_cavity_from_linewidth()

    def set_linewidth_v(self, linewidth_v: float) -> None:
        linewidth = float(linewidth_v)
        if not math.isfinite(linewidth) or linewidth <= 0:
            raise ValueError("linewidth_v must be > 0")
        self.linewidth_v = max(1e-5, linewidth)
        self._recalculate_cavity_from_linewidth()

    def set_fsr_hz(self, fsr_hz: float) -> None:
        fsr = float(fsr_hz)
        if not math.isfinite(fsr) or fsr <= 0:
            raise ValueError("fsr_hz must be > 0")
        self.fsr_hz = max(1_000.0, fsr)
        self._recalculate_cavity_from_linewidth()

    def initialize_control_from_sweep_center(self, sweep_center_v: float) -> None:
        target_voltage = float(sweep_center_v)
        if not math.isfinite(target_voltage):
            return
        gain = max(abs(self.actuator_gain), 1e-6)
        desired_control = target_voltage / gain
        self.control_output_v = _clip(
            desired_control,
            -self.control_limit_v,
            self.control_limit_v,
        )
        # Preserve the lock handover operating point as PID feedforward bias.
        self.control_bias_v = self.control_output_v
        self.pid_integrator = 0.0
        self.prev_error_v = 0.0
        self.last_effective_detuning_v = self._laser_with_disturbance - (
            self.control_output_v * self.actuator_gain
        )

    def set_monitor_mode(self, mode: str) -> None:
        mode = mode.strip().lower()
        if mode not in {"reflection", "transmission"}:
            raise ValueError("monitor mode must be 'reflection' or 'transmission'")
        self.monitor_mode = mode

    def step_disturbance(self, delta_v: float) -> None:
        self.disturbance_offset_v += float(delta_v)

    def kick_detuning(self, delta_v: float) -> None:
        self.laser_detuning_v = _clip(self.laser_detuning_v + float(delta_v), -1.2, 1.2)

    def schedule_ramp(self, delta_v: float, duration_s: float) -> None:
        delta = float(delta_v)
        duration = float(duration_s)
        if duration <= 0:
            self.step_disturbance(delta)
            return
        self.ramp_remaining_v = delta
        self.ramp_rate_v_per_s = delta / duration

    @staticmethod
    def _j0(x: float) -> float:
        x2 = x * x
        return (
            1.0
            - x2 / 4.0
            + (x2 * x2) / 64.0
            - (x2 * x2 * x2) / 2304.0
            + (x2 * x2 * x2 * x2) / 147456.0
        )

    @staticmethod
    def _j1(x: float) -> float:
        x2 = x * x
        return (
            x / 2.0
            - (x * x2) / 16.0
            + (x * x2 * x2) / 384.0
            - (x * x2 * x2 * x2) / 18432.0
        )

    def _cavity_reflection(self, detuning_hz: np.ndarray | float) -> np.ndarray:
        detuning_arr = np.asarray(detuning_hz, dtype=float)
        phase = 2.0 * math.pi * detuning_arr / max(self.fsr_hz, 1.0)
        exp_term = np.exp(1j * phase)
        numerator = self.input_mirror_r - (self.end_mirror_r * exp_term)
        denominator = 1.0 - (self.input_mirror_r * self.end_mirror_r * exp_term)
        return numerator / denominator

    def _cavity_transmission_power(self, detuning_hz: np.ndarray | float) -> np.ndarray:
        detuning_arr = np.asarray(detuning_hz, dtype=float)
        phase = 2.0 * math.pi * detuning_arr / max(self.fsr_hz, 1.0)
        exp_full = np.exp(1j * phase)
        exp_half = np.exp(0.5j * phase)
        transmission_prefactor = math.sqrt(max(0.0, 1.0 - (self.input_mirror_r**2))) * (
            math.sqrt(max(0.0, 1.0 - (self.end_mirror_r**2)))
        )
        denominator = 1.0 - (self.input_mirror_r * self.end_mirror_r * exp_full)
        field = transmission_prefactor * exp_half / denominator
        return np.abs(field) ** 2

    def _modulation_frequency_hz(self, raw_modulation: Any) -> float:
        value = float(raw_modulation)
        return max(0.0, (value / MHZ_UNIT) * 1_000_000.0)

    def _modulation_amplitude_vpp(self, raw_modulation_amp: Any) -> float:
        return max(0.0, float(raw_modulation_amp) / VPP_UNIT)

    def _pdh_error(
        self,
        detuning_v: np.ndarray | float,
        *,
        modulation_hz: float,
        modulation_vpp: float,
        demod_phase_deg: float,
        demod_multiplier: float,
    ) -> np.ndarray:
        detuning_hz = np.asarray(detuning_v, dtype=float) * self.scan_hz_per_v
        sideband_hz = max(1.0, modulation_hz)
        harmonic = max(1.0, float(demod_multiplier))
        harmonic_gain = 1.0 if harmonic <= 1.0 else (1.0 / (harmonic**2.2))
        beta = _clip(modulation_vpp * self.mod_index_per_vpp, 0.0, 2.8)
        j0 = self._j0(beta)
        j1 = self._j1(beta)
        r0 = self._cavity_reflection(detuning_hz)
        rp = self._cavity_reflection(detuning_hz + sideband_hz)
        rm = self._cavity_reflection(detuning_hz - sideband_hz)
        demod_complex = r0 * np.conj(rp) - np.conj(r0) * rm
        phase = math.radians(demod_phase_deg)
        mixed = (np.real(demod_complex) * math.cos(phase)) + (
            np.imag(demod_complex) * math.sin(phase)
        )
        return 2.0 * harmonic_gain * j0 * j1 * mixed

    def _monitor_signal(
        self, detuning_v: np.ndarray | float, *, modulation_hz: float, modulation_vpp: float
    ) -> np.ndarray:
        detuning_hz = np.asarray(detuning_v, dtype=float) * self.scan_hz_per_v
        sideband_hz = max(1.0, modulation_hz)
        beta = _clip(modulation_vpp * self.mod_index_per_vpp, 0.0, 2.8)
        j0 = self._j0(beta)
        j1 = self._j1(beta)

        if self.monitor_mode == "reflection":
            r0 = self._cavity_reflection(detuning_hz)
            rp = self._cavity_reflection(detuning_hz + sideband_hz)
            rm = self._cavity_reflection(detuning_hz - sideband_hz)
            power = (j0 * j0) * np.abs(r0) ** 2 + (j1 * j1) * (
                np.abs(rp) ** 2 + np.abs(rm) ** 2
            )
        else:
            t0 = self._cavity_transmission_power(detuning_hz)
            tp = self._cavity_transmission_power(detuning_hz + sideband_hz)
            tm = self._cavity_transmission_power(detuning_hz - sideband_hz)
            power = (j0 * j0) * t0 + (j1 * j1) * (tp + tm)

        return 1.6 * (power - 0.5)

    @staticmethod
    def _apply_invert_and_offset(
        signal: np.ndarray,
        *,
        invert: bool,
        offset_bits: float,
    ) -> np.ndarray:
        adjusted = -signal if invert else signal
        return adjusted + (float(offset_bits) / OFFSET_SCALE)

    def _channel_error_and_quadrature(
        self,
        detuning_v: np.ndarray | float,
        *,
        modulation_hz: float,
        modulation_vpp: float,
        demod_phase_deg: float,
        demod_multiplier: float,
        invert: bool,
        offset_bits: float,
    ) -> tuple[np.ndarray, np.ndarray]:
        error = self._pdh_error(
            detuning_v,
            modulation_hz=modulation_hz,
            modulation_vpp=modulation_vpp,
            demod_phase_deg=demod_phase_deg,
            demod_multiplier=demod_multiplier,
        )
        quadrature = self._pdh_error(
            detuning_v,
            modulation_hz=modulation_hz,
            modulation_vpp=modulation_vpp,
            demod_phase_deg=demod_phase_deg + 90.0,
            demod_multiplier=demod_multiplier,
        )
        return (
            self._apply_invert_and_offset(
                error,
                invert=invert,
                offset_bits=offset_bits,
            ),
            self._apply_invert_and_offset(
                quadrature,
                invert=invert,
                offset_bits=offset_bits,
            ),
        )

    def _combine_dual_error(
        self,
        error_a: np.ndarray,
        error_b: np.ndarray,
        *,
        channel_mixing: int,
        combined_offset: float,
    ) -> np.ndarray:
        a_factor, b_factor = _convert_channel_mixing_value(int(channel_mixing))
        combined = ((a_factor * error_a) + (b_factor * error_b)) / 256.0
        return combined + (float(combined_offset) / OFFSET_SCALE)

    def _combined_error_signal(
        self,
        detuning_v: np.ndarray | float,
        params: Any,
        *,
        apply_target_slope: bool = True,
    ) -> np.ndarray:
        modulation_hz = self._modulation_frequency_hz(params.modulation_frequency.value)
        modulation_vpp = self._modulation_amplitude_vpp(params.modulation_amplitude.value)

        error_a, _ = self._channel_error_and_quadrature(
            detuning_v,
            modulation_hz=modulation_hz,
            modulation_vpp=modulation_vpp,
            demod_phase_deg=float(params.demodulation_phase_a.value),
            demod_multiplier=float(params.demodulation_multiplier_a.value),
            invert=bool(params.invert_a.value),
            offset_bits=float(params.offset_a.value),
        )
        if bool(params.dual_channel.value):
            error_b, _ = self._channel_error_and_quadrature(
                detuning_v,
                modulation_hz=modulation_hz,
                modulation_vpp=modulation_vpp,
                demod_phase_deg=float(params.demodulation_phase_b.value),
                demod_multiplier=float(params.demodulation_multiplier_b.value),
                invert=bool(params.invert_b.value),
                offset_bits=float(params.offset_b.value),
            )
            combined = self._combine_dual_error(
                error_a,
                error_b,
                channel_mixing=int(params.channel_mixing.value),
                combined_offset=float(params.combined_offset.value),
            )
        else:
            combined = error_a

        if apply_target_slope and not bool(params.target_slope_rising.value):
            combined = -combined
        return np.asarray(combined, dtype=float)

    def determine_target_slope_rising(self, params: Any) -> bool:
        probe = max(self.linewidth_v * 0.01, 1e-4)
        lower = float(
            self._combined_error_signal(
                -probe,
                params,
                apply_target_slope=False,
            )
        )
        upper = float(
            self._combined_error_signal(
                probe,
                params,
                apply_target_slope=False,
            )
        )
        return upper >= lower

    def _apply_ramp(self, dt_s: float) -> None:
        if abs(self.ramp_remaining_v) <= 1e-12 or abs(self.ramp_rate_v_per_s) <= 1e-12:
            return
        step = self.ramp_rate_v_per_s * dt_s
        if abs(step) >= abs(self.ramp_remaining_v):
            self.disturbance_offset_v += self.ramp_remaining_v
            self.ramp_remaining_v = 0.0
            self.ramp_rate_v_per_s = 0.0
            return
        self.disturbance_offset_v += step
        self.ramp_remaining_v -= step

    def _advance_environment(self, dt_s: float) -> None:
        self._apply_ramp(dt_s)
        walk = self.walk_sigma_v_sqrt_s * math.sqrt(max(dt_s, 1e-6)) * self.rng.normal()
        self.laser_detuning_v = _clip(
            self.laser_detuning_v + (self.drift_v_per_s * dt_s) + walk, -1.2, 1.2
        )

    def _advance_pid(self, dt_s: float, params: Any) -> None:
        effective_detuning = self._laser_with_disturbance - (
            self.control_output_v * self.actuator_gain
        )
        effective_detuning_for_error = effective_detuning + (
            self.detuning_jitter_v * self.rng.normal()
        )
        error = float(
            self._combined_error_signal(effective_detuning_for_error, params)
        )
        error += float(self._electronics_noise())

        kp = float(params.p.value) / 2200.0
        ki = float(params.i.value) / 5000.0
        kd = float(params.d.value) / 10000.0

        # Keep integral authority consistent with output rails.
        # A fixed small integrator clamp creates an artificial ceiling (for example ~0.3)
        # even when control_output has not reached its true +/- control_limit_v bounds.
        if abs(ki) > 1e-9:
            integrator_limit = min(
                200.0,
                max(1.5, self.control_limit_v / abs(ki)),
            )
        else:
            integrator_limit = 1.5
        i_prev = self.pid_integrator
        i_candidate = _clip(
            self.pid_integrator + (error * dt_s),
            -integrator_limit,
            integrator_limit,
        )
        derivative = (error - self.prev_error_v) / max(dt_s, 1e-4)
        # Positive error should drive the actuator toward lower detuning.
        # The plant relation is: effective_detuning = laser - control * gain.
        # Therefore controller sign must be positive for negative feedback.
        control_candidate = (
            self.control_bias_v + kp * error + ki * i_candidate + kd * derivative
        )
        saturated_high = control_candidate > self.control_limit_v
        saturated_low = control_candidate < -self.control_limit_v
        drives_further_high = saturated_high and (error > 0)
        drives_further_low = saturated_low and (error < 0)
        if drives_further_high or drives_further_low:
            self.pid_integrator = i_prev
            control = self.control_bias_v + kp * error + ki * i_prev + kd * derivative
        else:
            self.pid_integrator = i_candidate
            control = control_candidate
        self.control_output_v = _clip(control, -self.control_limit_v, self.control_limit_v)
        self.prev_error_v = error
        self.last_error_v = error
        self.last_effective_detuning_v = effective_detuning_for_error

    def advance(self, dt_s: float, params: Any) -> None:
        self._advance_environment(dt_s)
        if bool(params.lock.value):
            self._advance_pid(dt_s, params)
        else:
            self.pid_integrator *= 0.9
            self.control_output_v *= 0.85
            self.last_effective_detuning_v = self._laser_with_disturbance
            self.last_error_v = 0.0

    def _build_unlocked_plot(self, params: Any) -> dict[str, np.ndarray]:
        center = float(params.sweep_center.value)
        amplitude = max(0.001, float(params.sweep_amplitude.value))
        if bool(params.sweep_pause.value):
            sweep_axis = np.full(N_POINTS, center, dtype=float)
        else:
            sweep_axis = np.linspace(center - amplitude, center + amplitude, N_POINTS)
        detuning = sweep_axis - self._laser_with_disturbance
        detuning = detuning + (self.detuning_jitter_v * self.rng.normal(size=N_POINTS))

        modulation_hz = self._modulation_frequency_hz(params.modulation_frequency.value)
        modulation_vpp = self._modulation_amplitude_vpp(params.modulation_amplitude.value)

        error_a, quad_a = self._channel_error_and_quadrature(
            detuning,
            modulation_hz=modulation_hz,
            modulation_vpp=modulation_vpp,
            demod_phase_deg=float(params.demodulation_phase_a.value),
            demod_multiplier=float(params.demodulation_multiplier_a.value),
            invert=bool(params.invert_a.value),
            offset_bits=float(params.offset_a.value),
        )

        noise_a = np.asarray(self._electronics_noise(size=N_POINTS), dtype=float)
        plot_data: dict[str, np.ndarray] = {
            "error_signal_1": _to_adc(error_a + noise_a),
            "error_signal_1_quadrature": _to_adc(quad_a + noise_a),
        }

        if bool(params.dual_channel.value):
            error_b, quad_b = self._channel_error_and_quadrature(
                detuning,
                modulation_hz=modulation_hz,
                modulation_vpp=modulation_vpp,
                demod_phase_deg=float(params.demodulation_phase_b.value),
                demod_multiplier=float(params.demodulation_multiplier_b.value),
                invert=bool(params.invert_b.value),
                offset_bits=float(params.offset_b.value),
            )
            noise_b = np.asarray(self._electronics_noise(size=N_POINTS), dtype=float)
            plot_data["error_signal_2"] = _to_adc(error_b + noise_b)
            plot_data["error_signal_2_quadrature"] = _to_adc(quad_b + noise_b)
        else:
            monitor = self._monitor_signal(
                detuning,
                modulation_hz=modulation_hz,
                modulation_vpp=modulation_vpp,
            )
            monitor_noise = np.asarray(
                self._electronics_noise(size=N_POINTS), dtype=float
            )
            plot_data["monitor_signal"] = _to_adc(monitor + monitor_noise)
        return plot_data

    def _build_locked_plot(self, params: Any) -> dict[str, np.ndarray | float]:
        n = N_POINTS
        control_noise = np.asarray(self._electronics_noise(size=n), dtype=float) * 0.2

        modulation_hz = self._modulation_frequency_hz(params.modulation_frequency.value)
        modulation_vpp = self._modulation_amplitude_vpp(params.modulation_amplitude.value)

        detuning_series = self.last_effective_detuning_v + (
            self.detuning_jitter_v * self.rng.normal(size=n)
        )
        error_a, _ = self._channel_error_and_quadrature(
            detuning_series,
            modulation_hz=modulation_hz,
            modulation_vpp=modulation_vpp,
            demod_phase_deg=float(params.demodulation_phase_a.value),
            demod_multiplier=float(params.demodulation_multiplier_a.value),
            invert=bool(params.invert_a.value),
            offset_bits=float(params.offset_a.value),
        )
        noise_a = np.asarray(self._electronics_noise(size=n), dtype=float)
        error_a = error_a + noise_a

        if bool(params.dual_channel.value):
            error_b, _ = self._channel_error_and_quadrature(
                detuning_series,
                modulation_hz=modulation_hz,
                modulation_vpp=modulation_vpp,
                demod_phase_deg=float(params.demodulation_phase_b.value),
                demod_multiplier=float(params.demodulation_multiplier_b.value),
                invert=bool(params.invert_b.value),
                offset_bits=float(params.offset_b.value),
            )
            noise_b = np.asarray(self._electronics_noise(size=n), dtype=float)
            error_b = error_b + noise_b
            error_series = self._combine_dual_error(
                error_a,
                error_b,
                channel_mixing=int(params.channel_mixing.value),
                combined_offset=float(params.combined_offset.value),
            )
        else:
            error_series = error_a

        if not bool(params.target_slope_rising.value):
            error_series = -error_series

        control_series = self.control_output_v + control_noise
        data: dict[str, np.ndarray | float] = {
            "error_signal": _to_adc(error_series),
            "control_signal": _to_adc(control_series),
        }
        if not bool(params.dual_channel.value):
            monitor_series = self._monitor_signal(
                detuning_series,
                modulation_hz=modulation_hz,
                modulation_vpp=modulation_vpp,
            )
            monitor_series = monitor_series + np.asarray(
                self._electronics_noise(size=n), dtype=float
            )
            data["monitor_signal"] = _to_adc(monitor_series)
        if bool(params.pid_on_slow_enabled.value):
            data["slow_control_signal"] = float(np.mean(control_series) * ADC_SCALE)
        return data

    def build_plot(self, params: Any) -> dict[str, np.ndarray | float]:
        if bool(params.lock.value):
            return self._build_locked_plot(params)
        return self._build_unlocked_plot(params)

    @staticmethod
    def build_signal_stats(plot_data: dict[str, np.ndarray | float]) -> dict[str, float]:
        stats: dict[str, float] = {}
        for name, value in plot_data.items():
            if isinstance(value, np.ndarray):
                arr = np.asarray(value, dtype=float)
                stats[f"{name}_mean"] = float(np.mean(arr))
                stats[f"{name}_std"] = float(np.std(arr))
                stats[f"{name}_max"] = float(np.max(arr))
                stats[f"{name}_min"] = float(np.min(arr))
        return stats

    def snapshot(self, params: Any) -> SimulatorStatus:
        return SimulatorStatus(
            lock=bool(params.lock.value),
            laser_detuning_v=self.laser_detuning_v,
            disturbance_offset_v=self.disturbance_offset_v,
            control_output_v=self.control_output_v,
            effective_detuning_v=self.last_effective_detuning_v,
            noise_sigma_v=self.noise_sigma_v,
            drift_v_per_s=self.drift_v_per_s,
            walk_sigma_v_sqrt_s=self.walk_sigma_v_sqrt_s,
            monitor_mode=self.monitor_mode,
            modulation_hz=self._modulation_frequency_hz(params.modulation_frequency.value),
            modulation_amp_vpp=self._modulation_amplitude_vpp(params.modulation_amplitude.value),
            linewidth_hz=self.linewidth_hz,
            linewidth_v=self.linewidth_v,
            fsr_hz=self.fsr_hz,
            scan_hz_per_v=self.scan_hz_per_v,
            detuning_jitter_v=self.detuning_jitter_v,
        )
