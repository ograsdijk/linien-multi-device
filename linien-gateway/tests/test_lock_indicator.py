from app.lock_indicator import LockIndicatorConfig, LockIndicatorEvaluator


def test_lock_indicator_returns_unknown_when_not_locked():
    evaluator = LockIndicatorEvaluator(LockIndicatorConfig())
    snapshot = evaluator.update(lock=False, to_plot=None, now=1.0)
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
    plot = {
        "error_signal": [0, 2, -2, 1, -1],
        "control_signal": [120, 120, 120, 120, 120],
    }

    first = evaluator.update(lock=True, to_plot=plot, now=1.0)
    second = evaluator.update(lock=True, to_plot=plot, now=1.2)
    third = evaluator.update(lock=True, to_plot=plot, now=1.5)

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
    plot = {
        "error_signal": [-2200, -900, 0, 1100, 2200],
        "control_signal": [-120, -80, -40, 0, 60],
    }

    evaluator.update(lock=True, to_plot=plot, now=2.0)
    snapshot = evaluator.update(lock=True, to_plot=plot, now=2.4)

    assert snapshot["state"] == "locked"
    assert snapshot["reasons"] == []
