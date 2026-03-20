from fastapi.testclient import TestClient

import app.main as main


class DummyAutoLockSession:
    def __init__(self) -> None:
        self.last_payload = None
        self.last_build_kwargs = None
        self.last_settings_payload = None

    def update_auto_lock_scan_settings(self, payload):
        self.last_settings_payload = payload
        return payload

    def auto_lock_from_scan(self, payload):
        self.last_payload = payload
        return {
            "target_index": 1024,
            "target_voltage": 0.01,
            "target_slope_rising": True,
            "score": 0.9,
            "center_abs_v": 0.002,
            "left_excursion_v": 0.1,
            "right_excursion_v": 0.11,
            "pair_excursion_v": 0.21,
            "symmetry": 0.91,
            "monitor_contrast_v": None,
            "detail": "Auto-lock started from scan.",
        }

    def build_manual_lock_row(self, **kwargs):
        self.last_build_kwargs = kwargs
        return {"lock_source": kwargs.get("lock_source")}


def test_auto_lock_scan_endpoint_passes_payload(monkeypatch):
    dummy = DummyAutoLockSession()
    device = type("Device", (), {"key": "test-device", "name": "test-device", "parameters": {}})()
    monkeypatch.setattr(main.device_store, "get_device", lambda _key: device)
    monkeypatch.setattr(main.device_store, "save_device", lambda _device: None)
    monkeypatch.setattr(main.device_config_store, "set_config", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(main, "_session_for_device", lambda _device: dummy)
    client = TestClient(main.app)

    payload = {
        "half_range_v": 0.12,
        "crossing_max_v": 0.03,
        "error_min": 0.1,
        "symmetry_min": 0.25,
        "allow_single_side": True,
        "single_error_min": 0.12,
        "smooth_window_pts": 7,
        "use_monitor": False,
        "monitor_contrast_min_v": 0.02,
    }
    response = client.post("/api/devices/test-device/control/auto_lock_scan", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["target_index"] == 1024
    assert body["target_slope_rising"] is True
    assert dummy.last_payload == payload
    assert dummy.last_settings_payload == payload


def test_auto_lock_scan_endpoint_maps_runtime_error(monkeypatch):
    class ErrorSession:
        def update_auto_lock_scan_settings(self, payload):
            return payload

        def auto_lock_from_scan(self, _payload):
            raise RuntimeError("not connected")

    device = type("Device", (), {"key": "test-device", "name": "test-device", "parameters": {}})()
    monkeypatch.setattr(main.device_store, "get_device", lambda _key: device)
    monkeypatch.setattr(main.device_store, "save_device", lambda _device: None)
    monkeypatch.setattr(main.device_config_store, "set_config", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(main, "_session_for_device", lambda _device: ErrorSession())
    client = TestClient(main.app)

    response = client.post("/api/devices/test-device/control/auto_lock_scan", json={})
    assert response.status_code == 409
    assert "not connected" in response.text


def test_auto_lock_scan_endpoint_maps_validation_error(monkeypatch):
    class ErrorSession:
        def update_auto_lock_scan_settings(self, payload):
            return payload

        def auto_lock_from_scan(self, _payload):
            raise ValueError("no candidate")

    device = type("Device", (), {"key": "test-device", "name": "test-device", "parameters": {}})()
    monkeypatch.setattr(main.device_store, "get_device", lambda _key: device)
    monkeypatch.setattr(main.device_store, "save_device", lambda _device: None)
    monkeypatch.setattr(main.device_config_store, "set_config", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(main, "_session_for_device", lambda _device: ErrorSession())
    client = TestClient(main.app)

    response = client.post("/api/devices/test-device/control/auto_lock_scan", json={})
    assert response.status_code == 422
    assert "no candidate" in response.text


def test_auto_lock_scan_endpoint_enqueues_auto_lock_source(monkeypatch):
    class DummyDevice:
        key = "test-device"
        name = "test-laser"
        parameters = {}

    class CapturingPostgres:
        def __init__(self) -> None:
            self.rows = []

        def set_event_callback(self, _callback):
            return

        def enqueue_lock_result(self, row):
            self.rows.append(row)
            return True

    dummy = DummyAutoLockSession()
    postgres = CapturingPostgres()
    monkeypatch.setattr(main.device_store, "get_device", lambda _key: DummyDevice())
    monkeypatch.setattr(main.device_store, "save_device", lambda _device: None)
    monkeypatch.setattr(main.device_config_store, "set_config", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(main, "_session_for_device", lambda _device: dummy)
    monkeypatch.setattr(main, "lock_result_postgres", postgres)
    client = TestClient(main.app)

    response = client.post("/api/devices/test-device/control/auto_lock_scan", json={})
    assert response.status_code == 200
    assert dummy.last_build_kwargs is not None
    assert dummy.last_build_kwargs["lock_source"] == "auto_lock_scan"
    assert postgres.rows
    assert postgres.rows[-1]["lock_source"] == "auto_lock_scan"
