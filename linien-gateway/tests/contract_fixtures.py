"""Real gateway responses for the orchestrator's contract tests.

Everything here comes from the production code paths: a real ``DeviceSession``
and the real FastAPI routes (through ``TestClient``), with only the hardware
boundary replaced -- a small simulated board that renders a PDH error trace for
whatever sweep geometry the session last wrote, shifting the feature by the
signed piezo-hysteresis model. The detectors, planner, IdentityGuard, hysteresis
window and JSON serialisation are all the real ones.

The committed files under ``tests/data/contract/`` are the output of
``generate()``; ``test_contract_fixtures.py`` regenerates them and asserts they
are identical, so they cannot go stale. To refresh them after an intended
change (from ``linien-gateway/``)::

    PYTHONPATH=$PWD python tests/contract_fixtures.py

Values that differ run to run (the run token, timestamps) are replaced by fixed
placeholders after the fact; nothing else is edited.
"""

from __future__ import annotations

import json
import re
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient

import app.main as main
from app.manual_lock_record import MOD_HZ_UNIT
from app.plot_processing import V
from app.session import DeviceSession

CONTRACT_DIR = Path(__file__).with_name("data") / "contract"

KEY = "contract-dev"
H = 0.085  # the model's own coefficient: the simulated board obeys it exactly
N_POINTS = 512
START_CENTER_V = 0.0
START_AMPLITUDE_V = 1.0
# Where the tracked carrier sits at the start geometry, and the serrodyne-order
# copy of it one "order" (45 mV) above.
CARRIER_V = 0.30
DECOY_DV = 0.170
FEATURE_HALF_WIDTH_V = 0.02
SIDEBAND_V = 0.100
MOD_HZ_RAW = 16 * MOD_HZ_UNIT  # 16 MHz

# Floats are kept to this many significant digits: enough to be exact for every
# consumer, and stable against last-bit differences between platforms.
_SIG_DIGITS = 10

# Placeholders for the values that change every run.
_TOKEN = "TOKEN"
_TIME_KEYS = {"expires_at": 0.0, "acquired_at": 0.0}


class _Manager:
    def publish(self, device_key: str, message: dict[str, Any]) -> None:
        pass


class _Param:
    def __init__(self, value: Any) -> None:
        self.value = value


class SimBoard:
    """Renders the error trace of a decaying-shifted PDH feature pair."""

    def __init__(self, session: DeviceSession) -> None:
        self.session = session
        self.frame_id = 0
        self.recent: list[tuple[int, np.ndarray]] = []
        session.plot_state = SimpleNamespace(recent_unlocked_traces=self.recent)

    def apparent_carrier_v(self) -> float:
        p = self.session.parameters
        lower = float(p.sweep_center.value) - abs(float(p.sweep_amplitude.value))
        start_lower = START_CENTER_V - START_AMPLITUDE_V
        return CARRIER_V - H * (lower - start_lower)

    def trace(self) -> np.ndarray:
        p = self.session.parameters
        c, a = float(p.sweep_center.value), abs(float(p.sweep_amplitude.value))
        v = np.linspace(c - a, c + a, N_POINTS)
        carrier = self.apparent_carrier_v()

        def lobe(v0: float, strength: float) -> np.ndarray:
            u = (v - v0) / FEATURE_HALF_WIDTH_V
            return strength * u / (1.0 + u * u)

        def triplet(v0: float, strength: float) -> np.ndarray:
            return (
                lobe(v0, strength)
                + lobe(v0 - SIDEBAND_V, -0.5 * strength)
                + lobe(v0 + SIDEBAND_V, -0.5 * strength)
            )

        signal = triplet(carrier, 1.0) + triplet(carrier + DECOY_DV, 0.6)
        return signal * 0.3 / np.max(np.abs(signal))

    def snapshot(self) -> tuple[np.ndarray, None, int, float]:
        """`_snapshot_auto_lock_traces_with_frame`: one NEW frame per call."""
        trace = self.trace()
        self.frame_id += 1
        self.recent.append((self.frame_id, trace * V))
        del self.recent[:-8]
        return trace, None, self.frame_id, time.time()


def _make_session() -> tuple[DeviceSession, SimBoard]:
    device = SimpleNamespace(
        key=KEY, name=KEY, host="127.0.0.1", port=18999, parameters={}
    )
    session = DeviceSession(device, _Manager())
    session.control = SimpleNamespace(
        exposed_write_registers=lambda: None, exposed_start_lock=lambda: None
    )
    session.parameters = SimpleNamespace(
        sweep_center=_Param(START_CENTER_V),
        sweep_amplitude=_Param(START_AMPLITUDE_V),
        target_slope_rising=_Param(True),
        modulation_frequency=_Param(MOD_HZ_RAW),
        lock=_Param(False),
    )
    board = SimBoard(session)
    # The calibrated feature width the detectors and planner work from; the
    # rest of the stored settings are the defaults.
    session.auto_lock_scan_settings["half_range_sweep_v"] = FEATURE_HALF_WIDTH_V

    def _write(center_v: float, amplitude_v: float, *, settle_s: float = 0.0) -> float:
        session.parameters.sweep_center.value = float(center_v)
        session.parameters.sweep_amplitude.value = float(amplitude_v)
        return time.time()

    session._snapshot_auto_lock_traces_with_frame = board.snapshot  # type: ignore[method-assign]
    session._set_sweep_geometry = _write  # type: ignore[method-assign]
    session._restore_sweep_geometry = lambda c, a: True  # type: ignore[method-assign]
    session._wait_for_fresh_unlocked_trace = lambda after, timeout: True  # type: ignore[method-assign]
    session._move_and_lock = lambda *a, **k: None  # type: ignore[method-assign]
    return session, board


def _scrub(value: Any) -> Any:
    """Fixed placeholders for the run token and wall-clock stamps; floats
    rounded to ``_SIG_DIGITS`` significant digits."""
    if isinstance(value, dict):
        return {
            k: (_TIME_KEYS[k] if k in _TIME_KEYS and v is not None
                else _TOKEN if k == "token" else _scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, float):
        return float(f"{value:.{_SIG_DIGITS}g}")
    return value


def _url(suffix: str) -> str:
    return f"/api/devices/{KEY}/control/{suffix}"


def generate() -> dict[str, dict[str, Any]]:
    """Every contract fixture, keyed by file stem, from one scripted scenario."""
    session, _board = _make_session()
    out: dict[str, dict[str, Any]] = {}

    async def _acquired(connected, timeout_s, skip_frames=1):
        return {KEY: {"frame_id": 0}}, {}

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(
            main, "_get_device_or_404", lambda key: SimpleNamespace(key=key)))
        stack.enter_context(mock.patch.object(
            main, "_session_for_device", lambda device: session))
        stack.enter_context(mock.patch.object(main, "_trigger_and_acquire", _acquired))
        stack.enter_context(mock.patch.object(main, "_emit_log", lambda **k: None))
        stack.enter_context(mock.patch.object(
            main, "_enqueue_auto_lock_row", lambda *a, **k: None))
        client = TestClient(main.app)
        try:
            _drive(client, session, out)
        finally:
            run = session._staged_autolock
            if run is not None and run.timer is not None:
                run.timer.cancel()
    return {name: _scrub(body) for name, body in out.items()}


def _post(client: TestClient, path: str, body: dict[str, Any] | None = None):
    return client.post(_url(path), json=body if body is not None else {})


def _pick(frame: dict[str, Any], index: int, extra: float | None = None) -> dict[str, Any]:
    selected: dict[str, Any] = {"frame_id": frame["frame_id"], "target_index": index}
    if extra is not None:
        selected["extra_tolerance_v"] = extra
    return {"selected": selected}


def _narrow(client: TestClient, run: str, begun: dict[str, Any], extra: float):
    """Two narrowing steps, each selecting the best candidate (the tracked
    feature). The first has nothing pending; the second is judged against the
    first's hysteresis prediction (with the caller's ``extra`` on top)."""
    frame, candidates = begun["frame"], begun["candidates"]
    steps = []
    for step_extra in (None, extra):
        step = _post(client, f"{run}/step",
                     _pick(frame, candidates[0]["target_index"], step_extra))
        assert step.status_code == 200 and step.json()["planner"]["action"] == "narrow", step.text
        steps.append(step.json())
        frame, candidates = step.json()["frame"], step.json()["candidates"]
    return steps


def _drive(client: TestClient, session: DeviceSession, out: dict[str, Any]) -> None:
    extra = 0.012

    # Read-only candidate detection with the coarse block and the analysed
    # frame's trace, before any run exists.
    response = client.post(
        _url("auto_lock_candidates") + "?include_coarse=true&include_trace=true"
    )
    assert response.status_code == 200, response.text
    out["auto_lock_candidates_include_coarse_and_trace"] = response.json()

    # Run 1: begin, narrow, narrow, a refused slip, done.
    begun = _post(client, "staged_autolock/begin")
    assert begun.status_code == 200, begun.text
    out["staged_begin"] = begun.json()
    run = f"staged_autolock/{begun.json()['token']}"
    out["staged_step_narrow"], out["staged_step_narrow_second"] = _narrow(
        client, run, begun.json(), extra
    )
    frame = out["staged_step_narrow_second"]["frame"]
    candidates = out["staged_step_narrow_second"]["candidates"]

    # The serrodyne-order copy: outside tolerance + extra.
    decoy = next(c for c in candidates if not c["identity_ok"])
    refused = _post(client, f"{run}/step", _pick(frame, decoy["target_index"], extra))
    assert refused.status_code == 422, refused.text
    out["staged_step_refused_422"] = refused.json()

    done = _post(client, f"{run}/step", _pick(frame, candidates[0]["target_index"]))
    assert done.status_code == 200 and done.json()["planner"]["action"] == "done", done.text
    out["staged_step_done"] = done.json()
    assert _post(client, f"{run}/abort").status_code == 200

    # Run 2, from the start geometry again (the simulated board's sweep is put
    # back by hand): the same two narrowing steps, then lock straight away, so
    # the lock's own hysteresis window is reported.
    session.parameters.sweep_center.value = START_CENTER_V
    session.parameters.sweep_amplitude.value = START_AMPLITUDE_V
    begun = _post(client, "staged_autolock/begin")
    assert begun.status_code == 200, begun.text
    run = f"staged_autolock/{begun.json()['token']}"
    _, second = _narrow(client, run, begun.json(), extra)
    lock = _post(client, f"{run}/lock", _pick(
        second["frame"], second["candidates"][0]["target_index"], extra))
    assert lock.status_code == 200, lock.text
    out["staged_lock"] = lock.json()


def write(directory: Path = CONTRACT_DIR) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, body in generate().items():
        path = directory / f"{name}.json"
        path.write_text(render(body))
        written.append(path)
    return written


_NUMBER_LIST = re.compile(r"\[\s*(-?[\d.eE+-]+(?:,\s*-?[\d.eE+-]+)*)\s*\]")


def render(body: Any) -> str:
    """The committed text form of one fixture: indented JSON with the numeric
    arrays (the trace) kept on one line each."""
    text = json.dumps(body, indent=1, sort_keys=True)
    text = _NUMBER_LIST.sub(
        lambda m: "[" + re.sub(r",\s+", ", ", m.group(1)) + "]", text
    )
    return text + "\n"


if __name__ == "__main__":
    for written_path in write():
        print(written_path)
