from __future__ import annotations

from types import SimpleNamespace

from app.session import DeviceSession
from app.stream import WebsocketManager


def _make_session() -> DeviceSession:
    device = SimpleNamespace(
        key="dev-1",
        name="dev-1",
        host="127.0.0.1",
        port=18862,
        parameters={},
    )
    return DeviceSession(device, WebsocketManager())


def _diagnosis(category: str = "server_crashed", lock_state: str = "likely_held"):
    return {
        "category": category,
        "lock_state": lock_state,
        "message": f"{category} / {lock_state}",
        "probed_at": 1.0,
        "uptime_s": 3600.0,
        "host_reachable": True,
        "server_running": False,
        "fpga_operating": True,
        "seconds_since_last_connected": 60.0,
    }


def test_apply_diagnosis_is_returned_in_status():
    session = _make_session()
    d = _diagnosis()
    session.apply_diagnosis(d)
    assert session.status()["diagnosis"] == d


def test_apply_diagnosis_emits_log_only_on_category_change():
    events: list[tuple] = []
    session = _make_session()
    session.set_log_event_callback(
        lambda level, source, code, message, key, details: events.append(
            (source, code, details.get("category") if details else None)
        )
    )

    session.apply_diagnosis(_diagnosis("server_crashed"))
    session.apply_diagnosis(_diagnosis("server_crashed", "locked"))  # same category
    session.apply_diagnosis(_diagnosis("rebooted", "lost"))  # changed category

    codes = [code for _src, code, _cat in events]
    assert codes == ["connection_diagnosis", "connection_diagnosis"]
    categories = [cat for _src, _code, cat in events]
    assert categories == ["server_crashed", "rebooted"]


def test_apply_diagnosis_dropped_when_connected():
    session = _make_session()
    session.connected = True
    session.apply_diagnosis(_diagnosis())
    assert session.status()["diagnosis"] is None


def test_request_probe_invokes_injected_callback():
    calls: list[str] = []
    session = _make_session()
    session.set_diagnosis_request_callback(calls.append)

    session.request_diagnosis_probe()

    assert calls == ["dev-1"]
    assert session.wants_diagnosis() is True


def test_reset_with_request_diagnosis_enqueues_probe():
    calls: list[str] = []
    session = _make_session()
    session.set_diagnosis_request_callback(calls.append)

    session._reset_connection_state(last_error="boom", request_diagnosis=True)

    assert calls == ["dev-1"]
    assert session.wants_diagnosis() is True


def test_reset_without_request_clears_diagnosis():
    calls: list[str] = []
    session = _make_session()
    session.set_diagnosis_request_callback(calls.append)
    session.apply_diagnosis(_diagnosis())
    session._wants_diagnosis = True

    session._reset_connection_state(last_error="bye", request_diagnosis=False)

    assert calls == []
    assert session.wants_diagnosis() is False
    assert session.status()["diagnosis"] is None


def test_seconds_since_last_connected_none_before_connect():
    session = _make_session()
    assert session.seconds_since_last_connected() is None
