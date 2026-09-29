from app.plot_processing import PlotState, build_plot_frame
import numpy as np


def test_build_plot_frame_unlocked():
    to_plot = {
        "error_signal_1": np.array([0, 1, 2, 3]),
        "monitor_signal": np.array([0, 1, 1, 0]),
    }
    params = {
        "lock": False,
        "dual_channel": False,
        "channel_mixing": 0,
        "combined_offset": 0,
        "modulation_frequency": 0,
        "pid_only_mode": False,
        "offset_a": 0,
        "offset_b": 0,
        "autolock_preparing": False,
        "sweep_amplitude": 1,
        "autolock_initial_sweep_amplitude": 1,
        "control_signal_history_length": 600,
    }
    state = PlotState()
    frame = build_plot_frame(to_plot, params, state)
    assert frame is not None
    assert "combined_error" in frame["series"]
    assert "error_signal_1" in frame["series"]


def test_build_plot_frame_locked():
    to_plot = {
        "error_signal": np.array([0, 1, 2, 3]),
        "control_signal": np.array([0, -1, -2, -3]),
    }
    params = {
        "lock": True,
        "dual_channel": False,
        "channel_mixing": 0,
        "combined_offset": 0,
        "modulation_frequency": 0,
        "pid_only_mode": False,
        "offset_a": 0,
        "offset_b": 0,
        "autolock_preparing": False,
        "sweep_amplitude": 1,
        "autolock_initial_sweep_amplitude": 1,
        "control_signal_history_length": 600,
    }
    state = PlotState()
    frame = build_plot_frame(to_plot, params, state)
    assert frame is not None
    assert "control_signal" in frame["series"]


_UNLOCKED_PARAMS = {
    "lock": False,
    "channel_mixing": 0,
    "combined_offset": 0,
    "modulation_frequency": 0,
    "pid_only_mode": False,
    "offset_a": 0,
    "offset_b": 0,
    "autolock_preparing": False,
    "sweep_amplitude": 1,
    "autolock_initial_sweep_amplitude": 1,
    "control_signal_history_length": 600,
}


def test_unlocked_frame_captures_true_monitor():
    to_plot = {
        "error_signal_1": np.array([0, 1, 2, 3]),
        "monitor_signal": np.array([5, 6, 6, 5]),
    }
    state = PlotState()
    build_plot_frame(to_plot, {**_UNLOCKED_PARAMS, "dual_channel": False}, state)
    assert state.last_monitor_signal is not None
    np.testing.assert_array_equal(state.last_monitor_signal, np.array([5, 6, 6, 5]))


def test_dual_error_signal_leaves_no_true_monitor():
    # Channel B carries a second error signal, not a monitor.
    to_plot = {
        "error_signal_1": np.array([0, 1, 2, 3]),
        "error_signal_2": np.array([3, 2, 1, 0]),
    }
    state = PlotState()
    build_plot_frame(to_plot, {**_UNLOCKED_PARAMS, "dual_channel": True}, state)
    # last_plot_data[1] is the conflated channel B (= error_signal_2)...
    assert state.last_plot_data is not None
    np.testing.assert_array_equal(state.last_plot_data[1], np.array([3, 2, 1, 0]))
    # ...but the true monitor is absent, so auto-lock won't treat it as one.
    assert state.last_monitor_signal is None


def test_unlocked_frame_id_increments_monotonically_per_stored_trace():
    """Frame identity for stored unlocked traces (serrodyne spec C1): each
    processed unlocked frame gets a new, strictly increasing frame_id, paired
    with the acquisition timestamp under the same state."""
    to_plot = {
        "error_signal_1": np.array([0, 1, 2, 3]),
        "monitor_signal": np.array([0, 1, 1, 0]),
    }
    state = PlotState()
    assert state.last_unlocked_frame_id == 0

    build_plot_frame(to_plot, {**_UNLOCKED_PARAMS, "dual_channel": False}, state)
    first_id = state.last_unlocked_frame_id
    first_at = state.last_unlocked_trace_at
    assert first_id == 1
    assert first_at is not None

    build_plot_frame(to_plot, {**_UNLOCKED_PARAMS, "dual_channel": False}, state)
    second_id = state.last_unlocked_frame_id
    second_at = state.last_unlocked_trace_at
    assert second_id == 2
    assert second_at is not None and second_at >= first_at

    # A locked frame does not touch the unlocked-trace identity.
    locked_to_plot = {
        "error_signal": np.array([0, 1, 2, 3]),
        "control_signal": np.array([0, -1, -2, -3]),
    }
    build_plot_frame(to_plot=locked_to_plot, params={**_UNLOCKED_PARAMS, "lock": True}, state=state)
    assert state.last_unlocked_frame_id == 2



def test_recent_unlocked_traces_are_keyed_by_frame_id_and_bounded():
    """Each stored unlocked frame's combined error is addressable by its
    frame_id (so a caller gets exactly the trace a detection analysed), and
    only the last RECENT_UNLOCKED_TRACES are kept."""
    from app.plot_processing import RECENT_UNLOCKED_TRACES

    state = PlotState()
    for i in range(RECENT_UNLOCKED_TRACES + 2):
        to_plot = {
            "error_signal_1": np.array([i, i + 1, i + 2, i + 3]),
            "monitor_signal": np.array([0, 1, 1, 0]),
        }
        build_plot_frame(to_plot, {**_UNLOCKED_PARAMS, "dual_channel": False}, state)

    ids = [fid for fid, _trace in state.recent_unlocked_traces]
    last = state.last_unlocked_frame_id
    assert ids == list(range(last - RECENT_UNLOCKED_TRACES + 1, last + 1))
    newest_id, newest_trace = state.recent_unlocked_traces[-1]
    assert newest_id == last
    np.testing.assert_array_equal(newest_trace, state.last_plot_data[2])
