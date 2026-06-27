from app.lock_indicator import LockIndicatorConfig, LockIndicatorEvaluator
from app.signal_stats import SignalStats, compute_signal_stats


def test_lock_indicator_returns_unknown_when_not_locked():
    evaluator = LockIndicatorEvaluator(LockIndicatorConfig())
    snapshot = evaluator.update(lock=False, stats=SignalStats(), now=1.0)
    assert snapshot["state"] == "unknown"
    assert "not_locked" in snapshot["reasons"]


def test_lock_indicator_marks_lost_when_control_is_stuck():
    config = LockIndicatorConfig(
        use_control=True,
        use_error=False,
        use_monitor=False,
        control_stuck_delta_counts=0,
        control_stuck_time_s=0.3,
    )
    evaluator = LockIndicatorEvaluator(config)
    stats = compute_signal_stats(
        {
            "error_signal": [0, 2, -2, 1, -1],
            "control_signal": [120, 120, 120, 120, 120],
        }
    )

    first = evaluator.update(lock=True, stats=stats, now=1.0)
    second = evaluator.update(lock=True, stats=stats, now=1.2)
    third = evaluator.update(lock=True, stats=stats, now=1.5)

    assert first["state"] in {"locked", "marginal", "lost"}
    assert second["state"] in {"locked", "marginal", "lost"}
    assert third["state"] == "lost"
    assert "control_stuck" in third["reasons"]


def test_lock_indicator_reaches_locked_when_signals_are_healthy():
    config = LockIndicatorConfig(
        good_hold_s=0.2,
        bad_hold_s=0.2,
        use_control=True,
        use_error=True,
        use_monitor=False,
        control_stuck_time_s=1.0,
        error_mean_abs_max_v=0.4,
        error_std_min_v=0.001,
        error_std_max_v=1.0,
    )
    evaluator = LockIndicatorEvaluator(config)
    stats = compute_signal_stats(
        {
            "error_signal": [-2200, -900, 0, 1100, 2200],
            "control_signal": [-120, -80, -40, 0, 60],
        }
    )

    evaluator.update(lock=True, stats=stats, now=2.0)
    snapshot = evaluator.update(lock=True, stats=stats, now=2.4)

    assert snapshot["state"] == "locked"
    assert snapshot["reasons"] == []


def test_disabled_indicator_does_not_consult_stats():
    """A disabled indicator reports unknown/disabled but leaves the stats
    pipeline untouched (stats are surfaced separately, not via the indicator)."""
    evaluator = LockIndicatorEvaluator(LockIndicatorConfig(enabled=False))
    stats = compute_signal_stats({"control_signal": [100, 100, 100]})
    snapshot = evaluator.update(lock=True, stats=stats, now=1.0)
    assert snapshot["state"] == "unknown"
    assert snapshot["reasons"] == ["disabled"]
    # control voltage is still meaningful and available from the stats object.
    assert stats.control_mean_v is not None
