from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Union

import numpy as np
import orjson
from fastapi import WebSocket

from .plot_processing import SUMMARY_SERIES_KEYS

logger = logging.getLogger(__name__)


def _encode_ws_payload(payload: Dict[str, Any]) -> str:
    """Encode a non-plot payload as a JSON string (status, params, etc).

    Plot frames use a binary path (encode_plot_frame_binary) when the
    subscriber opted into it. JSON path remains for plot frames when
    the subscriber didn't opt in.

    Uses orjson with OPT_SERIALIZE_NUMPY so numpy arrays serialize
    directly without an intermediate `tolist()`.
    """
    try:
        return orjson.dumps(payload, option=orjson.OPT_SERIALIZE_NUMPY).decode("utf-8")
    except TypeError:
        logger.warning(
            "orjson encode rejected payload, falling back to stdlib json. "
            "Output may contain NaN/Infinity literals which the browser "
            "cannot parse; treat as a data-quality bug.",
            exc_info=True,
        )
        return json.dumps(payload, default=_json_fallback_default)


def _json_fallback_default(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    raise TypeError(
        f"Object of type {type(value).__name__} is not JSON serializable"
    )


# --- Plot frame encoders --------------------------------------------------
#
# Plot frames after #6 hold numpy arrays in their `series` dict. The two
# encoders below diverge from here:
#
#   - encode_plot_frame_json: walks each array, substitutes NaN -> None
#     so the wire JSON is valid (browser JSON.parse rejects NaN literals).
#     Yields a text WS frame. Backwards-compat path for any subscriber
#     that didn't pass `binary=1`.
#
#   - encode_plot_frame_binary: a single bytes payload — small JSON
#     header followed by raw Float32 series bytes. Decodes on the
#     browser via zero-copy Float32Array views. ~5x smaller wire vs
#     JSON, ~300x faster to decode (M2 bench).
#
# Format (binary):
#
#   offset    bytes   description
#   --------- ------- ---------------------------------------------
#   0         4       magic 'PLOT' (0x504C4F54), big-endian
#   4         4       header JSON length (uint32 BE)
#   8         N       header JSON (utf-8) — series_names, n_points, lock_indicator,
#                     auto_relock, lock, dual_channel, signal_power, stats,
#                     lock_target, x_label, x_unit, type
#   8 + N     0..3    zero padding to next 4-byte boundary
#   ...       4*K*P   K = len(series_names), P = n_points,
#                     little-endian Float32 values, series in declared order
#
# Why this shape:
#   - one WS message per frame keeps client correlation simple.
#   - separating the header from the array data lets the client parse
#     the small JSON cheaply (~10 us for 1 KB) and slice typed-array
#     views from the rest with zero copy.
#   - Float32 (not Int16 quantized) keeps full precision for the
#     control signal which can swing across +/-1 V. Quantization would
#     save another 2x wire but adds a per-value lookup on decode.


def _array_to_json_safe(arr: Any) -> Any:
    """Convert a numpy array (or list) to a list with NaN -> None.

    Used only by the JSON fallback path; binary subscribers skip this.
    """
    if isinstance(arr, np.ndarray):
        if arr.dtype.kind == "f":
            # NaN -> None substitution. np.isnan only works on float dtypes.
            mask = np.isnan(arr)
            if not mask.any():
                return arr.tolist()
            out: list[Any] = arr.tolist()
            for i in np.flatnonzero(mask):
                out[int(i)] = None
            return out
        return arr.tolist()
    if isinstance(arr, list):
        return arr
    return arr


def encode_plot_frame_json(frame: Dict[str, Any]) -> str:
    """JSON-encode a plot frame, converting numpy series to NaN-safe lists."""
    series = frame.get("series")
    if isinstance(series, dict):
        converted = {key: _array_to_json_safe(value) for key, value in series.items()}
        json_frame = {**frame, "series": converted}
    else:
        json_frame = frame
    return _encode_ws_payload(json_frame)


_BINARY_MAGIC = b"PLOT"


def encode_plot_frame_binary(frame: Dict[str, Any]) -> bytes:
    """Binary-encode a plot frame: 8-byte preamble + JSON header + Float32 data."""
    series = frame.get("series") or {}
    if not isinstance(series, dict):
        series = {}
    series_names: list[str] = []
    arrays: list[np.ndarray] = []
    n_points = 0
    for name, value in series.items():
        if isinstance(value, np.ndarray):
            arr = value
        elif isinstance(value, list):
            # Legacy/history path — list-with-Nones; substitute NaN.
            arr = np.array(
                [float("nan") if v is None else float(v) for v in value],
                dtype=np.float32,
            )
        else:
            continue
        # Emit little-endian float32 explicitly: that is the documented wire
        # format and what the client's Float32Array view assumes. On x86/ARM
        # this matches the native dtype, so it is a no-op (copy=False).
        if arr.dtype != np.dtype("<f4"):
            arr = arr.astype("<f4", copy=False)
        if n_points == 0:
            n_points = int(arr.size)
        elif int(arr.size) != n_points:
            # Series with inconsistent length get padded/truncated to
            # the first-seen length so the receiver can compute slices
            # off a single `n_points`.
            if arr.size > n_points:
                arr = arr[:n_points]
            else:
                padded = np.full(n_points, np.nan, dtype=np.float32)
                padded[: arr.size] = arr
                arr = padded
        series_names.append(name)
        arrays.append(arr)

    header_obj = {
        "type": "plot_frame",
        "lock": bool(frame.get("lock")),
        "dual_channel": bool(frame.get("dual_channel")),
        "lock_indicator": frame.get("lock_indicator"),
        "auto_relock": frame.get("auto_relock"),
        "signal_power": frame.get("signal_power"),
        "stats": frame.get("stats"),
        "lock_target": frame.get("lock_target"),
        "x_label": frame.get("x_label"),
        "x_unit": frame.get("x_unit"),
        "series_names": series_names,
        "n_points": n_points,
    }
    header_bytes = orjson.dumps(header_obj)
    header_len = len(header_bytes)
    pad = (4 - ((8 + header_len) % 4)) % 4
    total = 8 + header_len + pad + len(arrays) * n_points * 4
    out = bytearray(total)
    out[0:4] = _BINARY_MAGIC
    struct.pack_into(">I", out, 4, header_len)
    out[8 : 8 + header_len] = header_bytes
    data_offset = 8 + header_len + pad
    if arrays:
        # Concatenate all series into one contiguous Float32 buffer.
        # Each tobytes() call is essentially a memcpy from the numpy
        # buffer; no Python-level per-element work.
        for i, arr in enumerate(arrays):
            slot = data_offset + i * n_points * 4
            out[slot : slot + n_points * 4] = arr.tobytes()
    return bytes(out)


def encode_message_for_connection(
    payload: Dict[str, Any], use_binary: bool
) -> Union[str, bytes]:
    """Pick the right encoder for the connection's protocol preference."""
    if payload.get("type") == "plot_frame":
        if use_binary:
            return encode_plot_frame_binary(payload)
        return encode_plot_frame_json(payload)
    return _encode_ws_payload(payload)


@dataclass
class ConnectionState:
    max_fps: float | None = None
    detail: str = "full"
    # When True, plot frames are sent as binary WebSocket messages
    # using encode_plot_frame_binary. Non-plot messages remain JSON.
    # Set from the `binary=1` query param at handshake time.
    binary: bool = False
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
        binary: bool = False,
        *,
        accept: bool = True,
    ) -> None:
        if accept:
            await websocket.accept()
        state = ConnectionState(
            max_fps=self._resolve_max_fps(max_fps),
            detail=self._normalize_detail(detail),
            binary=bool(binary),
            reliable_queue=asyncio.Queue(maxsize=self._reliable_queue_size),
        )
        state.sender_task = asyncio.create_task(
            self._sender_loop(device_key, websocket, state)
        )
        with self._connections_lock:
            self._connections.setdefault(device_key, {})[websocket] = state

    def update_max_fps(
        self, device_key: str, websocket: WebSocket, max_fps: float | None
    ) -> None:
        """Update the per-connection plot-frame FPS cap in place.

        Called from the per-stream receive loop in response to a
        `set_max_fps` control message from the client. Avoids tearing
        down and recreating the websocket every time the user changes
        the FPS selector — with 12 cards a full reconnect storm is
        noticeably stuttery.
        """
        resolved = self._resolve_max_fps(max_fps)
        with self._connections_lock:
            connections = self._connections.get(device_key)
            if connections is None:
                return
            state = connections.get(websocket)
            if state is None:
                return
            state.max_fps = resolved
            # Reset last_plot so a lower-to-higher transition doesn't
            # leave us starved until the next frame; the immediate frame
            # is allowed through.
            state.last_plot = 0.0

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
        # When a single "full" message is broadcast to many summary
        # subscribers, the filtered (summary) variant is identical for
        # all of them. Build it at most once per broadcast.
        summary_variant: Dict[str, Any] | None = None
        for websocket, state in connections:
            if is_plot_frame:
                if state.max_fps:
                    min_dt = 1.0 / state.max_fps
                    now = time.monotonic()
                    if now - state.last_plot < min_dt:
                        continue
                    state.last_plot = now
                if state.detail == "full":
                    payload = message
                else:
                    if summary_variant is None:
                        summary_variant = self.filter_plot_frame(
                            message, "summary"
                        )
                    payload = summary_variant
                self._enqueue_plot_frame(state, payload)
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
                encoded = encode_message_for_connection(payload, state.binary)
                if isinstance(encoded, bytes):
                    await websocket.send_bytes(encoded)
                else:
                    await websocket.send_text(encoded)
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
