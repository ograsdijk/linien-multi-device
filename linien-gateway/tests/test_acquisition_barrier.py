"""Regression tests for geometry changes and Linien acquisition freshness.

A plot value read back from the RemoteParameter cache is not a new acquisition.
Only a pushed `to_plot` update can advance the frame identity used by auto-lock.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from app.session import DeviceSession
from app.stream import WebsocketManager


class _Param:
    def __init__(self, value: Any) -> None:
        self.value = value
        self._cached_value = value


class _Parameters:
    def __init__(self) -> None:
        self.pause_acquisition = _Param(False)
        self.lock = _Param(False)
        self.dual_channel = _Param(False)
        self.channel_mixing = _Param(0)
        self.combined_offset = _Param(0)
        self.modulation_frequency = _Param(0)
        self.pid_only_mode = _Param(False)
        self.offset_a = _Param(0)
        self.offset_b = _Param(0)
        self.pid_on_slow_enabled = _Param(False)
        self.autolock_preparing = _Param(False)
        self.sweep_amplitude = _Param(0.255)
        self.sweep_center = _Param(0.537618285948415)
        self.autolock_initial_sweep_amplitude = _Param(1.0)
        self.control_signal_history_length = _Param(600)
        self.to_plot = _Param(None)

    def check_for_changed_parameters(self) -> None:
        pass


class _Control:
    def __init__(self, parameters: _Parameters) -> None:
        self.calls: list[str] = []
        self.parameters = parameters
        self.on_pause: Any = None

    def exposed_pause_acquisition(self) -> None:
        self.calls.append("pause")
        self.parameters.pause_acquisition.value = True
        self.parameters.pause_acquisition._cached_value = True
        if self.on_pause is not None:
            self.on_pause()

    def exposed_write_registers(self) -> None:
        self.calls.append("write")
        self.parameters.sweep_center._cached_value = self.parameters.sweep_center.value
        self.parameters.sweep_amplitude._cached_value = self.parameters.sweep_amplitude.value

    def exposed_continue_acquisition(self) -> None:
        self.calls.append("continue")
        self.parameters.pause_acquisition.value = False
        self.parameters.pause_acquisition._cached_value = False


def _session() -> DeviceSession:
    device = SimpleNamespace(
        key="dev-barrier", name="dev-barrier", host="127.0.0.1", port=18864,
        parameters={},
    )
    result = DeviceSession(device, WebsocketManager(default_plot_fps=None))
    result.parameters = _Parameters()
    result.control = _Control(result.parameters)
    return result


def _trace(tag: int) -> dict[str, np.ndarray]:
    # Distinct samples let the snapshot assertion prove which trace was kept.
    return {
        "error_signal_1": np.asarray([tag, tag + 1, tag, tag - 1], dtype=np.int16),
        "monitor_signal": np.asarray([0, 1, 1, 0], dtype=np.int16),
    }


class _StopAfterOnePoll:
    def __init__(self) -> None:
        self.calls = 0

    def is_set(self) -> bool:
        self.calls += 1
        return self.calls > 1


def test_poll_loop_does_not_reprocess_cached_to_plot_as_new_acquisition(monkeypatch):
    session = _session()
    cached = _trace(10)
    session.parameters.to_plot.value = cached
    session._on_to_plot(cached)
    first_id = session.plot_state.last_unlocked_frame_id
    first_at = session.plot_state.last_unlocked_trace_at

    # Simulate the old timeout fallback: no pushed plot arrived for >1 second,
    # so the poll loop used to read `to_plot.value` again. This is the same raw
    # cached acquisition and must not advance freshness after a geometry change.
    session.parameters.sweep_center.value = 0.596665672367565
    session.last_plot_timestamp = 0.0
    session._stop_event = _StopAfterOnePoll()
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    session._poll_loop()

    assert session.plot_state.last_unlocked_frame_id == first_id
    assert session.plot_state.last_unlocked_trace_at == first_at


def test_cached_plot_reread_cannot_advance_frame_identity_or_replace_geometry():
    session = _session()

    # A pushed acquisition at stage-2 geometry is accepted.
    session._on_to_plot(_trace(10))
    before = session._build_trace_snapshot(time.time())
    first_id = before["frame_id"]
    first_at = before["acquired_at"]
    assert first_id == 1
    assert before["sweep_center"] == pytest.approx(0.537618285948415)
    assert before["sweep_amplitude"] == pytest.approx(0.255)

    # A delayed callback for the pre-move sweep is delivered after pause has
    # become visible. It must not make that old sample look like a stage-3 sweep.
    session.parameters.pause_acquisition.value = True
    session.parameters.pause_acquisition._cached_value = True
    session.parameters.sweep_center.value = 0.596665672367565
    session.parameters.sweep_center._cached_value = 0.596665672367565
    session._on_to_plot(_trace(10))
    after = session._build_trace_snapshot(time.time())

    assert session.plot_state.last_unlocked_frame_id == first_id
    assert session.plot_state.last_unlocked_trace_at == first_at
    assert after["frame_id"] == first_id
    assert after["sweep_center"] == pytest.approx(0.537618285948415)
    assert after["sweep_amplitude"] == pytest.approx(0.255)
    assert after["combined_error"] == before["combined_error"]


def test_new_pushed_acquisition_is_associated_with_new_geometry():
    session = _session()
    session._on_to_plot(_trace(10))
    first_id = session.plot_state.last_unlocked_frame_id
    first_trace = _session_first_trace(session)
    session.control.on_pause = lambda: session._on_to_plot(_trace(10))

    # The barrier sequence parks acquisition, writes both sweep registers, and
    # resumes it with Linien's rotated acquisition UUID. Old in-flight frames
    # are dropped upstream after resume.
    session._set_sweep_geometry(0.596665672367565, 0.255)
    assert session.control.calls == ["pause", "write", "continue"]
    assert session.plot_state.last_unlocked_frame_id == first_id  # pause batch dropped
    assert session.parameters.sweep_center.value == pytest.approx(0.596665672367565)
    assert session.parameters.sweep_amplitude.value == pytest.approx(0.255)

    # One already-in-flight old UUID frame may arrive after resume. It can
    # advance the event counter, but must not by itself release the two-frame
    # settle barrier.
    baseline = time.time()
    session._on_to_plot(_trace(10))
    completed: list[bool] = []
    waiter = threading.Thread(
        target=lambda: completed.append(
            session._wait_for_fresh_unlocked_trace(baseline, timeout_s=1.0)
        )
    )
    waiter.start()
    time.sleep(0.08)
    assert waiter.is_alive()

    # The next pushed frame is from the resumed geometry and releases the wait.
    session._on_to_plot(_trace(11))
    waiter.join(timeout=1.0)
    assert not waiter.is_alive()
    assert completed == [True]
    after = session._build_trace_snapshot(time.time())
    assert after["frame_id"] == first_id + 2  # late old frame + verified new frame
    assert after["sweep_center"] == pytest.approx(0.596665672367565)
    assert after["sweep_amplitude"] == pytest.approx(0.255)
    assert after["combined_error"] != first_trace


def _session_first_trace(session: DeviceSession) -> list[float]:
    """Return the most recently accepted trace in volts for concise assertions."""
    trace = session.unlocked_trace_for_frame(session.plot_state.last_unlocked_frame_id)
    assert trace is not None
    return trace


def test_acquisition_wait_times_out_when_only_cached_value_is_replayed(monkeypatch):
    session = _session()
    session._on_to_plot(_trace(10))
    start = time.time()

    # No pushed frame arrived after the geometry UUID rotation, so the wait
    # must time out even if a cached value is still available.
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    session.parameters.pause_acquisition.value = True
    session.parameters.pause_acquisition._cached_value = True
    session._on_to_plot(_trace(10))
    assert not session._wait_for_fresh_unlocked_trace(start, timeout_s=0.0)


def test_pause_ack_timeout_resumes_without_writing_geometry(monkeypatch):
    session = _session()
    original_geometry = (
        session.parameters.sweep_center.value,
        session.parameters.sweep_amplitude.value,
    )

    # Simulate a pause RPC that succeeds remotely but whose true acknowledgment
    # never reaches the cached parameter queue. Geometry writes must not begin;
    # the finally path still attempts continue in case the server did pause.
    def pause_without_ack() -> None:
        session.control.calls.append("pause")

    session.control.exposed_pause_acquisition = pause_without_ack
    ticks = iter(range(100))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks) * 10.0)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="pause was not acknowledged"):
        session._set_sweep_geometry(0.596665672367565, 0.255)

    assert session.control.calls == ["pause", "continue"]
    assert (
        session.parameters.sweep_center.value,
        session.parameters.sweep_amplitude.value,
    ) == original_geometry


def test_missing_accepted_frame_geometry_refuses_live_parameter_fallback():
    session = _session()
    session._on_to_plot(_trace(10))
    frame_id = session.plot_state.last_unlocked_frame_id
    assert frame_id > 0

    # If frame metadata is lost, the current device parameters cannot stand in
    # for the geometry under which this stored signal was acquired.
    session._unlocked_geometry_by_frame.pop(frame_id)
    session.parameters.sweep_center.value = 0.596665672367565

    with pytest.raises(RuntimeError, match="No sweep geometry snapshot"):
        session._build_trace_snapshot(time.time())
    with pytest.raises(RuntimeError, match="No sweep geometry snapshot"):
        session._snapshot_sweep_params(frame_id=frame_id)
