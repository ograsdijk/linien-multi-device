from __future__ import annotations

import copy

import pytest
from linien_client import device as linien_device
from linien_client.device import Device

from app import device_store
from app.session import DeviceSession, PERSISTENT_SETTINGS_SNAPSHOT_KEY
from app.stream import WebsocketManager


class _Param:
    """A Linien parameter as linien-client exposes it.

    `cached` is what linien-client's local cache holds. Setting `value` goes
    to the board but does not update that cache, just like linien-client, and
    `call_immediately` callbacks report the cached value.
    """

    def __init__(self, value, *, restorable: bool = False):
        self.board = value
        self.cached = value
        self.restorable = restorable

    @property
    def value(self):
        return self.board

    @value.setter
    def value(self, new):
        self.board = new

    def add_callback(self, callback, call_immediately: bool = False) -> None:
        if call_immediately:
            callback(self.cached)


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


@pytest.fixture
def store(tmp_path, monkeypatch):
    """devices.json in a temp folder, so tests never touch the real one."""
    path = tmp_path / "devices.json"
    monkeypatch.setattr(device_store, "load_device_list", lambda: linien_device.load_device_list(path))
    monkeypatch.setattr(device_store, "add_device", lambda d: linien_device.add_device(d, path))
    monkeypatch.setattr(device_store, "update_device", lambda d: linien_device.update_device(d, path))
    device_store._invalidate_cache()
    yield path
    device_store._invalidate_cache()


def _snapshot(values: dict) -> dict:
    return {"version": 1, "updated_at": "2026-01-01T00:00:00+00:00", "values": values}


def _stored_values(key: str = "dev-1") -> dict | None:
    _stored, snapshot = device_store.get_settings_snapshot(key)
    return None if snapshot is None else snapshot["values"]


def _make_session(parameters: dict | None = None) -> DeviceSession:
    device = Device(key="dev-1", name="dev-1", host="127.0.0.1", parameters=parameters or {})
    device_store.save_device(device)
    return DeviceSession(device_store.get_device("dev-1"), WebsocketManager())


def _install_remote(session: DeviceSession, params: _Params) -> _Control:
    control = _Control()
    session.parameters = params
    session.control = control
    session._refresh_persistent_param_names_locked()
    return control


def _capture_log_events(session: DeviceSession) -> list[tuple]:
    events: list[tuple] = []
    session.set_log_event_callback(lambda *args: events.append(args))
    return events


def test_persistent_settings_seed_from_remote_on_first_connect(store):
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

    expected = {"p": 50, "target_slope_rising": False}
    assert session.device.parameters[PERSISTENT_SETTINGS_SNAPSHOT_KEY]["values"] == expected
    assert _stored_values() == expected
    assert control.write_count == 0


def test_persistent_settings_replay_to_remote_on_reconnect(store):
    session = _make_session(
        {PERSISTENT_SETTINGS_SNAPSHOT_KEY: _snapshot({"p": 75, "target_slope_rising": True})}
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


def test_gateway_set_param_updates_persistent_snapshot(store):
    session = _make_session({PERSISTENT_SETTINGS_SNAPSHOT_KEY: _snapshot({"p": 50})})
    params = _Params(p=_Param(50, restorable=True))
    control = _install_remote(session, params)

    session.set_param("p", 60, write_registers=False)

    assert params.p.value == 60
    assert control.write_count == 0
    assert session.device.parameters[PERSISTENT_SETTINGS_SNAPSHOT_KEY]["values"]["p"] == 60
    assert _stored_values()["p"] == 60


def test_live_remote_change_is_adopted_into_persistent_snapshot(store):
    session = _make_session({PERSISTENT_SETTINGS_SNAPSHOT_KEY: _snapshot({"p": 50})})
    _install_remote(session, _Params(p=_Param(50, restorable=True)))

    session._on_param_changed("p", 65)

    assert _stored_values()["p"] == 65


def test_gateway_write_echo_is_not_re_adopted(store):
    session = _make_session({PERSISTENT_SETTINGS_SNAPSHOT_KEY: _snapshot({"p": 50})})
    _install_remote(session, _Params(p=_Param(50, restorable=True)))
    events = _capture_log_events(session)

    session.set_param("p", 70, write_registers=False)
    changes_after_write = len(events)
    session._on_param_changed("p", 70)

    assert len(events) == changes_after_write
    assert "p" not in session._pending_gateway_param_writes
    assert _stored_values()["p"] == 70


def test_replay_active_suppresses_remote_default_adoption(store):
    session = _make_session({PERSISTENT_SETTINGS_SNAPSHOT_KEY: _snapshot({"p": 50})})
    _install_remote(session, _Params(p=_Param(0, restorable=True)))

    session._persistent_replay_active = True
    session._on_param_changed("p", 0)

    assert _stored_values()["p"] == 50


def test_stale_device_save_does_not_write_an_old_snapshot_back(store):
    """A whole-record save from an older copy must not revert the snapshot.

    This is how a restart could put old demodulation settings back on a
    board: the setting was saved, then another save from a copy taken
    earlier (last-healthy, recovery, influx logging, config sync, device
    edit) wrote the old snapshot over it, and the next connect replayed it.
    """
    session = _make_session(
        {PERSISTENT_SETTINGS_SNAPSHOT_KEY: _snapshot({"modulation_frequency": 603979776})}
    )
    stale_copy = device_store.get_device("dev-1")
    params = _Params(modulation_frequency=_Param(603979776, restorable=True))
    _install_remote(session, params)

    session.set_param("modulation_frequency", 503316480, write_registers=False)
    assert _stored_values()["modulation_frequency"] == 503316480

    # A request swaps in an older copy, then the session saves it.
    session.device = stale_copy
    session._persist_last_healthy()
    # And an unrelated whole-record save from another old copy.
    other = copy.deepcopy(stale_copy)
    other.parameters["influx_logging_state"] = {"enabled": False}
    device_store.save_device(other)

    assert _stored_values()["modulation_frequency"] == 503316480
    stored = device_store.get_device("dev-1")
    assert stored.parameters["influx_logging_state"] == {"enabled": False}
    assert stored.parameters["gateway_last_healthy"] is not None

    # The next connect restores the newer value, not the old one.
    restarted = DeviceSession(device_store.get_device("dev-1"), WebsocketManager())
    board = _Params(modulation_frequency=_Param(1, restorable=True))
    _install_remote(restarted, board)
    restarted._seed_or_replay_persistent_settings_locked()
    assert board.modulation_frequency.value == 503316480


def test_initial_callbacks_after_replay_keep_the_replayed_snapshot(store):
    """The first callbacks report linien-client's pre-replay cache; don't save that."""
    saved = {"modulation_frequency": 503316480, "demodulation_phase_a": 75}
    session = _make_session({PERSISTENT_SETTINGS_SNAPSHOT_KEY: _snapshot(saved)})
    params = _Params(
        modulation_frequency=_Param(603979776, restorable=True),
        demodulation_phase_a=_Param(50, restorable=True),
    )
    _install_remote(session, params)

    session._seed_or_replay_persistent_settings_locked()
    assert params.modulation_frequency.value == 503316480
    # linien-client's cache still holds the board's old values here.
    session._register_callbacks()
    assert _stored_values() == saved

    # The server then reports the replayed values: echoes, not changes.
    session._on_param_changed("modulation_frequency", 503316480)
    session._on_param_changed("demodulation_phase_a", 75)
    assert _stored_values() == saved
    assert session._pending_gateway_param_writes == {}

    # A real change made on the board afterwards is still adopted.
    session._on_param_changed("demodulation_phase_a", 80)
    assert _stored_values()["demodulation_phase_a"] == 80


def test_replay_and_snapshot_changes_are_logged_with_old_and_new_values(store):
    session = _make_session({PERSISTENT_SETTINGS_SNAPSHOT_KEY: _snapshot({"p": 75})})
    events = _capture_log_events(session)
    _install_remote(session, _Params(p=_Param(50, restorable=True)))

    session._seed_or_replay_persistent_settings_locked()
    session.set_param("p", 80, write_registers=False)

    by_code = {event[2]: event for event in events}
    replayed = by_code["settings_snapshot_replayed"]
    assert replayed[5] == {"replayed": {"p": {"board": 50, "saved": 75}}}
    changed = by_code["settings_snapshot_changed"]
    assert changed[5] == {"reason": "gateway_write", "changes": {"p": {"old": 75, "new": 80}}}
    assert "p: 75 -> 80" in changed[3]


def test_restorable_parameter_missing_from_snapshot_is_added_from_board(store):
    session = _make_session({PERSISTENT_SETTINGS_SNAPSHOT_KEY: _snapshot({"p": 50})})
    params = _Params(p=_Param(50, restorable=True), i=_Param(7, restorable=True))
    _install_remote(session, params)

    session._seed_or_replay_persistent_settings_locked()

    assert _stored_values() == {"p": 50, "i": 7}


def test_snapshot_keeps_a_history_of_changes_and_replays(store):
    """Earlier values stay recoverable from devices.json after the fact."""
    session = _make_session({PERSISTENT_SETTINGS_SNAPSHOT_KEY: _snapshot({"modulation_frequency": 603979776})})
    params = _Params(modulation_frequency=_Param(1, restorable=True))
    _install_remote(session, params)

    session._seed_or_replay_persistent_settings_locked()
    session.set_param("modulation_frequency", 503316480, write_registers=False)
    session._on_param_changed("modulation_frequency", 469762048)

    _stored, snapshot = device_store.get_settings_snapshot("dev-1")
    history = [(entry["reason"], entry["changes"]["modulation_frequency"]) for entry in snapshot["history"]]
    assert history == [
        ("replayed_onto_board", {"old": 1, "new": 603979776}),
        ("gateway_write", {"old": 603979776, "new": 503316480}),
        ("board_change", {"old": 503316480, "new": 469762048}),
    ]
    assert all(entry["at"] for entry in snapshot["history"])


def test_snapshot_history_is_bounded(store, monkeypatch):
    monkeypatch.setattr("app.session.SETTINGS_SNAPSHOT_HISTORY_LIMIT", 3)
    session = _make_session({PERSISTENT_SETTINGS_SNAPSHOT_KEY: _snapshot({"p": 0})})
    _install_remote(session, _Params(p=_Param(0, restorable=True)))

    for value in range(1, 6):
        session.set_param("p", value, write_registers=False)

    _stored, snapshot = device_store.get_settings_snapshot("dev-1")
    assert [entry["changes"]["p"]["new"] for entry in snapshot["history"]] == [3, 4, 5]
