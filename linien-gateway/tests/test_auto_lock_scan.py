import dataclasses

import numpy as np
import pytest

from app.auto_lock_scan import (
    AutoLockScanSettings,
    calibrate_auto_lock_settings,
    find_auto_lock_target,
)
from app.schemas import AutoLockScanSettings as SchemaAutoLockScanSettings

# Rough raw full-scale used to build test traces in *raw linien units* (the detector
# no longer normalizes — see auto_lock_scan units note).
SCALE = 8000.0


def _dispersive(n=2048, amplitude=0.3, width=0.05, center=0.0):
    """A dispersive feature (rising zero-crossing at ``center`` for amplitude>0) in
    raw units: negative lobe left, positive lobe right."""
    x = np.linspace(-1.0, 1.0, n)
    u = (x - center) / width
    return (amplitude * SCALE) * u * np.exp(-0.5 * u**2)


def _pdh_triplet(n=2048, carrier=0.4, sideband=0.15, width=0.03, sb_off=0.3):
    """Carrier (rising) at 0 plus two opposite-slope sidebands at ±sb_off."""
    return (
        _dispersive(n, amplitude=carrier, width=width, center=0.0)
        + _dispersive(n, amplitude=-sideband, width=width, center=-sb_off)
        + _dispersive(n, amplitude=-sideband, width=width, center=+sb_off)
    )


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
    # No modulation frequency supplied -> no Hz/V.
    assert result.hz_per_v is None


def test_amplitude_floor_rejects_dead_trace():
    n = 2048
    # Peak-to-peak ~40 raw, well below the default min_amplitude (100).
    error = 20.0 * np.sin(np.linspace(0.0, 8.0 * np.pi, n))
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
    # Strong negative lobe on the left, weak positive lobe on the right (asymmetric).
    error = np.where(x < 0.0, _dispersive(n, amplitude=0.3, width=0.05), 0.0) + np.where(
        x >= 0.0, _dispersive(n, amplitude=0.04, width=0.05), 0.0
    )

    strict = AutoLockScanSettings(error_min=1500.0, symmetry_min=0.6)
    with pytest.raises(ValueError):
        find_auto_lock_target(
            error_trace_v=error,
            monitor_trace_v=None,
            sweep_center_v=0.0,
            sweep_amplitude_v=1.0,
            settings=strict,
        )

    permissive = AutoLockScanSettings(
        error_min=1500.0, symmetry_min=0.6, allow_single_side=True, single_error_min=500.0
    )
    result = find_auto_lock_target(
        error_trace_v=error,
        monitor_trace_v=None,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        settings=permissive,
    )
    assert max(result.left_excursion, result.right_excursion) >= 500.0


def test_respects_preferred_slope():
    n = 2048
    error = _dispersive(n, amplitude=0.3, width=0.08)  # rising at center
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
    # Carrier selected (near center), sidebands at ±0.3 V -> Ω.
    assert abs(result.target_index - (n // 2)) < 40
    assert result.sideband_offset_v is not None
    assert 0.27 < result.sideband_offset_v < 0.33
    expected = mod_hz / 0.3
    assert result.hz_per_v is not None
    assert 0.9 * expected < result.hz_per_v < 1.1 * expected


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
    monitor = 3000.0 * np.exp(-0.5 * (x / 0.1) ** 2)  # transmission peak at center

    # Threshold below the peak -> accepted.
    ok = find_auto_lock_target(
        error_trace_v=error,
        monitor_trace_v=monitor,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        settings=AutoLockScanSettings(
            use_monitor=True, monitor_mode="locked_above", monitor_threshold=1000.0
        ),
        preferred_slope_rising=True,
    )
    assert ok.monitor_level is not None and ok.monitor_level > 1000.0

    # Threshold above the peak -> rejected.
    with pytest.raises(ValueError, match="monitor"):
        find_auto_lock_target(
            error_trace_v=error,
            monitor_trace_v=monitor,
            sweep_center_v=0.0,
            sweep_amplitude_v=1.0,
            settings=AutoLockScanSettings(
                use_monitor=True, monitor_mode="locked_above", monitor_threshold=5000.0
            ),
            preferred_slope_rising=True,
        )


def test_monitor_reflection_dip():
    n = 2048
    x = np.linspace(-1.0, 1.0, n)
    error = _dispersive(n, amplitude=0.3, width=0.05)
    monitor = 3000.0 - 2800.0 * np.exp(-0.5 * (x / 0.1) ** 2)  # reflection dip at center

    ok = find_auto_lock_target(
        error_trace_v=error,
        monitor_trace_v=monitor,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        settings=AutoLockScanSettings(
            use_monitor=True, monitor_mode="locked_below", monitor_threshold=1500.0
        ),
        preferred_slope_rising=True,
    )
    assert ok.monitor_level is not None and ok.monitor_level < 1500.0

    with pytest.raises(ValueError, match="monitor"):
        find_auto_lock_target(
            error_trace_v=error,
            monitor_trace_v=monitor,
            sweep_center_v=0.0,
            sweep_amplitude_v=1.0,
            settings=AutoLockScanSettings(
                use_monitor=True, monitor_mode="locked_below", monitor_threshold=100.0
            ),
            preferred_slope_rising=True,
        )


def test_monitor_selects_feature_with_signal():
    n = 2048
    x = np.linspace(-1.0, 1.0, n)
    # Two equally strong rising features at -0.3 and +0.3.
    error = _dispersive(n, amplitude=0.3, width=0.04, center=-0.3) + _dispersive(
        n, amplitude=0.3, width=0.04, center=0.3
    )
    # Monitor peaks only at +0.3, so only that feature passes the locked_above gate.
    monitor = 3000.0 * np.exp(-0.5 * ((x - 0.3) / 0.08) ** 2)
    result = find_auto_lock_target(
        error_trace_v=error,
        monitor_trace_v=monitor,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        settings=AutoLockScanSettings(
            use_monitor=True, monitor_mode="locked_above", monitor_threshold=1000.0
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
        settings=AutoLockScanSettings(use_monitor=True, monitor_threshold=1e9),
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
    assert calib.amplitude > 1000.0  # raw units
    assert calib.settings.error_min > 0.0
    assert calib.settings.min_amplitude > 0.0
    assert calib.settings.symmetry_min > 0.4
    assert calib.settings.use_monitor is False
    # The derived settings must actually lock the same feature.
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
    monitor = 3000.0 * np.exp(-0.5 * (x / 0.12) ** 2)  # transmission peak
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
