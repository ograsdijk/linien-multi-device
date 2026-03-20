from __future__ import annotations

import time
import logging
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


@dataclass
class AutoRelockConfig:
    enabled: bool = False
    trigger_hold_s: float = 0.8
    verify_hold_s: float = 1.2
    cooldown_s: float = 8.0
    unlocked_trace_timeout_s: float = 2.0
    max_attempts: int = 2

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "AutoRelockConfig":
        defaults = cls()
        if payload is None:
            return defaults
        return cls(
            enabled=_as_bool(payload.get("enabled"), defaults.enabled),
            trigger_hold_s=max(
                0.05, _as_float(payload.get("trigger_hold_s"), defaults.trigger_hold_s)
            ),
            verify_hold_s=max(
                0.05, _as_float(payload.get("verify_hold_s"), defaults.verify_hold_s)
            ),
            cooldown_s=max(0.0, _as_float(payload.get("cooldown_s"), defaults.cooldown_s)),
            unlocked_trace_timeout_s=max(
                0.1,
                _as_float(
                    payload.get("unlocked_trace_timeout_s"),
                    defaults.unlocked_trace_timeout_s,
                ),
            ),
            max_attempts=max(
                1, _as_int(payload.get("max_attempts"), defaults.max_attempts)
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AutoRelockController:
    def __init__(
        self,
        config: AutoRelockConfig | Mapping[str, Any] | None = None,
        event_hook: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        if isinstance(config, AutoRelockConfig):
            self._config = config
        else:
            self._config = AutoRelockConfig.from_mapping(config)
        self._event_hook = event_hook
        self._state = "idle"  # idle | lost_pending | waiting_unlocked_trace | verifying | cooldown
        self._lost_since: float | None = None
        self._wait_since: float | None = None
        self._verify_since: float | None = None
        self._verify_good_since: float | None = None
        self._cooldown_until: float | None = None
        self._attempts = 0
        self._last_trigger_at: float | None = None
        self._last_attempt_at: float | None = None
        self._last_success_at: float | None = None
        self._last_failure_at: float | None = None
        self._last_error: str | None = None

    def set_event_hook(
        self, event_hook: Callable[[str, dict[str, Any]], None] | None
    ) -> None:
        self._event_hook = event_hook

    def _emit_event(self, event: str, **payload: Any) -> None:
        if self._event_hook is None:
            return
        try:
            self._event_hook(event, payload)
        except Exception:
            logger.debug("Auto-relock event hook failed", exc_info=True)

    def _set_state(self, state: str) -> None:
        self._state = state

    def _reset_transient(self) -> None:
        self._lost_since = None
        self._wait_since = None
        self._verify_since = None
        self._verify_good_since = None

    def _enter_cooldown(self, now: float) -> None:
        self._reset_transient()
        self._attempts = 0
        if self._config.cooldown_s > 0:
            self._cooldown_until = now + float(self._config.cooldown_s)
            self._set_state("cooldown")
        else:
            self._cooldown_until = None
            self._set_state("idle")

    def _record_failure(self, reason: str, now: float) -> None:
        self._last_error = reason
        self._last_failure_at = now
        self._emit_event(
            "failure",
            reason=reason,
            attempts=int(self._attempts),
            max_attempts=int(self._config.max_attempts),
            state=self._state,
            at=now,
        )
        if self._attempts >= int(self._config.max_attempts):
            self._enter_cooldown(now)
            return
        self._set_state("lost_pending")
        # Retry promptly on the next tick.
        self._lost_since = now - float(self._config.trigger_hold_s)
        self._wait_since = None
        self._verify_since = None
        self._verify_good_since = None

    def _begin_attempt(self, now: float, start_sweep: Callable[[], None]) -> None:
        if self._attempts >= int(self._config.max_attempts):
            self._record_failure("max_attempts_reached", now)
            return
        self._attempts += 1
        self._last_attempt_at = now
        self._emit_event(
            "attempt",
            attempts=int(self._attempts),
            max_attempts=int(self._config.max_attempts),
            at=now,
        )
        try:
            start_sweep()
        except Exception as exc:
            self._record_failure(f"start_sweep_failed: {exc}", now)
            return
        self._wait_since = now
        self._set_state("waiting_unlocked_trace")

    def tick(
        self,
        *,
        lock: bool,
        indicator_state: str | None,
        unlocked_trace_at: float | None,
        start_sweep: Callable[[], None],
        start_relock: Callable[[], None],
        now: float | None = None,
    ) -> None:
        ts = float(now) if now is not None else time.time()

        if not self._config.enabled:
            self._set_state("idle")
            self._attempts = 0
            self._cooldown_until = None
            self._last_error = None
            self._reset_transient()
            return

        if self._cooldown_until is not None and ts < self._cooldown_until:
            self._set_state("cooldown")
            return
        if self._state == "cooldown":
            self._cooldown_until = None
            self._set_state("idle")

        if self._state == "waiting_unlocked_trace":
            has_fresh_unlocked_trace = (
                unlocked_trace_at is not None
                and (self._wait_since is None or unlocked_trace_at >= self._wait_since)
            )
            if has_fresh_unlocked_trace:
                try:
                    start_relock()
                except Exception as exc:
                    self._record_failure(f"start_relock_failed: {exc}", ts)
                    return
                self._verify_since = ts
                self._verify_good_since = None
                self._wait_since = None
                self._set_state("verifying")
                return
            if (
                self._wait_since is not None
                and (ts - self._wait_since) >= float(self._config.unlocked_trace_timeout_s)
            ):
                self._record_failure("unlocked_trace_timeout", ts)
            return

        if self._state == "verifying":
            healthy = bool(lock) and (indicator_state not in {"lost"})
            if healthy:
                if self._verify_good_since is None:
                    self._verify_good_since = ts
                if (ts - self._verify_good_since) >= float(self._config.verify_hold_s):
                    self._last_success_at = ts
                    self._last_error = None
                    self._emit_event(
                        "success",
                        attempts=int(self._attempts),
                        at=ts,
                    )
                    self._enter_cooldown(ts)
                return
            self._verify_good_since = None
            if self._verify_since is not None and (
                ts - self._verify_since
            ) >= max(1.0, float(self._config.verify_hold_s) * 2.0):
                self._record_failure("verify_failed", ts)
            return

        lost_now = bool(lock) and indicator_state == "lost"
        if lost_now:
            if self._lost_since is None:
                self._lost_since = ts
                self._set_state("lost_pending")
                return
            if (ts - self._lost_since) >= float(self._config.trigger_hold_s):
                if self._last_trigger_at is None or self._attempts == 0:
                    self._last_trigger_at = ts
                self._begin_attempt(ts, start_sweep)
            else:
                self._set_state("lost_pending")
            return

        self._lost_since = None
        if self._state == "lost_pending":
            self._set_state("idle")

    def get_config(self) -> dict[str, Any]:
        return self._config.to_dict()

    def set_config(
        self,
        payload: AutoRelockConfig | Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if isinstance(payload, AutoRelockConfig):
            self._config = payload
        else:
            self._config = AutoRelockConfig.from_mapping(payload)
        if not self._config.enabled:
            self._set_state("idle")
            self._attempts = 0
            self._cooldown_until = None
            self._last_error = None
            self._reset_transient()
        return self.get_config()

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        return self.set_config({**self.get_config(), "enabled": bool(enabled)})

    def get_status(self, now: float | None = None) -> dict[str, Any]:
        ts = float(now) if now is not None else time.time()
        cooldown_remaining = 0.0
        if self._cooldown_until is not None:
            cooldown_remaining = max(0.0, self._cooldown_until - ts)
        return {
            "enabled": bool(self._config.enabled),
            "state": self._state,
            "attempts": int(self._attempts),
            "max_attempts": int(self._config.max_attempts),
            "cooldown_remaining_s": float(cooldown_remaining),
            "last_trigger_at": self._last_trigger_at,
            "last_attempt_at": self._last_attempt_at,
            "last_success_at": self._last_success_at,
            "last_failure_at": self._last_failure_at,
            "last_error": self._last_error,
        }

    def get_state(self, now: float | None = None) -> dict[str, Any]:
        return {
            "config": self.get_config(),
            "status": self.get_status(now=now),
        }
