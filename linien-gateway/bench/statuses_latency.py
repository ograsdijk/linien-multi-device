"""Measure /api/devices/statuses latency with N connected devices.

Used to validate the gateway: cut RPyC contention on session hot path
commit. Before that change, status() did 2 RPyC calls per device (so
24 for 12 devices). After, it reads from local caches and does 0 RPyC
calls in steady state.

Usage: run with the gateway already up, with N devices already
configured and connected. The script issues a configurable number of
sequential /statuses requests and reports mean / median / p99 latency.

This does NOT spin up sims itself — that's done by the orchestrator
shell so we can wait for connection settling between launch and
measurement.
"""
from __future__ import annotations

import argparse
import statistics
import time
from typing import List

import httpx


def measure(base_url: str, count: int) -> List[float]:
    """Return list of /statuses request durations in milliseconds."""
    samples: List[float] = []
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        # warmup
        for _ in range(5):
            client.get("/api/devices/statuses")
        for _ in range(count):
            t0 = time.perf_counter()
            resp = client.get("/api/devices/statuses")
            t1 = time.perf_counter()
            resp.raise_for_status()
            samples.append((t1 - t0) * 1000.0)
    return samples


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--label", default="(no label)")
    args = parser.parse_args()

    print(f"[{args.label}] measuring {args.count} /statuses requests against {args.base_url}")
    samples = measure(args.base_url, args.count)
    samples_sorted = sorted(samples)
    p99 = samples_sorted[int(len(samples_sorted) * 0.99)] if samples_sorted else 0.0

    print(f"  count   = {len(samples)}")
    print(f"  mean    = {statistics.fmean(samples):7.2f} ms")
    print(f"  median  = {statistics.median(samples):7.2f} ms")
    print(f"  p99     = {p99:7.2f} ms")
    print(f"  min     = {min(samples):7.2f} ms")
    print(f"  max     = {max(samples):7.2f} ms")
    print(f"  stdev   = {statistics.stdev(samples):7.2f} ms")


if __name__ == "__main__":
    main()
