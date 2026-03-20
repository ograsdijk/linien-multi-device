from app.manual_lock_record import (
    MOD_AMP_SCALE,
    MOD_HZ_UNIT,
    OFFSET_SCALE,
    build_manual_lock_row,
)


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
