import app.main as main
from fastapi.testclient import TestClient


def test_device_statuses_returns_status_by_device_key(monkeypatch):
    class DummySession:
        def __init__(self, key: str) -> None:
            self.key = key

        def status(self):
            return {
                "connected": self.key == "device-a",
                "connecting": False,
                "last_error": None,
                "last_plot": None,
                "logging_active": False,
                "lock": None,
                "auto_relock": None,
            }

    devices = [
        type("Device", (), {"key": "device-a", "parameters": {}})(),
        type("Device", (), {"key": "device-b", "parameters": {}})(),
    ]

    monkeypatch.setattr(main.device_store, "list_devices", lambda: devices)
    monkeypatch.setattr(
        main, "_session_for_device", lambda device: DummySession(device.key)
    )
    client = TestClient(main.app)

    response = client.get("/api/devices/statuses")

    assert response.status_code == 200
    assert response.json() == {
        "device-a": {
            "connected": True,
            "connecting": False,
            "last_error": None,
            "last_plot": None,
            "logging_active": False,
            "lock": None,
            "auto_relock": None,
        },
        "device-b": {
            "connected": False,
            "connecting": False,
            "last_error": None,
            "last_plot": None,
            "logging_active": False,
            "lock": None,
            "auto_relock": None,
        },
    }
