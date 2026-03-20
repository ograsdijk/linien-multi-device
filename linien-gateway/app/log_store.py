from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections import deque
from typing import Any, Deque


class LogStore:
    def __init__(
        self,
        *,
        max_entries: int = 10_000,
        max_age_s: float = 24.0 * 60.0 * 60.0,
    ) -> None:
        self._max_entries = max(1, int(max_entries))
        self._max_age_s = max(60.0, float(max_age_s))
        self._entries: Deque[dict[str, Any]] = deque()
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def emit(
        self,
        *,
        level: int,
        source: str,
        message: str,
        device_key: str | None = None,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> dict[str, Any]:
        timestamp = float(ts) if ts is not None else time.time()
        levelno = self._normalize_level(level)
        entry = {
            "id": uuid.uuid4().hex,
            "ts": timestamp,
            "level": levelno,
            "level_name": logging.getLevelName(levelno).lower(),
            "device_key": device_key,
            "source": source,
            "code": code,
            "message": message,
            "details": details or {},
        }

        with self._lock:
            self._entries.append(entry)
            self._prune_locked(now=timestamp)
            subscribers = list(self._subscribers)
            loop = self._loop

        if loop is not None and subscribers:
            future = asyncio.run_coroutine_threadsafe(
                self._broadcast({"type": "log", "entry": entry}, subscribers),
                loop,
            )
            future.add_done_callback(lambda _fut: None)
        return entry

    def tail(self, *, limit: int = 500) -> list[dict[str, Any]]:
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
        while self._entries and float(self._entries[0].get("ts", 0.0)) < cutoff:
            self._entries.popleft()

    @staticmethod
    def _normalize_level(level: int) -> int:
        if level >= logging.CRITICAL:
            return logging.CRITICAL
        if level >= logging.ERROR:
            return logging.ERROR
        if level >= logging.WARNING:
            return logging.WARNING
        if level >= logging.INFO:
            return logging.INFO
        return logging.DEBUG
