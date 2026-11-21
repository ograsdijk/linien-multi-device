"""All-devices lock history, and comparing/restoring a record's saved settings."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import app.main as main
from app.session import DeviceSession
from app.stream import WebsocketManager
from tests.test_lock_acquisition_api import _record, client, service  # noqa: F401
from tests.test_session_persistent_settings import _Control, _Param, _Params


def _session_with(**values) -> tuple[DeviceSession, _Params, _Control]:
    device = SimpleNamespace(key="dev-1", name="dev-1", host="127.0.0.1", port=18862, parameters={})
    session = DeviceSession(device, WebsocketManager())
    params = _Params(
        lock=_Param(values.pop("lock", False)),
        **{name: _Param(value, restorable=True) for name, value in values.items()},
    )
    control = _Control()
    session.parameters = params
    session.control = control
    return session, params, control


def _saved_record(saved: dict, device_key: str = "dev-1") -> dict:
    row = _record(device_key=device_key)
    row["linien_config"] = {"schema_version": 1, "parameters": saved}
    return row


@pytest.fixture
def board(monkeypatch):
    holder = {}

    def use(**values):
        session, params, control = _session_with(**values)
        monkeypatch.setattr(main, "_get_session", lambda key: session)
        holder.update(session=session, params=params, control=control)
        return params, control

    return use


def test_all_devices_list_spans_devices_and_filters_by_device(service, client):
    now = time.time()
    rows = [_record(started=now - 30), _record(device_key="dev-2", started=now - 10)]
    for row in rows:
        service.enqueue_lock_result(row)

    body = client.get("/api/lock-acquisitions").json()
    assert body["device_key"] is None
    assert [i["device_key"] for i in body["items"]] == ["dev-2", "dev-1"]

    one = client.get("/api/lock-acquisitions?device_key=dev-1").json()
    assert [i["lock_acquisition_id"] for i in one["items"]] == [rows[0]["lock_acquisition_id"]]
    assert client.get("/api/lock-acquisitions?lock_source=nltl").status_code == 422


def test_settings_compare_lists_then_and_now(service, client, board):
    record = _saved_record({"modulation_frequency": 603979776, "demodulation_phase_a": 50})
    service.enqueue_lock_result(record)
    board(modulation_frequency=503316480, demodulation_phase_a=50)

    body = client.get(
        f"/api/devices/dev-1/lock-acquisitions/{record['lock_acquisition_id']}/settings"
    ).json()

    assert body["locked"] is False
    rows = {row["name"]: row for row in body["parameters"]}
    assert rows["modulation_frequency"] == {
        "name": "modulation_frequency", "at_lock": 603979776, "now": 503316480,
        "restorable": True, "differs": True,
    }
    assert rows["demodulation_phase_a"]["differs"] is False


def test_restore_writes_only_differing_restorable_settings_once(service, client, board):
    record = _saved_record(
        {"modulation_frequency": 603979776, "demodulation_phase_a": 50, "p": 50}
    )
    service.enqueue_lock_result(record)
    params, control = board(modulation_frequency=503316480, demodulation_phase_a=50, p=10)

    response = client.post(
        f"/api/devices/dev-1/lock-acquisitions/{record['lock_acquisition_id']}/restore-settings"
    )

    assert response.status_code == 200, response.text
    assert response.json()["applied"] == [
        {"name": "modulation_frequency", "old": 503316480, "new": 603979776},
        {"name": "p", "old": 10, "new": 50},
    ]
    assert params.modulation_frequency.value == 603979776
    assert params.p.value == 50
    assert control.write_count == 1


def test_restore_can_be_limited_to_named_settings(service, client, board):
    record = _saved_record({"modulation_frequency": 603979776, "p": 50})
    service.enqueue_lock_result(record)
    params, _control = board(modulation_frequency=503316480, p=10)

    response = client.post(
        f"/api/devices/dev-1/lock-acquisitions/{record['lock_acquisition_id']}/restore-settings",
        json={"names": ["p"]},
    )

    assert [row["name"] for row in response.json()["applied"]] == ["p"]
    assert params.modulation_frequency.value == 503316480


def test_restore_is_refused_while_locked(service, client, board):
    record = _saved_record({"modulation_frequency": 603979776})
    service.enqueue_lock_result(record)
    params, control = board(modulation_frequency=503316480, lock=True)

    response = client.post(
        f"/api/devices/dev-1/lock-acquisitions/{record['lock_acquisition_id']}/restore-settings"
    )

    assert response.status_code == 409
    assert "locked" in response.json()["detail"]
    assert params.modulation_frequency.value == 503316480
    assert control.write_count == 0


def test_restore_refuses_another_devices_record(service, client, board):
    record = _saved_record({"p": 50}, device_key="dev-2")
    service.enqueue_lock_result(record)
    board(p=10)

    response = client.post(
        f"/api/devices/dev-1/lock-acquisitions/{record['lock_acquisition_id']}/restore-settings"
    )

    assert response.status_code == 409
    assert "another device" in response.json()["detail"]
