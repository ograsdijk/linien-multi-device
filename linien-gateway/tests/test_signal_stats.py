import numpy as np

from app.signal_stats import ADC_SCALE, SignalStats, compute_signal_stats


def test_empty_when_no_signals():
    stats = compute_signal_stats({})
    assert stats == SignalStats()
    assert stats.control_mean_v is None


def test_control_stats_scaled_to_volts_and_counts():
    control = [-120, -80, -40, 0, 60]
    stats = compute_signal_stats({"control_signal": control})

    assert stats.control_mean_v == float(np.mean(control) / ADC_SCALE)
    assert stats.control_std_v == float(np.std(control) / ADC_SCALE)
    # range stays in raw counts (max - min), not volts
    assert stats.control_range_counts == float(max(control) - min(control))


def test_error_and_monitor_stats():
    error = [-2200, -900, 0, 1100, 2200]
    monitor = [100, 200, 300]
    stats = compute_signal_stats(
        {"error_signal": error, "monitor_signal": monitor}
    )

    assert stats.error_std_v == float(np.std(error) / ADC_SCALE)
    assert stats.error_mean_abs_v == abs(float(np.mean(error) / ADC_SCALE))
    assert stats.monitor_mean_v == float(np.mean(monitor) / ADC_SCALE)
    # control absent -> None
    assert stats.control_mean_v is None


def test_missing_signal_yields_none():
    stats = compute_signal_stats({"control_signal": [1, 2, 3]})
    assert stats.error_std_v is None
    assert stats.monitor_mean_v is None
    assert stats.control_mean_v is not None


def test_non_finite_values_are_sanitized():
    stats = compute_signal_stats(
        {"control_signal": [float("nan"), float("inf"), 0.0]}
    )
    # nan/inf coerced to 0 before reduction -> finite result, not None
    assert stats.control_mean_v == 0.0
