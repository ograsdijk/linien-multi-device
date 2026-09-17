"""Characterize how an auto-lock target moves with sweep center and amplitude.

The script uses only the gateway HTTP API and Python's standard library. For
each requested ``CENTER,AMPLITUDE`` pair it:

1. updates both sweep parameters with one register write;
2. optionally waits for the actuator to settle;
3. triggers and captures a complete fresh scan;
4. runs the read-only auto-lock candidate detector; and
5. saves the full trace and detector response as JSON.

A compact ``summary.csv`` and a run ``manifest.json`` are written alongside
the per-scan JSON files. The original center and amplitude are restored on
exit, including after Ctrl-C or an API error.

Examples (run from ``linien-gateway``):

    uv run python scripts/scan_target_characterization.py \
        --device DEVICE_KEY --approximate-center 0.9

    uv run python scripts/scan_target_characterization.py \
        --gateway http://192.168.1.10:8000 \
        --device DEVICE_KEY --approximate-center 0.9 \
        --repeats 3 --settle-seconds 1.0

    uv run python scripts/scan_target_characterization.py \
        --device DEVICE_KEY --approximate-center 0.9 \
        --scan 0,1 --scan 0.5,0.5 --scan 0.8,0.2

``--experiment hop`` runs a different design instead: randomized width hopping,
for separating laser/cavity drift from actuator hysteresis. The 2026-09-16 run
took each width once, in order, wide to narrow, so its 140.8 mV span mixes width
dependence with a drift that turned out to be oscillatory (119-140 s period,
71-136 mV peak-to-peak) — the two are not separable in that data. Hopping fixes
that with three things:

* every block visits all widths in a fresh random permutation, so width order is
  decorrelated from elapsed time;
* test scans hop straight from width to width, leaving a random approach
  direction at each width, which is what a hysteresis term can be fitted to; and
* fixed-width anchor scans are interleaved every ``--hop-anchor-every`` tests as
  a reference series, dense enough (~8 s) to interpolate the drift onto each
  test scan's timestamp and subtract it.

::

    uv run python scripts/scan_target_characterization.py \
        --device DEVICE_KEY --approximate-center 0.49 \
        --experiment hop --hop-blocks 10

Add ``--hop-sweep-speed`` (with ``--restore-sweep-speed``, since the gateway
cannot read the speed back) to cycle the ramp rate per block: a shift that
tracks sweep speed is dynamic actuator lag, one that does not is static
hysteresis or external frequency drift.

By default the scan widths come from ``DEFAULT_SCAN_WIDTHS_V`` below. Each
window is centered on ``--approximate-center`` where possible, then shifted
without resizing if it would cross a -1 or +1 V rail. For an approximate center
of 0.9 V this includes the 0/1, 0.5/0.5, and 0.8/0.2 center/amplitude pairs
discussed above. Custom ``--scan`` arguments replace the generated list. The
device must already be connected and unlocked. Acquiring a scan restarts its
sweep from the center.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


DEFAULT_SCAN_WIDTHS_V = [2.0, 1.75, 1.5, 1.25, 1.0, 0.8, 0.6, 0.4, 0.3, 0.2]
DEFAULT_DRIFT_WIDTHS_V = [2.0, 1.0, 0.4]
DEFAULT_HOP_WIDTHS_V = [1.5, 1.25, 1.0, 0.8, 0.6, 0.4, 0.3, 0.2]
DEFAULT_HOP_ANCHOR_WIDTH_V = 0.4
SWEEP_MIN_V = -1.0
SWEEP_MAX_V = 1.0


class GatewayError(RuntimeError):
    """A gateway request failed with its useful response detail preserved."""


def _api_base(raw: str) -> str:
    value = raw.strip().rstrip("/")
    if not value:
        raise argparse.ArgumentTypeError("gateway URL cannot be empty")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise argparse.ArgumentTypeError(
            "gateway must be an absolute http(s) URL, e.g. http://localhost:8000"
        )
    return value if value.endswith("/api") else f"{value}/api"


def _scan_pair(raw: str) -> tuple[float, float]:
    try:
        center_text, amplitude_text = raw.split(",", 1)
        center = float(center_text)
        amplitude = float(amplitude_text)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "scan must be CENTER,AMPLITUDE, for example 0.5,0.5"
        ) from exc
    if not math.isfinite(center) or not math.isfinite(amplitude):
        raise argparse.ArgumentTypeError("scan values must be finite")
    if amplitude <= 0:
        raise argparse.ArgumentTypeError("scan amplitude must be greater than zero")
    low = center - amplitude
    high = center + amplitude
    epsilon = 1e-9
    if low < SWEEP_MIN_V - epsilon or high > SWEEP_MAX_V + epsilon:
        raise argparse.ArgumentTypeError(
            f"scan {center:g},{amplitude:g} spans {low:g}..{high:g} V; "
            "the allowed sweep range is -1..+1 V"
        )
    return center, amplitude


def _approximate_center(raw: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("approximate center must be a number") from exc
    if not math.isfinite(value) or not SWEEP_MIN_V <= value <= SWEEP_MAX_V:
        raise argparse.ArgumentTypeError("approximate center must be between -1 and +1 V")
    return value


def _scan_for_width(approximate_center: float, width_v: float) -> tuple[float, float]:
    """Fit a fixed-width scan around the target, shifting at either rail."""
    width = min(SWEEP_MAX_V - SWEEP_MIN_V, abs(float(width_v)))
    low = approximate_center - width / 2.0
    high = approximate_center + width / 2.0
    if high > SWEEP_MAX_V:
        low -= high - SWEEP_MAX_V
        high = SWEEP_MAX_V
    if low < SWEEP_MIN_V:
        high += SWEEP_MIN_V - low
        low = SWEEP_MIN_V
    center = (low + high) / 2.0
    amplitude = (high - low) / 2.0
    return center, amplitude


def _width(raw: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("width must be a number") from exc
    if not math.isfinite(value) or not 0.0 < value <= SWEEP_MAX_V - SWEEP_MIN_V:
        raise argparse.ArgumentTypeError(
            f"width must be greater than 0 and at most {SWEEP_MAX_V - SWEEP_MIN_V:g} V"
        )
    return value


def _sweep_speed(raw: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("sweep speed must be an integer") from exc
    if not 0 <= value <= 15:
        raise argparse.ArgumentTypeError("sweep speed must be between 0 and 15")
    return value


def _hop_plan(
    *,
    approximate_center: float,
    widths: list[float],
    blocks: int,
    anchor_width: float | None,
    anchor_every: int,
    sweep_speeds: list[int | None],
    rng: "random.Random",
) -> list[dict[str, Any]]:
    """Build the randomized width-hopping schedule.

    Each block visits every width once, in a fresh random permutation, so that
    width order is decorrelated from elapsed time: the ~2 minute drift seen in
    the 2026-09-16 run cannot masquerade as width dependence across blocks.

    Consecutive test scans hop straight from one width to the next, which is
    what leaves an approach direction to measure — an anchor before every test
    would fix the history at ``anchor_width -> width`` and hide exactly the
    path dependence this run is after. Anchors are therefore interleaved every
    ``anchor_every`` test scans instead, as a fixed-width reference series that
    samples the drift densely enough (roughly every 8 s) to interpolate onto
    each test scan's timestamp and subtract.

    Sweep speed, when more than one is requested, is drawn per block rather
    than per scan: it changes the ramp rate, and a per-scan draw would confound
    dynamic actuator lag with the width hop that precedes it.

    One trap for the analysis, which a simulation of this schedule confirms:
    randomizing the order does *not* decorrelate width from approach direction.
    In a random permutation a wide scan is more often reached by a step up and a
    narrow one by a step down, so regressing target voltage on width alone
    charges the backlash to the width term. On a 10 mV backlash that bias was
    +19 mV/V and did not shrink with more blocks. Fitting width and approach
    direction (``width_step_v``) together brings it to +4 mV/V and recovers the
    backlash to well under a millivolt, so fit both.
    """
    plan: list[dict[str, Any]] = []
    since_anchor = 0
    for block in range(1, blocks + 1):
        order = list(widths)
        rng.shuffle(order)
        speed = sweep_speeds[(block - 1) % len(sweep_speeds)]
        for width in order:
            if anchor_width is not None and since_anchor >= anchor_every:
                center, amplitude = _scan_for_width(approximate_center, anchor_width)
                plan.append(
                    {
                        "block": block,
                        "is_anchor": True,
                        "width_v": anchor_width,
                        "center": center,
                        "amplitude": amplitude,
                        "sweep_speed": speed,
                    }
                )
                since_anchor = 0
            center, amplitude = _scan_for_width(approximate_center, width)
            plan.append(
                {
                    "block": block,
                    "is_anchor": False,
                    "width_v": width,
                    "center": center,
                    "amplitude": amplitude,
                    "sweep_speed": speed,
                }
            )
            since_anchor += 1
    for order_number, step in enumerate(plan, start=1):
        step["order"] = order_number
    return plan


def _request(
    api_base: str,
    method: str,
    path: str,
    payload: Any = None,
    *,
    timeout_s: float,
) -> Any:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(
        f"{api_base}{path}", data=data, headers=headers, method=method
    )
    try:
        with urlopen(request, timeout=timeout_s) as response:
            body = response.read()
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            decoded = json.loads(body)
            detail = decoded.get("detail", decoded)
        except json.JSONDecodeError:
            detail = body or exc.reason
        raise GatewayError(f"{method} {path}: HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise GatewayError(f"{method} {path}: {exc.reason}") from exc
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise GatewayError(f"{method} {path}: response was not JSON") from exc


def _set_sweep(
    api_base: str,
    device_path: str,
    center: float,
    amplitude: float,
    timeout_s: float,
) -> None:
    # Defer the register write on center so the board never observes a
    # half-updated center/amplitude pair.
    _request(
        api_base,
        "PATCH",
        f"{device_path}/params/sweep_center",
        {"value": center, "write_registers": False},
        timeout_s=timeout_s,
    )
    _request(
        api_base,
        "PATCH",
        f"{device_path}/params/sweep_amplitude",
        {"value": amplitude, "write_registers": True},
        timeout_s=timeout_s,
    )


def _set_sweep_speed(
    api_base: str,
    device_path: str,
    speed: int,
    timeout_s: float,
) -> None:
    # The register write is what actually loads the new ramp rate.
    _request(
        api_base,
        "PATCH",
        f"{device_path}/params/sweep_speed",
        {"value": int(speed), "write_registers": True},
        timeout_s=timeout_s,
    )


def _write_json(path: Path, value: Any, *, pretty: bool) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(
            value,
            handle,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
            allow_nan=False,
        )
        handle.write("\n")


def _candidate_summary(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"found": False, "reason": "invalid detector response"}
    candidate = result.get("candidate")
    if not result.get("found") or not isinstance(candidate, dict):
        return {
            "found": False,
            "reason": result.get("reason"),
        }
    return {
        "found": True,
        "reason": None,
        "target_voltage": candidate.get("target_voltage"),
        "target_index": candidate.get("target_index"),
        "score": candidate.get("score"),
        "target_slope_rising": candidate.get("target_slope_rising"),
        "pair_excursion": candidate.get("pair_excursion"),
        "symmetry": candidate.get("symmetry"),
        "sideband_offset_v": candidate.get("sideband_offset_v"),
        "hz_per_v": candidate.get("hz_per_v"),
        "detail": candidate.get("detail"),
    }


def _capture_measurement(
    *,
    api_base: str,
    device_path: str,
    output_dir: Path,
    run_number: int,
    phase: str,
    center: float,
    amplitude: float,
    timeout_s: float,
    repeat: int | None = None,
    point: int | None = None,
    hold: int | None = None,
    sample: int | None = None,
    elapsed_s: float | None = None,
    block: int | None = None,
    order: int | None = None,
    width_v: float | None = None,
    is_anchor: bool | None = None,
    sweep_speed: int | None = None,
    previous_center_v: float | None = None,
    previous_amplitude_v: float | None = None,
) -> dict[str, Any]:
    trace = _request(
        api_base,
        "POST",
        f"{device_path}/control/acquire_scan",
        timeout_s=timeout_s,
    )
    detection = _request(
        api_base,
        "POST",
        f"{device_path}/control/auto_lock_candidates",
        timeout_s=timeout_s,
    )
    summary = _candidate_summary(detection)
    trace_dict = trace if isinstance(trace, dict) else {}
    actual_center = trace_dict.get("sweep_center")
    actual_amplitude = trace_dict.get("sweep_amplitude")
    target_voltage = summary.get("target_voltage")
    target_minus_center = None
    target_fraction = None
    if isinstance(target_voltage, (int, float)):
        target_minus_center = float(target_voltage) - center
        target_fraction = (
            (float(target_voltage) - (center - amplitude)) / (2.0 * amplitude)
        )
    record = {
        "run": run_number,
        "phase": phase,
        "repeat": repeat,
        "point": point,
        "hold": hold,
        "sample": sample,
        "elapsed_s": elapsed_s,
        "block": block,
        "order": order,
        "width_v": width_v,
        "is_anchor": is_anchor,
        "sweep_speed": sweep_speed,
        # The physical predecessor, anchors included: hysteresis is a property
        # of the move that was actually made, not of the test sequence.
        "previous_center_v": previous_center_v,
        "previous_amplitude_v": previous_amplitude_v,
        "previous_width_v": (
            None
            if previous_amplitude_v is None
            else 2.0 * float(previous_amplitude_v)
        ),
        "width_step_v": (
            None
            if previous_amplitude_v is None
            else 2.0 * (amplitude - float(previous_amplitude_v))
        ),
        "center_step_v": (
            None if previous_center_v is None else center - float(previous_center_v)
        ),
        "requested_center_v": center,
        "requested_amplitude_v": amplitude,
        "scan_min_v": center - amplitude,
        "scan_max_v": center + amplitude,
        "actual_center_v": actual_center,
        "actual_amplitude_v": actual_amplitude,
        "trace_timestamp": trace_dict.get("timestamp"),
        "trace_points": trace_dict.get("n_points"),
        "target_minus_center_v": target_minus_center,
        "target_fraction_of_scan": target_fraction,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        **summary,
    }
    phase_label = {"range_sweep": "range", "drift_hold": "drift", "hop": "hop"}[phase]
    if phase == "range_sweep":
        position_label = f"r{repeat:02d}-p{point:02d}"
    elif phase == "drift_hold":
        position_label = f"h{hold:02d}-s{sample:03d}"
    else:
        kind = "anc" if is_anchor else "tst"
        position_label = f"b{block:02d}-o{order:03d}-{kind}"
    filename = (
        f"{phase_label}-{run_number:03d}-{position_label}"
        f"-c{center:+.4f}-a{amplitude:.4f}.json"
    )
    _write_json(
        output_dir / filename,
        {"metadata": record, "trace": trace, "detection": detection},
        pretty=False,
    )
    if summary.get("found"):
        print(
            f"  target={summary['target_voltage']:.6g} V "
            f"index={summary['target_index']} score={summary['score']:.4g}"
        )
    else:
        print(f"  no target: {summary.get('reason')}")
    return record


def _default_output_dir() -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return Path(f"scan-characterization-{stamp}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Step sweep ranges, detect the auto-lock target, and save full traces."
    )
    parser.add_argument(
        "--gateway",
        type=_api_base,
        default=_api_base("http://localhost:8000"),
        help="gateway base URL, with or without /api (default: %(default)s)",
    )
    parser.add_argument("--device", required=True, help="device key from the gateway UI/API")
    parser.add_argument(
        "--approximate-center",
        required=True,
        type=_approximate_center,
        metavar="VOLTS",
        help="approximate target/crossing voltage used to place generated scan windows",
    )
    parser.add_argument(
        "--scan",
        action="append",
        type=_scan_pair,
        metavar="CENTER,AMPLITUDE",
        help=(
            "absolute center,amplitude pair; repeat to override the generated "
            "windows based on --approximate-center"
        ),
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="number of times to run the complete scan list (default: %(default)s)",
    )
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=0.5,
        help="wait after changing the range before capture (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=45.0,
        help="timeout for each HTTP request (default: %(default)s)",
    )
    parser.add_argument(
        "--drift-scan",
        action="append",
        type=_scan_pair,
        metavar="CENTER,AMPLITUDE",
        help=(
            "absolute center,amplitude pair to hold during drift sampling; repeat "
            "to override the generated drift windows"
        ),
    )
    parser.add_argument(
        "--drift-duration-seconds",
        type=float,
        default=300.0,
        help="time to sample each held range; 0 disables drift phase (default: %(default)s)",
    )
    parser.add_argument(
        "--drift-interval-seconds",
        type=float,
        default=10.0,
        help="target start-to-start interval between drift scans (default: %(default)s)",
    )
    parser.add_argument(
        "--experiment",
        choices=("range-drift", "hop", "all"),
        default="range-drift",
        help=(
            "which phases to run: the original sequential range sweep plus drift "
            "holds, the randomized width-hopping run, or both (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--hop-width",
        action="append",
        type=_width,
        metavar="VOLTS",
        dest="hop_widths",
        help=(
            "full scan width to include in the hopping set; repeat to override "
            f"the default set ({', '.join(f'{w:g}' for w in DEFAULT_HOP_WIDTHS_V)})"
        ),
    )
    parser.add_argument(
        "--hop-blocks",
        type=int,
        default=10,
        help=(
            "number of randomized passes over the width set; each pass is a "
            "fresh permutation (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--hop-anchor-width",
        type=float,
        default=DEFAULT_HOP_ANCHOR_WIDTH_V,
        metavar="VOLTS",
        help=(
            "full scan width of the fixed reference scans interleaved through "
            "the hopping run; 0 disables anchoring (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--hop-anchor-every",
        type=int,
        default=2,
        help=(
            "insert an anchor scan after this many test scans (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--hop-seed",
        type=int,
        default=None,
        help="seed for the width permutations; recorded in the manifest either way",
    )
    parser.add_argument(
        "--hop-sweep-speed",
        action="append",
        type=_sweep_speed,
        metavar="SPEED",
        dest="hop_sweep_speeds",
        help=(
            "Linien sweep_speed (0-15) to cycle per block, to separate dynamic "
            "actuator lag from static hysteresis; repeat for more than one. "
            "Requires --restore-sweep-speed. Default: leave the speed untouched"
        ),
    )
    parser.add_argument(
        "--restore-sweep-speed",
        type=_sweep_speed,
        default=None,
        metavar="SPEED",
        help=(
            "the device's current sweep_speed, restored on exit. The gateway "
            "cannot read it back, so it must be supplied when --hop-sweep-speed "
            "is used"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="output directory (default: timestamped directory in the current directory)",
    )
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    if args.settle_seconds < 0:
        parser.error("--settle-seconds cannot be negative")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be greater than zero")
    if args.drift_duration_seconds < 0:
        parser.error("--drift-duration-seconds cannot be negative")
    if args.drift_interval_seconds <= 0:
        parser.error("--drift-interval-seconds must be greater than zero")
    if args.hop_blocks < 1:
        parser.error("--hop-blocks must be at least 1")
    if args.hop_anchor_every < 1:
        parser.error("--hop-anchor-every must be at least 1")
    if args.hop_anchor_width < 0:
        parser.error("--hop-anchor-width cannot be negative")
    if args.hop_anchor_width and not (
        0.0 < args.hop_anchor_width <= SWEEP_MAX_V - SWEEP_MIN_V
    ):
        parser.error(
            f"--hop-anchor-width must be at most {SWEEP_MAX_V - SWEEP_MIN_V:g} V"
        )
    if args.hop_sweep_speeds and args.restore_sweep_speed is None:
        parser.error("--hop-sweep-speed requires --restore-sweep-speed")
    return args


def main() -> int:
    args = _parse_args()
    scans: list[tuple[float, float]] = args.scan or [
        _scan_for_width(args.approximate_center, width)
        for width in DEFAULT_SCAN_WIDTHS_V
    ]
    drift_scans: list[tuple[float, float]] = args.drift_scan or [
        _scan_for_width(args.approximate_center, width)
        for width in DEFAULT_DRIFT_WIDTHS_V
    ]
    run_range_drift = args.experiment in ("range-drift", "all")
    run_hop = args.experiment in ("hop", "all")

    hop_seed = args.hop_seed if args.hop_seed is not None else random.randrange(2**32)
    hop_speeds: list[int | None] = list(args.hop_sweep_speeds or [None])
    hop_plan: list[dict[str, Any]] = (
        _hop_plan(
            approximate_center=args.approximate_center,
            widths=args.hop_widths or list(DEFAULT_HOP_WIDTHS_V),
            blocks=args.hop_blocks,
            anchor_width=args.hop_anchor_width or None,
            anchor_every=args.hop_anchor_every,
            sweep_speeds=hop_speeds,
            rng=random.Random(hop_seed),
        )
        if run_hop
        else []
    )

    output_dir = args.output or _default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=False)

    device_path = f"/devices/{quote(args.device, safe='')}"
    started_at = datetime.now(timezone.utc).isoformat()
    initial_trace: dict[str, Any] | None = None
    records: list[dict[str, Any]] = []
    restored = False

    exit_code = 1
    print(f"Saving results to {output_dir.resolve()}")
    try:
        status = _request(
            args.gateway,
            "GET",
            f"{device_path}/status",
            timeout_s=args.timeout_seconds,
        )
        if not isinstance(status, dict) or not status.get("connected"):
            raise GatewayError("device is not connected")
        if status.get("lock"):
            raise GatewayError("device is locked; stop the lock before running this script")

        # This gives us authoritative values to restore without relying on the
        # websocket-only parameter snapshot.
        initial_trace = _request(
            args.gateway,
            "POST",
            f"{device_path}/control/acquire_scan",
            timeout_s=args.timeout_seconds,
        )
        if not isinstance(initial_trace, dict):
            raise GatewayError("initial scan response was not an object")
        initial_center = float(initial_trace["sweep_center"])
        initial_amplitude = float(initial_trace["sweep_amplitude"])
        _write_json(output_dir / "initial_trace.json", initial_trace, pretty=False)
        print(
            f"Original sweep: center={initial_center:.6g} V, "
            f"amplitude={initial_amplitude:.6g} V"
        )

        run_number = 0
        for repeat in range(1, args.repeats + 1) if run_range_drift else ():
            for point_number, (center, amplitude) in enumerate(scans, start=1):
                run_number += 1
                print(
                    f"[{run_number}/{args.repeats * len(scans)}] "
                    f"repeat={repeat} center={center:g} V amplitude={amplitude:g} V",
                    flush=True,
                )
                _set_sweep(
                    args.gateway,
                    device_path,
                    center,
                    amplitude,
                    args.timeout_seconds,
                )
                if args.settle_seconds:
                    time.sleep(args.settle_seconds)

                records.append(
                    _capture_measurement(
                        api_base=args.gateway,
                        device_path=device_path,
                        output_dir=output_dir,
                        run_number=run_number,
                        phase="range_sweep",
                        center=center,
                        amplitude=amplitude,
                        timeout_s=args.timeout_seconds,
                        repeat=repeat,
                        point=point_number,
                    )
                )

        if run_range_drift and args.drift_duration_seconds > 0:
            print("Starting drift holds.")
            for hold_number, (center, amplitude) in enumerate(drift_scans, start=1):
                print(
                    f"[hold {hold_number}/{len(drift_scans)}] "
                    f"center={center:g} V amplitude={amplitude:g} V for "
                    f"{args.drift_duration_seconds:g} s",
                    flush=True,
                )
                _set_sweep(
                    args.gateway,
                    device_path,
                    center,
                    amplitude,
                    args.timeout_seconds,
                )
                if args.settle_seconds:
                    time.sleep(args.settle_seconds)
                hold_started = time.monotonic()
                sample_number = 1
                while True:
                    due_at = hold_started + (sample_number - 1) * args.drift_interval_seconds
                    if sample_number > 1 and due_at > hold_started + args.drift_duration_seconds:
                        break
                    wait_s = due_at - time.monotonic()
                    if wait_s > 0:
                        time.sleep(wait_s)
                    elapsed_s = time.monotonic() - hold_started
                    run_number += 1
                    print(
                        f"  sample={sample_number} elapsed={elapsed_s:.1f} s",
                        flush=True,
                    )
                    records.append(
                        _capture_measurement(
                            api_base=args.gateway,
                            device_path=device_path,
                            output_dir=output_dir,
                            run_number=run_number,
                            phase="drift_hold",
                            center=center,
                            amplitude=amplitude,
                            timeout_s=args.timeout_seconds,
                            hold=hold_number,
                            sample=sample_number,
                            elapsed_s=elapsed_s,
                        )
                    )
                    sample_number += 1

        if hop_plan:
            anchor_note = (
                f"anchor {args.hop_anchor_width:g} V every {args.hop_anchor_every}"
                if args.hop_anchor_width
                else "no anchors"
            )
            print(
                f"Starting randomized hopping: {args.hop_blocks} blocks, "
                f"{len(hop_plan)} scans, seed={hop_seed}, {anchor_note}.",
                flush=True,
            )
            previous_center: float | None = (
                records[-1]["requested_center_v"] if records else initial_center
            )
            previous_amplitude: float | None = (
                records[-1]["requested_amplitude_v"] if records else initial_amplitude
            )
            current_speed = args.restore_sweep_speed
            hop_started = time.monotonic()
            for step in hop_plan:
                run_number += 1
                kind = "anchor" if step["is_anchor"] else "test"
                print(
                    f"[hop {step['order']}/{len(hop_plan)}] block={step['block']} "
                    f"{kind} width={step['width_v']:g} V "
                    f"center={step['center']:g} V amplitude={step['amplitude']:g} V",
                    flush=True,
                )
                if step["sweep_speed"] is not None and step["sweep_speed"] != current_speed:
                    _set_sweep_speed(
                        args.gateway,
                        device_path,
                        step["sweep_speed"],
                        args.timeout_seconds,
                    )
                    current_speed = step["sweep_speed"]
                _set_sweep(
                    args.gateway,
                    device_path,
                    step["center"],
                    step["amplitude"],
                    args.timeout_seconds,
                )
                if args.settle_seconds:
                    time.sleep(args.settle_seconds)
                records.append(
                    _capture_measurement(
                        api_base=args.gateway,
                        device_path=device_path,
                        output_dir=output_dir,
                        run_number=run_number,
                        phase="hop",
                        center=step["center"],
                        amplitude=step["amplitude"],
                        timeout_s=args.timeout_seconds,
                        elapsed_s=time.monotonic() - hop_started,
                        block=step["block"],
                        order=step["order"],
                        width_v=step["width_v"],
                        is_anchor=step["is_anchor"],
                        sweep_speed=step["sweep_speed"],
                        previous_center_v=previous_center,
                        previous_amplitude_v=previous_amplitude,
                    )
                )
                previous_center = step["center"]
                previous_amplitude = step["amplitude"]
    except (GatewayError, KeyError, TypeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        exit_code = 1
    except KeyboardInterrupt:
        print("\nInterrupted; restoring the original sweep.", file=sys.stderr)
        exit_code = 130
    else:
        exit_code = 0
    finally:
        speed_restored = None
        if run_hop and args.hop_sweep_speeds and args.restore_sweep_speed is not None:
            try:
                _set_sweep_speed(
                    args.gateway,
                    device_path,
                    args.restore_sweep_speed,
                    args.timeout_seconds,
                )
                speed_restored = True
                print(f"Sweep speed restored to {args.restore_sweep_speed}.")
            except Exception as exc:  # noqa: BLE001 - cleanup must report, not mask
                speed_restored = False
                print(f"WARNING: failed to restore sweep speed: {exc}", file=sys.stderr)
                exit_code = exit_code or 1
        if initial_trace is not None:
            try:
                _set_sweep(
                    args.gateway,
                    device_path,
                    float(initial_trace["sweep_center"]),
                    float(initial_trace["sweep_amplitude"]),
                    args.timeout_seconds,
                )
                restored = True
                print("Original sweep center and amplitude restored.")
            except Exception as exc:  # noqa: BLE001 - cleanup must report, not mask
                print(f"WARNING: failed to restore original sweep: {exc}", file=sys.stderr)
                exit_code = exit_code or 1

        manifest = {
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "gateway": args.gateway,
            "device_key": args.device,
            "approximate_center_v": args.approximate_center,
            "scans": [
                {"center_v": center, "amplitude_v": amplitude}
                for center, amplitude in scans
            ],
            "repeats": args.repeats,
            "settle_seconds": args.settle_seconds,
            "drift_scans": [
                {"center_v": center, "amplitude_v": amplitude}
                for center, amplitude in drift_scans
            ],
            "drift_duration_seconds": args.drift_duration_seconds,
            "drift_interval_seconds": args.drift_interval_seconds,
            "experiment": args.experiment,
            "hop": (
                {
                    "widths_v": args.hop_widths or list(DEFAULT_HOP_WIDTHS_V),
                    "blocks": args.hop_blocks,
                    "anchor_width_v": args.hop_anchor_width or None,
                    "anchor_every": args.hop_anchor_every,
                    "seed": hop_seed,
                    "sweep_speeds": hop_speeds,
                    "restore_sweep_speed": args.restore_sweep_speed,
                    "sweep_speed_restored": speed_restored,
                    "planned_scans": len(hop_plan),
                    "plan": hop_plan,
                }
                if run_hop
                else None
            ),
            "original_sweep": (
                {
                    "center_v": initial_trace.get("sweep_center"),
                    "amplitude_v": initial_trace.get("sweep_amplitude"),
                }
                if initial_trace is not None
                else None
            ),
            "original_sweep_restored": restored,
            "completed_records": len(records),
            "records": records,
        }
        _write_json(output_dir / "manifest.json", manifest, pretty=True)
        with (output_dir / "summary.csv").open(
            "w", newline="", encoding="utf-8"
        ) as handle:
            fieldnames = [
                "run",
                "phase",
                "repeat",
                "point",
                "hold",
                "sample",
                "elapsed_s",
                "block",
                "order",
                "width_v",
                "is_anchor",
                "sweep_speed",
                "previous_center_v",
                "previous_amplitude_v",
                "previous_width_v",
                "width_step_v",
                "center_step_v",
                "requested_center_v",
                "requested_amplitude_v",
                "scan_min_v",
                "scan_max_v",
                "actual_center_v",
                "actual_amplitude_v",
                "trace_timestamp",
                "trace_points",
                "captured_at",
                "found",
                "target_voltage",
                "target_minus_center_v",
                "target_fraction_of_scan",
                "target_index",
                "score",
                "target_slope_rising",
                "pair_excursion",
                "symmetry",
                "sideband_offset_v",
                "hz_per_v",
                "detail",
                "reason",
            ]
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(records)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
