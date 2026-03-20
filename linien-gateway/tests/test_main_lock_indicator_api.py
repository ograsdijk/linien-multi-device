from fastapi.testclient import TestClient

import app.main as main


class DummySession:
    def __init__(self) -> None:
        self.config = {
            "enabled": True,
            "bad_hold_s": 1.0,
            "good_hold_s": 2.0,
            "use_control": True,
            "control_stuck_delta_counts": 0,
            "control_stuck_time_s": 1.5,
            "control_rail_threshold_v": 0.9,
            "control_rail_hold_s": 1.0,
            "use_error": True,
            "error_mean_abs_max_v": 0.2,
            "error_std_min_v": 0.001,
            "error_std_max_v": 0.8,
            "use_monitor": False,
            "monitor_mode": "locked_above",
            "monitor_threshold_v": 0.0,
        }
        self.last_payload = None

    def get_lock_indicator_config(self):
        return self.config

    def update_lock_indicator_config(self, payload):
        self.last_payload = payload
        self.config = {**self.config, **payload}
        return self.config


def test_get_lock_indicator_config(monkeypatch):
    session = DummySession()
    monkeypatch.setattr(main, "_get_session", lambda _key: session)
    client = TestClient(main.app)

    response = client.get("/api/devices/test-device/lock-indicator")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert "control_stuck_time_s" in body


def test_put_lock_indicator_config_persists_and_updates_session(monkeypatch):
    class DummyDevice:
        key = "test-device"
        parameters = {}

    saved_devices = []
    session = DummySession()
    device = DummyDevice()

    monkeypatch.setattr(main.device_store, "get_device", lambda _key: device)
    monkeypatch.setattr(main.device_store, "save_device", lambda dev: saved_devices.append(dev))
    monkeypatch.setattr(main.device_config_store, "set_config", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(main, "_session_for_device", lambda _device: session)
    client = TestClient(main.app)

    payload = {
        "enabled": True,
        "bad_hold_s": 0.5,
        "good_hold_s": 1.5,
        "use_control": True,
        "control_stuck_delta_counts": 0,
        "control_stuck_time_s": 1.0,
        "control_rail_threshold_v": 0.85,
        "control_rail_hold_s": 0.6,
        "use_error": True,
        "error_mean_abs_max_v": 0.3,
        "error_std_min_v": 0.002,
        "error_std_max_v": 0.7,
        "use_monitor": True,
        "monitor_mode": "locked_below",
        "monitor_threshold_v": -0.15,
    }

    response = client.put("/api/devices/test-device/lock-indicator", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["monitor_mode"] == "locked_below"
    assert session.last_payload is not None
    assert session.last_payload["error_mean_abs_max_v"] == 0.3
    assert "lock_indicator_config" in device.parameters
    assert device.parameters["lock_indicator_config"]["control_rail_threshold_v"] == 0.85
    assert saved_devices
