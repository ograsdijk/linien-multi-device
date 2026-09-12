"""A durable, per-device timeline of board and server events.

Everything the gateway knew about a board's history was last-value-only:
`DeviceSession._diagnosis_cache` is cleared on reconnect, `TelemetryEntry` keeps
one reading, and `LogStore` is RAM-only and 24 hours deep. So "this board
rebooted three times last night" -- the question you actually want answered on
the morning after -- was unanswerable.

This store keeps the transitions. It is deliberately small: a bounded ring per
device, written to one JSON file, holding only events that already exist as
one-shot transitions elsewhere in the gateway. Nothing here polls, and nothing
here is on a status path.

Two properties matter for the callers:

- **Appending is cheap and never raises.** Hooks live in the session poll
  threads and the telemetry poll loop, and a failure to record history must
  never disturb a connection or a reading.
- **Disk writes are debounced.** A reconnect storm would otherwise rewrite the
  whole file once per device; instead writes coalesce and the file is flushed
  at most every `flush_interval_s`.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Event kinds. Each corresponds to a transition the gateway already detects
# exactly once, so the timeline cannot fill with repeats of a steady state.
KIND_DISCONNECTED = "disconnected"
KIND_DIAGNOSIS = "diagnosis"
KIND_REBOOT_DETECTED = "reboot_detected"
KIND_TELEMETRY_OFFLINE = "telemetry_offline"
KIND_TELEMETRY_RECOVERED = "telemetry_recovered"
KIND_PERSISTENT_LOG_ENABLED = "persistent_log_enabled"

DEFAULT_MAX_PER_DEVICE = 200
DEFAULT_MAX_AGE_S = 30.0 * 24.0 * 60.0 * 60.0
DEFAULT_FLUSH_INTERVAL_S = 5.0


def _jsonable(value: Any, depth: int = 0) -> Any:
    """Coerce a value into something `json.dumps` will accept.

    `data` arrives from log-event callers whose `details` dicts are free-form,
    and one unserialisable object in one event would otherwise make every
    future write of the whole file fail. Coercing at the door keeps the failure
    local to the field that caused it.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if depth >= 3:
        return str(value)[:200]
    if isinstance(value, dict):
        return {
            str(key): _jsonable(item, depth + 1)
            for key, item in list(value.items())[:30]
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item, depth + 1) for item in list(value)[:30]]
    return str(value)[:200]


class BoardEventStore:
    def __init__(
        self,
        path: Path | str,
        *,
        max_per_device: int = DEFAULT_MAX_PER_DEVICE,
        max_age_s: float = DEFAULT_MAX_AGE_S,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
    ) -> None:
        self._path = Path(path)
        self._max_per_device = max(1, int(max_per_device))
        self._max_age_s = max(60.0, float(max_age_s))
        self._flush_interval_s = max(0.0, float(flush_interval_s))
        self._lock = threading.RLock()
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._boot_ids: dict[str, str] = {}
        self._dirty = False
        self._last_flush = 0.0
        self._load()

    # --- reading ---------------------------------------------------------

    def events(self, device_key: str, *, limit: int = DEFAULT_MAX_PER_DEVICE) -> list[dict]:
        """Most recent first, so the UI renders a timeline without reversing."""
        safe_limit = max(1, min(int(limit), self._max_per_device))
        with self._lock:
            self._prune_locked(device_key, now=time.time())
            items = self._events.get(device_key, [])
            return [dict(item) for item in reversed(items[-safe_limit:])]

    def last_boot_id(self, device_key: str) -> str | None:
        with self._lock:
            return self._boot_ids.get(device_key)

    # --- writing ---------------------------------------------------------

    def record(
        self,
        device_key: str,
        kind: str,
        *,
        detail: str | None = None,
        data: dict[str, Any] | None = None,
        boot_id: str | None = None,
        ts: float | None = None,
    ) -> dict[str, Any] | None:
        """Append one event. Returns it, or None if it was dropped.

        Never raises: callers are poll threads whose real job is the connection.
        """
        try:
            now = float(ts) if ts is not None else time.time()
            event = {
                "ts": now,
                "device_key": device_key,
                "kind": kind,
                "detail": (detail or "")[:500],
                "data": _jsonable(data or {}),
                "boot_id": boot_id,
            }
            with self._lock:
                items = self._events.setdefault(device_key, [])
                items.append(event)
                if boot_id:
                    self._boot_ids[device_key] = boot_id
                self._prune_locked(device_key, now=now)
                self._dirty = True
                self._maybe_flush_locked(now)
            return event
        except Exception:  # noqa: BLE001 - history must never break a caller
            logger.debug("Failed recording board event key=%s", device_key, exc_info=True)
            return None

    def note_boot_id(self, device_key: str, boot_id: str) -> bool:
        """Record the board's current boot id; True when it changed.

        A changed boot id is proof of a reboot, where the uptime comparison in
        `diagnosis.py` is only a threshold. The first sighting for a device is
        not a reboot -- we simply had nothing to compare against.
        """
        if not boot_id:
            return False
        with self._lock:
            previous = self._boot_ids.get(device_key)
            self._boot_ids[device_key] = boot_id
            if previous is None or previous == boot_id:
                self._dirty = self._dirty or previous is None
                return False
        self.record(
            device_key,
            KIND_REBOOT_DETECTED,
            detail="The board restarted (kernel boot id changed).",
            boot_id=boot_id,
        )
        return True

    def forget(self, device_key: str) -> None:
        with self._lock:
            self._events.pop(device_key, None)
            self._boot_ids.pop(device_key, None)
            self._dirty = True
        self.flush()

    # --- persistence -----------------------------------------------------

    def flush(self) -> None:
        """Write the file if anything changed. Best effort; never raises."""
        with self._lock:
            if not self._dirty:
                return
            payload = {
                "version": 1,
                "boot_ids": dict(self._boot_ids),
                "events": {key: list(items) for key, items in self._events.items()},
            }
            self._dirty = False
            self._last_flush = time.time()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self._path.with_suffix(self._path.suffix + ".tmp")
            temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temp_path.replace(self._path)
        except (OSError, TypeError, ValueError):
            # Never fatal: the timeline is a diagnostic aid, and losing a write
            # must not take down whatever was being diagnosed.
            logger.warning("Failed writing board events to %s", self._path, exc_info=True)

    def _maybe_flush_locked(self, now: float) -> None:
        if now - self._last_flush < self._flush_interval_s:
            return
        # Released and re-taken inside flush(); the lock is reentrant.
        self.flush()

    def _load(self) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError:
            logger.warning("Failed reading board events at %s", self._path, exc_info=True)
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Malformed board events at %s; ignoring.", self._path)
            return
        if not isinstance(data, dict):
            return
        boot_ids = data.get("boot_ids")
        if isinstance(boot_ids, dict):
            self._boot_ids = {
                key: value
                for key, value in boot_ids.items()
                if isinstance(key, str) and isinstance(value, str)
            }
        events = data.get("events")
        if not isinstance(events, dict):
            return
        now = time.time()
        for key, items in events.items():
            if not isinstance(key, str) or not isinstance(items, list):
                continue
            kept = [
                item
                for item in items
                if isinstance(item, dict) and isinstance(item.get("ts"), (int, float))
            ]
            if kept:
                self._events[key] = kept
                self._prune_locked(key, now=now)

    def _prune_locked(self, device_key: str, *, now: float) -> None:
        items = self._events.get(device_key)
        if not items:
            return
        cutoff = now - self._max_age_s
        if any(float(item.get("ts", 0.0)) < cutoff for item in items):
            items = [item for item in items if float(item.get("ts", 0.0)) >= cutoff]
        if len(items) > self._max_per_device:
            items = items[-self._max_per_device :]
        self._events[device_key] = items
