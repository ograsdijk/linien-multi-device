"""A host-side stand-in for the Red Pitaya `rp-telemetry` daemon.

Lets the gateway's telemetry polling, caching, staleness handling, and UI be
exercised without a physical board. It speaks the same line protocol as
`rp-telemetry/src/rp_telemetry.c`:

    STATUS\\n   ->  RPT1 57.34 cpu=3.2 load1=0.41 memtotal=509216 ...\\n
                   (or RPT1 ERR XADC\\n with --fail)
    VERSION\\n  ->  RPT1 VERSION 1.3.0\\n
    other      ->  RPT1 ERR COMMAND\\n

The host-metric tail is simulated too, so the gateway's parsing, the InfluxDB
fields and the UI can be exercised without a board. `--no-metrics` suppresses
it, which is how a board still running the 1.1.0 daemon looks.

This is a *development* tool that runs on your workstation. It is deliberately
not what gets deployed: the real board runs the C daemon precisely so no Python
process has to exist there.

    linien-rp-telemetry-sim --port 18864
    linien-rp-telemetry-sim --port 18865 --base 62 --fail

Point a device's host at the machine running this and the gateway will poll it
like any other board. Note the gateway reports `not_installed` for a simulated
board until an install record exists, because it cannot tell a stopped daemon
from an absent one over TCP alone -- that is expected in simulation.
"""

from __future__ import annotations

import argparse
import math
import shutil
import socket
import socketserver
import time

PROTOCOL_ID = "RPT1"
VERSION = "1.4.0"
MAX_REQUEST = 64
STARTED_AT = time.monotonic()
CLIENT_TIMEOUT_S = 2.0


def simulated_temperature(base_c: float, swing_c: float, period_s: float) -> float:
    """A slow sinusoid around `base_c`, so the UI shows a value that moves."""
    if period_s <= 0:
        return base_c
    phase = (time.time() % period_s) / period_s
    return base_c + swing_c * math.sin(2 * math.pi * phase)


def simulated_metrics(options) -> str:
    """The `key=value` tail of a STATUS line, as the C daemon builds it.

    CPU and memory wander with their own periods so the UI's quantized
    change-detection (which deliberately ignores small moves) can be seen doing
    both things: staying quiet, and updating when it should.
    """
    if options.no_metrics:
        return ""
    cpu = max(0.0, min(100.0, options.cpu + 3.0 * math.sin(time.time() / 47.0)))
    load1 = max(0.0, cpu / 100.0 * 2.0)
    total_kb = options.mem_total_kb
    used_fraction = max(
        0.0, min(0.99, options.mem_used / 100.0 + 0.05 * math.sin(time.time() / 91.0))
    )
    avail_kb = int(total_kb * (1.0 - used_fraction))
    uptime = time.monotonic() - STARTED_AT + options.uptime_offset
    # A slow wander of +-6 mV, so the 10 mV push binning can be seen working.
    rail = (
        ""
        if options.vccaux_v <= 0
        else f" vccaux={options.vccaux_v + 0.006 * math.sin(time.time() / 73.0):.3f}"
    )
    try:
        root_free_kb = shutil.disk_usage("/").free // 1024
    except OSError:
        root_free_kb = 0
    return (
        f" cpu={cpu:.1f} load1={load1:.2f} memtotal={total_kb} "
        f"memavail={avail_kb} uptime={uptime:.1f} rootfree={root_free_kb}"
        f"{rail}"
    )


class TelemetryHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(CLIENT_TIMEOUT_S)
        try:
            raw = self.request.recv(MAX_REQUEST)
        except (socket.timeout, OSError):
            return
        if not raw:
            return
        command = raw.decode("ascii", "replace").strip().split("\n")[0].strip()

        options = self.server.options  # type: ignore[attr-defined]
        if command == "STATUS":
            if options.fail:
                response = f"{PROTOCOL_ID} ERR XADC\n"
            else:
                temperature = simulated_temperature(
                    options.base, options.swing, options.period
                )
                response = (
                    f"{PROTOCOL_ID} {temperature:.2f}"
                    f"{simulated_metrics(options)}\n"
                )
        elif command == "VERSION":
            response = f"{PROTOCOL_ID} VERSION {options.version}\n"
        else:
            response = f"{PROTOCOL_ID} ERR COMMAND\n"
        try:
            self.request.sendall(response.encode("ascii"))
        except OSError:
            pass


class TelemetryServer(socketserver.TCPServer):
    allow_reuse_address = True

    def __init__(self, address, handler, options):
        self.options = options
        super().__init__(address, handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Simulate the Red Pitaya rp-telemetry daemon."
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18864)
    parser.add_argument(
        "--base", type=float, default=57.0, help="mean reported temperature in C"
    )
    parser.add_argument(
        "--swing", type=float, default=1.5, help="peak deviation from --base in C"
    )
    parser.add_argument(
        "--period", type=float, default=600.0, help="oscillation period in seconds"
    )
    parser.add_argument(
        "--version",
        default=VERSION,
        help="version to report; set it to something else to exercise the "
        "gateway's update-available handling",
    )
    parser.add_argument(
        "--cpu", type=float, default=6.0, help="mean reported CPU busy percent"
    )
    parser.add_argument(
        "--mem-used", type=float, default=42.0, help="mean memory used, percent"
    )
    parser.add_argument(
        "--mem-total-kb", type=int, default=509216, help="MemTotal to report, in kB"
    )
    parser.add_argument(
        "--uptime-offset",
        type=float,
        default=0.0,
        help="seconds to add to the simulator's own uptime",
    )
    parser.add_argument(
        "--vccaux-v",
        type=float,
        default=1.8,
        help="mean vccaux rail to report; 0 omits vccaux, like a 1.3.0 board",
    )
    parser.add_argument(
        "--no-metrics",
        action="store_true",
        help="omit the host-metric tail, like a board still running 1.1.0",
    )
    parser.add_argument(
        "--fail",
        action="store_true",
        help="always answer 'RPT1 ERR XADC' to exercise the error state",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    options = build_parser().parse_args(argv)
    server = TelemetryServer((options.host, options.port), TelemetryHandler, options)
    print(
        f"rp-telemetry simulator listening on {options.host}:{options.port} "
        f"(version {options.version})"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
