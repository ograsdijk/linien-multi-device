from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import websockets


@dataclass
class ClientStats:
    messages: int = 0
    plot_frames: int = 0
    statuses: int = 0
    param_updates: int = 0


async def _run_client(url: str, duration_s: float) -> ClientStats:
    stats = ClientStats()
    deadline = time.monotonic() + duration_s
    async with websockets.connect(url, max_size=10 * 1024 * 1024) as ws:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            payload: dict[str, Any]
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            stats.messages += 1
            msg_type = payload.get("type")
            if msg_type == "plot_frame":
                stats.plot_frames += 1
            elif msg_type == "status":
                stats.statuses += 1
            elif msg_type == "param_update":
                stats.param_updates += 1
    return stats


def _build_url(base_ws_url: str, device_key: str, max_fps: float | None) -> str:
    suffix = ""
    if max_fps is not None and max_fps > 0:
        suffix = "?" + urlencode({"max_fps": str(max_fps)})
    return f"{base_ws_url.rstrip('/')}/api/devices/{device_key}/stream{suffix}"


async def _main(args: argparse.Namespace) -> None:
    url = _build_url(args.base_ws_url, args.device_key, args.max_fps)
    clients = [
        asyncio.create_task(_run_client(url, args.duration_s))
        for _ in range(args.clients)
    ]
    results = await asyncio.gather(*clients)
    total = ClientStats()
    for item in results:
        total.messages += item.messages
        total.plot_frames += item.plot_frames
        total.statuses += item.statuses
        total.param_updates += item.param_updates

    print("Stream load probe summary")
    print(f"clients={args.clients} duration_s={args.duration_s:.1f} url={url}")
    print(f"total_messages={total.messages}")
    print(f"total_plot_frames={total.plot_frames}")
    print(f"total_statuses={total.statuses}")
    print(f"total_param_updates={total.param_updates}")
    per_client_plot_fps = (
        (total.plot_frames / max(args.clients, 1)) / max(args.duration_s, 0.001)
    )
    print(f"avg_plot_fps_per_client={per_client_plot_fps:.2f}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open many websocket clients and report received message rates."
    )
    parser.add_argument(
        "--base-ws-url",
        default="ws://127.0.0.1:8000",
        help="Gateway base websocket URL (default: ws://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--device-key",
        required=True,
        help="Device key to subscribe to.",
    )
    parser.add_argument(
        "--clients",
        type=int,
        default=10,
        help="Number of concurrent websocket clients (default: 10)",
    )
    parser.add_argument(
        "--duration-s",
        type=float,
        default=15.0,
        help="Probe duration in seconds (default: 15)",
    )
    parser.add_argument(
        "--max-fps",
        type=float,
        default=None,
        help="Optional max_fps query param sent by each client.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(_main(parse_args()))
