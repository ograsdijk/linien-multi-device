import numpy as np
import pytest

from app.auto_lock_scan import AutoLockScanSettings, find_auto_lock_target


def test_auto_lock_scan_finds_rising_crossing_near_center():
    n_points = 2048
    x = np.linspace(-1.0, 1.0, n_points)
    error_trace_v = 0.25 * np.tanh(x / 0.05)
    monitor_trace_v = 0.4 - np.exp(-(x / 0.15) ** 2)

    result = find_auto_lock_target(
        error_trace_v=error_trace_v,
        monitor_trace_v=monitor_trace_v,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        settings=AutoLockScanSettings(),
    )

    assert abs(result.target_index - (n_points // 2)) < 40
    assert result.target_slope_rising is True
    assert abs(result.target_voltage) < 0.05
    assert result.pair_excursion_v > 0.1


def test_auto_lock_scan_single_side_toggle_changes_acceptance():
    n_points = 2048
    x = np.linspace(-1.0, 1.0, n_points)
    error_trace_v = np.where(
        x < 0.0,
        0.24 * np.tanh(x / 0.05),
        0.03 * np.tanh(x / 0.05),
    )

    strict_settings = AutoLockScanSettings(
        allow_single_side=False,
        error_min=0.12,
        symmetry_min=0.6,
    )
    with pytest.raises(ValueError):
        find_auto_lock_target(
            error_trace_v=error_trace_v,
            monitor_trace_v=None,
            sweep_center_v=0.0,
            sweep_amplitude_v=1.0,
            settings=strict_settings,
        )

    permissive_settings = AutoLockScanSettings(
        allow_single_side=True,
        single_error_min=0.09,
        error_min=0.12,
        symmetry_min=0.6,
    )
    result = find_auto_lock_target(
        error_trace_v=error_trace_v,
        monitor_trace_v=None,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        settings=permissive_settings,
    )
    assert result.pair_excursion_v > 0.09


def test_auto_lock_scan_use_monitor_requires_monitor_trace():
    n_points = 2048
    x = np.linspace(-1.0, 1.0, n_points)
    error_trace_v = 0.2 * np.tanh(x / 0.08)

    with pytest.raises(ValueError, match="Monitor trace unavailable"):
        find_auto_lock_target(
            error_trace_v=error_trace_v,
            monitor_trace_v=None,
            sweep_center_v=0.0,
            sweep_amplitude_v=1.0,
            settings=AutoLockScanSettings(use_monitor=True),
        )


def test_auto_lock_scan_respects_preferred_slope():
    n_points = 2048
    x = np.linspace(-1.0, 1.0, n_points)
    error_trace_v = 0.2 * np.tanh(x / 0.08)

    with pytest.raises(ValueError):
        find_auto_lock_target(
            error_trace_v=error_trace_v,
            monitor_trace_v=None,
            sweep_center_v=0.0,
            sweep_amplitude_v=1.0,
            settings=AutoLockScanSettings(),
            preferred_slope_rising=False,
        )

    result = find_auto_lock_target(
        error_trace_v=error_trace_v,
        monitor_trace_v=None,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        settings=AutoLockScanSettings(),
        preferred_slope_rising=True,
    )
    assert result.target_slope_rising is True


def test_auto_lock_scan_handles_steep_crossing_with_strict_center_threshold():
    n_points = 2048
    x = np.linspace(-1.0, 1.0, n_points)
    center_v = 0.00035
    # Steep slope where raw sampled point near crossing can exceed 10 mV.
    error_trace_v = 0.7 * np.tanh((x - center_v) / 0.008)
    result = find_auto_lock_target(
        error_trace_v=error_trace_v,
        monitor_trace_v=None,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        settings=AutoLockScanSettings(
            crossing_max_v=0.01,
            error_min=0.2,
            symmetry_min=0.2,
            smooth_window_pts=1,
        ),
        preferred_slope_rising=True,
    )
    assert abs(result.target_voltage - center_v) < 0.002
    assert result.center_abs_v <= 0.01


def test_auto_lock_scan_reports_slope_hint_when_only_opposite_slope_passes():
    n_points = 2048
    x = np.linspace(-1.0, 1.0, n_points)
    error_trace_v = 0.25 * np.tanh(x / 0.05)

    with pytest.raises(ValueError, match="target slope"):
        find_auto_lock_target(
            error_trace_v=error_trace_v,
            monitor_trace_v=None,
            sweep_center_v=0.0,
            sweep_amplitude_v=1.0,
            settings=AutoLockScanSettings(
                error_min=0.1,
                symmetry_min=0.2,
                allow_single_side=True,
                single_error_min=0.08,
            ),
            preferred_slope_rising=False,
        )
