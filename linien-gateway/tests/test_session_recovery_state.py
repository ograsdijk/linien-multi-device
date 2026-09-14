"""A finished reboot record must not outlive the condition it describes.

`failed` is a terminal phase and the record is persisted to devices.json, so
nothing ever retracted it: a reboot that timed out while the board was still
coming up left "Reboot failed: Timed out waiting for the Red Pitaya to reboot"
on the device card permanently — even once the board was connected again, and
across gateway restarts.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from app import session as session_module
from app.session import RECOVERY_STATE_KEY, DeviceSession
from app.stream import WebsocketManager


def _make_session(parameters: dict | None = None) -> DeviceSession:
    device = SimpleNamespace(
        key="dev-1",
        name="dev-1",
        host="127.0.0.1",
        port=18862,
        parameters=parameters if parameters is not None else {},
    )
    return DeviceSession(device, WebsocketManager())


def _failed_record() -> dict:
    return {
        "operation_id": "op-1",
        "phase": "failed",
        "updated_at": time.time(),
        "error": "Timed out waiting for the Red Pitaya to reboot",
    }


def test_failed_recovery_is_reported_until_cleared(monkeypatch):
    monkeypatch.setattr(session_module.device_store, "save_device", lambda _d: None)
    session = _make_session({RECOVERY_STATE_KEY: _failed_record()})

    # Before: the card has something to show.
    assert session.status()["recovery"]["phase"] == "failed"

    with session._state_lock:
        session._clear_finished_recovery_locked()

    assert session.status()["recovery"] is None


def test_clearing_a_finished_recovery_unpersists_it(monkeypatch):
    saved: list[object] = []
    monkeypatch.setattr(
        session_module.device_store, "save_device", lambda d: saved.append(d)
    )
    parameters = {RECOVERY_STATE_KEY: _failed_record()}
    session = _make_session(parameters)

    with session._state_lock:
        session._clear_finished_recovery_locked()

    # Must leave devices.json too, or a gateway restart resurrects the message.
    assert RECOVERY_STATE_KEY not in parameters
    assert saved == [session.device]


def test_running_recovery_is_never_cleared(monkeypatch):
    monkeypatch.setattr(session_module.device_store, "save_device", lambda _d: None)
    session = _make_session()
    session._recovery = {
        "operation_id": "op-2",
        "phase": "waiting_for_boot",
        "updated_at": time.time(),
        "error": None,
    }

    with session._state_lock:
        session._clear_finished_recovery_locked()

    # A reboot still in flight owns the record; clearing it would drop the
    # "Rebooting" badge and re-enable the buttons mid-operation.
    assert session._recovery is not None
    assert session._recovery["phase"] == "waiting_for_boot"
    assert session.recovery_active() is True


def test_clear_is_a_noop_without_a_record(monkeypatch):
    saved: list[object] = []
    monkeypatch.setattr(
        session_module.device_store, "save_device", lambda d: saved.append(d)
    )
    session = _make_session()

    with session._state_lock:
        session._clear_finished_recovery_locked()

    assert session._recovery is None
    assert saved == []  # no pointless devices.json write on every connect
