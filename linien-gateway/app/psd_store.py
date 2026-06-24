from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from typing import Any, Deque


class PsdStore:
    """Thread-safe fan-out of PSD measurements to /api/psd/stream subscribers.

    Mirrors LogStore: producers (the device poll threads, via session
    `_on_psd_data` -> the gateway `_emit_psd`) call `emit()` from worker
    threads; subscribers are asyncio queues drained by the WebSocket handler
    on the event loop. Partial measurements are broadcast live but only
    *complete* ones are retained in the bounded history used to seed a
    newly-opened tab via `tail()`.
    """

    def __init__(
        self,
        *,
        max_entries: int = 500,
        max_age_s: float = 24.0 * 60.0 * 60.0,
    ) -> None:
        self._max_entries = max(1, int(max_entries))
        self._max_age_s = max(60.0, float(max_age_s))
        # Completed measurements only, most-recent last.
        self._entries: Deque[dict[str, Any]] = deque()
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def emit(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Broadcast one PSD measurement (partial or complete) to subscribers.

        `entry` must already carry `device_key`. Returns the entry unchanged.
        """
        timestamp = float(entry.get("time") or time.time())
        with self._lock:
            if entry.get("complete"):
                # Dedupe by uuid so a completed run replaces its last partial
                # snapshot in the history rather than accumulating duplicates.
                uuid = entry.get("uuid")
                if uuid is not None:
                    self._entries = deque(
                        item for item in self._entries if item.get("uuid") != uuid
                    )
                self._entries.append(entry)
                self._prune_locked(now=timestamp)
            subscribers = list(self._subscribers)
            loop = self._loop

        if loop is not None and subscribers:
            future = asyncio.run_coroutine_threadsafe(
                self._broadcast({"type": "psd", "entry": entry}, subscribers),
                loop,
            )
            future.add_done_callback(lambda _fut: None)
        return entry

    def tail(self, *, limit: int = 200) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), self._max_entries))
        with self._lock:
            self._prune_locked(now=time.time())
            if safe_limit >= len(self._entries):
                return [dict(item) for item in self._entries]
            items = list(self._entries)[-safe_limit:]
            return [dict(item) for item in items]

    def clear(self) -> int:
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            return count

    def subscribe(self, *, maxsize: int = 300) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=max(10, int(maxsize)))
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    async def _broadcast(
        self, payload: dict[str, Any], subscribers: list[asyncio.Queue]
    ) -> None:
        for q in subscribers:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # Drop the oldest queued message to make room — a slow consumer
                # shouldn't wedge the producer.
                try:
                    _ = q.get_nowait()
                except asyncio.QueueEmpty:
                    continue
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    continue

    def _prune_locked(self, *, now: float) -> None:
        while len(self._entries) > self._max_entries:
            self._entries.popleft()
        cutoff = now - self._max_age_s
        while self._entries and float(self._entries[0].get("time", 0.0) or 0.0) < cutoff:
            self._entries.popleft()
