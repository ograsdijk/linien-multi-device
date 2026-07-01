"""status() must surface the lock-indicator control metrics over REST.

The mean/std control voltage is computed per plot frame by the lock indicator
but was previously only available over the plot WebSocket. status() now exposes
`control_mean_v` / `control_std_v` / `lock_indicator_state` so a REST poller
(e.g. the EC recenter servo) can read a scalar control voltage.
"""

from __future__ import annotations

from typing import Any

from app.session import DeviceSession, _lock_error_mhz
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


def test_status_exposes_lock_error_from_error_std_and_slope() -> None:
    """error_std_v (live) + discriminator slope (last auto-lock scan) yield the
    in-loop lock error in MHz: lock_error_mhz = error_std_v / slope."""
    session = _make_session()
    session._discriminator_slope_v_per_mhz = 0.05  # a.u. per MHz
    session.last_plot_frame = {
        "lock": True,
        "lock_indicator": {"state": "locked"},
        "signal_stats": {
            "control_mean_v": 0.1,
            "control_std_v": 0.004,
            "error_std_v": 0.008,
        },
    }

    status = session.status()

    assert status["error_std_v"] == 0.008
    assert status["discriminator_slope_v_per_mhz"] == 0.05
    assert status["lock_error_mhz"] == 0.008 / 0.05  # 0.16 MHz


def test_status_lock_error_null_without_slope() -> None:
    """error_std_v is still exposed, but lock_error_mhz is null until a slope
    has been measured by an auto-lock scan."""
    session = _make_session()
    assert session._discriminator_slope_v_per_mhz is None
    session.last_plot_frame = {
        "lock": True,
        "lock_indicator": {"state": "locked"},
        "signal_stats": {"error_std_v": 0.008},
    }

    status = session.status()

    assert status["error_std_v"] == 0.008
    assert status["discriminator_slope_v_per_mhz"] is None
    assert status["lock_error_mhz"] is None


def test_lock_error_mhz_helper_guards() -> None:
    assert _lock_error_mhz(0.008, 0.05) == 0.008 / 0.05
    assert _lock_error_mhz(None, 0.05) is None
    assert _lock_error_mhz(0.008, None) is None
    assert _lock_error_mhz(0.008, 0.0) is None  # zero slope -> no division
    assert _lock_error_mhz(0.008, -0.05) is None  # non-physical slope
    assert _lock_error_mhz(float("nan"), 0.05) is None
