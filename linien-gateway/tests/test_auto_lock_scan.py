import numpy as np
import pytest

from app.auto_lock_scan import (
    AutoLockCalibrationFactors,
    AutoLockScanSettings,
    calibrate_auto_lock_settings,
    find_auto_lock_target,
)


def _dispersive_trace(n_points: int = 2048, amplitude: float = 0.3, width: float = 0.1):
    """A PDH-like dispersive feature: rising zero crossing at 0, lobes at +/-width."""
    x = np.linspace(-1.0, 1.0, n_points)
    return amplitude * (x / width) * np.exp(-0.5 * (x / width) ** 2)


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


def test_calibrate_derives_settings_that_lock_the_feature():
    n_points = 2048
    error_trace_v = _dispersive_trace(n_points, amplitude=0.3, width=0.1)

    calib = calibrate_auto_lock_settings(
        error_trace_v=error_trace_v,
        monitor_trace_v=None,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        base=AutoLockScanSettings(),
        preferred_slope_rising=True,
    )

    # Amplitude is a few tenths of a volt; feature half-width tracks `width`.
    assert 0.3 < calib.amplitude_v < 0.45
    assert 0.07 < calib.feature_half_width_v < 0.14
    assert 0.1 < calib.settings.half_range_v < 0.2
    # Thresholds are fractions of what the reference actually showed.
    assert 0.12 < calib.settings.error_min < 0.22
    assert calib.settings.crossing_max_v < calib.settings.error_min
    assert calib.settings.symmetry_min > 0.5
    # Optional features stay off unless requested.
    assert calib.settings.use_monitor is False
    assert calib.settings.allow_single_side is False

    # The derived settings must actually lock the same feature.
    result = find_auto_lock_target(
        error_trace_v=error_trace_v,
        monitor_trace_v=None,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        settings=calib.settings,
        preferred_slope_rising=True,
    )
    assert abs(result.target_index - (n_points // 2)) < 40
    assert result.target_slope_rising is True


def test_calibrate_rejects_flat_trace():
    error_trace_v = np.zeros(2048)
    with pytest.raises(ValueError, match="No PDH-like signal"):
        calibrate_auto_lock_settings(
            error_trace_v=error_trace_v,
            monitor_trace_v=None,
            sweep_center_v=0.0,
            sweep_amplitude_v=1.0,
            base=AutoLockScanSettings(),
        )


def test_calibrate_monitor_option():
    n_points = 2048
    x = np.linspace(-1.0, 1.0, n_points)
    error_trace_v = _dispersive_trace(n_points, amplitude=0.3, width=0.1)
    # Asymmetric monitor: clearly different level either side of the crossing.
    monitor_trace_v = 0.2 + 0.2 * np.tanh(x / 0.1)

    # Requested but absent -> error.
    with pytest.raises(ValueError, match="Monitor signal requested"):
        calibrate_auto_lock_settings(
            error_trace_v=error_trace_v,
            monitor_trace_v=None,
            sweep_center_v=0.0,
            sweep_amplitude_v=1.0,
            base=AutoLockScanSettings(),
            include_monitor=True,
        )

    calib = calibrate_auto_lock_settings(
        error_trace_v=error_trace_v,
        monitor_trace_v=monitor_trace_v,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        base=AutoLockScanSettings(),
        preferred_slope_rising=True,
        include_monitor=True,
    )
    assert calib.settings.use_monitor is True
    assert calib.monitor_contrast_v is not None and calib.monitor_contrast_v > 0.0
    assert calib.settings.monitor_contrast_min_v > 0.0


def test_calibrate_monitor_zero_contrast_leaves_monitor_off():
    # A symmetric monitor (e.g. a transmission peak) has ~zero left/right
    # contrast; calibration must NOT force use_monitor on (that would make the
    # detector reject the very feature) — it should leave it disabled.
    n_points = 2048
    x = np.linspace(-1.0, 1.0, n_points)
    error_trace_v = _dispersive_trace(n_points, amplitude=0.3, width=0.1)
    monitor_trace_v = 0.4 - np.exp(-((x / 0.1) ** 2))  # symmetric about x=0

    calib = calibrate_auto_lock_settings(
        error_trace_v=error_trace_v,
        monitor_trace_v=monitor_trace_v,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        base=AutoLockScanSettings(),
        preferred_slope_rising=True,
        include_monitor=True,
    )
    assert calib.settings.use_monitor is False


def test_calibrate_single_side_option():
    n_points = 2048
    error_trace_v = _dispersive_trace(n_points, amplitude=0.3, width=0.1)

    calib = calibrate_auto_lock_settings(
        error_trace_v=error_trace_v,
        monitor_trace_v=None,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        base=AutoLockScanSettings(),
        preferred_slope_rising=True,
        allow_single_side=True,
    )
    assert calib.settings.allow_single_side is True
    assert calib.settings.single_error_min > 0.0


def test_calibrate_self_check_rejects_unlockable_trace():
    # Pure noise that scrapes past the amplitude floor but has no real
    # dispersive feature must fail the self-check rather than persist garbage.
    rng = np.random.default_rng(0)
    error_trace_v = 0.02 * rng.standard_normal(2048)
    with pytest.raises(ValueError):
        calibrate_auto_lock_settings(
            error_trace_v=error_trace_v,
            monitor_trace_v=None,
            sweep_center_v=0.0,
            sweep_amplitude_v=1.0,
            base=AutoLockScanSettings(),
        )


def test_calibrate_picks_stronger_feature_consistent_with_find():
    # Two dispersive features: a strong one off-centre and a weak one centred.
    # Calibration must select (and tune for) whichever feature find would lock.
    n_points = 2048
    x = np.linspace(-1.0, 1.0, n_points)
    strong = 0.4 * ((x - 0.4) / 0.05) * np.exp(-0.5 * ((x - 0.4) / 0.05) ** 2)
    weak = 0.12 * ((x + 0.3) / 0.05) * np.exp(-0.5 * ((x + 0.3) / 0.05) ** 2)
    error_trace_v = strong + weak

    calib = calibrate_auto_lock_settings(
        error_trace_v=error_trace_v,
        monitor_trace_v=None,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        base=AutoLockScanSettings(),
        preferred_slope_rising=True,
    )
    result = find_auto_lock_target(
        error_trace_v=error_trace_v,
        monitor_trace_v=None,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        settings=calib.settings,
        preferred_slope_rising=True,
    )
    # The calibrated anchor and find's choice must agree (same feature).
    assert abs(result.target_index - calib.target_index) < 40
    # And it should be the stronger feature near x = +0.4.
    assert calib.target_voltage > 0.2


def test_calibrate_wide_feature_not_truncated():
    # Lobes at +/-0.3 sit beyond the old n/8 search window; feature width must
    # not be capped at ~0.25.
    n_points = 2048
    error_trace_v = _dispersive_trace(n_points, amplitude=0.3, width=0.3)
    calib = calibrate_auto_lock_settings(
        error_trace_v=error_trace_v,
        monitor_trace_v=None,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        base=AutoLockScanSettings(),
        preferred_slope_rising=True,
    )
    assert calib.feature_half_width_v > 0.27
    assert calib.settings.half_range_v > 0.3


def test_calibrate_baseline_offset_does_not_inflate_half_range():
    # A downward baseline tilt on the left (never crossing back through zero)
    # must not pull the lobe-peak search out to the far edge and balloon
    # half_range_v toward its 2.0 clamp.
    n_points = 2048
    x = np.linspace(-1.0, 1.0, n_points)
    feature = _dispersive_trace(n_points, amplitude=0.3, width=0.1)
    tilt = np.where(x < 0.0, 0.3 * x, 0.0)  # 0 at center, -0.3 at far left
    error_trace_v = feature + tilt

    calib = calibrate_auto_lock_settings(
        error_trace_v=error_trace_v,
        monitor_trace_v=None,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        base=AutoLockScanSettings(),
        preferred_slope_rising=True,
    )
    # The real feature half-width is ~0.1; allow margin but reject the
    # baseline-driven blow-up (which produced ~0.9 width / ~1.2 half_range).
    assert calib.feature_half_width_v < 0.3
    assert calib.settings.half_range_v < 0.4


def test_calibrate_sharp_narrow_feature_not_false_rejected():
    # A sharp feature occupying few points used to read ~0 amplitude under a
    # p98-p2 estimate and be wrongly rejected as "below noise floor".
    n_points = 2048
    error_trace_v = _dispersive_trace(n_points, amplitude=0.5, width=0.01)
    calib = calibrate_auto_lock_settings(
        error_trace_v=error_trace_v,
        monitor_trace_v=None,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        base=AutoLockScanSettings(),
        preferred_slope_rising=True,
    )
    assert calib.amplitude_v > 0.3
    assert calib.settings.error_min > 0.0


def test_calibrate_min_amplitude_override_allows_small_signal():
    n_points = 2048
    error_trace_v = _dispersive_trace(n_points, amplitude=0.006, width=0.1)

    # Below the default floor -> rejected.
    with pytest.raises(ValueError, match="No PDH-like signal"):
        calibrate_auto_lock_settings(
            error_trace_v=error_trace_v,
            monitor_trace_v=None,
            sweep_center_v=0.0,
            sweep_amplitude_v=1.0,
            base=AutoLockScanSettings(),
            preferred_slope_rising=True,
        )

    # Lowering the floor lets the (clean) small signal calibrate.
    calib = calibrate_auto_lock_settings(
        error_trace_v=error_trace_v,
        monitor_trace_v=None,
        sweep_center_v=0.0,
        sweep_amplitude_v=1.0,
        base=AutoLockScanSettings(),
        preferred_slope_rising=True,
        factors=AutoLockCalibrationFactors(min_amplitude_v=0.001),
    )
    assert calib.settings.error_min > 0.0
