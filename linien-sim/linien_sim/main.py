from __future__ import annotations

import argparse
import shlex
import threading
from typing import Sequence

from rpyc.utils.server import ThreadedServer

from .service import VirtualLinienControlService

HELP_TEXT = """
Commands:
  help
  status
  lock
  sweep
  noise <electronics_sigma_v>
  drift <v_per_s>
  walk <sigma_v_per_sqrt_s>
  jitter <sigma_v>
  step <delta_v>
  kick <delta_v>
  ramp <delta_v> <seconds>
  monitor <reflection|transmission>
  phase <deg> [a|b|active]
  modfreq <hz>
  modamp <vpp>
  linewidthhz <hz>
  linewidthv <v>
  fsrhz <hz>
  pid <p> <i> <d>
  seed <int>
  exit
""".strip()


def _print_status(service: VirtualLinienControlService) -> None:
    status = service.cli_status()
    lock_text = "locked" if status.lock else "sweep"
    print(
        f"mode={lock_text} detuning={status.laser_detuning_v:+.4f} V "
        f"disturbance={status.disturbance_offset_v:+.4f} V "
        f"effective={status.effective_detuning_v:+.4f} V "
        f"control={status.control_output_v:+.4f} V"
    )
    print(
        f"noise(electronics)={status.noise_sigma_v:.5f} V "
        f"drift={status.drift_v_per_s:+.5f} V/s "
        f"walk={status.walk_sigma_v_sqrt_s:.5f} V/sqrt(s) "
        f"jitter={status.detuning_jitter_v:.5f} V monitor={status.monitor_mode}"
    )
    print(
        f"modulation={status.modulation_hz / 1_000_000:.4f} MHz "
        f"amp={status.modulation_amp_vpp:.4f} Vpp"
    )
    print(
        f"linewidth={status.linewidth_hz / 1_000_000:.4f} MHz "
        f"({status.linewidth_v:.5f} V)"
    )
    print(f"fsr={status.fsr_hz / 1_000_000:.4f} MHz")
    print(f"scan={status.scan_hz_per_v / 1_000_000:.4f} MHz/V")


def _run_repl(service: VirtualLinienControlService) -> None:
    print(HELP_TEXT)
    while True:
        try:
            line = input("linien-sim> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            print(f"Parse error: {exc}")
            continue
        if not parts:
            continue
        cmd = parts[0].lower()

        try:
            if cmd in {"exit", "quit"}:
                break
            if cmd == "help":
                print(HELP_TEXT)
                continue
            if cmd == "status":
                _print_status(service)
                continue
            if cmd == "lock":
                service.exposed_start_lock()
                print("Started lock mode.")
                continue
            if cmd == "sweep":
                service.exposed_start_sweep()
                print("Started sweep mode.")
                continue
            if cmd == "noise" and len(parts) == 2:
                service.cli_set_noise(float(parts[1]))
                print("Updated noise.")
                continue
            if cmd == "drift" and len(parts) == 2:
                service.cli_set_drift(float(parts[1]))
                print("Updated drift.")
                continue
            if cmd == "walk" and len(parts) == 2:
                service.cli_set_walk(float(parts[1]))
                print("Updated random walk.")
                continue
            if cmd == "jitter" and len(parts) == 2:
                service.cli_set_detuning_jitter(float(parts[1]))
                print("Updated laser/cavity jitter.")
                continue
            if cmd == "step" and len(parts) == 2:
                service.cli_step_disturbance(float(parts[1]))
                print("Applied step disturbance.")
                continue
            if cmd == "kick" and len(parts) == 2:
                service.cli_kick(float(parts[1]))
                print("Applied instantaneous detuning kick.")
                continue
            if cmd == "ramp" and len(parts) == 3:
                service.cli_schedule_ramp(float(parts[1]), float(parts[2]))
                print("Scheduled ramp disturbance.")
                continue
            if cmd == "monitor" and len(parts) == 2:
                service.cli_set_monitor_mode(parts[1])
                print("Updated monitor mode.")
                continue
            if cmd == "phase" and len(parts) in {2, 3}:
                channel = parts[2].lower() if len(parts) == 3 else "active"
                service.cli_set_phase_deg(float(parts[1]), channel=channel)
                print("Updated demodulation phase.")
                continue
            if cmd == "modfreq" and len(parts) == 2:
                service.cli_set_modfreq_hz(float(parts[1]))
                print("Updated modulation frequency.")
                continue
            if cmd == "modamp" and len(parts) == 2:
                service.cli_set_modamp_vpp(float(parts[1]))
                print("Updated modulation amplitude.")
                continue
            if cmd == "linewidthhz" and len(parts) == 2:
                service.cli_set_linewidth_hz(float(parts[1]))
                print("Updated linewidth (Hz).")
                continue
            if cmd == "linewidthv" and len(parts) == 2:
                service.cli_set_linewidth_v(float(parts[1]))
                print("Updated linewidth (V).")
                continue
            if cmd == "fsrhz" and len(parts) == 2:
                service.cli_set_fsr_hz(float(parts[1]))
                print("Updated cavity FSR (Hz).")
                continue
            if cmd == "pid" and len(parts) == 4:
                service.cli_set_pid(float(parts[1]), float(parts[2]), float(parts[3]))
                print("Updated PID gains.")
                continue
            if cmd == "seed" and len(parts) == 2:
                service.cli_set_seed(int(parts[1]))
                print("Updated RNG seed.")
                continue
            print("Unknown command. Type 'help' for supported commands.")
        except Exception as exc:  # noqa: BLE001 - interactive command loop
            print(f"Command failed: {exc}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Virtual Linien PDH simulator")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18863)
    parser.add_argument("--username", default="root")
    parser.add_argument("--password", default="root")
    parser.add_argument("--no-auth", action="store_true")
    parser.add_argument("--frame-rate", type=float, default=20.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--ui",
        choices=("repl", "tui"),
        default="repl",
        help="Interactive interface mode.",
    )
    parser.add_argument("--linewidth-hz", type=float, default=None)
    parser.add_argument("--linewidth-v", type=float, default=None)
    parser.add_argument("--fsr-hz", type=float, default=None)
    parser.add_argument("--jitter-v", type=float, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    service = VirtualLinienControlService(
        username=args.username,
        password=args.password,
        no_auth=args.no_auth,
        frame_rate_hz=args.frame_rate,
        seed=args.seed,
        linewidth_hz=args.linewidth_hz,
        linewidth_v=args.linewidth_v,
        fsr_hz=args.fsr_hz,
        jitter_v=args.jitter_v,
    )
    service.start()

    server = ThreadedServer(
        service,
        hostname=args.host,
        port=args.port,
        authenticator=service.make_authenticator(),
        protocol_config={"allow_pickle": True, "allow_public_attrs": True},
    )

    server_thread = threading.Thread(
        target=server.start,
        daemon=True,
        name="linien-sim-rpyc",
    )
    server_thread.start()

    print(
        f"Virtual Linien device listening on {args.host}:{args.port} "
        f"(username='{args.username}', no_auth={bool(args.no_auth)})"
    )
    print("Add this as a regular device in linien-web and click Connect.")

    try:
        if args.ui == "tui":
            try:
                from .tui import run_tui
            except Exception as exc:  # noqa: BLE001 - runtime import fallback
                print(f"Failed to start TUI ({exc}). Falling back to REPL.")
                _run_repl(service)
            else:
                run_tui(service)
        else:
            _run_repl(service)
    finally:
        service.stop()
        server.close()
        server_thread.join(timeout=2.0)


if __name__ == "__main__":
    main()
