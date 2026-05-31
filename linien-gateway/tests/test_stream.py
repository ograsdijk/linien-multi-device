import asyncio
import logging
import threading
import time

from app.plot_processing import SUMMARY_SERIES_KEYS
from app.stream import ConnectionState, WebsocketManager


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


def _snapshot_state(state: ConnectionState) -> dict:
    return {
        "max_fps": state.max_fps,
        "detail": state.detail,
        "last_plot": state.last_plot,
        "latest_plot_message": state.latest_plot_message,
    }


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


def test_peek_required_detail_is_pure_and_reflects_subscribers():
    async def run() -> None:
        manager = WebsocketManager(default_plot_fps=None, max_plot_fps_cap=None)
        ws_full = DummyWebSocket()
        ws_summary = DummyWebSocket()
        await manager.register("dev-a", ws_full, max_fps=10.0, detail="full")
        await manager.register("dev-a", ws_summary, max_fps=10.0, detail="summary")

        state_full = manager._connections["dev-a"][ws_full]
        state_summary = manager._connections["dev-a"][ws_summary]
        snap_full_before = _snapshot_state(state_full)
        snap_summary_before = _snapshot_state(state_summary)

        # Multiple peeks must not mutate any per-connection state. This is the
        # regression guard against the old `prepare_plot_frame` which advanced
        # `last_plot` and was therefore unsafe to call from producer code that
        # had not yet decided to broadcast.
        for _ in range(5):
            assert manager.peek_required_detail("dev-a") == "full"

        assert _snapshot_state(state_full) == snap_full_before
        assert _snapshot_state(state_summary) == snap_summary_before
        assert state_full.last_plot == 0.0
        assert state_summary.last_plot == 0.0

        await manager.unregister("dev-a", ws_full)
        assert manager.peek_required_detail("dev-a") == "summary"

        await manager.unregister("dev-a", ws_summary)
        assert manager.peek_required_detail("dev-a") is None
        assert manager.peek_required_detail("nonexistent-device") is None

    asyncio.run(run())


def test_broadcast_fps_throttle_is_per_subscriber():
    async def run() -> None:
        manager = WebsocketManager(default_plot_fps=None, max_plot_fps_cap=None)
        ws_fast = DummyWebSocket()
        ws_slow = DummyWebSocket()
        await manager.register("dev-a", ws_fast, max_fps=60.0, detail="full")
        # ws_slow: 5 fps -> min_dt=200ms, so two broadcasts ~50ms apart yield 1.
        await manager.register("dev-a", ws_slow, max_fps=5.0, detail="full")

        # First broadcast: both subscribers should accept (their last_plot==0,
        # so the throttle gate lets it through).
        await manager.broadcast(
            "dev-a",
            {"type": "plot_frame", "series": {"combined_error": [1.0]}, "seq": 1},
        )
        # Sleep ~50ms — long enough for ws_fast (60 fps -> 16.7ms min_dt) to
        # accept again, but well under ws_slow's 200ms gate.
        await asyncio.sleep(0.05)
        await manager.broadcast(
            "dev-a",
            {"type": "plot_frame", "series": {"combined_error": [2.0]}, "seq": 2},
        )

        # Let sender loops drain.
        await asyncio.sleep(0.05)

        fast_frames = [m for m in ws_fast.sent if m.get("type") == "plot_frame"]
        slow_frames = [m for m in ws_slow.sent if m.get("type") == "plot_frame"]

        assert len(fast_frames) == 2, (
            f"fast subscriber (60 fps) should accept both frames, got {len(fast_frames)}: {fast_frames}"
        )
        assert len(slow_frames) == 1, (
            f"slow subscriber (5 fps) should be throttled to exactly 1 frame "
            f"within ~50ms, got {len(slow_frames)}: {slow_frames}"
        )
        assert slow_frames[0]["seq"] == 1

        await manager.unregister("dev-a", ws_fast)
        await manager.unregister("dev-a", ws_slow)

    asyncio.run(run())


def test_filter_plot_frame_uses_summary_series_keys():
    extra_keys = ["control_signal_history", "slow_history", "monitor_signal_history"]
    all_keys = list(SUMMARY_SERIES_KEYS) + extra_keys
    message = {
        "type": "plot_frame",
        "lock": True,
        "series": {key: [1.0, 2.0] for key in all_keys},
    }

    filtered = WebsocketManager.filter_plot_frame(message, "summary")
    assert set(filtered["series"].keys()) == set(SUMMARY_SERIES_KEYS)
    # Non-series fields must be preserved.
    assert filtered["type"] == "plot_frame"
    assert filtered["lock"] is True
    # Original must not be mutated.
    assert set(message["series"].keys()) == set(all_keys)

    # detail="full" returns the same object unchanged.
    full = WebsocketManager.filter_plot_frame(message, "full")
    assert full is message

    # Missing or non-dict `series` is returned unchanged.
    no_series = {"type": "plot_frame"}
    assert WebsocketManager.filter_plot_frame(no_series, "summary") is no_series

    bad_series = {"type": "plot_frame", "series": "not a dict"}
    assert WebsocketManager.filter_plot_frame(bad_series, "summary") is bad_series


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
