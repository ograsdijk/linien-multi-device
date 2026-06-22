from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.session import DeviceSession


class RecordingManager:
    """Minimal WebsocketManager stand-in that records publish() calls."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    def publish(self, device_key: str, message: dict[str, Any]) -> None:
        self.published.append((device_key, message))


def _make_session() -> tuple[DeviceSession, RecordingManager]:
    device = SimpleNamespace(
        key="dev-1",
        name="dev-1",
        host="127.0.0.1",
        port=18862,
        parameters={},
    )
    manager = RecordingManager()
    return DeviceSession(device, manager), manager


def _status_messages(manager: RecordingManager) -> list[dict[str, Any]]:
    return [msg for _key, msg in manager.published if msg.get("type") == "status"]


def test_publish_status_emits_keyed_status_payload():
    session, manager = _make_session()

    session._publish_status()

    assert manager.published, "expected a publish() call"
    key, message = manager.published[-1]
    assert key == "dev-1"
    assert message["type"] == "status"
    assert message["connected"] is False
    assert message["connecting"] is False


def test_disconnect_publishes_disconnected_status():
    """An intentional disconnect must tell streaming clients connected=False so
    the UI leaves its greyed-out state without waiting for a backstop poll."""
    session, manager = _make_session()
    session.connected = True

    session._reset_connection_state(last_error="bye", request_diagnosis=False)

    statuses = _status_messages(manager)
    assert statuses, "disconnect should publish a status update"
    assert statuses[-1]["connected"] is False
    assert statuses[-1]["last_error"] == "bye"
    # Intentional disconnect clears diagnosis before publishing.
    assert statuses[-1]["diagnosis"] is None


def test_poll_failure_reset_publishes_status():
    """An unexpected drop (poll failure / failed connect) also publishes the
    disconnected status; the diagnosis probe is requested out of band."""
    session, manager = _make_session()
    session.connected = True

    session._reset_connection_state(last_error="boom", request_diagnosis=True)

    statuses = _status_messages(manager)
    assert statuses, "poll-failure reset should publish a status update"
    assert statuses[-1]["connected"] is False
    assert statuses[-1]["last_error"] == "boom"
    assert session.wants_diagnosis() is True
