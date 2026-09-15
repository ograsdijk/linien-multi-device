from fastapi.testclient import TestClient

import app.main as main
from app.lock_approach import ApproachAborted, ApproachSettings


class DummySession:
    def __init__(self) -> None:
        self.settings = ApproachSettings().__dict__.copy()
        self.last_payload = None

    def get_lock_approach_settings(self):
        return dict(self.settings)

    def update_lock_approach_settings(self, payload):
        self.last_payload = payload
        self.settings = {**self.settings, **payload}
        return dict(self.settings)


def _device():
    return type(
        "Device", (), {"key": "test-device", "name": "test-device", "parameters": {}}
    )()


def _patch_store(monkeypatch, session):
    device = _device()
    monkeypatch.setattr(main.device_store, "get_device", lambda _key: device)
    monkeypatch.setattr(main.device_store, "save_device", lambda _device: None)
    monkeypatch.setattr(
        main.device_config_store, "set_config", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(main, "_session_for_device", lambda _device: session)
    monkeypatch.setattr(main, "_get_session", lambda _key: session)


def test_lock_approach_settings_default_to_disabled(monkeypatch):
    session = DummySession()
    _patch_store(monkeypatch, session)
    client = TestClient(main.app)

    response = client.get("/api/devices/test-device/lock-approach-settings")

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["capture_fraction"] == 0.5
    assert body["approach_from_below"] is True


def test_updating_lock_approach_settings_persists_them(monkeypatch):
    session = DummySession()
    _patch_store(monkeypatch, session)
    saved: list[tuple] = []
    monkeypatch.setattr(
        main.device_config_store,
        "set_config",
        lambda *args, **kwargs: saved.append(args) or {},
    )
    client = TestClient(main.app)

    payload = ApproachSettings().__dict__.copy()
    payload.update({"enabled": True, "approach_offset_v": 0.08, "settle_ms": 500})
    response = client.put(
        "/api/devices/test-device/lock-approach-settings", json=payload
    )

    assert response.status_code == 200
    assert response.json()["approach_offset_v"] == 0.08
    assert session.last_payload["settle_ms"] == 500
    assert saved and saved[0][1] == main.CONFIG_LOCK_APPROACH


def test_unknown_lock_approach_fields_are_rejected(monkeypatch):
    session = DummySession()
    _patch_store(monkeypatch, session)
    client = TestClient(main.app)

    payload = ApproachSettings().__dict__.copy()
    payload["verify_window_v"] = 0.02  # removed: the tolerance is derived now
    response = client.put(
        "/api/devices/test-device/lock-approach-settings", json=payload
    )

    assert response.status_code == 422


def test_out_of_range_lock_approach_values_are_rejected(monkeypatch):
    session = DummySession()
    _patch_store(monkeypatch, session)
    client = TestClient(main.app)

    payload = ApproachSettings().__dict__.copy()
    payload["ramp_step_v"] = 0.0  # a zero step would plan an infinite ramp
    response = client.put(
        "/api/devices/test-device/lock-approach-settings", json=payload
    )

    assert response.status_code == 422


def test_the_started_event_carries_the_guarded_move_numbers():
    """How far the center moved and how much correction it needed belong on the
    per-device record, not only in the HTTP response."""
    details = main._auto_lock_event_details(
        {
            "target_voltage": 0.2,
            "target_index": 1024,
            "score": 0.9,
            "approach": {
                "center_move_v": 0.5,
                "center_correction_v": 0.03,
                "center_offset_v": 0.002,
                "capture_tolerance_v": 0.01,
                "attempts": [{"from_below": True}, {"from_below": True}],
            },
        }
    )

    assert details["center_move_v"] == 0.5
    assert details["center_correction_v"] == 0.03
    assert details["approach_attempts"] == 2
    assert details["approach_from_below"] is True


def test_the_started_event_is_unchanged_without_a_guarded_move():
    details = main._auto_lock_event_details(
        {"target_voltage": 0.2, "target_index": 1024, "score": 0.9}
    )

    assert set(details) == {"target_voltage", "target_index", "score"}


class AbortingSession(DummySession):
    """auto_lock_from_scan that aborts the way a guarded move does."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict] = []
        self.report = {
            "enabled": True,
            "accepted": False,
            "target_voltage": 0.2,
            "commanded_voltage": 0.26,
            "start_voltage": -0.3,
            "center_move_v": 0.56,
            "center_correction_v": 0.06,
            "center_offset_v": None,
            "capture_tolerance_v": 0.01,
            "rejection_bound_v": 0.08,
            "attempts": [
                {"attempt": 1, "from_below": True, "direct": True, "offset_v": 0.06},
                {"attempt": 2, "from_below": False, "direct": False, "offset_v": -0.06},
            ],
        }

    def update_auto_lock_scan_settings(self, payload):
        return payload

    def auto_lock_from_scan(self, _payload):
        raise ApproachAborted("Auto-lock aborted: never confirmed.", self.report)

    def build_manual_lock_row(self, **kwargs):
        self.rows.append(kwargs)
        return {"lock_source": kwargs.get("lock_source")}


def test_an_aborted_auto_lock_is_still_recorded_in_postgres(monkeypatch):
    session = AbortingSession()
    _patch_store(monkeypatch, session)
    enqueued: list[dict] = []
    monkeypatch.setattr(
        main.lock_result_postgres,
        "enqueue_lock_result",
        lambda row: enqueued.append(row) or True,
    )
    events: list[dict] = []
    monkeypatch.setattr(
        main, "_emit_log", lambda **kwargs: events.append(kwargs)
    )
    client = TestClient(main.app)

    response = client.post(
        "/api/devices/test-device/control/auto_lock_scan",
        json=main.AutoLockScanSettings().model_dump(),
    )

    assert response.status_code == 409
    # The failed attempt is written as a failure row, carrying its offsets.
    assert session.rows and session.rows[0]["success"] is False
    assert session.rows[0]["approach"]["center_move_v"] == 0.56
    assert enqueued
    failure = [e for e in events if e.get("code") == "auto_lock_scan_failed"][0]
    assert failure["details"]["center_move_v"] == 0.56
    assert failure["details"]["approach_attempts"] == 2
