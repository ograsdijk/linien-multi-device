from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.session import DeviceSession


class RecordingManager:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    def publish(self, device_key: str, message: dict[str, Any]) -> None:
        self.published.append((device_key, message))


class FakeParam:
    def __init__(self, value: Any) -> None:
        self.value = value

    def add_callback(self, callback, call_immediately: bool = True) -> None:
        if call_immediately:
            callback(self.value)


class FakeParameters:
    """Mimics linien_client's parameter set: iterating yields (name, param),
    and add_callback(call_immediately=True) replays the current value."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._params = {name: FakeParam(value) for name, value in values.items()}

    def __iter__(self):
        return iter(self._params.items())


def _make_session(values: dict[str, Any]) -> tuple[DeviceSession, RecordingManager]:
    device = SimpleNamespace(
        key="dev-1", name="dev-1", host="127.0.0.1", port=18862, parameters={}
    )
    manager = RecordingManager()
    session = DeviceSession(device, manager)
    session.parameters = FakeParameters(values)
    return session, manager


def test_connect_replay_publishes_one_snapshot_not_a_param_update_burst():
    """The call_immediately=True replay must not flood subscriber queues.

    Each subscriber's reliable queue is bounded and overflowing it drops the
    connection, so ~90 individual param_update publishes on every connect
    could evict live subscribers.
    """
    values = {f"p{index}": index for index in range(90)}
    session, manager = _make_session(values)

    session._register_callbacks()

    types = [message["type"] for _key, message in manager.published]
    assert types.count("param_update") == 0
    assert types.count("param_snapshot") == 1
    _key, snapshot = next(
        (key, msg) for key, msg in manager.published if msg["type"] == "param_snapshot"
    )
    assert snapshot["params"] == values


def test_param_updates_after_registration_still_publish_individually():
    session, manager = _make_session({"p0": 0})
    session._register_callbacks()
    manager.published.clear()

    session._on_param_changed("p0", 7)

    assert manager.published == [
        ("dev-1", {"type": "param_update", "name": "p0", "value": 7})
    ]


def test_suppression_flag_is_cleared_when_registration_raises():
    session, manager = _make_session({"p0": 0})

    class Boom(FakeParameters):
        def __iter__(self):
            raise RuntimeError("registration failed")

    session.parameters = Boom({})
    try:
        session._register_callbacks()
    except RuntimeError:
        pass
    assert session._suppress_param_publish is False
