"""Compare parallel vs staggered 'Connect all' strategy.

Drives 12 bench-NN devices that already exist in the gateway.
For each strategy, disconnects all first, then issues the connects,
then polls /statuses until all 12 report connected. Reports total
time + error rate per strategy.
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import httpx


async def disconnect_all(client: httpx.AsyncClient, count: int) -> None:
    await asyncio.gather(
        *(client.post(f"/api/devices/bench-{i:02d}/disconnect") for i in range(count)),
        return_exceptions=True,
    )
    # Wait for them to actually be disconnected.
    for _ in range(60):
        r = await client.get("/api/devices/statuses")
        statuses = r.json()
        if all(
            not statuses.get(f"bench-{i:02d}", {}).get("connected", False)
            and not statuses.get(f"bench-{i:02d}", {}).get("connecting", False)
            for i in range(count)
        ):
            return
        await asyncio.sleep(0.25)


async def wait_until_all_connected(
    client: httpx.AsyncClient, count: int, timeout_s: float = 30.0
) -> tuple[float, int, list[str]]:
    """Return (elapsed, errors_seen, last_per_device_errors)."""
    start = time.perf_counter()
    deadline = start + timeout_s
    while time.perf_counter() < deadline:
        r = await client.get("/api/devices/statuses")
        statuses = r.json()
        connected = sum(
            1
            for i in range(count)
            if statuses.get(f"bench-{i:02d}", {}).get("connected", False)
        )
        if connected == count:
            errs = [
                statuses[f"bench-{i:02d}"].get("last_error") or ""
                for i in range(count)
            ]
            return time.perf_counter() - start, sum(1 for e in errs if e), errs
        await asyncio.sleep(0.1)
    r = await client.get("/api/devices/statuses")
    statuses = r.json()
    errs = [statuses.get(f"bench-{i:02d}", {}).get("last_error") or "" for i in range(count)]
    return time.perf_counter() - start, sum(1 for e in errs if e), errs


async def parallel_connect(client: httpx.AsyncClient, count: int) -> tuple[float, int]:
    await disconnect_all(client, count)
    await asyncio.gather(
        *(client.post(f"/api/devices/bench-{i:02d}/connect") for i in range(count)),
        return_exceptions=True,
    )
    elapsed, err_count, _ = await wait_until_all_connected(client, count)
    return elapsed, err_count


async def staggered_connect(
    client: httpx.AsyncClient, count: int, stagger_ms: float
) -> tuple[float, int]:
    await disconnect_all(client, count)
    delay = stagger_ms / 1000.0

    async def kick(i: int) -> None:
        await asyncio.sleep(i * delay)
        await client.post(f"/api/devices/bench-{i:02d}/connect")

    await asyncio.gather(*(kick(i) for i in range(count)), return_exceptions=True)
    elapsed, err_count, _ = await wait_until_all_connected(client, count)
    return elapsed, err_count


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument("--trials", type=int, default=3)
    args = parser.parse_args()

    async with httpx.AsyncClient(base_url=args.base_url, timeout=60.0) as client:
        # Make sure they exist + initial all-connected baseline.
        r = await client.get("/api/devices")
        devices = r.json()
        present = {d["key"] for d in devices}
        for i in range(args.count):
            if f"bench-{i:02d}" not in present:
                print(f"missing device bench-{i:02d} - run setup_devices.py first")
                return

        configs: list[tuple[str, callable]] = [
            ("parallel (current behavior)", lambda: parallel_connect(client, args.count)),
            ("staggered 50ms", lambda: staggered_connect(client, args.count, 50)),
            ("staggered 100ms", lambda: staggered_connect(client, args.count, 100)),
            ("staggered 200ms", lambda: staggered_connect(client, args.count, 200)),
        ]

        for name, fn in configs:
            samples: list[float] = []
            errors: list[int] = []
            print(f"\n=== {name} ===")
            for trial in range(args.trials):
                elapsed, err_count = await fn()
                samples.append(elapsed)
                errors.append(err_count)
                print(f"  trial {trial + 1}: {elapsed * 1000:.0f} ms, {err_count} errors")
            print(
                f"  mean: {statistics.fmean(samples) * 1000:.0f} ms, "
                f"min: {min(samples) * 1000:.0f}, max: {max(samples) * 1000:.0f}, "
                f"errors total: {sum(errors)}/{args.trials * args.count}"
            )


if __name__ == "__main__":
    asyncio.run(main())
