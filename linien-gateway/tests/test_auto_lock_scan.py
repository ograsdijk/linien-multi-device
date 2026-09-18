import dataclasses

import numpy as np
import pytest

from app.auto_lock_scan import (
    AutoLockScanSettings,
    calibrate_auto_lock_settings,
    feature_resolution_samples,
    find_coarse_auto_lock_target,
    find_auto_lock_target,
    _sideband_offset_pts,
    max_lockable_amplitude_v,
    scan_too_wide_to_lock,
)
from app.schemas import AutoLockScanSettings as SchemaAutoLockScanSettings

# Traces are in PLOT units (the fixed ADC_SCALE/V display scale, ~±1 full scale) — the
# same units the detector receives after session divides by ADC_SCALE.


def _dispersive(n=2048, amplitude=0.3, width=0.05, center=0.0):
    """A dispersive feature (rising zero-crossing at ``center`` for amplitude>0):
    negative lobe left, positive lobe right."""
    x = np.linspace(-1.0, 1.0, n)
    u = (x - center) / width
    return amplitude * u * np.exp(-0.5 * u**2)


def _pdh_triplet(n=2048, carrier=0.4, sideband=0.15, width=0.03, sb_off=0.3):
    """Carrier (rising) at 0 plus two opposite-slope sidebands at ±sb_off."""
    return (
        _dispersive(n, amplitude=carrier, width=width, center=0.0)
        + _dispersive(n, amplitude=-sideband, width=width, center=-sb_off)
        + _dispersive(n, amplitude=-sideband, width=width, center=+sb_off)
    )


def test_feature_resolution_is_calibrated_width_per_sample_not_scan_threshold():
    settings = AutoLockScanSettings(half_range_sweep_v=0.04)
    # Same physical scan amplitude, doubled samples -> doubled resolution.
    assert feature_resolution_samples(settings, 2048, 1.0) == pytest.approx(40.94)
    assert feature_resolution_samples(settings, 1024, 1.0) == pytest.approx(20.46)
    # Same ADC depth, halving scan amplitude also doubles the feature samples.
    assert feature_resolution_samples(settings, 2048, 0.5) == pytest.approx(81.88)


def test_coarse_tracker_requires_pdh_sidebands_and_reports_snr_metrics():
    error = _pdh_triplet(n=2048, width=0.012, sb_off=0.25)
    coarse = find_coarse_auto_lock_target(
        error_trace_v=error,
        monitor_trace_v=None,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        settings=AutoLockScanSettings(signal_type="pdh", half_range_sweep_v=0.01),
        preferred_slope_rising=True,
        modulation_frequency_hz=20e6,
    )
    assert abs(coarse.result.target_voltage) < 0.03
    assert coarse.result.sideband_offset_v is not None
    assert coarse.metrics["method"] == "multiscale_extrema_pair"
    assert coarse.metrics["snr"] > 6



def test_finds_rising_crossing_near_center():
    n = 2048
    error = _dispersive(n, amplitude=0.3, width=0.05)
    result = find_auto_lock_target(
        error_trace_v=error,
        monitor_trace_v=None,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        settings=AutoLockScanSettings(),
    )
    assert abs(result.target_index - (n // 2)) < 40
    assert result.target_slope_rising is True
    assert abs(result.target_voltage) < 0.05
    assert result.pair_excursion > AutoLockScanSettings().error_min
    assert result.hz_per_v is None  # no modulation frequency supplied


def test_amplitude_floor_rejects_dead_trace():
    n = 2048
    # Peak-to-peak ~0.006, below the default min_amplitude (0.01).
    error = 0.003 * np.sin(np.linspace(0.0, 8.0 * np.pi, n))
    with pytest.raises(ValueError, match="min_amplitude|lockable signal"):
        find_auto_lock_target(
            error_trace_v=error,
            monitor_trace_v=None,
            sweep_center_v=0.0,
            sweep_amplitude_v=1.0,
            settings=AutoLockScanSettings(),
        )


def test_single_side_toggle_changes_acceptance():
    n = 2048
    x = np.linspace(-1.0, 1.0, n)
    error = np.where(x < 0.0, _dispersive(n, amplitude=0.3, width=0.05), 0.0) + np.where(
        x >= 0.0, _dispersive(n, amplitude=0.04, width=0.05), 0.0
    )

    strict = AutoLockScanSettings(error_min=0.3, symmetry_min=0.6)
    with pytest.raises(ValueError):
        find_auto_lock_target(
            error_trace_v=error,
            monitor_trace_v=None,
            sweep_center_v=0.0,
            sweep_amplitude_v=1.0,
            settings=strict,
        )

    permissive = AutoLockScanSettings(
        error_min=0.3, symmetry_min=0.6, allow_single_side=True, single_error_min=0.1
    )
    result = find_auto_lock_target(
        error_trace_v=error,
        monitor_trace_v=None,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        settings=permissive,
    )
    assert max(result.left_excursion, result.right_excursion) >= 0.1


def test_respects_preferred_slope():
    n = 2048
    error = _dispersive(n, amplitude=0.3, width=0.08)
    with pytest.raises(ValueError):
        find_auto_lock_target(
            error_trace_v=error,
            monitor_trace_v=None,
            sweep_center_v=0.0,
            sweep_amplitude_v=1.0,
            settings=AutoLockScanSettings(),
            preferred_slope_rising=False,
        )
    result = find_auto_lock_target(
        error_trace_v=error,
        monitor_trace_v=None,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        settings=AutoLockScanSettings(),
        preferred_slope_rising=True,
    )
    assert result.target_slope_rising is True


def test_pdh_mode_recovers_hz_per_v():
    n = 2048
    error = _pdh_triplet(n, carrier=0.4, sideband=0.15, width=0.03, sb_off=0.3)
    mod_hz = 30.0e6
    result = find_auto_lock_target(
        error_trace_v=error,
        monitor_trace_v=None,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        settings=AutoLockScanSettings(signal_type="pdh"),
        preferred_slope_rising=True,
        modulation_frequency_hz=mod_hz,
    )
    assert abs(result.target_index - (n // 2)) < 40
    assert result.sideband_offset_v is not None
    assert 0.27 < result.sideband_offset_v < 0.33
    expected = mod_hz / 0.3
    assert result.hz_per_v is not None
    assert 0.9 * expected < result.hz_per_v < 1.1 * expected


def test_pdh_mode_recovers_discriminator_slope():
    n = 2048
    error = _pdh_triplet(n, carrier=0.4, sideband=0.15, width=0.03, sb_off=0.3)
    result = find_auto_lock_target(
        error_trace_v=error,
        monitor_trace_v=None,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        settings=AutoLockScanSettings(signal_type="pdh"),
        preferred_slope_rising=True,
        modulation_frequency_hz=30.0e6,
    )
    # D = |error-curve slope [a.u./V]| / hz_per_v [Hz/V] * 1e6 -> a.u./MHz.
    # The synthetic carrier slope is ~A/width = 13.3 a.u./V and hz_per_v ~ 1e8
    # Hz/V, so D ~ 0.13 a.u./MHz (smoothing/window lower it somewhat).
    assert result.discriminator_slope_v_per_mhz is not None
    assert 0.03 < result.discriminator_slope_v_per_mhz < 0.4


def test_discriminator_slope_none_without_modulation_frequency():
    n = 2048
    error = _pdh_triplet(n)
    result = find_auto_lock_target(
        error_trace_v=error,
        monitor_trace_v=None,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        settings=AutoLockScanSettings(signal_type="pdh"),
        preferred_slope_rising=True,
        modulation_frequency_hz=None,
    )
    assert result.hz_per_v is None
    assert result.discriminator_slope_v_per_mhz is None


def test_dispersive_mode_skips_hz_per_v():
    n = 2048
    error = _pdh_triplet(n)
    result = find_auto_lock_target(
        error_trace_v=error,
        monitor_trace_v=None,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        settings=AutoLockScanSettings(signal_type="dispersive"),
        preferred_slope_rising=True,
        modulation_frequency_hz=30.0e6,
    )
    assert result.hz_per_v is None
    assert result.sideband_offset_v is None


def test_monitor_transmission_gates_and_passes():
    n = 2048
    x = np.linspace(-1.0, 1.0, n)
    error = _dispersive(n, amplitude=0.3, width=0.05)
    monitor = 0.7 * np.exp(-0.5 * (x / 0.1) ** 2)  # transmission peak at center

    ok = find_auto_lock_target(
        error_trace_v=error,
        monitor_trace_v=monitor,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        settings=AutoLockScanSettings(
            use_monitor=True, monitor_mode="locked_above", monitor_threshold=0.1
        ),
        preferred_slope_rising=True,
    )
    assert ok.monitor_level is not None and ok.monitor_level > 0.1

    with pytest.raises(ValueError, match="monitor"):
        find_auto_lock_target(
            error_trace_v=error,
            monitor_trace_v=monitor,
            sweep_center_v=0.0,
            sweep_amplitude_v=1.0,
            settings=AutoLockScanSettings(
                use_monitor=True, monitor_mode="locked_above", monitor_threshold=0.9
            ),
            preferred_slope_rising=True,
        )


def test_monitor_reflection_dip():
    n = 2048
    x = np.linspace(-1.0, 1.0, n)
    error = _dispersive(n, amplitude=0.3, width=0.05)
    monitor = 0.7 - 0.65 * np.exp(-0.5 * (x / 0.1) ** 2)  # reflection dip at center

    ok = find_auto_lock_target(
        error_trace_v=error,
        monitor_trace_v=monitor,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        settings=AutoLockScanSettings(
            use_monitor=True, monitor_mode="locked_below", monitor_threshold=0.3
        ),
        preferred_slope_rising=True,
    )
    assert ok.monitor_level is not None and ok.monitor_level < 0.3

    with pytest.raises(ValueError, match="monitor"):
        find_auto_lock_target(
            error_trace_v=error,
            monitor_trace_v=monitor,
            sweep_center_v=0.0,
            sweep_amplitude_v=1.0,
            settings=AutoLockScanSettings(
                use_monitor=True, monitor_mode="locked_below", monitor_threshold=0.01
            ),
            preferred_slope_rising=True,
        )


def test_monitor_selects_feature_with_signal():
    n = 2048
    x = np.linspace(-1.0, 1.0, n)
    error = _dispersive(n, amplitude=0.3, width=0.04, center=-0.3) + _dispersive(
        n, amplitude=0.3, width=0.04, center=0.3
    )
    # Monitor peaks only at +0.3, so only that feature passes the locked_above gate.
    monitor = 0.7 * np.exp(-0.5 * ((x - 0.3) / 0.08) ** 2)
    result = find_auto_lock_target(
        error_trace_v=error,
        monitor_trace_v=monitor,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        settings=AutoLockScanSettings(
            use_monitor=True, monitor_mode="locked_above", monitor_threshold=0.1
        ),
        preferred_slope_rising=True,
    )
    assert result.target_voltage > 0.2


def test_no_monitor_locks_on_error_alone():
    n = 2048
    error = _dispersive(n, amplitude=0.3, width=0.05)
    # use_monitor True but no monitor trace -> degrade to error-only, no raise.
    result = find_auto_lock_target(
        error_trace_v=error,
        monitor_trace_v=None,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        settings=AutoLockScanSettings(use_monitor=True, monitor_threshold=1.0),
        preferred_slope_rising=True,
    )
    assert result.monitor_level is None
    assert abs(result.target_index - (n // 2)) < 40


def test_engine_and_schema_settings_stay_in_parity():
    """The engine dataclass and the Pydantic boundary model must declare the same
    field names and defaults, so the two definitions cannot drift."""
    engine_fields = {f.name: f.default for f in dataclasses.fields(AutoLockScanSettings)}
    schema_fields = {
        name: info.default
        for name, info in SchemaAutoLockScanSettings.model_fields.items()
    }
    assert engine_fields.keys() == schema_fields.keys()
    assert engine_fields == schema_fields


def test_calibrate_derives_settings_that_lock():
    n = 2048
    error = _dispersive(n, amplitude=0.3, width=0.08)
    calib = calibrate_auto_lock_settings(
        error_trace_v=error,
        monitor_trace_v=None,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        base=AutoLockScanSettings(),
        preferred_slope_rising=True,
    )
    assert calib.amplitude > 0.1  # plot units
    assert calib.settings.error_min > 0.0
    assert calib.settings.min_amplitude > 0.0
    assert calib.settings.symmetry_min > 0.4
    assert calib.settings.use_monitor is False
    result = find_auto_lock_target(
        error_trace_v=error,
        monitor_trace_v=None,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        settings=calib.settings,
        preferred_slope_rising=True,
    )
    assert abs(result.target_index - (n // 2)) < 40


def test_calibrate_monitor_sets_threshold():
    n = 2048
    x = np.linspace(-1.0, 1.0, n)
    error = _dispersive(n, amplitude=0.3, width=0.08)
    monitor = 0.7 * np.exp(-0.5 * (x / 0.12) ** 2)  # transmission peak
    calib = calibrate_auto_lock_settings(
        error_trace_v=error,
        monitor_trace_v=monitor,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        base=AutoLockScanSettings(monitor_mode="locked_above"),
        preferred_slope_rising=True,
        include_monitor=True,
    )
    assert calib.settings.use_monitor is True
    assert calib.settings.monitor_mode == "locked_above"
    assert calib.settings.monitor_threshold > 0.0


def test_calibrate_monitor_mode_mismatch_raises():
    """The monitor is a safety check: if the configured mode contradicts the measured
    peak/dip direction, calibration fails loudly rather than silently accepting."""
    n = 2048
    x = np.linspace(-1.0, 1.0, n)
    error = _dispersive(n, amplitude=0.3, width=0.08)
    monitor = 0.7 * np.exp(-0.5 * (x / 0.12) ** 2)  # peaks on resonance (transmission)
    with pytest.raises(ValueError, match="HIGHER on resonance|locked_above"):
        calibrate_auto_lock_settings(
            error_trace_v=error,
            monitor_trace_v=monitor,
            sweep_center_v=0.0,
            sweep_amplitude_v=1.0,
            base=AutoLockScanSettings(monitor_mode="locked_below"),  # wrong for a peak
            preferred_slope_rising=True,
            include_monitor=True,
        )


def test_calibrate_rejects_flat_trace():
    error = np.zeros(2048)
    with pytest.raises(ValueError):
        calibrate_auto_lock_settings(
            error_trace_v=error,
            monitor_trace_v=None,
            sweep_center_v=0.0,
            sweep_amplitude_v=1.0,
            base=AutoLockScanSettings(),
        )


def test_calibrate_flat_monitor_raises():
    # A flat monitor has no contrast at the feature; calibration must fail loudly
    # (the median baseline equals the on-resonance level — no fabricated contrast).
    n = 2048
    error = _dispersive(n, amplitude=0.3, width=0.08)
    monitor = np.full(n, 0.5)
    with pytest.raises(ValueError, match="peak above|contrast"):
        calibrate_auto_lock_settings(
            error_trace_v=error,
            monitor_trace_v=monitor,
            sweep_center_v=0.0,
            sweep_amplitude_v=1.0,
            base=AutoLockScanSettings(monitor_mode="locked_above"),
            preferred_slope_rising=True,
            include_monitor=True,
        )


def test_monitor_rejects_wrong_side_candidate():
    # Two rising features; the stronger ERROR feature (-0.3) sits where the monitor DIPS
    # below baseline (wrong side), the weaker (+0.3) where it PEAKS. Even with a low/mis-set
    # absolute threshold, the wrong-side reject must keep the lock off the dip feature.
    n = 2048
    x = np.linspace(-1.0, 1.0, n)
    error = _dispersive(n, amplitude=0.4, width=0.04, center=-0.3) + _dispersive(
        n, amplitude=0.25, width=0.04, center=0.3
    )
    monitor = (
        0.5
        + 0.3 * np.exp(-0.5 * ((x - 0.3) / 0.06) ** 2)
        - 0.3 * np.exp(-0.5 * ((x + 0.3) / 0.06) ** 2)
    )
    result = find_auto_lock_target(
        error_trace_v=error,
        monitor_trace_v=monitor,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        settings=AutoLockScanSettings(
            use_monitor=True, monitor_mode="locked_above", monitor_threshold=0.1
        ),
        preferred_slope_rising=True,
    )
    assert result.target_voltage > 0.2  # picked the +0.3 peak, not the stronger-error dip


def test_calibrate_edge_feature_baseline_robust():
    # Feature near the left trace end: the median baseline must not be contaminated by the
    # feature (the old mean-of-ends baseline would have flipped the direction).
    n = 2048
    x = np.linspace(-1.0, 1.0, n)
    error = _dispersive(n, amplitude=0.3, width=0.05, center=-0.85)
    monitor = 0.1 + 0.6 * np.exp(-0.5 * ((x + 0.85) / 0.08) ** 2)  # transmission peak
    calib = calibrate_auto_lock_settings(
        error_trace_v=error,
        monitor_trace_v=monitor,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        base=AutoLockScanSettings(monitor_mode="locked_above"),
        preferred_slope_rising=True,
        include_monitor=True,
    )
    assert calib.settings.use_monitor is True
    assert calib.settings.monitor_mode == "locked_above"


def test_calibrate_narrow_monitor_dip_captured():
    # A very narrow dip must be captured by the directional extremum (not washed by a
    # fixed-window mean), so the threshold sits well below baseline.
    n = 2048
    x = np.linspace(-1.0, 1.0, n)
    error = _dispersive(n, amplitude=0.3, width=0.08)
    monitor = 0.7 - 0.6 * np.exp(-0.5 * (x / 0.01) ** 2)  # width ~0.01 (≪ half_range)
    calib = calibrate_auto_lock_settings(
        error_trace_v=error,
        monitor_trace_v=monitor,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        base=AutoLockScanSettings(monitor_mode="locked_below"),
        preferred_slope_rising=True,
        include_monitor=True,
    )
    assert calib.settings.use_monitor is True
    assert calib.settings.monitor_threshold < 0.6  # captured the deep dip, not stuck at baseline


def test_calibrate_monitor_aware_anchor():
    # Error is strongest at -0.3 but the monitor (the safety signal of the correct feature)
    # peaks at +0.3. Calibration must pick the monitor-consistent crossing and converge,
    # not abort with "could not converge".
    n = 2048
    x = np.linspace(-1.0, 1.0, n)
    error = _dispersive(n, amplitude=0.32, width=0.04, center=-0.3) + _dispersive(
        n, amplitude=0.3, width=0.04, center=0.3
    )
    monitor = 0.1 + 0.6 * np.exp(-0.5 * ((x - 0.3) / 0.06) ** 2)
    calib = calibrate_auto_lock_settings(
        error_trace_v=error,
        monitor_trace_v=monitor,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        base=AutoLockScanSettings(monitor_mode="locked_above"),
        preferred_slope_rising=True,
        include_monitor=True,
    )
    assert calib.target_voltage > 0.2


# ------------------------------------------------- scan too wide to lock from

def _settings(**kw):
    return AutoLockScanSettings.from_mapping({"signal_type": "pdh", **kw})


def test_a_signal_filling_a_quarter_of_the_scan_is_lockable():
    # sideband +/-0.05 V -> 0.1 V wide signal; exactly a quarter of a 0.4 V span.
    assert not scan_too_wide_to_lock(_settings(), sweep_amplitude_v=0.2,
                                     sideband_offset_v=0.05)


def test_a_signal_that_is_a_speck_on_the_scan_is_not():
    """The failure this exists for: the centre move to a target that far away
    is one long hysteretic jump, and it lands on a different feature."""
    assert scan_too_wide_to_lock(_settings(), sweep_amplitude_v=1.0,
                                 sideband_offset_v=0.05)


def test_the_fraction_is_configurable():
    wide = dict(sweep_amplitude_v=1.0, sideband_offset_v=0.05)
    assert scan_too_wide_to_lock(_settings(), **wide)
    assert not scan_too_wide_to_lock(_settings(min_signal_scan_fraction=0.05), **wide)
    assert not scan_too_wide_to_lock(_settings(min_signal_scan_fraction=0.0), **wide)


def test_an_unmeasured_sideband_spacing_does_not_force_narrowing():
    """None means "could not measure", not "too wide". Reading it as too wide
    narrows every dispersive device and every scan that did not resolve the
    sidebands -- including ones that lock perfectly well."""
    assert not scan_too_wide_to_lock(_settings(), sweep_amplitude_v=1.0,
                                     sideband_offset_v=None)
    assert not scan_too_wide_to_lock(
        _settings(signal_type="dispersive"), sweep_amplitude_v=1.0,
        sideband_offset_v=0.001,
    )


def test_the_goal_amplitude_is_the_widest_scan_that_passes():
    settings = _settings()
    amp = max_lockable_amplitude_v(settings, 0.05)
    assert amp == pytest.approx(0.2)
    assert not scan_too_wide_to_lock(settings, sweep_amplitude_v=amp,
                                     sideband_offset_v=0.05)
    assert scan_too_wide_to_lock(settings, sweep_amplitude_v=amp * 1.01,
                                 sideband_offset_v=0.05)


# ------------------------------------- the coarse tracker honours the monitor
#
# The coarse detector steers the narrowing walk. It used to accept
# monitor_trace_v and ignore it, so on a device where the monitor is the only
# thing telling two crossings apart, the walk could track onto the wrong one and
# burn its whole stage budget before the strict detections at the end noticed.


def _two_identical_features(n=2048):
    """Two crossings the error signal cannot tell apart, at -0.4 and +0.4."""
    return (
        _dispersive(n, amplitude=0.4, width=0.03, center=-0.4)
        + _dispersive(n, amplitude=0.4, width=0.03, center=+0.4)
    )


def _monitor_peak_at(center, n=2048, height=0.8):
    """A transmission peak marking one of them as the real feature."""
    x = np.linspace(-1.0, 1.0, n)
    return height * np.exp(-0.5 * ((x - center) / 0.03) ** 2)


def _coarse(error, monitor, **kw):
    settings = AutoLockScanSettings.from_mapping(
        {"signal_type": "dispersive", "half_range_sweep_v": 0.06, **kw}
    )
    return find_coarse_auto_lock_target(
        error_trace_v=error,
        monitor_trace_v=monitor,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        settings=settings,
        preferred_slope_rising=True,
    )


def test_the_coarse_tracker_picks_the_crossing_the_monitor_marks():
    error = _two_identical_features()
    for marked in (-0.4, 0.4):
        candidate = _coarse(error, _monitor_peak_at(marked), use_monitor=True)
        assert candidate.result.target_voltage == pytest.approx(marked, abs=0.05)


def test_the_coarse_tracker_ignores_the_monitor_when_it_is_not_calibrated_in():
    """use_monitor is set by calibration; an uncalibrated monitor must not
    start gating candidates."""
    error = _two_identical_features()
    a = _coarse(error, _monitor_peak_at(-0.4), use_monitor=False)
    b = _coarse(error, _monitor_peak_at(0.4), use_monitor=False)
    assert a.result.target_voltage == pytest.approx(b.result.target_voltage)


def test_a_monitor_that_rejects_everything_says_so():
    """'no signal-to-noise' would send the operator after the wrong problem."""
    error = _two_identical_features()
    # A monitor that DIPS at both crossings, on a device configured for peaks:
    # every candidate sits below the baseline the rest of the trace sets.
    monitor = (
        1.0
        - _monitor_peak_at(-0.4, height=1.0)
        - _monitor_peak_at(0.4, height=1.0)
    )
    with pytest.raises(ValueError, match="rejected by the monitor"):
        _coarse(error, monitor, use_monitor=True)


def test_the_coarse_tracker_reports_the_monitor_level_it_used():
    candidate = _coarse(
        _two_identical_features(), _monitor_peak_at(0.4), use_monitor=True
    )
    assert candidate.result.monitor_level is not None


# --- Sideband spacing must be a measurement, not an assertion ----------------
#
# Field payloads from a DFB whose scan carries several features had the strict
# detector reporting 16-20 mV spacings against the coarse tracker's 32 mV at the
# same geometry. The spacing is set by the modulation frequency and the laser's
# tuning coefficient, so the two cannot honestly differ; the strict path was
# pairing the carrier with a neighbouring feature's crossing. A wrong spacing is
# worse than none, because it rescales scan_too_wide_to_lock, the refinement
# centre-step allowance and the tracking identity all at once.

_FIELD_HALF_RANGE_V = 2.275 * 2 * 0.8 / 2047  # 1.778 mV, from resolution 2.275 at ±0.8 V
_FIELD_SIDEBAND_V = 0.032


def _field_settings(**overrides):
    base = dict(
        signal_type="pdh",
        half_range_sweep_v=_FIELD_HALF_RANGE_V,
        error_min=0.0005,
        single_error_min=0.0005,
        min_amplitude=0.0005,
    )
    base.update(overrides)
    return AutoLockScanSettings(**base)


def _field_pdh_trace(center_v, amplitude_v, neighbour_dv=None, n=2048, carrier_v=0.4841):
    """A PDH triplet at the field's feature width, optionally with a neighbour."""
    v = np.linspace(center_v - amplitude_v, center_v + amplitude_v, n)

    def lobe(v0, strength):
        u = (v - v0) / _FIELD_HALF_RANGE_V
        return strength * u / (1.0 + u * u)

    def triplet(v0, strength):
        return (
            lobe(v0, strength)
            + lobe(v0 - _FIELD_SIDEBAND_V, -0.5 * strength)
            + lobe(v0 + _FIELD_SIDEBAND_V, -0.5 * strength)
        )

    signal = triplet(carrier_v, 1.0)
    if neighbour_dv is not None:
        signal = signal + triplet(carrier_v + neighbour_dv, 0.6)
    return signal * 0.0022 / np.max(np.abs(signal))


def _detect(trace, center_v, amplitude_v, settings):
    strict = find_auto_lock_target(
        error_trace_v=trace,
        monitor_trace_v=None,
        sweep_center_v=center_v,
        sweep_amplitude_v=amplitude_v,
        settings=settings,
        preferred_slope_rising=True,
        modulation_frequency_hz=25e6,
    )
    coarse = find_coarse_auto_lock_target(
        error_trace_v=trace,
        monitor_trace_v=None,
        sweep_center_v=center_v,
        sweep_amplitude_v=amplitude_v,
        settings=settings,
        preferred_slope_rising=True,
        modulation_frequency_hz=25e6,
    )
    return strict, coarse.result


@pytest.mark.parametrize("neighbour_dv", [-0.045, 0.045])
def test_a_neighbouring_feature_never_yields_a_spacing_below_the_true_one(neighbour_dv):
    """The crossing of an adjacent feature is nearer than the real sideband."""
    settings = _field_settings()
    trace = _field_pdh_trace(0.2649, 0.4, neighbour_dv=neighbour_dv)
    strict, coarse = _detect(trace, 0.2649, 0.4, settings)

    # Both detectors still place the carrier correctly; only the spacing was at risk.
    assert strict.target_voltage == pytest.approx(0.4841, abs=3 * _FIELD_HALF_RANGE_V)

    # Either it measures the true spacing or it declines -- never a smaller number.
    if strict.sideband_offset_v is not None:
        assert strict.sideband_offset_v == pytest.approx(_FIELD_SIDEBAND_V, rel=0.15)
    # And it may not contradict the coarse tracker looking at the same trace.
    if strict.sideband_offset_v is not None and coarse.sideband_offset_v is not None:
        assert strict.sideband_offset_v == pytest.approx(
            coarse.sideband_offset_v, rel=0.2
        )


def test_a_spacing_is_never_asserted_from_one_side_alone():
    """With the upper sideband off the end of the scan there is no pair to average.

    The old code averaged whatever offsets it had, so a single side was returned
    as if it were the mean of two -- indistinguishable, to every caller, from a
    measurement that had actually been checked.
    """
    settings = _field_settings()
    # Carrier one sideband's width inside the top rail: +Omega falls outside.
    center_v, amplitude_v = 0.0, 0.5
    carrier_v = amplitude_v - 0.5 * _FIELD_SIDEBAND_V
    trace = _field_pdh_trace(center_v, amplitude_v, carrier_v=carrier_v)
    strict, _ = _detect(trace, center_v, amplitude_v, settings)

    assert strict.sideband_offset_v is None


def test_a_crossing_inside_the_carrier_window_is_not_a_sideband():
    """A crossing within the calibrated feature width belongs to the carrier.

    Tested on the offset helper directly: a trace crafted to survive the
    detector's smoother would be testing the smoother, not the exclusion rule.
    """
    n = 400
    anchor = 200
    error = np.zeros(n)
    # Carrier: rising through zero at `anchor`.
    error[:anchor] = -1.0
    error[anchor:] = 1.0
    # True -Omega and +Omega falling crossings, 40 samples out on both sides.
    error[anchor - 40 :] = np.where(
        np.arange(anchor - 40, n) < anchor, 1.0, error[anchor - 40 :]
    )
    error = np.concatenate([
        np.full(anchor - 40, 1.0),   # above zero
        np.full(40, -1.0),           # -Omega falling crossing at anchor-40
        np.full(40, 1.0),            # carrier rising crossing at anchor
        np.full(n - anchor - 40, -1.0),  # +Omega falling crossing at anchor+40
    ])
    assert _sideband_offset_pts(
        error, anchor, True, exclusion_pts=3
    ) == pytest.approx(40.0)

    # A crossing 10 samples out, inside a 24-sample carrier window, is not a
    # sideband: the true pair at ±40 must still be the measurement.
    noisy = error.copy()
    noisy[anchor + 10 : anchor + 14] = -1.0
    assert _sideband_offset_pts(
        noisy, anchor, True, exclusion_pts=24
    ) == pytest.approx(40.0)
    # Without the exclusion window that stray crossing halves the spacing.
    assert _sideband_offset_pts(noisy, anchor, True, exclusion_pts=1) is None
