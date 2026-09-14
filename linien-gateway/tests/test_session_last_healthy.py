"""What the gateway knows about a board must outlive the gateway process.

A Red Pitaya rebooted at 12:49, taking its FPGA lock with it. The gateway was
restarted afterwards, which erased the in-memory `_last_connected_at`, so the
exact reboot test (`uptime < our absence`) had nothing to compare against and
fell back to the 600 s uptime threshold. Two hours of uptime sailed past it and
the operator was told "linien-server is down; the FPGA is running but not
locked" about a board whose lock had been gone since before lunch.

A gateway restart is not a board event, so neither half of the evidence may be
lost to one.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from app import session as session_module
from app.session import LAST_HEALTHY_KEY, DeviceSession
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


def test_the_last_connected_time_survives_a_gateway_restart():
    an_hour_ago = time.time() - 3600.0
    session = _make_session({LAST_HEALTHY_KEY: {"at": an_hour_ago, "boot_id": None}})

    since = session.seconds_since_last_connected()

    assert since is not None
    assert 3590.0 < since < 3610.0


def test_a_device_we_have_never_connected_to_still_reports_nothing():
    """The threshold fallback is for exactly this case and must stay reachable."""
    assert _make_session().seconds_since_last_connected() is None


def test_the_boot_id_survives_a_gateway_restart():
    session = _make_session({LAST_HEALTHY_KEY: {"at": 1.0, "boot_id": "boot-a"}})

    assert session.last_healthy_boot_id() == "boot-a"


def test_a_stored_time_from_the_future_is_clamped():
    """A clock that moved backwards must not make every board look rebooted.

    `seconds_since_last_connected` would go negative, and any uptime at all is
    greater than a negative absence -- inverting the test rather than failing it.
    """
    session = _make_session(
        {LAST_HEALTHY_KEY: {"at": time.time() + 86_400.0, "boot_id": None}}
    )

    since = session.seconds_since_last_connected()

    assert since is not None
    assert since >= 0.0


def test_junk_in_the_store_is_ignored_rather_than_fatal():
    """devices.json is hand-editable, and a session that cannot be built is a
    device that cannot be used."""
    session = _make_session({LAST_HEALTHY_KEY: {"at": "yesterday", "boot_id": 42}})

    assert session.seconds_since_last_connected() is None
    assert session.last_healthy_boot_id() is None


def test_a_missing_record_is_not_an_error():
    session = _make_session({LAST_HEALTHY_KEY: "not a dict"})

    assert session.seconds_since_last_connected() is None


def test_persisting_writes_both_halves_to_the_device(monkeypatch):
    saved: list = []
    monkeypatch.setattr(session_module.device_store, "save_device", saved.append)
    session = _make_session()
    session._last_connected_at = 1234.0
    session._last_healthy_boot_id = "boot-b"

    session._persist_last_healthy()

    assert saved
    assert session.device.parameters[LAST_HEALTHY_KEY] == {
        "at": 1234.0,
        "boot_id": "boot-b",
    }
