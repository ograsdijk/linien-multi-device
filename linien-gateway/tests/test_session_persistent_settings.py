from __future__ import annotations

import copy
from types import SimpleNamespace

from app.session import DeviceSession, PERSISTENT_SETTINGS_SNAPSHOT_KEY
from app.stream import WebsocketManager


class _Param:
    def __init__(self, value, *, restorable: bool = False):
        self.value = value
        self.restorable = restorable

    def add_callback(self, *_args, **_kwargs) -> None:
        return None


class _Params:
    def __init__(self, **params: _Param):
        self._params = params
        for name, param in params.items():
            setattr(self, name, param)

    def __iter__(self):
        return iter(self._params.items())


class _Control:
    def __init__(self) -> None:
        self.write_count = 0

    def exposed_write_registers(self) -> None:
        self.write_count += 1


def _make_session(parameters: dict | None = None) -> DeviceSession:
    device = SimpleNamespace(
        key="dev-1",
        name="dev-1",
        host="127.0.0.1",
        port=18862,
        parameters=parameters or {},
    )
    return DeviceSession(device, WebsocketManager())


def _install_remote(session: DeviceSession, params: _Params) -> _Control:
    control = _Control()
    session.parameters = params
    session.control = control
    return control


def test_persistent_settings_seed_from_remote_on_first_connect(monkeypatch):
    saved = []
    monkeypatch.setattr(
        "app.session.device_store.save_device",
        lambda device: saved.append(copy.deepcopy(device.parameters)),
    )
    session = _make_session()
    control = _install_remote(
        session,
        _Params(
            p=_Param(50, restorable=True),
            target_slope_rising=_Param(False, restorable=False),
            lock=_Param(True, restorable=False),
        ),
    )

    session._seed_or_replay_persistent_settings_locked()

    snapshot = session.device.parameters[PERSISTENT_SETTINGS_SNAPSHOT_KEY]
    assert snapshot["values"] == {"p": 50, "target_slope_rising": False}
    assert control.write_count == 0
    assert saved[-1][PERSISTENT_SETTINGS_SNAPSHOT_KEY]["values"] == snapshot["values"]


def test_persistent_settings_replay_to_remote_on_reconnect(monkeypatch):
    monkeypatch.setattr("app.session.device_store.save_device", lambda _device: None)
    session = _make_session(
        {
            PERSISTENT_SETTINGS_SNAPSHOT_KEY: {
                "version": 1,
                "values": {"p": 75, "target_slope_rising": True},
            }
        }
    )
    params = _Params(
        p=_Param(50, restorable=True),
        target_slope_rising=_Param(False, restorable=False),
    )
    control = _install_remote(session, params)

    session._seed_or_replay_persistent_settings_locked()

    assert params.p.value == 75
    assert params.target_slope_rising.value is True
    assert control.write_count == 1


def test_gateway_set_param_updates_persistent_snapshot(monkeypatch):
    monkeypatch.setattr("app.session.device_store.save_device", lambda _device: None)
    session = _make_session(
        {
            PERSISTENT_SETTINGS_SNAPSHOT_KEY: {
                "version": 1,
                "values": {"p": 50},
            }
        }
    )
    params = _Params(p=_Param(50, restorable=True))
    control = _install_remote(session, params)
    session._refresh_persistent_param_names_locked()

    session.set_param("p", 60, write_registers=False)

    assert params.p.value == 60
    assert control.write_count == 0
    assert session.device.parameters[PERSISTENT_SETTINGS_SNAPSHOT_KEY]["values"]["p"] == 60


def test_live_remote_change_is_adopted_into_persistent_snapshot(monkeypatch):
    monkeypatch.setattr("app.session.device_store.save_device", lambda _device: None)
    session = _make_session(
        {
            PERSISTENT_SETTINGS_SNAPSHOT_KEY: {
                "version": 1,
                "values": {"p": 50},
            }
        }
    )
    _install_remote(session, _Params(p=_Param(50, restorable=True)))
    session._refresh_persistent_param_names_locked()

    session._on_param_changed("p", 65)

    assert session.device.parameters[PERSISTENT_SETTINGS_SNAPSHOT_KEY]["values"]["p"] == 65


def test_gateway_write_echo_is_not_re_adopted(monkeypatch):
    saved = []
    monkeypatch.setattr(
        "app.session.device_store.save_device",
        lambda device: saved.append(copy.deepcopy(device.parameters)),
    )
    session = _make_session(
        {
            PERSISTENT_SETTINGS_SNAPSHOT_KEY: {
                "version": 1,
                "values": {"p": 50},
            }
        }
    )
    _install_remote(session, _Params(p=_Param(50, restorable=True)))
    session._refresh_persistent_param_names_locked()

    session.set_param("p", 70, write_registers=False)
    saved_after_write = len(saved)
    session._on_param_changed("p", 70)

    assert len(saved) == saved_after_write
    assert "p" not in session._pending_gateway_param_writes


def test_replay_active_suppresses_remote_default_adoption(monkeypatch):
    monkeypatch.setattr("app.session.device_store.save_device", lambda _device: None)
    session = _make_session(
        {
            PERSISTENT_SETTINGS_SNAPSHOT_KEY: {
                "version": 1,
                "values": {"p": 50},
            }
        }
    )
    _install_remote(session, _Params(p=_Param(0, restorable=True)))
    session._refresh_persistent_param_names_locked()

    session._persistent_replay_active = True
    session._on_param_changed("p", 0)

    assert session.device.parameters[PERSISTENT_SETTINGS_SNAPSHOT_KEY]["values"]["p"] == 50
