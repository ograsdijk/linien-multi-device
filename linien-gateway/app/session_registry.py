from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable, Iterator, TypeVar

if TYPE_CHECKING:
    from .session import DeviceSession
else:
    DeviceSession = Any  # pragma: no cover - runtime typing helper

T = TypeVar("T")


class SessionRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, DeviceSession] = {}
        self._key_locks: dict[str, threading.RLock] = {}

    def _get_or_create_key_lock(self, key: str) -> threading.RLock:
        with self._lock:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._key_locks[key] = lock
            return lock

    @contextmanager
    def lock_for(self, key: str) -> Iterator[None]:
        lock = self._get_or_create_key_lock(key)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def get(self, key: str) -> DeviceSession | None:
        with self._lock:
            return self._sessions.get(key)

    def get_or_create(
        self,
        key: str,
        factory: Callable[[], DeviceSession],
    ) -> DeviceSession:
        with self._lock:
            existing = self._sessions.get(key)
            if existing is not None:
                return existing
            created = factory()
            self._sessions[key] = created
            return created

    def remove(self, key: str) -> DeviceSession | None:
        with self._lock:
            removed = self._sessions.pop(key, None)
            # Keep per-key lock allocated to avoid lock replacement races with in-flight users.
            return removed

    def update_device(self, key: str, update: Callable[[DeviceSession], T]) -> T | None:
        with self._lock:
            session = self._sessions.get(key)
        if session is None:
            return None
        return update(session)
