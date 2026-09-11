"""A host-side stand-in for the Red Pitaya `rp-telemetry` daemon.

Lets the gateway's telemetry polling, caching, staleness handling, and UI be
exercised without a physical board. It speaks the same line protocol as
`rp-telemetry/src/rp_telemetry.c`:

    STATUS\\n   ->  RPT1 57.34\\n     (or RPT1 ERR XADC\\n with --fail)
    VERSION\\n  ->  RPT1 VERSION 1.0.0\\n
    other      ->  RPT1 ERR COMMAND\\n

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
import socket
import socketserver
import time

PROTOCOL_ID = "RPT1"
VERSION = "1.0.0"
MAX_REQUEST = 64
CLIENT_TIMEOUT_S = 2.0


def simulated_temperature(base_c: float, swing_c: float, period_s: float) -> float:
    """A slow sinusoid around `base_c`, so the UI shows a value that moves."""
    if period_s <= 0:
        return base_c
    phase = (time.time() % period_s) / period_s
    return base_c + swing_c * math.sin(2 * math.pi * phase)


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
                response = f"{PROTOCOL_ID} {temperature:.2f}\n"
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
