import asyncio
import logging
import threading
import time

from app.stream import WebsocketManager


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
