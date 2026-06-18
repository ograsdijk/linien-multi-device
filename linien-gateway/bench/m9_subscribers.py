"""Open N WebSocket subscribers to bench-NN devices and drain frames.

Drives the gateway's _on_to_plot publish path so we can measure
per-section timings under realistic 12-card load.
"""
from __future__ import annotations

import argparse
import asyncio

import websockets


async def subscribe(device_key: str, base_ws: str, max_fps: float, detail: str) -> None:
    url = f"{base_ws}/api/devices/{device_key}/stream?max_fps={max_fps}&detail={detail}"
    bytes_received = 0
    frames = 0
    async with websockets.connect(url, max_size=10_000_000) as ws:
        try:
            while True:
                msg = await ws.recv()
                bytes_received += len(msg) if isinstance(msg, (bytes, bytearray)) else len(msg.encode())
                frames += 1
        except websockets.ConnectionClosed:
            pass
    print(f"  {device_key}: {frames} frames, {bytes_received/1024:.1f} KB")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-ws", default="ws://127.0.0.1:8000")
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument("--max-fps", type=float, default=10)
    parser.add_argument("--detail", choices=["summary", "full"], default="summary")
    parser.add_argument("--duration-s", type=float, default=20.0)
    args = parser.parse_args()

    tasks = [
        asyncio.create_task(
            subscribe(f"bench-{i:02d}", args.base_ws, args.max_fps, args.detail)
        )
        for i in range(args.count)
    ]
    print(f"subscribing to {args.count} streams at {args.max_fps} fps ({args.detail})")
    await asyncio.sleep(args.duration_s)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    print(f"done after {args.duration_s}s")


if __name__ == "__main__":
    asyncio.run(main())
