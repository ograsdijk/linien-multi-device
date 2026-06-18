from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.main as main


def test_device_stream_sends_snapshot_before_registered_messages(monkeypatch):
    class DummySession:
        def snapshot(self):
            return {
                "params": {"p": 1},
                "plot_frame": {
                    "type": "plot_frame",
                    "lock": False,
                    "series": {"combined_error": [0.1]},
                    "signal_power": {"channel1": None, "channel2": None},
                    "stats": {"error_std": None, "control_std": None},
                    "x_label": "sweep voltage",
                    "x_unit": "V",
                },
                "status": {
                    "connected": True,
                    "connecting": False,
                    "last_error": None,
                    "last_plot": 1.0,
                    "logging_active": False,
                    "lock": False,
                    "auto_relock": None,
                },
            }

    # The handler now validates the key against device_store before opening
    # the socket (rejects unknown keys without allocating a per-key lock).
    monkeypatch.setattr(
        main.device_store, "get_device", lambda _key: SimpleNamespace(key=_key)
    )
    monkeypatch.setattr(main, "_get_session", lambda _key: DummySession())

    with TestClient(main.app) as client:
        with client.websocket_connect("/api/devices/dev-a/stream") as websocket:
            assert websocket.receive_json() == {
                "type": "param_update",
                "name": "p",
                "value": 1,
            }
            assert websocket.receive_json()["type"] == "plot_frame"
            assert websocket.receive_json() == {
                "type": "status",
                "connected": True,
                "connecting": False,
                "last_error": None,
                "last_plot": 1.0,
                "logging_active": False,
                "lock": False,
                "auto_relock": None,
            }

            main.manager.publish(
                "dev-a",
                {
                    "type": "status",
                    "connected": True,
                    "connecting": False,
                    "last_error": None,
                    "last_plot": 2.0,
                    "logging_active": False,
                    "lock": True,
                    "auto_relock": None,
                },
            )

            assert websocket.receive_json()["lock"] is True
