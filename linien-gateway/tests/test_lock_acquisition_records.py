"""One lock-acquisition record per attempt, holding the trace it was decided on.

Each test drives a real DeviceSession through one acquisition path (manual
lock, one-shot auto-lock with refinement, staged lock, auto-relock) against a
fake board and plot stream, and inspects the row handed to the PostgreSQL
writer. The detectors are stubbed; the capture, pairing and recording code is
the production code.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.main as main
import app.session as session_module
from app.auto_lock_scan import AutoLockScanResult
from app.lock_acquisition import LockAcquisition
from app.manual_lock_postgres import prepare_row
from app.manual_lock_record import ADC_SCALE, build_acquisition_row
from app.plot_processing import PlotState, build_plot_frame
from app.session import DeviceSession

N = 2048


# ------------------------------------------------------------------ fakes


class _Manager:
    def publish(self, *_args, **_kwargs) -> None:
        return None


class _Param:
    def __init__(self, value: Any) -> None:
        self.value = value


class _Parameters:
    def __init__(self, **values: Any) -> None:
        for name, value in values.items():
            setattr(self, name, _Param(value))


class _Postgres:
    """Captures rows the way the writer would receive them."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def enqueue_lock_result(self, row: dict[str, Any]) -> bool:
        # Everything the writer gets must be serializable for its JSON columns.
        json.dumps(prepare_row(row)["trace_meta"])
        self.rows.append(row)
        return True

    def get_state(self) -> dict[str, Any]:
        return {"config": {"enabled": True}, "status": {"active": True}}


class _Stream:
    """A plot stream whose frames keep arriving (so fresh-frame waits pass)
    and whose current frame the test controls."""

    def __init__(self) -> None:
        self.last_plot_data: list[Any] | None = None
        self.last_monitor_signal: np.ndarray | None = None
        self.last_unlocked_frame_id = 0
        self.flowing = True
        self._stamp = time.time()
        # Set by _session(): frames record the sweep geometry they arrive at,
        # as DeviceSession._on_to_plot does.
        self.session: DeviceSession | None = None

    @property
    def last_unlocked_trace_at(self) -> float:
        return time.time() if self.flowing else self._stamp

    def show(self, error_counts: np.ndarray, monitor: np.ndarray | None = None) -> int:
        self.last_monitor_signal = monitor
        second = monitor if monitor is not None else error_counts
        self.last_plot_data = [error_counts, second, error_counts]
        self.last_unlocked_frame_id += 1
        self._stamp = time.time()
        if self.session is not None and self.session.parameters is not None:
            params = self.session.parameters
            self.session._unlocked_geometry_by_frame[self.last_unlocked_frame_id] = (
                float(params.sweep_center.value),
                float(params.sweep_amplitude.value),
            )
        return self.last_unlocked_frame_id


class _Control:
    def __init__(self, session: DeviceSession) -> None:
        self.session = session
        self.lock_commands = 0
        self.fail_lock: Exception | None = None

    def exposed_write_registers(self) -> None:
        return None

    def exposed_start_lock(self) -> None:
        if self.fail_lock is not None:
            raise self.fail_lock
        self.lock_commands += 1
        self.session.parameters.lock.value = True
        # What arrives after the lock engages: a locked-operation frame that
        # must never be mistaken for the acquisition sweep.
        stream = self.session.plot_state
        if isinstance(stream, _Stream):
            stream.show(np.full(N, 7.0))


def _session(**param_overrides: Any) -> tuple[DeviceSession, _Postgres]:
    postgres = _Postgres()
    device = SimpleNamespace(
        key="dev-key-1", name="Laser A", host="127.0.0.1", port=1, parameters={}
    )
    session = DeviceSession(device, _Manager(), lock_result_postgres_service=postgres)
    params = {
        "sweep_center": 0.1,
        "sweep_amplitude": 0.5,
        "sweep_pause": False,
        "target_slope_rising": True,
        "modulation_frequency": 0.0,
        "modulation_amplitude": 100,
        "demodulation_phase_a": 12.0,
        "demodulation_phase_b": 0.0,
        "offset_a": 0,
        "offset_b": 0,
        "control_channel": 0,
        "dual_channel": False,
        "channel_mixing": 0,
        "p": 50,
        "i": 100,
        "d": 0,
        "lock": False,
    }
    params.update(param_overrides)
    session.parameters = _Parameters(**params)
    session.control = _Control(session)
    session.plot_state = _Stream()
    session.plot_state.session = session
    return session, postgres


def _result(target_voltage: float, *, sideband_offset_v: float | None = 0.2):
    return AutoLockScanResult(
        target_index=1024,
        target_voltage=float(target_voltage),
        target_slope_rising=True,
        score=0.9,
        left_excursion=0.15,
        right_excursion=0.16,
        pair_excursion=0.31,
        symmetry=0.94,
        monitor_level=None,
        hz_per_v=None,
        sideband_offset_v=sideband_offset_v,
    )


def _ramp(scale: float) -> np.ndarray:
    return np.linspace(-scale, scale, N)


# ------------------------------------------------------------ manual lock


def test_manual_lock_records_the_sweep_copied_before_the_lock_command():
    session, postgres = _session()
    unlocked = _ramp(4000.0)
    frame_id = session.plot_state.show(unlocked, monitor=np.full(N, 900.0))

    acquisition_id = session.start_lock()

    assert session.control.lock_commands == 1
    assert len(postgres.rows) == 1
    row = postgres.rows[0]
    assert row["lock_acquisition_id"] == acquisition_id
    assert row["lock_source"] == "manual_lock"
    # The unlocked sweep, not the locked frame that arrived after the command.
    assert row["trace_y"] == pytest.approx((unlocked / ADC_SCALE).tolist())
    assert row["secondary_trace_y"] == pytest.approx([900.0 / ADC_SCALE] * N)
    assert row["secondary_trace_role"] == "monitor"
    assert row["trace_y_role"] == "combined_error"
    assert row["trace_meta"]["purpose"] == "pre_lock_snapshot"
    assert row["trace_meta"]["frame_id"] == frame_id
    assert row["trace_meta"]["geometry_source"] == "frame_callback"
    assert row["trace_meta"]["n_samples"] == N
    assert row["trace_meta"]["x_axis_source"] == "reconstructed_from_sweep"
    # The geometry of that sweep, and an x axis built from it.
    assert row["sweep_center_v"] == pytest.approx(0.1)
    assert row["sweep_amplitude_v"] == pytest.approx(0.5)
    assert row["trace_x"][0] == pytest.approx(0.1 - 0.5)
    assert row["trace_x"][-1] == pytest.approx(0.1 + 0.5)
    assert row["lock_command_status"] == "accepted"
    assert row["success"] is True
    assert row["lock_command_at"] >= row["attempt_started_at"]
    assert row["failure_stage"] is None


def test_a_rejected_manual_lock_is_recorded_with_its_trace_and_reports_its_id():
    session, postgres = _session()
    session.plot_state.show(_ramp(4000.0))
    session.control.fail_lock = RuntimeError("board refused")

    with pytest.raises(RuntimeError) as excinfo:
        session.start_lock()

    assert len(postgres.rows) == 1
    row = postgres.rows[0]
    assert excinfo.value.lock_acquisition_id == row["lock_acquisition_id"]
    assert row["success"] is False
    assert row["lock_command_status"] == "rejected"
    assert row["failure_stage"] == "lock_command"
    assert row["failure_reason"] == "board refused"
    assert row["trace_y"] is not None


def test_a_stale_cached_sweep_is_not_stored_as_the_acquisition_trace():
    session, postgres = _session()
    session.plot_state.show(_ramp(4000.0))
    session.plot_state.flowing = False
    session.plot_state._stamp = time.time() - 60.0

    session.start_lock()

    row = postgres.rows[0]
    assert row["trace_y"] is None
    assert row["trace_x"] is None
    assert row["secondary_trace_y"] is None
    assert row["trace_unavailable_reason"].startswith("stale_unlocked_trace")
    # The lock itself is unaffected.
    assert row["lock_command_status"] == "accepted"


def test_ids_are_stable_within_an_attempt_and_unique_between_attempts():
    session, postgres = _session()
    events: list[dict[str, Any]] = []
    session._emit_log_event = lambda **kwargs: events.append(kwargs)  # type: ignore[method-assign]

    session.plot_state.show(_ramp(4000.0))
    first = session.start_lock()
    session.parameters.lock.value = False
    session.plot_state.show(_ramp(3000.0))
    second = session.start_lock()

    assert first != second
    # Rows are upserts by ID: the first attempt's row is written again when
    # the second attempt closes its still-open verification -- same ID, so the
    # same row, never a new one.
    ids = [row["lock_acquisition_id"] for row in postgres.rows]
    assert list(dict.fromkeys(ids)) == [first, second]
    latest_first = [row for row in postgres.rows if row["lock_acquisition_id"] == first][-1]
    assert latest_first["verification_status"] == "unavailable"
    assert latest_first["verification_reason"] == "superseded_by_new_attempt"
    enqueued = [e for e in events if e["code"] == "lock_result_postgres_enqueued"]
    assert {e["details"]["lock_acquisition_id"] for e in enqueued} == {first, second}


def test_a_precheck_refusal_is_not_an_acquisition():
    session, postgres = _session()
    session.control = None

    with pytest.raises(RuntimeError, match="not connected"):
        session.start_lock()

    assert postgres.rows == []


# ------------------------------------------------- dual channel labelling


def _real_stream_frame(session: DeviceSession, to_plot: dict[str, Any], **params: Any):
    session.plot_state = PlotState()
    frame = build_plot_frame(
        to_plot,
        {"lock": False, "channel_mixing": 0, "combined_offset": 0, **params},
        session.plot_state,
        detail="summary",
        build_series=False,
    )
    assert frame is not None


def test_dual_channel_second_input_is_recorded_as_an_error_signal_not_a_monitor():
    session, postgres = _session(dual_channel=True)
    # Integer ADC counts, as the board sends them (Linien's mixer shifts bits).
    error_1 = _ramp(3000.0).astype(np.int64)
    error_2 = _ramp(-1500.0).astype(np.int64)
    _real_stream_frame(
        session,
        {"error_signal_1": error_1, "error_signal_2": error_2},
        dual_channel=True,
    )

    session.start_lock()

    row = postgres.rows[0]
    assert row["secondary_trace_role"] == "error_signal_2"
    assert row["secondary_trace_y"] == pytest.approx((error_2 / ADC_SCALE).tolist())
    assert row["trace_meta"]["secondary_role"] == "error_signal_2"
    assert row["trace_meta"]["dual_channel"] is True


def test_single_channel_second_input_is_recorded_as_the_monitor():
    session, postgres = _session()
    monitor = np.full(N, 1200.0)
    _real_stream_frame(
        session,
        {"error_signal_1": _ramp(3000.0), "monitor_signal": monitor},
        dual_channel=False,
    )

    session.start_lock()

    row = postgres.rows[0]
    assert row["secondary_trace_role"] == "monitor"
    assert row["secondary_trace_y"] == pytest.approx((monitor / ADC_SCALE).tolist())


# -------------------------------------------- one-shot auto-lock + refinement

WIDE = (0.0, 1.0)
NARROW = (0.21, 0.08)
TARGET_V = 0.2


def _refining_session(monkeypatch):
    """Strict rejects the wide scan, a (stubbed) refinement walk narrows it,
    and strict accepts the narrowed scan's fresh frame."""
    session, postgres = _session(sweep_center=WIDE[0], sweep_amplitude=WIDE[1])
    wide_trace = _ramp(2000.0)
    narrow_trace = _ramp(6000.0)
    session.plot_state.show(wide_trace)
    seen: dict[str, Any] = {}

    def fake_find(**kwargs):
        if kwargs["sweep_amplitude_v"] == pytest.approx(WIDE[1]):
            raise ValueError("under-resolved at this scan")
        seen["final_trace"] = kwargs["error_trace_v"]
        return _result(TARGET_V)

    monkeypatch.setattr(session_module, "find_auto_lock_target", fake_find)
    monkeypatch.setattr(
        session_module,
        "find_coarse_auto_lock_target",
        lambda **_kw: SimpleNamespace(result=_result(TARGET_V), metrics={}),
    )

    def fake_refine(settings, acceptance, center_v, amplitude_v, **_kwargs):
        session.parameters.sweep_center.value = NARROW[0]
        session.parameters.sweep_amplitude.value = NARROW[1]
        seen["narrow_frame_id"] = session.plot_state.show(narrow_trace)
        result, c, a, _res = session._capture_auto_lock_target(
            settings, after=time.time()
        )
        # A later frame lands before the handover (the walk's last word is
        # the verified detection, not whatever is cached now).
        session.plot_state.show(_ramp(100.0))
        return result, {
            "attempted": True,
            "trigger": "under_resolved",
            "final_center_v": c,
            "final_amplitude_v": a,
            "stages": [],
        }

    monkeypatch.setattr(session, "_trajectory_refine_auto_lock", fake_refine)
    return session, postgres, narrow_trace, seen


def test_auto_lock_after_refinement_records_the_refined_trace_with_its_own_geometry(
    monkeypatch,
):
    session, postgres, narrow_trace, seen = _refining_session(monkeypatch)

    payload = session.auto_lock_from_scan(None)

    assert len(postgres.rows) == 1
    row = postgres.rows[0]
    assert payload["lock_acquisition_id"] == row["lock_acquisition_id"]
    assert row["lock_source"] == "auto_lock_scan"
    # The trace the locked-on detection analysed...
    assert row["trace_y"] == pytest.approx((narrow_trace / ADC_SCALE).tolist())
    assert np.array_equal(seen["final_trace"], narrow_trace / ADC_SCALE)
    assert row["trace_meta"]["frame_id"] == seen["narrow_frame_id"]
    assert row["trace_meta"]["geometry_source"] == "frame_callback"
    assert row["trace_meta"]["purpose"] == "final_detection"
    assert row["trace_meta"]["detector"] == "strict"
    # ...with the narrowed geometry it was measured at: not the wide scan the
    # attempt started from, and not the lock point _move_and_lock wrote into
    # sweep_center afterwards.
    assert session.parameters.sweep_center.value == pytest.approx(TARGET_V)
    assert row["sweep_center_v"] == pytest.approx(NARROW[0])
    assert row["sweep_amplitude_v"] == pytest.approx(NARROW[1])
    assert row["trace_x"][0] == pytest.approx(NARROW[0] - NARROW[1])
    assert row["trace_x"][-1] == pytest.approx(NARROW[0] + NARROW[1])
    assert row["refinement"]["final_amplitude_v"] == pytest.approx(NARROW[1])
    assert row["lock_command_status"] == "accepted"
    # Characterized from the detection it locked on, not a re-detection, and
    # the config snapshot is the one the lock was engaged with.
    assert row["characterization"]["result_source"] == "locked_detection"
    assert row["zero_crossing_sweep_v"] == pytest.approx(TARGET_V)
    assert row["linien_config"]["captured_at_stage"] == "pre_lock_command"
    assert row["linien_config"]["parameters"]["sweep_amplitude"] == pytest.approx(
        NARROW[1]
    )
    assert row["verification_status"] == "pending"


def test_a_direct_auto_lock_records_the_scan_it_detected_on(monkeypatch):
    session, postgres = _session(sweep_center=0.0, sweep_amplitude=0.05)
    trace = _ramp(5000.0)
    frame_id = session.plot_state.show(trace)
    monkeypatch.setattr(
        session_module, "find_auto_lock_target", lambda **_kw: _result(0.01)
    )

    session.auto_lock_from_scan(None)

    row = postgres.rows[0]
    assert row["trace_y"] == pytest.approx((trace / ADC_SCALE).tolist())
    assert row["trace_meta"]["frame_id"] == frame_id
    assert row["sweep_amplitude_v"] == pytest.approx(0.05)
    assert row["refinement"] is None


def test_an_auto_lock_with_no_trace_is_recorded_without_a_waveform():
    session, postgres = _session()
    # Connected and unlocked, but no sweep has arrived yet.

    with pytest.raises(RuntimeError, match="No unlocked trace"):
        session.auto_lock_from_scan(None)

    assert len(postgres.rows) == 1
    row = postgres.rows[0]
    assert row["trace_x"] is None
    assert row["trace_y"] is None
    assert row["secondary_trace_y"] is None
    assert row["trace_unavailable_reason"] == "no_trace_captured"
    assert row["failure_stage"] == "trace_capture"
    assert row["lock_command_status"] == "not_sent"
    assert row["success"] is False


# ------------------------------------------------------------- auto-relock


def test_a_successful_relock_is_recorded_once_under_auto_relock(monkeypatch):
    session, postgres = _session(sweep_center=0.0, sweep_amplitude=0.05)
    session.plot_state.show(_ramp(5000.0))
    monkeypatch.setattr(
        session_module, "find_auto_lock_target", lambda **_kw: _result(0.01)
    )

    session._start_auto_relock()

    assert len(postgres.rows) == 1
    row = postgres.rows[0]
    assert row["lock_source"] == "auto_relock"
    assert row["lock_command_status"] == "accepted"
    status = session.auto_relock.get_status()
    assert status["last_lock_acquisition_id"] == row["lock_acquisition_id"]


def test_a_failed_relock_is_recorded_once_with_the_frame_it_analysed(monkeypatch):
    session, postgres = _session(sweep_center=0.0, sweep_amplitude=0.05)
    trace = _ramp(5000.0)
    session.plot_state.show(trace)

    def reject(**_kw):
        raise ValueError("no crossing")

    monkeypatch.setattr(session_module, "find_auto_lock_target", reject)
    monkeypatch.setattr(session_module, "find_coarse_auto_lock_target", reject)

    with pytest.raises(ValueError):
        session._start_auto_relock()

    assert len(postgres.rows) == 1
    row = postgres.rows[0]
    assert row["lock_source"] == "auto_relock"
    assert row["success"] is False
    assert row["failure_stage"] == "detection"
    assert row["lock_command_status"] == "not_sent"
    # The frame the attempt analysed before giving up, labelled as such.
    assert row["trace_y"] == pytest.approx((trace / ADC_SCALE).tolist())
    assert row["trace_meta"]["purpose"] == "last_analysed_frame"
    assert (
        session.auto_relock.get_status()["last_lock_acquisition_id"]
        == row["lock_acquisition_id"]
    )


# ------------------------------------------------------------ staged lock


def test_staged_lock_records_one_attempt_with_its_verification_frame(monkeypatch):
    session, postgres = _session(sweep_center=0.0, sweep_amplitude=1.0)
    selected = _result(0.1)

    def begin_detect(settings, after=None):
        frame = {
            "frame_id": 1, "acquired_at": time.time(), "sweep_center_v": 0.0,
            "sweep_amplitude_v": 1.0, "n_points": N, "modulation_frequency_hz": 10e6,
            "sideband_spacing_samples": 50.0, "noise_floor": 0.001,
        }
        return [selected], 0.0, 1.0, 2.0, frame

    session._capture_auto_lock_candidates_strict = begin_detect  # type: ignore[method-assign]
    token = session.staged_autolock_begin(None, 60.0)["token"]
    # Lock verification runs the real capture on fresh frames.
    del session._capture_auto_lock_candidates_strict
    analysed: list[tuple[int, np.ndarray]] = []

    def find_candidates(**kwargs):
        stream = session.plot_state
        analysed.append((stream.last_unlocked_frame_id, kwargs["error_trace_v"]))
        # Each confirmation frame is a new sweep.
        stream.show(_ramp(1000.0 * (len(analysed) + 1)))
        return [_result(0.1)]

    session.plot_state.show(_ramp(1000.0))
    monkeypatch.setattr(session_module, "find_auto_lock_candidates", find_candidates)

    payload = session.staged_autolock_lock(token, 1, selected.target_index)

    assert len(postgres.rows) == 1
    row = postgres.rows[0]
    assert payload["lock_acquisition_id"] == row["lock_acquisition_id"]
    assert row["lock_source"] == "staged_autolock"
    assert row["lock_command_status"] == "accepted"
    # The second confirmation frame is the one the lock was decided on.
    assert len(analysed) == 2
    frame_id, trace = analysed[1]
    assert row["trace_meta"]["frame_id"] == frame_id
    assert row["trace_y"] == pytest.approx(trace.tolist())
    assert row["trace_meta"]["selected_frame_id"] == 1
    assert row["trace_meta"]["selected_target_index"] == selected.target_index
    assert row["sweep_amplitude_v"] == pytest.approx(1.0)


# ------------------------------------------------ no artificial acquisitions


def test_changing_parameters_while_locked_records_nothing():
    session, postgres = _session(lock=True)
    session._update_persistent_setting = lambda *_a: True  # type: ignore[method-assign]

    session.set_param("p", 80, write_registers=True)
    session.set_param("i", 120, write_registers=True)
    session.set_param("offset_a", 25, write_registers=True)
    session.write_registers()

    assert postgres.rows == []


def test_one_attempt_is_recorded_only_once():
    session, postgres = _session()
    acquisition = LockAcquisition(
        device_key="dev-key-1", laser_name="Laser A", lock_source="manual_lock"
    )

    session._close_acquisition(acquisition, None, event_source="manual_lock")
    session._close_acquisition(acquisition, None, event_source="manual_lock")

    assert len(postgres.rows) == 1


def test_a_record_built_without_any_trace_has_null_traces_not_placeholders():
    acquisition = LockAcquisition(
        device_key="dev-key-1", laser_name="Laser A", lock_source="manual_lock"
    )

    row = build_acquisition_row(acquisition)

    assert row["trace_x"] is None
    assert row["trace_y"] is None
    assert row["secondary_trace_y"] is None
    assert row["trace_x_units"] is None
    assert row["trace_unavailable_reason"] == "no_trace_captured"


# -------------------------------------------------------- API compatibility


def test_start_lock_errors_keep_their_string_detail_and_carry_the_id_in_a_header(
    monkeypatch,
):
    class FailingSession:
        def start_lock(self):
            exc = RuntimeError("board refused")
            exc.lock_acquisition_id = "3f1c2b9a-1111-4a2b-8c3d-000000000001"
            raise exc

    monkeypatch.setattr(main, "_get_session", lambda _key: FailingSession())
    client = TestClient(main.app)

    response = client.post("/api/devices/dev/control/start_lock")

    assert response.status_code == 409
    assert response.json() == {"detail": "board refused"}
    assert (
        response.headers[main.LOCK_ACQUISITION_ID_HEADER]
        == "3f1c2b9a-1111-4a2b-8c3d-000000000001"
    )


def test_a_precheck_error_has_no_acquisition_id():
    error = main._lock_attempt_http_error(409, "Device not connected", RuntimeError())

    assert isinstance(error, HTTPException)
    assert error.detail == "Device not connected"
    assert not error.headers


def test_a_structured_error_detail_gains_the_acquisition_id():
    exc = RuntimeError("walk aborted")
    exc.lock_acquisition_id = "3f1c2b9a-1111-4a2b-8c3d-000000000002"

    error = main._lock_attempt_http_error(
        409, {"message": "walk aborted", "refinement": {"stages": []}}, exc
    )

    assert error.detail == {
        "message": "walk aborted",
        "refinement": {"stages": []},
        "lock_acquisition_id": "3f1c2b9a-1111-4a2b-8c3d-000000000002",
    }


def test_the_auto_lock_response_schema_accepts_the_id_and_still_accepts_old_payloads():
    from app.schemas import AutoLockScanResult as ResponseModel

    old = {
        "target_index": 1, "target_voltage": 0.0, "target_slope_rising": True,
        "score": 0.5, "left_excursion": 0.1, "right_excursion": 0.1,
        "pair_excursion": 0.2, "symmetry": 0.9,
    }
    assert ResponseModel(**old).lock_acquisition_id is None
    assert (
        ResponseModel(**old, lock_acquisition_id="abc").lock_acquisition_id == "abc"
    )
