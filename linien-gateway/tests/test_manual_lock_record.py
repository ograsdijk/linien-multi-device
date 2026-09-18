from app.manual_lock_record import (
    MOD_AMP_SCALE,
    MOD_HZ_UNIT,
    OFFSET_SCALE,
    build_manual_lock_row,
)

PARAMS = {
    "control_channel": 0,
    "modulation_frequency": 2 * MOD_HZ_UNIT,
    "modulation_amplitude": 0.5 * MOD_AMP_SCALE,
    "demodulation_phase_a": 10,
    "offset_a": 100,
    "p": 1.1,
    "i": 2.2,
    "d": 3.3,
    "sweep_center": 0.1,
    "sweep_amplitude": 0.8,
}


def test_build_manual_lock_row_uses_control_channel_b_and_conversions():
    params = {
        "control_channel": 1,
        "modulation_frequency": 2 * MOD_HZ_UNIT,
        "modulation_amplitude": 0.5 * MOD_AMP_SCALE,
        "demodulation_phase_a": 10,
        "demodulation_phase_b": 25,
        "offset_a": 100,
        "offset_b": 200,
        "p": 1.1,
        "i": 2.2,
        "d": 3.3,
        "sweep_center": 0.0,
        "sweep_amplitude": 1.0,
    }
    row = build_manual_lock_row(
        device_name="Laser A",
        device_key="laser-a",
        lock_source="manual_lock",
        params=params,
        trace_y=[0.1, 0.2, 0.3],
        monitor_trace_y=[-0.1, -0.2, -0.3],
    )

    assert row["laser_name"] == "Laser A"
    assert row["lock_source"] == "manual_lock"
    assert row["success"] is True
    assert row["modulation_frequency_hz"] == 2_000_000.0
    assert row["modulation_amplitude"] == 0.5
    assert row["demod_phase_deg"] == 25
    assert row["signal_offset_volts"] == 200 / OFFSET_SCALE
    assert row["pid_p"] == 1.1
    assert row["pid_i"] == 2.2
    assert row["pid_d"] == 3.3
    assert row["trace_x"] == [-1.0, 0.0, 1.0]
    assert row["trace_y"] == [0.1, 0.2, 0.3]
    assert row["monitor_trace_y"] == [-0.1, -0.2, -0.3]
    assert row["trace_x_units"] == "V"
    assert row["trace_y_units"] == "V"
    assert row["monitor_trace_y_units"] == "V"


def test_build_manual_lock_row_falls_back_to_device_key_and_index_trace_x():
    row = build_manual_lock_row(
        device_name="",
        device_key="device-key",
        lock_source="auto_lock_scan",
        params={},
        trace_y=[1, None, "bad"],
        monitor_trace_y=[2, 3],
    )

    assert row["laser_name"] == "device-key"
    assert row["lock_source"] == "auto_lock_scan"
    assert row["trace_x"] == [0.0, 1.0, 2.0]
    assert len(row["trace_y"]) == 3
    assert len(row["monitor_trace_y"]) == 3


def _approach_report():
    return {
        "enabled": True,
        "accepted": True,
        "target_voltage": 0.2,
        "commanded_voltage": 0.23,
        "start_voltage": -0.3,
        "center_move_v": 0.53,
        "center_correction_v": 0.03,
        "center_offset_v": 0.002,
        "capture_tolerance_v": 0.01,
        "rejection_bound_v": 0.08,
        "attempts": [
            {"attempt": 1, "from_below": True, "direct": True, "offset_v": 0.03},
            {"attempt": 2, "from_below": True, "direct": False, "offset_v": 0.002},
        ],
    }






def test_the_sweep_is_recorded_as_columns_not_only_inside_trace_x():
    row = build_manual_lock_row(
        device_name="laser-1",
        device_key="dev-1",
        params=PARAMS,
        trace_y=[0.0, 1.0],
        monitor_trace_y=None,
    )

    assert row["sweep_center_v"] == PARAMS["sweep_center"]
    assert row["sweep_amplitude_v"] == PARAMS["sweep_amplitude"]






