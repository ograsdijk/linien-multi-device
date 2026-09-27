"""Characterization/regression tests for ``_trajectory_refine_auto_lock``.

These pin the CURRENT behaviour of the one-shot refinement walk -- the exact
sequence of (center_v, amplitude_v) geometry writes, which detector served
each stage, the final chosen target voltage, the refinement log's stage
kinds, and the abort-path error messages -- against several scenarios,
including a multi-feature trace (a weaker tracked feature coexisting with a
stronger decoy of identical PDH morphology) and an under-resolved start that
needs several narrowing stages before the strict detector accepts.

They are written and committed against the UNMODIFIED
``_trajectory_refine_auto_lock`` / stage-runner-free implementation, and must
keep passing, unchanged, after it is refactored to route each stage through a
shared, selector-parameterised stage runner. Any diff in this file alongside
the refactor commit is itself a regression.

Mocking follows the style already used by
``tests/test_session_lock_refinement.py``'s ``_make_session``/
``_walking_session``: patch the session's ``_capture_auto_lock_target`` /
``_coarse_auto_lock_target`` / ``_set_sweep_geometry`` methods directly (the
exact call sites the refactor is required to keep using), rather than the
lower-level trace detectors, so the test exercises the real narrowing loop,
IdentityGuard, and final-verification logic.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

import app.session as session_module
from app.auto_lock_scan import AutoLockScanResult, AutoLockScanSettings
from app.lock_acceptance import AcceptanceSettings
from app.session import DeviceSession


class _RecordingManager:
    def publish(self, device_key: str, message: dict[str, Any]) -> None:
        pass


class _FakeParam:
    def __init__(self, value: Any) -> None:
        self.value = value


class _FakeParameters:
    def __init__(self, **values: Any) -> None:
        for name, value in values.items():
            setattr(self, name, _FakeParam(value))


class _FakeControl:
    def __init__(self) -> None:
        self.write_count = 0
        self.lock_started = False

    def exposed_write_registers(self) -> None:
        self.write_count += 1

    def exposed_start_lock(self) -> None:
        self.lock_started = True


def _result(
    target_voltage: float,
    *,
    sideband_offset_v: float | None = 0.03,
    slope_rising: bool = True,
    score: float = 0.9,
) -> AutoLockScanResult:
    return AutoLockScanResult(
        target_index=1024,
        target_voltage=float(target_voltage),
        target_slope_rising=slope_rising,
        score=score,
        left_excursion=0.15,
        right_excursion=0.16,
        pair_excursion=0.31,
        symmetry=0.94,
        monitor_level=None,
        hz_per_v=None,
        sideband_offset_v=sideband_offset_v,
    )


def _make_bare_session() -> tuple[DeviceSession, _FakeControl]:
    device = SimpleNamespace(
        key="dev-char", name="dev-char", host="127.0.0.1", port=18863, parameters={}
    )
    session = DeviceSession(device, _RecordingManager())
    session.parameters = _FakeParameters(
        sweep_center=0.0,
        sweep_amplitude=1.0,
        target_slope_rising=True,
        modulation_frequency=0.0,
        lock=False,
    )
    control = _FakeControl()
    session.control = control
    session.auto_lock_scan_settings["half_range_sweep_v"] = 0.02
    return session, control


def _geometry_recorder(session: DeviceSession) -> list[tuple[float, float]]:
    """Records every (center_v, amplitude_v) the walk commands, in order."""
    writes: list[tuple[float, float]] = []

    def _fake_set_sweep_geometry(center_v, amplitude_v, *, settle_s=0.0):
        writes.append((float(center_v), float(amplitude_v)))
        session.parameters.sweep_center.value = float(center_v)
        session.parameters.sweep_amplitude.value = float(amplitude_v)
        return time.time()

    session._set_sweep_geometry = _fake_set_sweep_geometry  # type: ignore[method-assign]
    return writes


def _run_refine(
    session: DeviceSession,
    *,
    start_center_v: float,
    start_amplitude_v: float,
    initial_target: AutoLockScanResult,
    initial_center_v: float,
    initial_amplitude_v: float,
    initial_resolution: float,
    initial_detector: str = "coarse",
    trace_length: int = 2048,
    half_range_sweep_v: float = 0.02,
):
    session.auto_lock_scan_settings["half_range_sweep_v"] = half_range_sweep_v
    settings = AutoLockScanSettings.from_mapping(session.auto_lock_scan_settings)
    acceptance = AcceptanceSettings.from_mapping(session.lock_acceptance_settings)
    return session._trajectory_refine_auto_lock(
        settings,
        acceptance,
        start_center_v,
        start_amplitude_v,
        initial_target=initial_target,
        initial_center_v=initial_center_v,
        initial_amplitude_v=initial_amplitude_v,
        initial_resolution=initial_resolution,
        initial_detector=initial_detector,
        trace_length=trace_length,
    )


# --------------------------------------------------------------------------
# Exact golden comparison. The loose assertions in each scenario document
# intent; the golden file pins EVERYTHING the walk did -- every geometry write,
# the ordered detector calls with the geometry each ran at, the final result
# and the full refinement log -- as recorded from the pre-refactor
# implementation. Regenerate ONLY deliberately, with
# ``REFINEMENT_GOLDEN_CAPTURE=1`` against code whose behaviour is the intended
# reference, and review the diff.
# --------------------------------------------------------------------------


_GOLDEN_PATH = Path(__file__).with_name("data") / "refinement_characterization_golden.json"


def _normalize(value: Any) -> Any:
    if isinstance(value, float):
        return None if not math.isfinite(value) else round(value, 12)
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, dict):
        return {
            str(k): _normalize(v)
            for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
            # Wall-clock values are the only nondeterministic content.
            if not str(k).endswith(("_at", "_ts", "timestamp", "elapsed_s", "duration_s"))
        }
    if isinstance(value, AutoLockScanResult):
        return _normalize(value.to_dict())
    return value


def _recording(session: DeviceSession, name: str, fn, calls: list):
    def _wrapped(*args, **kwargs):
        calls.append(
            [
                name,
                float(session.parameters.sweep_center.value),
                float(session.parameters.sweep_amplitude.value),
            ]
        )
        return fn(*args, **kwargs)

    return _wrapped


def _assert_golden(scenario: str, record: dict[str, Any]) -> None:
    record = _normalize(record)
    if os.environ.get("REFINEMENT_GOLDEN_CAPTURE") == "1":
        golden = json.loads(_GOLDEN_PATH.read_text()) if _GOLDEN_PATH.exists() else {}
        golden[scenario] = record
        _GOLDEN_PATH.parent.mkdir(exist_ok=True)
        _GOLDEN_PATH.write_text(json.dumps(golden, indent=1, sort_keys=True) + chr(10))
        return
    golden = json.loads(_GOLDEN_PATH.read_text())
    assert record == golden[scenario]


# --------------------------------------------------------------------------
# Scenario 1: multi-feature trace -- a weaker tracked feature must be kept
# through narrowing even though a stronger decoy of identical PDH morphology
# (same slope, same sideband spacing) exists elsewhere on the original wide
# scan. The walk narrows around whatever it is CURRENTLY tracking (its own
# last detected voltage), not a global rescan for the best-scoring feature,
# so the decoy's higher score must never divert it.
# --------------------------------------------------------------------------

WEAK_FEATURE_V = 0.180
STRONG_DECOY_V = -0.410
SIDEBAND_V = 0.028


def test_multi_feature_trace_keeps_the_seeded_weaker_target(monkeypatch):
    """Coarse seed picks the weaker feature; a stronger decoy of identical
    morphology exists elsewhere. Pin the exact geometry sequence, the detector
    used per stage, and the final chosen voltage."""
    session, control = _make_bare_session()
    writes = _geometry_recorder(session)

    # Detections converge on the weaker feature (small measurement noise, same
    # slope/sideband every time) -- as if the decoy, present in the raw trace,
    # is never re-examined because the walk only re-detects around its own
    # last target voltage's neighbourhood via the planner's bounded recentre.
    strict_calls = {"n": 0}

    def _strict(settings, traces=None, after=None):
        strict_calls["n"] += 1
        if strict_calls["n"] == 1:
            raise ValueError("scan still too wide for the strict detector")
        return (
            _result(WEAK_FEATURE_V, sideband_offset_v=SIDEBAND_V),
            session.parameters.sweep_center.value,
            session.parameters.sweep_amplitude.value,
            10.5,
        )

    def _coarse(settings, after=None):
        return (
            _result(WEAK_FEATURE_V, sideband_offset_v=SIDEBAND_V),
            session.parameters.sweep_center.value,
            session.parameters.sweep_amplitude.value,
            4.0,
            {"decoy_present_at": STRONG_DECOY_V, "decoy_score": 5.0},
        )

    calls: list = []
    monkeypatch.setattr(
        session, "_capture_auto_lock_target", _recording(session, "strict", _strict, calls)
    )
    monkeypatch.setattr(
        session, "_coarse_auto_lock_target", _recording(session, "coarse", _coarse, calls)
    )
    monkeypatch.setattr(session, "_restore_sweep_geometry", lambda c, a: True)

    result, refinement = _run_refine(
        session,
        start_center_v=0.0,
        start_amplitude_v=1.0,
        initial_target=_result(WEAK_FEATURE_V, sideband_offset_v=SIDEBAND_V),
        initial_center_v=0.0,
        initial_amplitude_v=1.0,
        initial_resolution=2.0,
        initial_detector="coarse",
    )

    # Pinned: the walk never wrote a geometry anywhere near the decoy.
    assert all(abs(c) < 0.9 for c, _a in writes)
    assert result.target_voltage == pytest.approx(WEAK_FEATURE_V)
    stage_kinds = [s.get("kind") for s in refinement["stages"]]
    assert stage_kinds[0] == "initial"
    assert "narrow" in stage_kinds
    assert stage_kinds[-1] == "final_verify"
    assert refinement["attempted"] is True
    assert refinement["trigger"] == "under_resolved"
    assert set(refinement.keys()) >= {
        "attempted", "trigger", "original_center_v", "original_amplitude_v",
        "final_center_v", "final_amplitude_v", "initial_resolution_samples",
        "stages", "restored",
    }
    _assert_golden(
        "multi_feature",
        {"writes": writes, "calls": calls, "result": result, "refinement": refinement},
    )


# --------------------------------------------------------------------------
# Scenario 2: an under-resolved start that needs several narrowing stages
# before the strict detector accepts (coarse -> coarse -> coarse -> strict).
# --------------------------------------------------------------------------

TARGET_V = 0.050


def test_an_under_resolved_start_needs_several_narrowing_stages(monkeypatch):
    session, control = _make_bare_session()
    writes = _geometry_recorder(session)

    strict_calls = {"n": 0}

    def _strict(settings, traces=None, after=None):
        strict_calls["n"] += 1
        # Rejects on the first two narrowing attempts, then accepts.
        if strict_calls["n"] < 3:
            raise ValueError("scan still too wide for the strict detector")
        return (
            _result(TARGET_V, sideband_offset_v=SIDEBAND_V),
            session.parameters.sweep_center.value,
            session.parameters.sweep_amplitude.value,
            12.0,
        )

    coarse_calls = {"n": 0}

    def _coarse(settings, after=None):
        coarse_calls["n"] += 1
        return (
            _result(TARGET_V, sideband_offset_v=SIDEBAND_V),
            session.parameters.sweep_center.value,
            session.parameters.sweep_amplitude.value,
            3.0,
            {"call": coarse_calls["n"]},
        )

    calls: list = []
    monkeypatch.setattr(
        session, "_capture_auto_lock_target", _recording(session, "strict", _strict, calls)
    )
    monkeypatch.setattr(
        session, "_coarse_auto_lock_target", _recording(session, "coarse", _coarse, calls)
    )
    monkeypatch.setattr(session, "_restore_sweep_geometry", lambda c, a: True)

    result, refinement = _run_refine(
        session,
        start_center_v=0.0,
        start_amplitude_v=1.0,
        initial_target=_result(TARGET_V, sideband_offset_v=SIDEBAND_V),
        initial_center_v=0.0,
        initial_amplitude_v=1.0,
        initial_resolution=1.5,
        initial_detector="coarse",
    )

    assert result.target_voltage == pytest.approx(TARGET_V)
    # More than one amplitude write happened -- several real narrowing stages.
    amplitudes = [a for _c, a in writes]
    assert len(amplitudes) >= 2
    assert all(
        amplitudes[i] <= amplitudes[i - 1] + 1e-9 for i in range(1, len(amplitudes))
    ), "amplitude must never widen during the narrowing schedule"
    stage_kinds = [s.get("kind") for s in refinement["stages"]]
    assert stage_kinds.count("narrow") >= 2
    assert stage_kinds[-1] == "final_verify"
    # Two fresh strict detections happened at the final, unchanged geometry.
    assert strict_calls["n"] >= 3
    _assert_golden(
        "several_narrowing_stages",
        {"writes": writes, "calls": calls, "result": result, "refinement": refinement},
    )


# --------------------------------------------------------------------------
# Scenario 3: abort path -- exhausting the narrowing-stage budget. Pins the
# exact error message, the failure_kind, and that geometry was restored.
# --------------------------------------------------------------------------


def test_running_out_of_refinement_stages_reports_the_pinned_message(monkeypatch):
    session, control = _make_bare_session()
    writes = _geometry_recorder(session)

    # Always rejected by strict, and the coarse tracker never converges (score
    # keeps the scan "too wide to lock"), so the walk burns its whole budget.
    def _strict(settings, traces=None, after=None):
        raise ValueError("scan still too wide for the strict detector")

    def _coarse(settings, after=None):
        return (
            _result(0.5, sideband_offset_v=0.001),  # tiny sideband -> stays "too wide"
            session.parameters.sweep_center.value,
            session.parameters.sweep_amplitude.value,
            1.0,
            {},
        )

    restored = {"called_with": None}

    def _restore(c, a):
        restored["called_with"] = (c, a)
        return True

    calls: list = []
    monkeypatch.setattr(
        session, "_capture_auto_lock_target", _recording(session, "strict", _strict, calls)
    )
    monkeypatch.setattr(
        session, "_coarse_auto_lock_target", _recording(session, "coarse", _coarse, calls)
    )
    monkeypatch.setattr(session, "_restore_sweep_geometry", _restore)

    with pytest.raises(session_module.TrajectoryRefinementAborted) as excinfo:
        _run_refine(
            session,
            start_center_v=0.0,
            start_amplitude_v=1.0,
            initial_target=_result(0.5, sideband_offset_v=0.001),
            initial_center_v=0.0,
            initial_amplitude_v=1.0,
            initial_resolution=0.5,
            initial_detector="coarse",
        )

    diagnostics = excinfo.value.refinement
    assert diagnostics["failure"] == (
        "No lockable scan after 16 trajectory refinement stages."
    )
    assert diagnostics["restored"] is True
    assert restored["called_with"] == (0.0, 1.0)
    assert str(excinfo.value).startswith(
        "Trajectory-aware auto-lock refinement failed: "
        "No lockable scan after 16 trajectory refinement stages. "
        "(scan geometry restored)."
    )
    _assert_golden(
        "stage_budget_exhausted",
        {
            "writes": writes,
            "calls": calls,
            "message": str(excinfo.value),
            "refinement": diagnostics,
        },
    )


# --------------------------------------------------------------------------
# Scenario 4: the apparent feature position depends on the scan geometry
# (scan-history/hysteresis offset), so every stage re-detects it somewhere
# new and the planner's bounded recentring is exercised on each step.
# --------------------------------------------------------------------------


def test_geometry_dependent_feature_position_is_tracked_stage_by_stage(monkeypatch):
    session, control = _make_bare_session()
    writes = _geometry_recorder(session)

    def _apparent_v() -> float:
        amp = float(session.parameters.sweep_amplitude.value)
        center = float(session.parameters.sweep_center.value)
        # The feature appears shifted by a history-dependent offset.
        return 0.12 + 0.035 * amp - 0.1 * (center - 0.12)

    strict_calls = {"n": 0}

    def _strict(settings, traces=None, after=None):
        strict_calls["n"] += 1
        if float(session.parameters.sweep_amplitude.value) > 0.3:
            raise ValueError("scan still too wide for the strict detector")
        return (
            _result(_apparent_v(), sideband_offset_v=SIDEBAND_V),
            session.parameters.sweep_center.value,
            session.parameters.sweep_amplitude.value,
            11.0,
        )

    def _coarse(settings, after=None):
        return (
            _result(_apparent_v(), sideband_offset_v=SIDEBAND_V),
            session.parameters.sweep_center.value,
            session.parameters.sweep_amplitude.value,
            3.0,
            {},
        )

    calls: list = []
    monkeypatch.setattr(
        session, "_capture_auto_lock_target", _recording(session, "strict", _strict, calls)
    )
    monkeypatch.setattr(
        session, "_coarse_auto_lock_target", _recording(session, "coarse", _coarse, calls)
    )
    monkeypatch.setattr(session, "_restore_sweep_geometry", lambda c, a: True)

    outcome: dict[str, Any] = {}
    try:
        result, refinement = _run_refine(
            session,
            start_center_v=0.0,
            start_amplitude_v=1.0,
            initial_target=_result(0.155, sideband_offset_v=SIDEBAND_V),
            initial_center_v=0.0,
            initial_amplitude_v=1.0,
            initial_resolution=1.5,
            initial_detector="coarse",
        )
        outcome = {"result": result, "refinement": refinement}
    except session_module.TrajectoryRefinementAborted as exc:
        outcome = {"message": str(exc), "refinement": exc.refinement}
    assert len(writes) >= 2
    _assert_golden(
        "geometry_dependent_position",
        {"writes": writes, "calls": calls, **outcome},
    )
