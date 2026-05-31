from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict

from fastapi import WebSocket

from .plot_processing import SUMMARY_SERIES_KEYS

logger = logging.getLogger(__name__)


@dataclass
class ConnectionState:
    max_fps: float | None = None
    detail: str = "full"
    last_plot: float = 0.0
    reliable_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    latest_plot_message: Dict[str, Any] | None = None
    wake_event: asyncio.Event = field(default_factory=asyncio.Event)
    sender_task: asyncio.Task | None = None


class WebsocketManager:
    def __init__(
        self,
        *,
        default_plot_fps: float | None = 60.0,
        max_plot_fps_cap: float | None = 60.0,
        drop_old_plot_frames: bool = True,
        reliable_queue_size: int = 256,
    ) -> None:
        self._connections: Dict[str, Dict[WebSocket, ConnectionState]] = {}
        self._connections_lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._default_plot_fps = self._normalize_positive_fps(default_plot_fps)
        self._max_plot_fps_cap = self._normalize_positive_fps(max_plot_fps_cap)
        if (
            self._default_plot_fps is not None
            and self._max_plot_fps_cap is not None
            and self._default_plot_fps > self._max_plot_fps_cap
        ):
            self._default_plot_fps = self._max_plot_fps_cap
        self._drop_old_plot_frames = bool(drop_old_plot_frames)
        self._reliable_queue_size = max(10, int(reliable_queue_size))

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def register(
        self,
        device_key: str,
        websocket: WebSocket,
        max_fps: float | None = None,
        detail: str = "full",
        *,
        accept: bool = True,
    ) -> None:
        if accept:
            await websocket.accept()
        state = ConnectionState(
            max_fps=self._resolve_max_fps(max_fps),
            detail=self._normalize_detail(detail),
            reliable_queue=asyncio.Queue(maxsize=self._reliable_queue_size),
        )
        state.sender_task = asyncio.create_task(
            self._sender_loop(device_key, websocket, state)
        )
        with self._connections_lock:
            self._connections.setdefault(device_key, {})[websocket] = state

    async def unregister(self, device_key: str, websocket: WebSocket) -> None:
        with self._connections_lock:
            connections = self._connections.get(device_key)
            if connections is None:
                return
            state = connections.pop(websocket, None)
            if not connections:
                self._connections.pop(device_key, None)
        if state is None:
            return
        sender_task = state.sender_task
        state.sender_task = None
        if sender_task is None:
            return
        current_task = asyncio.current_task()
        if sender_task is current_task:
            return
        sender_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sender_task

    async def broadcast(self, device_key: str, message: Dict[str, Any]) -> None:
        with self._connections_lock:
            connections = list(self._connections.get(device_key, {}).items())
        if not connections:
            return
        stale: list[WebSocket] = []
        is_plot_frame = message.get("type") == "plot_frame"
        for websocket, state in connections:
            if is_plot_frame:
                if state.max_fps:
                    min_dt = 1.0 / state.max_fps
                    now = time.monotonic()
                    if now - state.last_plot < min_dt:
                        continue
                    state.last_plot = now
                self._enqueue_plot_frame(state, self.filter_plot_frame(message, state.detail))
                state.wake_event.set()
                continue
            try:
                state.reliable_queue.put_nowait(message)
                state.wake_event.set()
            except asyncio.QueueFull:
                stale.append(websocket)
        for websocket in stale:
            await self.unregister(device_key, websocket)

    def peek_required_detail(self, device_key: str) -> str | None:
        """Return the highest detail level required by any current subscriber.

        Pure read: snapshots the current connection set and returns "full" if
        any subscriber needs full detail, "summary" if only summary subscribers
        exist, or None if there are no subscribers. Does NOT mutate any
        per-connection state — it is a hint used by producers to decide
        whether to publish at all. Per-subscriber fps throttling and detail
        filtering happen in `broadcast`.
        """
        with self._connections_lock:
            connections = list(self._connections.get(device_key, {}).values())
        if not connections:
            return None
        for state in connections:
            if state.detail == "full":
                return "full"
        return "summary"

    def publish(self, device_key: str, message: Dict[str, Any]) -> None:
        if self._loop is None:
            return
        future = asyncio.run_coroutine_threadsafe(
            self.broadcast(device_key, message), self._loop
        )
        future.add_done_callback(
            lambda fut: self._handle_publish_result(device_key, message, fut)
        )

    def _handle_publish_result(
        self,
        device_key: str,
        message: Dict[str, Any],
        future: asyncio.Future,
    ) -> None:
        try:
            future.result()
        except Exception:  # noqa: BLE001 - log and continue, publish must stay best effort
            logger.warning(
                "Websocket publish failed for device=%s type=%s",
                device_key,
                message.get("type"),
                exc_info=True,
            )

    async def _sender_loop(
        self,
        device_key: str,
        websocket: WebSocket,
        state: ConnectionState,
    ) -> None:
        try:
            while True:
                payload = self._dequeue_next(state)
                if payload is None:
                    state.wake_event.clear()
                    payload = self._dequeue_next(state)
                    if payload is None:
                        await state.wake_event.wait()
                        continue
                await websocket.send_json(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.unregister(device_key, websocket)

    def _dequeue_next(self, state: ConnectionState) -> Dict[str, Any] | None:
        try:
            return state.reliable_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        message = state.latest_plot_message
        if message is None:
            return None
        state.latest_plot_message = None
        return message

    def _enqueue_plot_frame(
        self, state: ConnectionState, message: Dict[str, Any]
    ) -> None:
        if self._drop_old_plot_frames:
            state.latest_plot_message = message
            return
        if state.latest_plot_message is None:
            state.latest_plot_message = message

    @staticmethod
    def filter_plot_frame(message: Dict[str, Any], detail: str) -> Dict[str, Any]:
        if detail == "full":
            return message
        series = message.get("series")
        if not isinstance(series, dict):
            return message
        return {
            **message,
            "series": {
                key: value
                for key, value in series.items()
                if key in SUMMARY_SERIES_KEYS
            },
        }

    @staticmethod
    def _normalize_detail(value: str | None) -> str:
        return "summary" if value == "summary" else "full"

    def _resolve_max_fps(self, requested: float | None) -> float | None:
        fps = self._normalize_positive_fps(requested)
        if fps is None:
            fps = self._default_plot_fps
        if fps is None:
            return None
        if self._max_plot_fps_cap is not None:
            return min(fps, self._max_plot_fps_cap)
        return fps

    @staticmethod
    def _normalize_positive_fps(value: float | None) -> float | None:
        if value is None:
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(numeric) or numeric <= 0:
            return None
        return numeric
