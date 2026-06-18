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


def _trigger_first_attempt(c: AutoRelockController, **kw) -> None:
    """Drive a fresh controller to its first relock attempt.

    From idle the first 'lost' tick only arms _lost_since; the second tick
    (trigger_hold elapsed) actually begins the attempt.
    """
    c.tick(lock=True, indicator_state="lost", unlocked_trace_at=None, now=0.0, **kw)
    c.tick(lock=True, indicator_state="lost", unlocked_trace_at=None, now=0.0, **kw)


def test_retry_fires_after_sweep_mode_failure_while_unlocked():
    sweeps: list[int] = []
    relocks: list[int] = []
    kw = dict(start_sweep=lambda: sweeps.append(1), start_relock=lambda: relocks.append(1))
    c = _controller()

    _trigger_first_attempt(c, **kw)
    assert c._state == "waiting_unlocked_trace"
    assert c._attempts == 1
    assert len(sweeps) == 1

    # No fresh unlocked trace within the timeout -> failure; device is now
    # sweeping (lock=False), attempts remain -> retry primed.
    c.tick(lock=False, indicator_state=None, unlocked_trace_at=None, now=1.0, **kw)
    assert c._state == "lost_pending"
    assert c._retry_primed is True

    # Device STILL unlocked -> the primed retry must fire anyway (the bug: it
    # never did, because the lock=True+"lost" trigger can't match).
    c.tick(lock=False, indicator_state=None, unlocked_trace_at=None, now=1.1, **kw)
    assert c._attempts == 2
    assert c._state == "waiting_unlocked_trace"
    assert len(sweeps) == 2


def test_retry_budget_exhausts_into_cooldown():
    kw = dict(start_sweep=lambda: None, start_relock=lambda: None)
    c = _controller(cooldown_s=10.0, max_attempts=2)

    _trigger_first_attempt(c, **kw)
    c.tick(lock=False, indicator_state=None, unlocked_trace_at=None, now=1.0, **kw)
    c.tick(lock=False, indicator_state=None, unlocked_trace_at=None, now=1.1, **kw)  # retry
    # second attempt also times out -> attempts == max -> cooldown.
    c.tick(lock=False, indicator_state=None, unlocked_trace_at=None, now=2.0, **kw)
    assert c._state == "cooldown"
    assert c.get_status(now=2.0)["cooldown_remaining_s"] > 0.0


def test_verify_failed_still_retries():
    relocks: list[int] = []
    kw = dict(start_sweep=lambda: None, start_relock=lambda: relocks.append(1))
    c = _controller(unlocked_trace_timeout_s=10.0, verify_hold_s=0.0, cooldown_s=10.0)

    _trigger_first_attempt(c, **kw)
    # fresh unlocked trace -> relock attempt -> verifying.
    c.tick(lock=False, indicator_state=None, unlocked_trace_at=0.5, now=0.6, **kw)
    assert c._state == "verifying"
    assert relocks == [1]
    # verify never stays healthy long enough -> verify_failed -> primed.
    c.tick(lock=False, indicator_state="lost", unlocked_trace_at=0.5, now=1.7, **kw)
    assert c._state == "lost_pending"
    assert c._retry_primed is True
    # primed retry fires.
    c.tick(lock=False, indicator_state=None, unlocked_trace_at=0.5, now=1.8, **kw)
    assert c._attempts == 2
    assert c._state == "waiting_unlocked_trace"


def test_disable_clears_primed_retry():
    kw = dict(start_sweep=lambda: None, start_relock=lambda: None)
    c = _controller()
    _trigger_first_attempt(c, **kw)
    c.tick(lock=False, indicator_state=None, unlocked_trace_at=None, now=1.0, **kw)
    assert c._retry_primed is True
    c.set_config({**c.get_config(), "enabled": False})
    assert c._retry_primed is False
    assert c._state == "idle"
