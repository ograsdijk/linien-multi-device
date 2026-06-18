"""Smoke test: subscribe to one WS in binary mode, validate the format."""
from __future__ import annotations

import asyncio
import json
import struct

import websockets


async def main() -> None:
    url = "ws://127.0.0.1:8000/api/devices/sm/stream?max_fps=5&detail=summary&binary=1"
    async with websockets.connect(url, max_size=10_000_000) as ws:
        # Drain the initial snapshot messages (params + maybe a plot frame + status)
        # then capture the first 5 streamed plot frames.
        frames_seen = 0
        binary_frames = 0
        text_frames = 0
        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                binary_frames += 1
                # Validate format.
                if len(msg) < 8:
                    raise RuntimeError(f"too short: {len(msg)}")
                magic = msg[0:4]
                if magic != b"PLOT":
                    raise RuntimeError(f"bad magic: {magic!r}")
                (header_len,) = struct.unpack(">I", msg[4:8])
                header_bytes = msg[8 : 8 + header_len]
                header = json.loads(header_bytes.decode("utf-8"))
                pad = (4 - ((8 + header_len) % 4)) % 4
                data_offset = 8 + header_len + pad
                n_series = len(header.get("series_names", []))
                n_points = int(header.get("n_points", 0))
                expected_data = n_series * n_points * 4
                actual_data = len(msg) - data_offset
                print(
                    f"  binary frame #{binary_frames}: total={len(msg)} bytes, "
                    f"header_len={header_len}, "
                    f"n_series={n_series}, n_points={n_points}, "
                    f"data_bytes={actual_data} (expected {expected_data}), "
                    f"lock={header.get('lock')}, "
                    f"series={header.get('series_names')}"
                )
                if actual_data != expected_data:
                    raise RuntimeError("data size mismatch")
                # Decode first few values of the first series to sanity-check.
                if n_series > 0 and n_points > 0:
                    first_arr = msg[data_offset : data_offset + min(16, n_points) * 4]
                    floats = struct.unpack(
                        f"<{len(first_arr) // 4}f", first_arr
                    )
                    print(
                        f"    first series ({header['series_names'][0]}) "
                        f"first {len(floats)} values: {floats[:4]}"
                    )
                frames_seen += 1
            else:
                text_frames += 1
                preview = msg[:120]
                print(f"  text msg #{text_frames}: {preview}")
            if frames_seen >= 5:
                break
        print(f"done. binary={binary_frames}, text={text_frames}")


if __name__ == "__main__":
    asyncio.run(main())
