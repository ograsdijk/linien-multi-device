"""status() must surface the cached Red Pitaya telemetry -- and nothing else.

The telemetry fields come from RpTelemetryManager's in-memory cache via a
provider callback. status() is on the /api/devices/statuses fan-out path, so it
must never make a remote call, and a broken provider must not take the whole
status payload down with it.
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


def test_status_has_no_telemetry_fields_without_a_provider() -> None:
    status = _make_session().status()
    assert "rp_temperature_c" not in status
    assert "rp_telemetry" not in status


def test_status_merges_the_cached_telemetry_fields() -> None:
    session = _make_session()
    session.set_telemetry_provider(
        lambda key: {
            "rp_temperature_c": 57.3,
            "rp_temperature_sampled_at": 1700.0,
            "rp_telemetry": {"state": "running", "version": "1.0.0"},
        }
    )

    status = session.status()

    assert status["rp_temperature_c"] == 57.3
    assert status["rp_temperature_sampled_at"] == 1700.0
    assert status["rp_telemetry"]["state"] == "running"
    # The pre-existing payload is untouched.
    assert status["connected"] is False
    assert "diagnosis" in status and "recovery" in status


def test_provider_receives_the_device_key() -> None:
    session = _make_session()
    seen: list[str] = []
    session.set_telemetry_provider(lambda key: seen.append(key) or {})
    session.status()
    assert seen == ["dev-a"]


def test_a_failing_provider_does_not_break_status() -> None:
    session = _make_session()

    def boom(_key):
        raise RuntimeError("cache exploded")

    session.set_telemetry_provider(boom)

    status = session.status()

    assert status["connected"] is False
    assert "rp_temperature_c" not in status


def test_provider_can_be_cleared() -> None:
    session = _make_session()
    session.set_telemetry_provider(lambda key: {"rp_temperature_c": 1.0})
    assert session.status()["rp_temperature_c"] == 1.0
    session.set_telemetry_provider(None)
    assert "rp_temperature_c" not in session.status()
