from __future__ import annotations

from app.auto_relock import AutoRelockConfig, AutoRelockController


def _controller(**overrides) -> AutoRelockController:
    cfg = dict(
        enabled=True,
        trigger_hold_s=0.0,
        verify_hold_s=0.0,
        cooldown_s=10.0,
        unlocked_trace_timeout_s=0.5,
        max_attempts=2,
    )
    cfg.update(overrides)
    return AutoRelockController(AutoRelockConfig(**cfg))


def _trigger_first_attempt(c: AutoRelockController) -> None:
    """Drive a fresh controller to its first relock attempt and complete the
    sweep. From idle the first 'lost' tick only arms _lost_since; the second
    returns the 'sweep' action."""
    assert c.tick(lock=True, indicator_state="lost", unlocked_trace_at=None, now=0.0) is None
    assert c.tick(lock=True, indicator_state="lost", unlocked_trace_at=None, now=0.0) == "sweep"
    c.complete_action("sweep", True, now=0.0)
    assert c._state == "waiting_unlocked_trace"


def test_retry_fires_after_sweep_mode_failure_while_unlocked():
    c = _controller()
    _trigger_first_attempt(c)
    assert c._attempts == 1

    # No fresh unlocked trace within the timeout -> failure; device is now
    # sweeping (lock=False), attempts remain -> retry primed.
    assert c.tick(lock=False, indicator_state=None, unlocked_trace_at=None, now=1.0) is None
    assert c._state == "lost_pending"
    assert c._retry_primed is True

    # Device STILL unlocked -> the primed retry must still produce a sweep.
    action = c.tick(lock=False, indicator_state=None, unlocked_trace_at=None, now=1.1)
    assert action == "sweep"
    c.complete_action("sweep", True, now=1.1)
    assert c._attempts == 2
    assert c._state == "waiting_unlocked_trace"


def test_retry_budget_exhausts_into_cooldown():
    c = _controller(cooldown_s=10.0, max_attempts=2)
    _trigger_first_attempt(c)
    assert c.tick(lock=False, indicator_state=None, unlocked_trace_at=None, now=1.0) is None
    assert c.tick(lock=False, indicator_state=None, unlocked_trace_at=None, now=1.1) == "sweep"
    c.complete_action("sweep", True, now=1.1)
    # second attempt also times out -> attempts == max -> cooldown.
    assert c.tick(lock=False, indicator_state=None, unlocked_trace_at=None, now=2.0) is None
    assert c._state == "cooldown"
    assert c.get_status(now=2.0)["cooldown_remaining_s"] > 0.0


def test_verify_failed_still_retries():
    c = _controller(unlocked_trace_timeout_s=10.0, verify_hold_s=0.0, cooldown_s=10.0)
    _trigger_first_attempt(c)
    # fresh unlocked trace -> relock action -> verifying.
    assert c.tick(lock=False, indicator_state=None, unlocked_trace_at=0.5, now=0.6) == "relock"
    c.complete_action("relock", True, now=0.6)
    assert c._state == "verifying"
    # verify never stays healthy long enough -> verify_failed -> primed.
    assert c.tick(lock=False, indicator_state="lost", unlocked_trace_at=0.5, now=1.7) is None
    assert c._state == "lost_pending"
    assert c._retry_primed is True
    # primed retry -> sweep.
    assert c.tick(lock=False, indicator_state=None, unlocked_trace_at=0.5, now=1.8) == "sweep"
    c.complete_action("sweep", True, now=1.8)
    assert c._attempts == 2
    assert c._state == "waiting_unlocked_trace"


def test_full_relock_success_path():
    c = _controller(verify_hold_s=0.0, unlocked_trace_timeout_s=10.0, cooldown_s=5.0)
    _trigger_first_attempt(c)
    assert c.tick(lock=False, indicator_state=None, unlocked_trace_at=0.5, now=0.6) == "relock"
    c.complete_action("relock", True, now=0.6)
    # Healthy lock long enough -> success -> cooldown.
    assert c.tick(lock=True, indicator_state="locked", unlocked_trace_at=0.5, now=0.7) is None
    assert c._state == "cooldown"
    assert c.get_status()["last_success_at"] is not None


def test_sweep_action_failure_records_and_reprimes():
    c = _controller(max_attempts=3)
    assert c.tick(lock=True, indicator_state="lost", unlocked_trace_at=None, now=0.0) is None
    assert c.tick(lock=True, indicator_state="lost", unlocked_trace_at=None, now=0.0) == "sweep"
    # The device I/O (start_sweep) failed.
    c.complete_action("sweep", False, "boom", now=0.0)
    assert c._state == "lost_pending"
    assert c._retry_primed is True
    assert "start_sweep_failed" in (c.get_status()["last_error"] or "")


def test_stale_action_result_ignored_after_disable():
    # The core #26 safety: a result reported after the controller was reset
    # mid-action must not resurrect the cancelled attempt.
    c = _controller()
    assert c.tick(lock=True, indicator_state="lost", unlocked_trace_at=None, now=0.0) is None
    assert c.tick(lock=True, indicator_state="lost", unlocked_trace_at=None, now=0.0) == "sweep"
    # User disables while the sweep RPyC is in flight.
    c.set_config({**c.get_config(), "enabled": False})
    assert c._state == "idle"
    assert c._pending_action is None
    # Late success must be ignored.
    c.complete_action("sweep", True, now=0.1)
    assert c._state == "idle"
    assert c._pending_action is None


def test_disable_clears_primed_retry():
    c = _controller()
    _trigger_first_attempt(c)
    assert c.tick(lock=False, indicator_state=None, unlocked_trace_at=None, now=1.0) is None
    assert c._retry_primed is True
    c.set_config({**c.get_config(), "enabled": False})
    assert c._retry_primed is False
    assert c._state == "idle"
