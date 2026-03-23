from fastapi.testclient import TestClient

import app.main as main


def test_persist_influx_logging_state_keeps_interval_and_params(monkeypatch):
    class DummyDevice:
        key = "test-device"
        parameters = {}

    saved_devices = []
    device = DummyDevice()
    monkeypatch.setattr(main.device_store, "save_device", lambda dev: saved_devices.append(dev))

    main._persist_influx_logging_state(
        device,
        {
            "enabled": True,
            "interval_s": 2.5,
            "params": ["p", "i", "p", " "],
            "params_configured": True,
        },
    )

    assert saved_devices
    state = device.parameters["influx_logging_state"]
    assert state["enabled"] is True
    assert state["interval_s"] == 2.5
    assert state["params"] == ["p", "i"]


def test_update_logging_param_persists_param_selection(monkeypatch):
    class DummyDevice:
        key = "test-device"
        parameters = {}

    class DummySession:
        def logging_set_param(self, name, enabled):
            assert name == "p"
            assert enabled is True
            return {
                "enabled": False,
                "interval_s": 1.25,
                "params": ["p"],
                "params_configured": True,
            }

    saved_devices = []
    device = DummyDevice()
    monkeypatch.setattr(main.device_store, "get_device", lambda _key: device)
    monkeypatch.setattr(main.device_store, "save_device", lambda dev: saved_devices.append(dev))
    monkeypatch.setattr(main, "_session_for_device", lambda _device: DummySession())

    client = TestClient(main.app)
    response = client.patch("/api/devices/test-device/logging/param/p", json={"enabled": True})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert saved_devices
    state = device.parameters["influx_logging_state"]
    assert state["enabled"] is False
    assert state["interval_s"] == 1.25
    assert state["params"] == ["p"]


def test_update_logging_params_persists_full_selection(monkeypatch):
    class DummyDevice:
        key = "test-device"
        parameters = {}

    class DummySession:
        def logging_set_params(self, names):
            assert names == ["p", "i"]
            return {
                "enabled": True,
                "interval_s": 0.8,
                "params": ["p", "i"],
                "params_configured": True,
            }

    saved_devices = []
    device = DummyDevice()
    monkeypatch.setattr(main.device_store, "get_device", lambda _key: device)
    monkeypatch.setattr(main.device_store, "save_device", lambda dev: saved_devices.append(dev))
    monkeypatch.setattr(main, "_session_for_device", lambda _device: DummySession())

    client = TestClient(main.app)
    response = client.put("/api/devices/test-device/logging/params", json={"names": ["p", "i"]})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert saved_devices
    state = device.parameters["influx_logging_state"]
    assert state["enabled"] is True
    assert state["interval_s"] == 0.8
    assert state["params"] == ["p", "i"]
