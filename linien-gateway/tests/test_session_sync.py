from __future__ import annotations

from types import SimpleNamespace

from app.auto_relock import AutoRelockConfig
from app.lock_indicator import LockIndicatorConfig
from app.session import DeviceSession
from app.stream import WebsocketManager


class _ConfigSpy:
    def __init__(self, initial: dict):
        self._config = dict(initial)
        self.set_calls = 0

    def get_config(self) -> dict:
        return dict(self._config)

    def set_config(self, payload) -> dict:
        self.set_calls += 1
        self._config = dict(payload)
        return self.get_config()


def _make_session(parameters: dict) -> DeviceSession:
    device = SimpleNamespace(
        key="dev-1",
        name="dev-1",
        host="127.0.0.1",
        port=18862,
        parameters=parameters,
    )
    return DeviceSession(device, WebsocketManager())


def test_sync_config_does_not_reset_when_device_config_unchanged():
    lock_cfg = LockIndicatorConfig(enabled=True, error_std_min_v=0.01).to_dict()
    relock_cfg = AutoRelockConfig(enabled=True, trigger_hold_s=1.2).to_dict()
    session = _make_session(
        {
            "lock_indicator_config": dict(lock_cfg),
            "auto_relock_config": dict(relock_cfg),
            "auto_lock_scan_settings": {"half_range_v": 0.5},
            "influx_logging_state": {"enabled": True, "interval_s": 1.0},
        }
    )

    lock_spy = _ConfigSpy(lock_cfg)
    relock_spy = _ConfigSpy(relock_cfg)
    session.lock_indicator = lock_spy  # type: ignore[assignment]
    session.auto_relock = relock_spy  # type: ignore[assignment]
    session.auto_lock_scan_settings = {"half_range_v": 0.5}
    session.influx_logging_state = {"enabled": True, "interval_s": 1.0}

    session.sync_lock_indicator_config_from_device()

    assert lock_spy.set_calls == 0
    assert relock_spy.set_calls == 0


def test_sync_config_applies_when_device_config_changed():
    initial_lock_cfg = LockIndicatorConfig(enabled=True, error_std_min_v=0.01).to_dict()
    initial_relock_cfg = AutoRelockConfig(enabled=True, trigger_hold_s=1.2).to_dict()
    session = _make_session(
        {
            "lock_indicator_config": dict(initial_lock_cfg),
            "auto_relock_config": dict(initial_relock_cfg),
        }
    )

    lock_spy = _ConfigSpy(initial_lock_cfg)
    relock_spy = _ConfigSpy(initial_relock_cfg)
    session.lock_indicator = lock_spy  # type: ignore[assignment]
    session.auto_relock = relock_spy  # type: ignore[assignment]

    session.device.parameters["lock_indicator_config"] = {
        "enabled": True,
        "error_std_min_v": 0.02,
    }
    session.device.parameters["auto_relock_config"] = {
        "enabled": True,
        "trigger_hold_s": 2.5,
    }

    session.sync_lock_indicator_config_from_device()

    assert lock_spy.set_calls == 1
    assert relock_spy.set_calls == 1


def test_influx_logging_state_normalizes_params_from_device():
    session = _make_session(
        {
            "influx_logging_state": {
                "enabled": True,
                "interval_s": "2.5",
                "params": ["p", "i", "p", "", None],
            }
        }
    )

    assert session.influx_logging_state == {
        "enabled": True,
        "interval_s": 2.5,
        "params": ["p", "i"],
        "params_configured": True,
    }


def test_set_influx_logging_state_updates_params():
    session = _make_session({})

    state = session.set_influx_logging_state(
        enabled=True,
        interval_s=0.05,
        params=["d", "p", "d", ""],
        params_configured=True,
    )

    assert state == {
        "enabled": True,
        "interval_s": 0.1,
        "params": ["d", "p"],
        "params_configured": True,
    }


def test_snapshot_returns_copies_of_cached_state():
    session = _make_session({})
    session.param_cache_serialized = {"nested": {"value": 1}}
    session.last_plot_frame = {
        "type": "plot_frame",
        "lock": True,
        "series": {"combined_error": [0.1, 0.2]},
        "signal_power": {"channel1": None, "channel2": None},
        "stats": {"error_std": None, "control_std": None},
        "x_label": "time",
        "x_unit": "us",
    }
    session.last_plot_timestamp = 123.0

    snapshot = session.snapshot()
    snapshot["params"]["nested"]["value"] = 99
    snapshot["plot_frame"]["series"]["combined_error"][0] = 42.0

    assert session.param_cache_serialized["nested"]["value"] == 1
    assert session.last_plot_frame["series"]["combined_error"][0] == 0.1
    assert snapshot["status"]["last_plot"] == 123.0
