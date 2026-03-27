import asyncio
import logging
import threading
import time

from app.stream import WebsocketManager


class DummyWebSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.sent: list[dict] = []
        self.release_send = asyncio.Event()
        self.release_send.set()

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict) -> None:
        await self.release_send.wait()
        self.sent.append(payload)


def test_publish_logs_when_broadcast_future_fails(caplog):
    manager = WebsocketManager()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    manager.set_loop(loop)

    async def failing_broadcast(_device_key, _message):
        raise RuntimeError("boom")

    manager.broadcast = failing_broadcast  # type: ignore[assignment]

    with caplog.at_level(logging.WARNING):
        manager.publish("dev-a", {"type": "status"})
        time.sleep(0.1)

    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=1.0)
    loop.close()

    assert "Websocket publish failed" in caplog.text


def test_register_applies_default_and_cap_fps():
    async def run() -> None:
        manager = WebsocketManager(default_plot_fps=60, max_plot_fps_cap=60)
        ws_default = DummyWebSocket()
        ws_capped = DummyWebSocket()
        await manager.register("dev-a", ws_default, max_fps=None)
        await manager.register("dev-a", ws_capped, max_fps=120.0)

        state_default = manager._connections["dev-a"][ws_default]
        state_capped = manager._connections["dev-a"][ws_capped]

        assert state_default.max_fps == 60
        assert state_capped.max_fps == 60

        await manager.unregister("dev-a", ws_default)
        await manager.unregister("dev-a", ws_capped)

    asyncio.run(run())


def test_plot_frames_drop_old_when_enabled():
    async def run() -> None:
        manager = WebsocketManager(
            default_plot_fps=None,
            max_plot_fps_cap=None,
            drop_old_plot_frames=True,
        )
        ws = DummyWebSocket()
        ws.release_send.clear()
        await manager.register("dev-a", ws, max_fps=None)

        await manager.broadcast("dev-a", {"type": "plot_frame", "seq": 1})
        await manager.broadcast("dev-a", {"type": "plot_frame", "seq": 2})
        await manager.broadcast("dev-a", {"type": "plot_frame", "seq": 3})

        assert ws.sent == []

        ws.release_send.set()
        await asyncio.sleep(0.05)

        assert [msg["seq"] for msg in ws.sent if msg.get("type") == "plot_frame"] == [3]

        await manager.unregister("dev-a", ws)

    asyncio.run(run())


def test_plot_frames_keep_old_when_drop_old_disabled():
    async def run() -> None:
        manager = WebsocketManager(
            default_plot_fps=None,
            max_plot_fps_cap=None,
            drop_old_plot_frames=False,
        )
        ws = DummyWebSocket()
        ws.release_send.clear()
        await manager.register("dev-a", ws, max_fps=None)

        await manager.broadcast("dev-a", {"type": "plot_frame", "seq": 1})
        await manager.broadcast("dev-a", {"type": "plot_frame", "seq": 2})

        ws.release_send.set()
        await asyncio.sleep(0.05)

        assert [msg["seq"] for msg in ws.sent if msg.get("type") == "plot_frame"] == [1]

        await manager.unregister("dev-a", ws)

    asyncio.run(run())


def test_reliable_messages_are_not_dropped():
    async def run() -> None:
        manager = WebsocketManager(default_plot_fps=None, max_plot_fps_cap=None)
        ws = DummyWebSocket()
        ws.release_send.clear()
        await manager.register("dev-a", ws, max_fps=None)

        await manager.broadcast("dev-a", {"type": "status", "seq": 1})
        await asyncio.sleep(0.01)
        await manager.broadcast("dev-a", {"type": "status", "seq": 2})

        ws.release_send.set()
        await asyncio.sleep(0.05)

        assert [msg["seq"] for msg in ws.sent if msg.get("type") == "status"] == [1, 2]

        await manager.unregister("dev-a", ws)

    asyncio.run(run())
