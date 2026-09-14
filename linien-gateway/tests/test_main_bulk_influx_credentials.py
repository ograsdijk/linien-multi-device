"""GET /api/devices/logging/credentials -- the whole fleet in one call.

Reading them one key at a time forced the operator to select every board in
the UI dropdown before its settings were readable, and since that endpoint is
also what primes the telemetry credential cache, boards nobody clicked stayed
unprimed.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as main


def _device(key: str) -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        name=key,
        host="10.0.0.1",
        port=18862,
        username="root",
        password="root",
        parameters={},
    )


def _credentials(measurement: str) -> SimpleNamespace:
    return SimpleNamespace(
        url="http://influx:8086",
        org="lab",
        token="secret",
        bucket="linien",
        measurement=measurement,
    )


class _FakeSession:
    def __init__(self, key: str, *, connected: bool = True, behaviour=None) -> None:
        self.key = key
        self.connected = connected
        self._behaviour = behaviour

    def logging_get_credentials(self):
        if self._behaviour is not None:
            return self._behaviour()
        return _credentials(self.key)


@pytest.fixture
def fleet(monkeypatch):
    """Three devices with per-key session behaviour the test can swap in."""
    sessions: dict[str, _FakeSession] = {}
    devices = [_device("dev-1"), _device("dev-2"), _device("dev-3")]
    monkeypatch.setattr(main.device_store, "list_devices", lambda: devices)
    monkeypatch.setattr(
        main, "_session_for_device", lambda device: sessions[device.key]
    )
    primed: list[tuple[str, object]] = []
    monkeypatch.setattr(
        main.telemetry_manager,
        "set_influx_credentials",
        lambda key, credentials: primed.append((key, credentials)),
    )
    for device in devices:
        sessions[device.key] = _FakeSession(device.key)
    return SimpleNamespace(sessions=sessions, devices=devices, primed=primed)


def test_returns_every_device_in_one_call(fleet):
    payload = TestClient(main.app).get("/api/devices/logging/credentials").json()

    assert set(payload) == {"dev-1", "dev-2", "dev-3"}
    assert payload["dev-2"]["connected"] is True
    assert payload["dev-2"]["credentials"]["measurement"] == "dev-2"
    assert payload["dev-2"]["error"] is None


def test_primes_the_telemetry_cache_for_the_whole_fleet(fleet):
    TestClient(main.app).get("/api/devices/logging/credentials")

    # The point of the endpoint: no board is left unprimed just because nobody
    # selected it in the dropdown.
    assert sorted(key for key, _ in fleet.primed) == ["dev-1", "dev-2", "dev-3"]


def test_disconnected_devices_are_reported_not_skipped(fleet):
    fleet.sessions["dev-2"] = _FakeSession("dev-2", connected=False)

    payload = TestClient(main.app).get("/api/devices/logging/credentials").json()

    # The UI needs to say "offline", which it cannot do if the key is missing.
    assert payload["dev-2"] == {
        "connected": False,
        "credentials": None,
        "error": None,
    }
    assert payload["dev-1"]["credentials"] is not None
    assert [key for key, _ in fleet.primed] == ["dev-1", "dev-3"]


def test_one_failing_board_does_not_fail_the_batch(fleet):
    def boom():
        raise RuntimeError("RPyC exploded")

    fleet.sessions["dev-1"] = _FakeSession("dev-1", behaviour=boom)

    payload = TestClient(main.app).get("/api/devices/logging/credentials").json()

    assert payload["dev-1"]["credentials"] is None
    assert "RPyC exploded" in payload["dev-1"]["error"]
    assert payload["dev-3"]["credentials"]["measurement"] == "dev-3"


def test_a_wedged_board_is_bounded_and_the_rest_still_answer(fleet, monkeypatch):
    monkeypatch.setattr(main, "BULK_CREDENTIALS_TIMEOUT_S", 0.05)
    # Released only after the assertions: until then this stands in for a board
    # whose RPyC lock is held indefinitely.
    release = threading.Event()

    def wedged():
        release.wait(timeout=10.0)
        return _credentials("dev-1")

    fleet.sessions["dev-1"] = _FakeSession("dev-1", behaviour=wedged)

    # Driven on a loop of our own rather than through TestClient: tearing a
    # TestClient down joins the default executor, so the timing would measure
    # the stuck worker thread instead of the handler it is supposed to bound.
    loop = asyncio.new_event_loop()
    try:
        started = time.monotonic()
        payload = loop.run_until_complete(main.get_all_logging_credentials())
        elapsed = time.monotonic() - started

        assert elapsed < 2.0, "one wedged board held up the whole fleet"
        assert payload["dev-1"]["credentials"] is None
        assert "Timed out" in payload["dev-1"]["error"]
        assert payload["dev-2"]["credentials"]["measurement"] == "dev-2"
    finally:
        release.set()
        loop.close()
