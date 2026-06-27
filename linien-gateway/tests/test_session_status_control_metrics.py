"""status() must surface the lock-indicator control metrics over REST.

The mean/std control voltage is computed per plot frame by the lock indicator
but was previously only available over the plot WebSocket. status() now exposes
`control_mean_v` / `control_std_v` / `lock_indicator_state` so a REST poller
(e.g. the EC recenter servo) can read a scalar control voltage.
"""

from __future__ import annotations

from typing import Any

from app.session import DeviceSession
from app.stream import WebsocketManager


class _DummyDevice:
    key = "dev-a"
    name = "Device A"
    parameters: dict[str, Any] = {}


def _make_session() -> DeviceSession:
    manager = WebsocketManager(default_plot_fps=None, max_plot_fps_cap=None)
    manager.publish = lambda *_a, **_k: None  # type: ignore[assignment]
    return DeviceSession(_DummyDevice(), manager)


def test_status_exposes_control_metrics_from_cached_frame() -> None:
    session = _make_session()
    session.last_plot_frame = {
        "lock": True,
        "lock_indicator": {"state": "locked"},
        "signal_stats": {"control_mean_v": 0.125, "control_std_v": 0.004},
    }

    status = session.status()

    assert status["control_mean_v"] == 0.125
    assert status["control_std_v"] == 0.004
    assert status["lock_indicator_state"] == "locked"
    assert status["lock"] is True


def test_status_exposes_control_metrics_when_indicator_disabled() -> None:
    """Regression: disabling the lock indicator must not hide the control
    voltage. Stats live in signal_stats, independent of the indicator."""
    session = _make_session()
    session.last_plot_frame = {
        "lock": True,
        "lock_indicator": {"state": "unknown", "reasons": ["disabled"]},
        "signal_stats": {"control_mean_v": 0.2, "control_std_v": 0.005},
    }

    status = session.status()

    assert status["control_mean_v"] == 0.2
    assert status["control_std_v"] == 0.005
    assert status["lock_indicator_state"] == "unknown"


def test_status_control_metrics_null_without_frame() -> None:
    session = _make_session()
    assert session.last_plot_frame is None

    status = session.status()

    assert status["control_mean_v"] is None
    assert status["control_std_v"] is None
    assert status["lock_indicator_state"] is None


def test_status_coerces_non_finite_control_mean_to_null() -> None:
    session = _make_session()
    session.last_plot_frame = {
        "lock": True,
        "lock_indicator": {"state": "locked"},
        "signal_stats": {"control_mean_v": float("nan"), "control_std_v": None},
    }

    status = session.status()

    assert status["control_mean_v"] is None
    assert status["control_std_v"] is None
