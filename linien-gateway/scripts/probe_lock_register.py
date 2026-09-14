"""Run the gateway's own out-of-band diagnosis against one device, verbosely.

A manual `ssh rp 'python3 -c ...'` can succeed while the gateway still reports
the register as unreadable: the gateway goes through Fabric/paramiko, which
runs a *non-interactive* shell with a different PATH and can fail at the
transport level. This script exercises the exact same code path
(`open_ssh_connection` -> `_LOCK_BIT_CMDS`) and prints each method's raw exit
code, stdout and stderr, so the difference is visible instead of collapsed into
"unreadable".

Usage:
    uv run python scripts/probe_lock_register.py --host 192.168.1.2 \
        [--user root] [--password root] [--port 18862]
"""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

from fabric import Connection

from app import diagnosis
from app.ssh import open_ssh_connection


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password", default="root")
    parser.add_argument(
        "--port",
        type=int,
        default=18862,
        help="linien-server RPyC port, used only for the TCP liveness check",
    )
    args = parser.parse_args()

    device = SimpleNamespace(
        host=args.host, port=args.port, username=args.user, password=args.password
    )

    print(f"== TCP check {args.host}:{args.port}")
    listening = diagnosis._tcp_open(args.host, args.port, diagnosis.TCP_PROBE_TIMEOUT_S)
    print(f"   linien-server listening: {listening}")

    print("== SSH: environment and per-method register read")
    with open_ssh_connection(device, Connection) as conn:
        env = conn.run(
            'echo "shell=$0"; echo "PATH=$PATH"; id; command -v timeout python3 python',
            hide=True,
            warn=True,
            timeout=diagnosis.SSH_COMMAND_TIMEOUT_S,
        )
        print((env.stdout or "").rstrip())
        if env.stderr:
            print(f"   stderr: {env.stderr.strip()}")

        uptime_s, fpga_operating = diagnosis._read_uptime_and_fpga(conn)
        print(f"   uptime_s={uptime_s} fpga_operating={fpga_operating}")

        for name, cmd in diagnosis._LOCK_BIT_CMDS:
            try:
                result = conn.run(
                    cmd, hide=True, warn=True, timeout=diagnosis.SSH_COMMAND_TIMEOUT_S
                )
            except Exception as exc:  # noqa: BLE001 - reporting tool, show everything
                print(f"   {name}: raised {type(exc).__name__}: {exc}")
                continue
            print(
                f"   {name}: exit={result.exited} "
                f"stdout={(result.stdout or '').strip()!r} "
                f"stderr={(result.stderr or '').strip()!r}"
            )

    print("== Full probe_device / classify_diagnosis result")
    # seconds_since_last_connected is unknown here; use 0 so the register read is
    # gated only on uptime and FPGA state, as it would be right after a drop.
    probe = diagnosis.probe_device(device, seconds_since_last_connected=0.0)
    print(f"   {probe}")
    verdict = diagnosis.classify_diagnosis(
        probe, host=args.host, seconds_since_last_connected=0.0, probed_at=0.0
    )
    print(json.dumps(verdict, indent=2, default=str))


if __name__ == "__main__":
    main()
