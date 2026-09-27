"""Tests for the optional imperfect-serrodyne order model (spec §C3).

Covers:
 - disabled == pre-existing single-order behaviour (byte-for-byte regression),
 - the pure `serrodyne_feature_offsets_hz` / `serrodyne_order_weights`
   formulas,
 - feature displacement in the simulator's actual PDH error signal when
   `frequency_hz` changes, both in the internal Hz coordinate and in sweep
   samples,
 - weight-vs-power behaviour (peak at p_opt, carrier/2nd-order growth away
   from it, +1/-1 asymmetry),
 - an optional integration test against the linien-gateway candidate
   detector, if importable without modifying it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

from linien_sim.model import ADC_SCALE, VirtualPdhModel
from linien_sim.parameters import MHZ_UNIT, SimParameters, VPP_UNIT
from linien_sim.serrodyne import (
    SerrodyneConfig,
    serrodyne_feature_offsets_hz,
    serrodyne_order_weights,
)

MODULATION_HZ_REPRESENTATIVE = 10_000_000.0  # 10 MHz, representative only (see AGENTS/spec)


def _make_params(*, modulation_hz: float = MODULATION_HZ_REPRESENTATIVE) -> SimParameters:
    params = SimParameters()
    params.modulation_frequency.value = int(modulation_hz / 1_000_000.0 * MHZ_UNIT)
    params.modulation_amplitude.value = int(1.0 * VPP_UNIT)
    params.sweep_center.value = 0.0
    params.sweep_amplitude.value = 1.0
    params.lock.value = False
    params.dual_channel.value = False
    return params


def _quiet_model(seed: int = 1234) -> VirtualPdhModel:
    """A model with all stochastic contributions zeroed, for exact comparisons."""
    model = VirtualPdhModel(seed=seed)
    model.set_noise_sigma(0.0)
    model.set_detuning_jitter(0.0)
    model.set_walk_sigma(0.0)
    model.set_drift(0.0)
    return model


# ---------------------------------------------------------------------------
# Disabled == pre-existing behaviour
# ---------------------------------------------------------------------------


def test_disabled_pdh_error_matches_single_order_impl():
    model = _quiet_model()
    rng = np.random.default_rng(7)
    detuning_v = rng.uniform(-0.5, 0.5, size=2048)
    kwargs = dict(
        modulation_hz=MODULATION_HZ_REPRESENTATIVE,
        modulation_vpp=1.0,
        demod_phase_deg=100.0,
        demod_multiplier=1.0,
    )
    assert model.serrodyne.enabled is False
    wrapped = model._pdh_error(detuning_v, **kwargs)
    direct = model._pdh_error_single_order(detuning_v, **kwargs)
    np.testing.assert_array_equal(wrapped, direct)


def test_disabled_monitor_matches_single_order_impl():
    model = _quiet_model()
    rng = np.random.default_rng(8)
    detuning_v = rng.uniform(-0.5, 0.5, size=2048)
    kwargs = dict(modulation_hz=MODULATION_HZ_REPRESENTATIVE, modulation_vpp=1.0)
    wrapped = model._monitor_signal(detuning_v, **kwargs)
    direct = model._monitor_signal_single_order(detuning_v, **kwargs)
    np.testing.assert_array_equal(wrapped, direct)


def test_disabled_build_plot_is_byte_for_byte_unchanged_by_serrodyne_config():
    """Configuring (but not enabling) serrodyne with arbitrary values must not
    perturb the trace at all, for a fixed seed -- proving the disabled path is
    identical to the pre-feature behaviour."""
    seed = 42
    params = _make_params()

    baseline_model = _quiet_model(seed=seed)
    baseline_plot = baseline_model.build_plot(params)

    mutated_model = _quiet_model(seed=seed)
    mutated_model.configure_serrodyne(
        enabled=False,  # still disabled
        frequency_hz=3_500_000.0,
        rf_power_dbm=-4.0,
        orders=(-2, -1, 0, 1, 2),
        sweep_frequency_sign=-1,
        p_opt_dbm=1.5,
        use_power_dependence=False,
        fixed_base_weights={0: 0.5, 1: 0.5, -1: 0.5, 2: 0.5, -2: 0.5},
    )
    mutated_plot = mutated_model.build_plot(params)

    assert set(baseline_plot) == set(mutated_plot)
    for key, value in baseline_plot.items():
        np.testing.assert_array_equal(value, mutated_plot[key], err_msg=f"key={key}")


# ---------------------------------------------------------------------------
# Pure functions: offsets and weights
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [-2, -1, 0, 1, 2])
@pytest.mark.parametrize("sign", [1, -1])
def test_feature_offsets_hz_formula(n, sign):
    f_serrodyne = 3_300_000.0
    offsets = serrodyne_feature_offsets_hz(f_serrodyne, [n], sweep_frequency_sign=sign)
    assert offsets[n] == pytest.approx(-n * sign * f_serrodyne)


@pytest.mark.parametrize("n,sign,delta_hz", [(1, 1, 500_000.0), (2, -1, 250_000.0), (-1, 1, 400_000.0)])
def test_feature_offsets_hz_moves_by_minus_n_s_delta_f(n, sign, delta_hz):
    f0 = 4_000_000.0
    off0 = serrodyne_feature_offsets_hz(f0, [n], sweep_frequency_sign=sign)[n]
    off1 = serrodyne_feature_offsets_hz(f0 + delta_hz, [n], sweep_frequency_sign=sign)[n]
    assert (off1 - off0) == pytest.approx(-n * sign * delta_hz)


def test_order_weights_fixed_mode_matches_documented_example():
    cfg = SerrodyneConfig(
        use_power_dependence=False,
        orders=(-2, -1, 0, 1, 2),
        fixed_base_weights={0: 0.10, 1: 0.82, -1: 0.03, 2: 0.05, -2: 0.02},
    )
    weights = serrodyne_order_weights(power_dbm=0.0, cfg=cfg)
    assert weights == {0: 0.10, 1: 0.82, -1: 0.03, 2: 0.05, -2: 0.02}
    # Fixed mode ignores power entirely.
    weights_other_power = serrodyne_order_weights(power_dbm=15.0, cfg=cfg)
    assert weights_other_power == weights


def test_order_weights_fixed_mode_missing_order_defaults_zero():
    cfg = SerrodyneConfig(
        use_power_dependence=False,
        orders=(3,),
        fixed_base_weights={0: 0.1},
    )
    assert serrodyne_order_weights(0.0, cfg) == {3: 0.0}


def test_order_weights_desired_order_peaks_exactly_at_p_opt():
    cfg = SerrodyneConfig(orders=(1,), p_opt_dbm=-3.0, desired_peak_weight=0.82)
    powers = np.linspace(-20.0, 15.0, 71)
    values = [serrodyne_order_weights(p, cfg)[1] for p in powers]
    best_idx = int(np.argmax(values))
    assert powers[best_idx] == pytest.approx(-3.0, abs=0.5)
    assert values[best_idx] == pytest.approx(0.82, rel=1e-9)


def test_order_weights_carrier_and_second_order_grow_away_from_optimum():
    cfg = SerrodyneConfig(orders=(0, 1, 2, -2), p_opt_dbm=2.0)
    at_opt = serrodyne_order_weights(2.0, cfg)
    far = serrodyne_order_weights(2.0 + 25.0, cfg)
    assert far[0] > at_opt[0]
    assert far[2] > at_opt[2]
    assert far[-2] > at_opt[-2]
    # Monotonic growth with |deviation|, not just a single far point.
    deviations = [0.0, 2.0, 6.0, 12.0, 25.0]
    carrier_values = [serrodyne_order_weights(2.0 + d, cfg)[0] for d in deviations]
    assert carrier_values == sorted(carrier_values)


def test_order_weights_minus_one_asymmetry_respected():
    cfg = SerrodyneConfig(orders=(1, -1), p_opt_dbm=0.0, asymmetry_ratio=0.05)
    for power in (-10.0, 0.0, 3.0, 12.0):
        weights = serrodyne_order_weights(power, cfg)
        assert weights[-1] == pytest.approx(0.05 * weights[1])
        assert weights[-1] < weights[1]


# ---------------------------------------------------------------------------
# Feature displacement in the actual simulated PDH error signal
# ---------------------------------------------------------------------------


def _zero_crossing_x(x: np.ndarray, y: np.ndarray) -> float:
    """Sub-sample rising zero-crossing location of the single dominant feature
    in ``y`` (assumed dispersive: one sign change), via linear interpolation."""
    sign = np.sign(y)
    sign_changes = np.where(np.diff(sign) != 0)[0]
    assert len(sign_changes) >= 1, "expected at least one zero crossing"
    idx = sign_changes[len(sign_changes) // 2]
    x0, x1 = x[idx], x[idx + 1]
    y0, y1 = y[idx], y[idx + 1]
    frac = -y0 / (y1 - y0)
    return float(x0 + frac * (x1 - x0))


def _isolated_order_model(order: int, sign: int, f_serrodyne_hz: float) -> VirtualPdhModel:
    model = _quiet_model()
    model.configure_serrodyne(
        enabled=True,
        frequency_hz=f_serrodyne_hz,
        orders=(order,),
        sweep_frequency_sign=sign,
        use_power_dependence=False,
        fixed_base_weights={order: 1.0},
    )
    return model


@pytest.mark.parametrize("order", [-2, -1, 1, 2])
@pytest.mark.parametrize("sign", [1, -1])
def test_single_order_feature_crossing_matches_predicted_offset_hz(order, sign):
    f0 = 3_000_000.0
    model = _isolated_order_model(order, sign, f0)
    detuning_v = np.linspace(-0.9, 0.9, 4001)
    signal = model._pdh_error(
        detuning_v,
        modulation_hz=MODULATION_HZ_REPRESENTATIVE,
        modulation_vpp=1.0,
        demod_phase_deg=100.0,
        demod_multiplier=1.0,
    )
    crossing_v = _zero_crossing_x(detuning_v, signal)
    crossing_hz = crossing_v * model.scan_hz_per_v
    expected_hz = -order * sign * f0
    assert crossing_hz == pytest.approx(expected_hz, abs=20.0)


@pytest.mark.parametrize("order", [-2, -1, 1, 2])
@pytest.mark.parametrize("sign", [1, -1])
def test_feature_moves_by_minus_n_s_delta_f_in_hz_and_samples(order, sign):
    f0 = 3_000_000.0
    delta_f = 400_000.0
    detuning_v = np.linspace(-0.9, 0.9, 4001)
    step_v = detuning_v[1] - detuning_v[0]

    model0 = _isolated_order_model(order, sign, f0)
    sig0 = model0._pdh_error(
        detuning_v,
        modulation_hz=MODULATION_HZ_REPRESENTATIVE,
        modulation_vpp=1.0,
        demod_phase_deg=100.0,
        demod_multiplier=1.0,
    )
    model1 = _isolated_order_model(order, sign, f0 + delta_f)
    sig1 = model1._pdh_error(
        detuning_v,
        modulation_hz=MODULATION_HZ_REPRESENTATIVE,
        modulation_vpp=1.0,
        demod_phase_deg=100.0,
        demod_multiplier=1.0,
    )
    x0 = _zero_crossing_x(detuning_v, sig0)
    x1 = _zero_crossing_x(detuning_v, sig1)

    # Hz-domain displacement.
    delta_hz_observed = (x1 - x0) * model0.scan_hz_per_v
    assert delta_hz_observed == pytest.approx(-order * sign * delta_f, abs=20.0)

    # Sample-domain displacement: Delta_x = -n * s * N_SB * Delta_f / f_PDH,
    # with N_SB = f_PDH / scan_hz_per_v / step_v (spec §P derivation).
    f_pdh = MODULATION_HZ_REPRESENTATIVE
    n_sb_samples = f_pdh / model0.scan_hz_per_v / step_v
    delta_x_samples_predicted = -order * sign * n_sb_samples * delta_f / f_pdh
    delta_x_samples_observed = (x1 - x0) / step_v
    assert delta_x_samples_observed == pytest.approx(delta_x_samples_predicted, rel=5e-4)


def test_carrier_order_zero_does_not_move_with_frequency():
    f0 = 3_000_000.0
    delta_f = 900_000.0
    detuning_v = np.linspace(-0.3, 0.3, 4001)

    model0 = _isolated_order_model(0, 1, f0)
    model1 = _isolated_order_model(0, 1, f0 + delta_f)
    kwargs = dict(
        modulation_hz=MODULATION_HZ_REPRESENTATIVE,
        modulation_vpp=1.0,
        demod_phase_deg=100.0,
        demod_multiplier=1.0,
    )
    sig0 = model0._pdh_error(detuning_v, **kwargs)
    sig1 = model1._pdh_error(detuning_v, **kwargs)
    np.testing.assert_allclose(sig0, sig1)


def test_monitor_signal_shows_features_at_same_offsets_as_error_signal():
    order, sign, f0 = 1, 1, 2_500_000.0
    model = _isolated_order_model(order, sign, f0)
    detuning_v = np.linspace(-0.9, 0.9, 4001)
    monitor = model._monitor_signal(detuning_v, modulation_hz=MODULATION_HZ_REPRESENTATIVE, modulation_vpp=1.0)
    # The monitor is not dispersive (it's a level dip/peak), so locate the
    # feature by its extremum rather than a zero crossing, and compare to the
    # error signal's zero crossing (same underlying cavity resonance).
    error = model._pdh_error(
        detuning_v,
        modulation_hz=MODULATION_HZ_REPRESENTATIVE,
        modulation_vpp=1.0,
        demod_phase_deg=100.0,
        demod_multiplier=1.0,
    )
    error_crossing = _zero_crossing_x(detuning_v, error)
    monitor_extremum = detuning_v[int(np.argmin(monitor))]
    assert monitor_extremum == pytest.approx(error_crossing, abs=0.01)


# ---------------------------------------------------------------------------
# Optional integration test with the linien-gateway detector
# ---------------------------------------------------------------------------

_GATEWAY_ROOT = Path(__file__).resolve().parents[2] / "linien-gateway"


def _import_gateway_auto_lock_scan():
    if not _GATEWAY_ROOT.is_dir():
        return None
    added = str(_GATEWAY_ROOT) not in sys.path
    if added:
        sys.path.insert(0, str(_GATEWAY_ROOT))
    try:
        from app.auto_lock_scan import (  # noqa: PLC0415
            AutoLockScanSettings,
            find_auto_lock_candidates,
        )
    except Exception:
        return None
    return AutoLockScanSettings, find_auto_lock_candidates


def test_multi_order_trace_yields_candidates_ordered_by_weight():
    imported = _import_gateway_auto_lock_scan()
    if imported is None:
        pytest.skip(
            "linien-gateway app.auto_lock_scan not importable from this env; "
            "skipping the cross-repo integration check (see spec §C3)."
        )
    AutoLockScanSettings, find_auto_lock_candidates = imported

    params = _make_params()
    params.sweep_amplitude.value = 0.9
    model = _quiet_model(seed=99)
    # Strictly descending, well-separated weights so acceptance/ordering is
    # unambiguous; feature amplitude scales ~linearly with weight since every
    # order reuses the identical single-order PDH shape.
    model.configure_serrodyne(
        enabled=True,
        frequency_hz=2_000_000.0,
        orders=(1, 0, -1, 2, -2),
        sweep_frequency_sign=1,
        use_power_dependence=False,
        fixed_base_weights={1: 0.9, 0: 0.5, -1: 0.3, 2: 0.15, -2: 0.05},
    )
    plot = model.build_plot(params)
    error_v = np.asarray(plot["error_signal_1"], dtype=float) / ADC_SCALE

    settings = AutoLockScanSettings(
        signal_type="pdh",
        allow_single_side=True,
        use_monitor=False,
        half_range_sweep_v=0.12,
        error_min=0.01,
        symmetry_min=0.05,
        single_error_min=0.01,
        min_amplitude=0.005,
        smooth_window_pts=5,
    )
    candidates = find_auto_lock_candidates(
        error_trace_v=error_v,
        monitor_trace_v=None,
        sweep_center_v=float(params.sweep_center.value),
        sweep_amplitude_v=float(params.sweep_amplitude.value),
        settings=settings,
        modulation_frequency_hz=MODULATION_HZ_REPRESENTATIVE,
    )
    assert len(candidates) >= 2, "expected multiple accepted candidates from the multi-order trace"

    amplitudes = [c.feature_amplitude for c in candidates]
    assert amplitudes == sorted(amplitudes, reverse=True), (
        "find_auto_lock_candidates already sorts by score, and score should "
        "track feature_amplitude here since orders differ only by weight"
    )
