from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict

from fastapi import WebSocket

logger = logging.getLogger(__name__)


@dataclass
class ConnectionState:
    max_fps: float | None = None
    last_plot: float = 0.0


class WebsocketManager:
    def __init__(self) -> None:
        self._connections: Dict[str, Dict[WebSocket, ConnectionState]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def register(
        self, device_key: str, websocket: WebSocket, max_fps: float | None = None
    ) -> None:
        await websocket.accept()
        if max_fps is not None and max_fps <= 0:
            max_fps = None
        self._connections.setdefault(device_key, {})[websocket] = ConnectionState(
            max_fps=max_fps
        )

    async def unregister(self, device_key: str, websocket: WebSocket) -> None:
        if device_key in self._connections:
            self._connections[device_key].pop(websocket, None)
            if not self._connections[device_key]:
                self._connections.pop(device_key, None)

    async def broadcast(self, device_key: str, message: Dict[str, Any]) -> None:
        if device_key not in self._connections:
            return
        stale = []
        now = time.monotonic()
        for websocket, state in list(self._connections[device_key].items()):
            if message.get("type") == "plot_frame" and state.max_fps:
                min_dt = 1.0 / state.max_fps
                if now - state.last_plot < min_dt:
                    continue
                state.last_plot = now
            try:
                await websocket.send_json(message)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            await self.unregister(device_key, websocket)

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
