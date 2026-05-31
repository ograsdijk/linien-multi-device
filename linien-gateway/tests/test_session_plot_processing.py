"""Regression tests for session._on_to_plot under the gateway plot-frame refactor.

The critical invariant being guarded: `_on_to_plot` must ALWAYS build the
plot frame at full detail and update internal state (`lock_indicator.update`,
`auto_relock.tick`, `last_plot_frame`, lock-transition logging) regardless of
whether any websocket subscribers exist. Only the outbound `manager.publish`
call is gated by `peek_required_detail(...) is not None or auto_relock_enabled`.

Previously a buggy gating path skipped the entire frame build when there were
no subscribers, which left dependent features (auto-relock, lock indicator,
manual lock snapshots) starved of plot updates.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from app.session import DeviceSession
from app.stream import WebsocketManager


class _DummyDevice:
    key = "dev-a"
    name = "Device A"
    parameters: dict[str, Any] = {}


class _Param:
    def __init__(self, value: Any) -> None:
        self.value = value


class _DummyParameters:
    """Minimal stand-in for the real `LinienClient.parameters` namespace.

    Exposes every attribute that `_on_to_plot`/`_plot_params`/`_derive_lock_value`
    reads from `self.parameters` as a `_Param` with a `.value` field.
    """

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
        self.sweep_amplitude = _Param(1.0)
        self.autolock_initial_sweep_amplitude = _Param(1.0)
        self.control_signal_history_length = _Param(600)


def _make_unlocked_to_plot() -> dict[str, np.ndarray]:
    n = 16
    return {
        "error_signal_1": np.zeros(n, dtype=np.int16),
        "error_signal_2": np.zeros(n, dtype=np.int16),
        "monitor_signal": np.zeros(n, dtype=np.int16),
    }


def _build_session() -> tuple[DeviceSession, WebsocketManager, list[dict]]:
    manager = WebsocketManager(default_plot_fps=None, max_plot_fps_cap=None)
    published: list[dict] = []

    def _capture_publish(device_key: str, message: dict) -> None:
        published.append({"device_key": device_key, "message": message})

    # Replace publish so it records calls instead of needing a running loop.
    manager.publish = _capture_publish  # type: ignore[assignment]

    session = DeviceSession(_DummyDevice(), manager)
    session.parameters = _DummyParameters()
    return session, manager, published


def test_on_to_plot_updates_state_with_zero_subscribers():
    """No subscribers => still build frame, still update lock_indicator + cache.

    The frame must NOT be published (gate returns None and auto_relock is
    disabled by default), but every other side-effect must occur.
    """
    session, manager, published = _build_session()

    assert manager.peek_required_detail("dev-a") is None
    assert session.last_plot_frame is None

    # Spy on lock_indicator.update to confirm it ran at least once.
    update_calls: list[dict] = []
    real_update = session.lock_indicator.update

    def _spy_update(**kwargs):
        update_calls.append(kwargs)
        return real_update(**kwargs)

    session.lock_indicator.update = _spy_update  # type: ignore[assignment]

    session._on_to_plot(_make_unlocked_to_plot())

    # Frame must have been built and cached even though nobody is subscribed.
    assert session.last_plot_frame is not None, (
        "last_plot_frame must be populated even with zero subscribers — "
        "regression guard for the critical bug where _on_to_plot short-circuited"
    )
    assert session.last_plot_timestamp is not None
    assert "series" in session.last_plot_frame
    assert "lock_indicator" in session.last_plot_frame
    assert "auto_relock" in session.last_plot_frame

    # lock_indicator.update must have been invoked exactly once.
    assert len(update_calls) == 1

    # And because there are no subscribers and auto_relock is not enabled,
    # the outbound publish must NOT have been called.
    assert published == [], (
        f"publish must be gated off when no subscriber needs the frame and "
        f"auto_relock is disabled, but got: {published}"
    )


def test_on_to_plot_publishes_when_subscriber_present():
    """With a subscriber, the frame is published in addition to state updates."""
    session, manager, published = _build_session()

    # Register a fake connection directly in the manager state so
    # peek_required_detail returns "full" without needing a real WebSocket.
    from app.stream import ConnectionState

    fake_ws = object()
    manager._connections["dev-a"] = {
        fake_ws: ConnectionState(max_fps=None, detail="full"),
    }

    assert manager.peek_required_detail("dev-a") == "full"

    session._on_to_plot(_make_unlocked_to_plot())

    assert session.last_plot_frame is not None
    assert len(published) == 1
    assert published[0]["device_key"] == "dev-a"
    assert published[0]["message"] is session.last_plot_frame
