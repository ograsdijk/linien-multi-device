"""Configuration snapshot, trace characterization and lock verification of a
lock-acquisition record."""

from __future__ import annotations

import json
import math
import time
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from app.auto_lock_scan import AutoLockScanSettings
from app.linien_config import LINIEN_CONFIG_SCHEMA_VERSION, build_linien_config
from app.lock_acquisition import TraceSnapshot, VerificationTracker, unavailable_trace
from app.lock_characterization import characterize_trace
from app.manual_lock_record import ADC_SCALE, MOD_HZ_UNIT
from app.session import DeviceSession

N = 2048

# The parameters the record must capture (from the task specification, not
# from the implementation's own list).
REQUIRED_PARAMETERS = (
    "mod_channel", "control_channel", "sweep_channel", "slow_control_channel",
    "modulation_frequency", "modulation_amplitude",
    "demodulation_phase_a", "demodulation_phase_b",
    "demodulation_multiplier_a", "demodulation_multiplier_b",
    "dual_channel", "channel_mixing", "offset_a", "offset_b", "combined_offset",
    "p", "i", "d", "target_slope_rising",
    "pid_on_slow_enabled", "pid_on_slow_strength", "pid_only_mode",
    "filter_automatic_a", "filter_automatic_b",
    "filter_1_enabled_a", "filter_1_enabled_b", "filter_2_enabled_a", "filter_2_enabled_b",
    "filter_1_type_a", "filter_1_type_b", "filter_2_type_a", "filter_2_type_b",
    "filter_1_frequency_a", "filter_1_frequency_b",
    "filter_2_frequency_a", "filter_2_frequency_b",
    "polarity_fast_out1", "polarity_fast_out2", "polarity_analog_out0",
    "sweep_center", "sweep_amplitude", "sweep_speed", "sweep_pause",
)


# ------------------------------------------------------------------ fakes


class _Manager:
    def publish(self, *_a, **_k) -> None:
        return None


class _Param:
    def __init__(self, value: Any) -> None:
        self.value = value


class _Parameters:
    def __init__(self, **values: Any) -> None:
        for name, value in values.items():
            setattr(self, name, _Param(value))


class _Postgres:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def enqueue_lock_result(self, row: dict[str, Any]) -> bool:
        self.rows.append(row)
        return True

    def get_state(self) -> dict[str, Any]:
        return {"config": {"enabled": True}, "status": {"active": True}}


class _Stream:
    def __init__(self) -> None:
        self.last_plot_data = None
        self.last_monitor_signal = None
        self.last_unlocked_frame_id = 0
        self.last_unlocked_trace_at = None

    def show(self, error_counts: np.ndarray) -> None:
        self.last_plot_data = [error_counts, error_counts, error_counts]
        self.last_unlocked_frame_id += 1
        self.last_unlocked_trace_at = time.time()


class _Control:
    def __init__(self) -> None:
        self.fail: Exception | None = None

    def exposed_start_lock(self) -> None:
        if self.fail is not None:
            raise self.fail


def _full_parameters(**overrides: Any) -> dict[str, Any]:
    values = {name: 0 for name in REQUIRED_PARAMETERS}
    values.update(
        sweep_center=0.0, sweep_amplitude=1.0, sweep_pause=False, sweep_speed=8,
        modulation_frequency=30 * MOD_HZ_UNIT, modulation_amplitude=4000,
        target_slope_rising=True, dual_channel=False, p=50, i=100,
        to_plot=b"plot buffer", control_signal_history={"values": [1, 2, 3]},
        lock=False,
    )
    values.update(overrides)
    return values


def _session(params: dict[str, Any] | None = None):
    postgres = _Postgres()
    device = SimpleNamespace(key="dev-1", name="Laser A", host="h", port=1, parameters={})
    session = DeviceSession(device, _Manager(), lock_result_postgres_service=postgres)
    session.parameters = _Parameters(**(params or _full_parameters()))
    session.control = _Control()
    session.plot_state = _Stream()
    return session, postgres


def _dispersive(amplitude: float, width: float, center: float) -> np.ndarray:
    x = np.linspace(-1.0, 1.0, N)
    u = (x - center) / width
    return amplitude * u * np.exp(-0.5 * u**2)


def _pdh_triplet() -> np.ndarray:
    """Carrier (rising, at 0 V) and two opposite-slope sidebands at +/-0.3 V,
    on a 0 +/- 1 V sweep, in plot volts."""
    return (
        _dispersive(0.4, 0.03, 0.0)
        + _dispersive(-0.15, 0.03, -0.3)
        + _dispersive(-0.15, 0.03, 0.3)
    )


def _snapshot(error_v: np.ndarray, **kw: Any) -> TraceSnapshot:
    fields = dict(
        error_v=error_v, secondary_v=None, secondary_role=None, frame_id=1,
        frame_processed_at=time.time(), sweep_center_v=0.0, sweep_amplitude_v=1.0,
        geometry_read_at=time.time(), purpose="pre_lock_snapshot", sweep_paused=False,
    )
    fields.update(kw)
    return TraceSnapshot(**fields)


# --------------------------------------------------------- config snapshot


def test_the_config_snapshot_holds_every_required_parameter_raw():
    session, postgres = _session()
    session.plot_state.show(_pdh_triplet() * ADC_SCALE)

    session.start_lock()

    config = postgres.rows[0]["linien_config"]
    assert config["schema_version"] == LINIEN_CONFIG_SCHEMA_VERSION
    assert config["captured_at_stage"] == "pre_lock_command"
    for name in REQUIRED_PARAMETERS:
        assert name in config["parameters"], name
    # Raw internal units, not the converted columns.
    assert config["parameters"]["modulation_frequency"] == 30 * MOD_HZ_UNIT
    assert postgres.rows[0]["modulation_frequency_hz"] == pytest.approx(30e6)
    # No plot buffers or histories, even though the device exposes them.
    assert "to_plot" not in config["parameters"]
    assert "control_signal_history" not in config["parameters"]
    # The gateway's own lock settings travel with it.
    assert set(config["gateway"]) == {"auto_lock_scan", "lock_acceptance", "lock_indicator"}
    json.dumps(config, allow_nan=False)


def test_missing_and_unreadable_parameters_are_listed_not_fatal():
    session, postgres = _session(
        {"sweep_center": 0.0, "sweep_amplitude": 1.0, "lock": False, "p": float("nan")}
    )
    session.plot_state.show(_pdh_triplet() * ADC_SCALE)

    session.start_lock()

    assert len(postgres.rows) == 1
    config = postgres.rows[0]["linien_config"]
    assert "modulation_frequency" in config["missing"]
    assert "filter_1_type_a" in config["missing"]
    assert config["parameters"]["sweep_amplitude"] == 1.0
    # Non-finite values become null: valid JSON for the jsonb column.
    assert config["parameters"]["p"] is None
    json.dumps(config, allow_nan=False)


def test_a_parameter_that_raises_on_read_is_listed_as_missing():
    def read(name: str, default: Any) -> Any:
        if name == "p":
            raise RuntimeError("rpyc hiccup")
        return 1 if name != "i" else default

    config = build_linien_config(read, captured_at_stage="attempt_start")

    assert "p" in config["missing"]
    assert "i" in config["missing"]
    assert config["parameters"]["d"] == 1


# ------------------------------------------------------- characterization


def test_a_pdh_trace_is_characterized_with_units_kept_apart():
    # Calibrated to the feature (lobe half-width 0.03 V), as auto-lock
    # calibration would: the slope fit spans half of this window, so an
    # uncalibrated, wider window flattens the slope estimate.
    result = characterize_trace(
        _snapshot(_pdh_triplet()),
        settings=AutoLockScanSettings(signal_type="pdh", half_range_sweep_v=0.03),
        preferred_slope_rising=True,
        modulation_frequency_hz=30e6,
    )

    assert result["status"] == "ok"
    assert result["zero_crossing_sweep_v"] == pytest.approx(0.0, abs=2e-3)
    # Carrier slope on the sweep axis: amplitude / width = 13.3 error-V per
    # sweep-V for the ideal curve; the fit window flattens it a little.
    assert 10.0 < result["slope_error_v_per_sweep_v"] < 14.0
    # MHz-calibrated slope only because the sidebands calibrate the axis:
    # 0.3 sweep-V <-> 30 MHz.
    calibration = result["frequency_calibration"]
    assert calibration["hz_per_sweep_v"] == pytest.approx(1e8, rel=0.05)
    assert result["discriminator_slope_v_per_mhz"] == pytest.approx(
        abs(result["slope_error_v_per_sweep_v"]) / 1e8 * 1e6, rel=0.25
    )
    assert result["peak_to_peak_v"] == pytest.approx(0.485, rel=0.1)
    assert result["feature_width_sweep_v"] == pytest.approx(0.06, rel=0.15)
    assert result["baseline_v"] == pytest.approx(0.0, abs=0.01)


def test_no_mhz_slope_without_a_frequency_calibration():
    result = characterize_trace(
        _snapshot(_pdh_triplet()),
        settings=AutoLockScanSettings(signal_type="pdh"),
        preferred_slope_rising=True,
        modulation_frequency_hz=None,
    )

    assert result["status"] == "ok"
    assert result["slope_error_v_per_sweep_v"] is not None
    assert result["discriminator_slope_v_per_mhz"] is None
    assert result["frequency_calibration"] is None


def test_a_trace_without_a_feature_reports_why_instead_of_numbers():
    flat = np.random.default_rng(1).normal(0.0, 1e-4, N)

    result = characterize_trace(
        _snapshot(flat), settings=AutoLockScanSettings(signal_type="pdh")
    )

    assert result["status"] == "no_feature_detected"
    assert result["reason"]
    assert "zero_crossing_sweep_v" not in result
    assert "discriminator_slope_v_per_mhz" not in result


@pytest.mark.parametrize(
    ("trace", "status"),
    [
        (None, "unavailable"),
        (unavailable_trace("stale_unlocked_trace", purpose="pre_lock_snapshot"), "unavailable"),
        (_snapshot(_pdh_triplet(), sweep_paused=True), "not_applicable"),
        (_snapshot(_pdh_triplet(), sweep_center_v=None, sweep_amplitude_v=None), "not_applicable"),
    ],
)
def test_characterization_declines_traces_it_cannot_vouch_for(trace, status):
    result = characterize_trace(trace, settings=AutoLockScanSettings())

    assert result["status"] == status


def test_a_characterization_crash_does_not_stop_the_lock_or_the_record(monkeypatch):
    import app.lock_characterization as lc

    def boom(**_kw):
        raise ZeroDivisionError("bad trace")

    monkeypatch.setattr(lc, "find_auto_lock_target", boom)
    session, postgres = _session()
    session.plot_state.show(_pdh_triplet() * ADC_SCALE)

    session.start_lock()

    row = postgres.rows[0]
    assert row["lock_command_status"] == "accepted"
    assert row["characterization_status"] == "error"
    assert "bad trace" in row["characterization"]["reason"]


def test_a_manual_lock_record_carries_the_characterization_columns():
    session, postgres = _session()
    session.plot_state.show(_pdh_triplet() * ADC_SCALE)

    session.start_lock()

    row = postgres.rows[0]
    assert row["characterization_status"] == "ok"
    assert row["characterization"]["result_source"] == "diagnostic_detection"
    assert row["zero_crossing_sweep_v"] == pytest.approx(0.0, abs=2e-3)
    assert row["discriminator_slope_v_per_mhz"] is not None
    assert not math.isnan(row["peak_to_peak_v"])


# ------------------------------------------------------------- verification


def _tracker() -> VerificationTracker:
    return VerificationTracker(commanded_at=100.0, hold_s=1.0, timeout_s=10.0)


def test_verified_after_the_indicator_holds_locked():
    tracker = _tracker()
    assert tracker.observe(100.1, lock=True, indicator_state="unknown") is None
    assert tracker.observe(100.5, lock=True, indicator_state="locked") is None
    assert tracker.observe(101.6, lock=True, indicator_state="locked") == ("verified", None)


def test_a_locked_spell_must_be_continuous():
    tracker = _tracker()
    tracker.observe(100.2, lock=True, indicator_state="locked")
    tracker.observe(100.8, lock=True, indicator_state="marginal")
    assert tracker.observe(101.3, lock=True, indicator_state="locked") is None


def test_lost_indicator_or_dropped_lock_fails_verification():
    lost = _tracker()
    assert lost.observe(100.3, lock=True, indicator_state="lost") == (
        "failed", "lock_indicator_lost"
    )
    dropped = _tracker()
    # A sweep frame right after the command is still in flight, not a drop.
    assert dropped.observe(100.1, lock=False, indicator_state="unknown") is None
    assert dropped.observe(101.5, lock=False, indicator_state="unknown") == (
        "failed", "lock_dropped"
    )


def test_timeout_without_confirmation_fails_and_no_frames_is_unavailable():
    marginal = _tracker()
    marginal.observe(100.5, lock=True, indicator_state="marginal")
    assert marginal.check_deadline(110.5) == ("failed", "not_confirmed_within_timeout")
    silent = _tracker()
    assert silent.check_deadline(106.0) == ("unavailable", "no_plot_frames")


def _feed(session: DeviceSession, t: float, state: str) -> None:
    with session._state_lock:
        done = session._observe_verification(t, True, state)
    if done is not None:
        session._resolve_pending_verification(done[1], only=done[0])


def test_verification_updates_the_same_record_and_does_not_block_the_lock():
    session, postgres = _session()
    session.plot_state.show(_pdh_triplet() * ADC_SCALE)

    acquisition_id = session.start_lock()

    # The lock call returned with verification still open.
    assert postgres.rows[0]["verification_status"] == "pending"
    commanded = postgres.rows[0]["lock_command_at"]
    _feed(session, commanded + 0.1, "locked")
    _feed(session, commanded + 2.0, "locked")

    assert len(postgres.rows) == 2
    update = postgres.rows[1]
    assert update["lock_acquisition_id"] == acquisition_id
    assert update["verification_status"] == "verified"
    assert update["verification_completed_at"] >= commanded
    # Command accepted and verified are separate facts.
    assert update["success"] is True
    # Later frames do not touch the closed record again.
    _feed(session, commanded + 5.0, "lost")
    assert len(postgres.rows) == 2


def test_verification_is_unavailable_when_the_lock_indicator_is_off():
    session, postgres = _session()
    session.update_lock_indicator_config({"enabled": False})
    session.plot_state.show(_pdh_triplet() * ADC_SCALE)

    session.start_lock()

    row = postgres.rows[0]
    assert row["verification_status"] == "unavailable"
    assert row["verification_reason"] == "lock_indicator_disabled"


def test_a_rejected_lock_command_is_not_verified():
    session, postgres = _session()
    session.plot_state.show(_pdh_triplet() * ADC_SCALE)
    session.control.fail = RuntimeError("refused")

    with pytest.raises(RuntimeError):
        session.start_lock()

    assert postgres.rows[0]["verification_status"] == "not_attempted"
    assert session._pending_verification is None
