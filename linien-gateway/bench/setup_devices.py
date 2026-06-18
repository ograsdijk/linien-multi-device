"""One-shot device configurator for the /statuses bench.

Configures N devices in the gateway pointing at N sims on
127.0.0.1:base_port .. base_port+N-1, then connects them all and
waits until they're all reporting connected.
"""
from __future__ import annotations

import argparse
import time

import httpx


def configure_and_connect(base_url: str, count: int, base_port: int) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        existing = client.get("/api/devices").json()
        existing_keys = {d["key"] for d in existing}
        for i in range(count):
            key = f"bench-{i:02d}"
            if key in existing_keys:
                continue
            payload = {
                "key": key,
                "name": key,
                "host": "127.0.0.1",
                "port": base_port + i,
                "username": "root",
                "password": "root",
                "parameters": {},
            }
            r = client.post("/api/devices", json=payload)
            r.raise_for_status()
        # Issue connect for every bench device.
        for i in range(count):
            key = f"bench-{i:02d}"
            r = client.post(f"/api/devices/{key}/connect")
            r.raise_for_status()
        # Wait for them to be connected.
        deadline = time.time() + 30.0
        while time.time() < deadline:
            statuses = client.get("/api/devices/statuses").json()
            connected = sum(
                1
                for i in range(count)
                if statuses.get(f"bench-{i:02d}", {}).get("connected")
            )
            if connected == count:
                print(f"all {count} devices connected")
                return
            time.sleep(0.5)
        statuses = client.get("/api/devices/statuses").json()
        for i in range(count):
            k = f"bench-{i:02d}"
            print(f"  {k}: {statuses.get(k)}")
        raise RuntimeError(f"only {connected}/{count} devices connected within 30s")


def cleanup_devices(base_url: str, count: int) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        for i in range(count):
            key = f"bench-{i:02d}"
            try:
                client.delete(f"/api/devices/{key}")
            except Exception:
                pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument("--base-port", type=int, default=18863)
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    if args.cleanup:
        cleanup_devices(args.base_url, args.count)
    else:
        configure_and_connect(args.base_url, args.count, args.base_port)


if __name__ == "__main__":
    main()
