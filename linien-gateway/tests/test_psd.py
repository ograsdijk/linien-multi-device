"""Tests for the gateway PSD relay: decode -> strip -> stitch -> emit.

The gateway never computes the PSD; it decodes the pickled dict the device
publishes to psd_data_partial/psd_data_complete, drops the large raw `signals`,
and stitches the per-decimation (f, psd) segments into one ascending log-log
curve in V / Sqrt[Hz]. These tests guard that contract and the PsdStore.
"""

from __future__ import annotations

import pickle
import time
from typing import Any

import numpy as np
import pytest

from app.plot_processing import V
from app.psd_store import PsdStore
from app.session import DeviceSession, flo_to_max_decimation
from app.stream import WebsocketManager


class _DummyDevice:
    key = "dev-a"
    name = "Device A"
    parameters: dict[str, Any] = {}


def _make_session() -> DeviceSession:
    manager = WebsocketManager(default_plot_fps=None, max_plot_fps_cap=None)
    manager.publish = lambda *a, **k: None  # type: ignore[assignment]
    return DeviceSession(_DummyDevice(), manager)


# --- _stitch_psd_curve -----------------------------------------------------


def test_stitch_basic_monotonic_and_scaled():
    psds = {
        # decimation 16 = low frequencies (processed first)
        16: (np.array([1.0, 2.0, 3.0]), np.array([8192.0, 16384.0, 24576.0])),
        # decimation 12 = higher frequencies; overlap below f=3 must be trimmed
        12: (np.array([2.0, 3.0, 4.0, 5.0]), np.array([1.0, 1.0, 8192.0, 16384.0])),
    }
    curve = DeviceSession._stitch_psd_curve(psds)
    freqs = [pt["f"] for pt in curve]
    psd = [pt["psd"] for pt in curve]

    # Ascending, overlap trimmed (4,5 from dec 12 only, since >3).
    assert freqs == [1.0, 2.0, 3.0, 4.0, 5.0]
    # psd scaled by V (counts -> Volt): 8192/8192 == 1.0, 24576/8192 == 3.0 ...
    assert psd[0] == 1.0
    assert psd[2] == 3.0
    assert psd[3] == 1.0  # 8192 / V
    assert all(isinstance(x, float) for x in freqs + psd)


def test_stitch_filters_nonfinite_and_nonpositive():
    psds = {
        8: (
            np.array([1.0, 2.0, np.nan, 4.0, 5.0]),
            np.array([8192.0, 0.0, 8192.0, -8192.0, 8192.0]),
        ),
    }
    curve = DeviceSession._stitch_psd_curve(psds)
    freqs = [pt["f"] for pt in curve]
    # f=2 dropped (psd 0), f=nan dropped, f=4 dropped (psd negative); 1 and 5 kept.
    assert freqs == [1.0, 5.0]


def test_stitch_handles_empty_or_bad_input():
    assert DeviceSession._stitch_psd_curve(None) == []
    assert DeviceSession._stitch_psd_curve({}) == []
    assert DeviceSession._stitch_psd_curve({0: ("bad",)}) == []
    # Mismatched lengths -> segment skipped.
    assert DeviceSession._stitch_psd_curve({0: (np.array([1.0]), np.array([]))}) == []


# --- band-limited RMS + peaking + flo->max_decimation ----------------------


def _flat_curve(level: float, f0: float, f1: float, n: int = 101):
    fs = np.linspace(f0, f1, n)
    return [{"f": float(f), "psd": float(level)} for f in fs]


def test_band_rms_full_and_clipped():
    # Flat ASD=2 over [1,101] -> RMS = 2*sqrt(bandwidth).
    curve = _flat_curve(2.0, 1.0, 101.0)
    full = DeviceSession._curve_rms(curve)
    assert full == pytest.approx(2.0 * np.sqrt(100.0), rel=1e-3)
    band = DeviceSession._curve_rms(curve, 1.0, 51.0)
    assert band == pytest.approx(2.0 * np.sqrt(50.0), rel=1e-3)
    # Clipping reduces the integrated RMS.
    assert band < full


def test_peaking_flat_vs_spike():
    flat = _flat_curve(1.0, 1.0, 100.0, n=50)
    assert DeviceSession._curve_peaking(flat) == pytest.approx(1.0, rel=1e-6)
    spike = _flat_curve(1.0, 1.0, 100.0, n=50)
    spike[25]["psd"] = 10.0  # tall narrow servo-bump-like peak
    assert DeviceSession._curve_peaking(spike) > 5.0


def test_flo_to_max_decimation_table():
    assert flo_to_max_decimation(1) == 20
    assert flo_to_max_decimation(10) == 16
    assert flo_to_max_decimation(20) == 12
    assert flo_to_max_decimation(100) == 12
    assert flo_to_max_decimation(300) == 8
    assert flo_to_max_decimation(5000) == 8  # clamped at min_dec
    assert flo_to_max_decimation(None) == 24  # clamped at max_dec


# --- _on_psd_data (decode + strip + emit) ----------------------------------


def _pickled_payload(*, complete: bool) -> bytes:
    return pickle.dumps(
        {
            "uuid": "abcdefghij",
            "time": 123.5,
            "p": np.int64(2500),
            "i": np.int64(1800),
            "d": 0,
            # Large raw arrays that MUST be stripped before forwarding.
            "signals": {16: np.zeros(16384), 12: np.zeros(16384)},
            "psds": {
                16: (np.array([1.0, 2.0]), np.array([8192.0, 16384.0])),
                12: (np.array([3.0, 4.0]), np.array([8192.0, 8192.0])),
            },
            "fitness": np.float64(42.0),
            "complete": complete,
        }
    )


def test_on_psd_data_strips_signals_and_forwards_curve():
    session = _make_session()
    captured: list[tuple[str, dict[str, Any]]] = []
    session.set_psd_event_callback(lambda key, payload: captured.append((key, payload)))

    session._on_psd_data(_pickled_payload(complete=True), complete=True)

    assert len(captured) == 1
    key, payload = captured[0]
    assert key == "dev-a"
    assert payload["device_key"] == "dev-a"
    assert "signals" not in payload
    assert payload["uuid"] == "abcdefghij"
    assert payload["complete"] is True
    # numpy scalars coerced to plain Python numbers.
    assert payload["p"] == 2500 and isinstance(payload["p"], int)
    assert payload["fitness"] == 42.0 and isinstance(payload["fitness"], float)
    # RMS computed in correct units (Volts) from the stitched ASD curve.
    assert isinstance(payload["rms_v"], float) and payload["rms_v"] > 0.0
    freqs = [pt["f"] for pt in payload["curve"]]
    assert freqs == [1.0, 2.0, 3.0, 4.0]
    assert payload["curve"][0]["psd"] == 8192.0 / V


def test_on_psd_data_ignores_none_and_missing_callback():
    session = _make_session()
    # No callback set -> no crash.
    session._on_psd_data(_pickled_payload(complete=False), complete=False)
    captured: list[Any] = []
    session.set_psd_event_callback(lambda key, payload: captured.append(payload))
    # None value -> ignored.
    session._on_psd_data(None, complete=True)
    assert captured == []


# --- PsdStore --------------------------------------------------------------


def test_psd_store_retains_completed_and_dedupes_by_uuid():
    now = time.time()
    store = PsdStore(max_entries=10)
    store.emit({"device_key": "d", "uuid": "u1", "complete": False, "time": now})
    # Partial not retained in tail history.
    assert store.tail() == []
    store.emit(
        {"device_key": "d", "uuid": "u1", "complete": True, "time": now, "fitness": 1.0}
    )
    store.emit(
        {
            "device_key": "d",
            "uuid": "u1",
            "complete": True,
            "time": now + 1.0,
            "fitness": 2.0,
        }
    )
    tail = store.tail()
    assert len(tail) == 1  # deduped by uuid
    assert tail[0]["fitness"] == 2.0
