from fastapi.testclient import TestClient

import app.main as main
from app.auto_lock_scan import AutoLockCalibration, AutoLockScanSettings


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
            "left_excursion": 0.15,
            "right_excursion": 0.16,
            "pair_excursion": 0.31,
            "symmetry": 0.91,
            "monitor_level": None,
            "hz_per_v": None,
            "sideband_offset_v": None,
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
        "signal_type": "pdh",
        "allow_single_side": True,
        "use_monitor": False,
        "monitor_mode": "locked_above",
        "half_range_sweep_v": 0.12,
        "error_min": 0.1,
        "symmetry_min": 0.25,
        "single_error_min": 0.12,
        "min_amplitude": 0.02,
        "smooth_window_pts": 7,
        "monitor_threshold": 0.1,
    }
    response = client.post("/api/devices/test-device/control/auto_lock_scan", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["target_index"] == 1024
    assert body["target_slope_rising"] is True
    assert dummy.last_payload == payload
    assert dummy.last_settings_payload == payload


def test_auto_lock_scan_endpoint_rejects_legacy_v_keys(monkeypatch):
    """Legacy ``_v`` keys are no longer accepted (aliases removed); extra='forbid' -> 422."""
    dummy = DummyAutoLockSession()
    device = type("Device", (), {"key": "test-device", "name": "test-device", "parameters": {}})()
    monkeypatch.setattr(main.device_store, "get_device", lambda _key: device)
    monkeypatch.setattr(main.device_store, "save_device", lambda _device: None)
    monkeypatch.setattr(main.device_config_store, "set_config", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(main, "_session_for_device", lambda _device: dummy)
    client = TestClient(main.app)

    legacy_payload = {
        "half_range_v": 0.12,
        "crossing_max_v": 0.03,
        "monitor_contrast_min_frac": 0.02,
    }
    response = client.post(
        "/api/devices/test-device/control/auto_lock_scan", json=legacy_payload
    )
    assert response.status_code == 422


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


def test_auto_lock_scan_does_not_fail_when_enqueue_raises(monkeypatch):
    class DummyDevice:
        key = "test-device"
        name = "test-laser"
        parameters = {}

    class RaisingPostgres:
        def set_event_callback(self, _callback):
            return

        def enqueue_lock_result(self, _row):
            raise RuntimeError("queue failure")

    dummy = DummyAutoLockSession()
    monkeypatch.setattr(main.device_store, "get_device", lambda _key: DummyDevice())
    monkeypatch.setattr(main.device_store, "save_device", lambda _device: None)
    monkeypatch.setattr(main.device_config_store, "set_config", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(main, "_session_for_device", lambda _device: dummy)
    monkeypatch.setattr(main, "lock_result_postgres", RaisingPostgres())
    client = TestClient(main.app)

    response = client.post("/api/devices/test-device/control/auto_lock_scan", json={})
    assert response.status_code == 200
    assert response.json()["detail"] == "Auto-lock started from scan."


def test_calibrate_endpoint_returns_settings_and_diagnostics(monkeypatch):
    captured = {}

    class CalibratingSession:
        def calibrate_auto_lock_settings(self, *, include_monitor, allow_single_side):
            captured["include_monitor"] = include_monitor
            captured["allow_single_side"] = allow_single_side
            settings = AutoLockScanSettings(
                half_range_sweep_v=0.13,
                error_min=0.18,
                symmetry_min=0.7,
                allow_single_side=allow_single_side,
                use_monitor=include_monitor,
            )
            return AutoLockCalibration(
                settings=settings,
                amplitude=0.36,
                feature_half_width_v=0.1,
                target_index=1024,
                target_voltage=0.0,
                target_slope_rising=True,
                symmetry=1.0,
                monitor_level=0.6 if include_monitor else None,
                hz_per_v=1.0e8,
                detail="Calibrated from trace.",
            )

        def update_auto_lock_scan_settings(self, payload):
            captured["settings_payload"] = payload
            return payload

    device = type("Device", (), {"key": "test-device", "name": "test-device", "parameters": {}})()
    monkeypatch.setattr(main.device_store, "get_device", lambda _key: device)
    monkeypatch.setattr(main.device_store, "save_device", lambda _device: None)
    monkeypatch.setattr(main.device_config_store, "set_config", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(main, "_session_for_device", lambda _device: CalibratingSession())
    client = TestClient(main.app)

    response = client.post(
        "/api/devices/test-device/control/auto_lock_scan/calibrate",
        json={"include_monitor": True, "allow_single_side": False},
    )
    assert response.status_code == 200
    body = response.json()
    assert captured["include_monitor"] is True
    assert captured["allow_single_side"] is False
    assert body["settings"]["half_range_sweep_v"] == 0.13
    assert body["settings"]["use_monitor"] is True
    assert body["amplitude"] == 0.36
    assert body["hz_per_v"] == 1.0e8
    assert body["target_slope_rising"] is True
    assert body["detail"] == "Calibrated from trace."
    # The derived settings were persisted through the normal update path.
    assert captured["settings_payload"]["half_range_sweep_v"] == 0.13


def test_calibrate_endpoint_maps_value_error(monkeypatch):
    class ErrorSession:
        def calibrate_auto_lock_settings(self, **_kwargs):
            raise ValueError("No PDH-like signal detected")

    device = type("Device", (), {"key": "test-device", "name": "test-device", "parameters": {}})()
    monkeypatch.setattr(main.device_store, "get_device", lambda _key: device)
    monkeypatch.setattr(main, "_session_for_device", lambda _device: ErrorSession())
    client = TestClient(main.app)

    response = client.post(
        "/api/devices/test-device/control/auto_lock_scan/calibrate", json={}
    )
    assert response.status_code == 422
    assert "No PDH-like signal" in response.text


def test_calibrate_endpoint_maps_runtime_error(monkeypatch):
    class ErrorSession:
        def calibrate_auto_lock_settings(self, **_kwargs):
            raise RuntimeError("No unlocked trace available")

    device = type("Device", (), {"key": "test-device", "name": "test-device", "parameters": {}})()
    monkeypatch.setattr(main.device_store, "get_device", lambda _key: device)
    monkeypatch.setattr(main, "_session_for_device", lambda _device: ErrorSession())
    client = TestClient(main.app)

    response = client.post(
        "/api/devices/test-device/control/auto_lock_scan/calibrate", json={}
    )
    assert response.status_code == 409
    assert "No unlocked trace available" in response.text
