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
            # Params arrive coalesced as one param_snapshot rather than one
            # param_update per parameter.
            assert websocket.receive_json() == {
                "type": "param_snapshot",
                "params": {"p": 1},
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



def test_stream_is_registered_before_its_snapshot_is_read(monkeypatch):
    """Registration must precede the snapshot read.

    This is the connect race: connect() publishes "connected" as soon as the
    session comes up. While the stream registered only *after* reading and
    writing its snapshot, a publish landing in that window was broadcast to a
    connection set that did not yet contain this socket, so it went nowhere.
    Because status is published only on transitions and the client's backstop
    poll skips streaming devices, a lost "connected" left the UI greyed out as
    "Not connected" indefinitely while plot frames flowed behind it.

    Asserting the ordering directly is the reliable form: `publish()` only
    schedules the broadcast on the event loop, so a test that publishes from
    inside snapshot() passes under either ordering.
    """
    seen: dict[str, object] = {}

    class DummySession:
        def snapshot(self):
            seen["registered_detail"] = main.manager.peek_required_detail("dev-a")
            return {
                "params": {},
                "plot_frame": None,
                "status": {
                    "connected": True,
                    "connecting": False,
                    "last_error": None,
                    "last_plot": None,
                    "logging_active": False,
                    "lock": False,
                    "auto_relock": None,
                },
            }

    monkeypatch.setattr(
        main.device_store, "get_device", lambda _key: SimpleNamespace(key=_key)
    )
    monkeypatch.setattr(main, "_get_session", lambda _key: DummySession())

    with TestClient(main.app) as client:
        with client.websocket_connect("/api/devices/dev-a/stream?detail=full") as ws:
            assert ws.receive_json()["type"] == "status"

    # None would mean the socket was not yet in the connection set when the
    # snapshot was taken — i.e. the drop window was still open.
    assert seen["registered_detail"] == "full"
