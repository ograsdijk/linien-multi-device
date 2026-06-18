from __future__ import annotations

import threading
from types import SimpleNamespace

from app.session import DeviceSession
from app.stream import WebsocketManager


def _make_session() -> DeviceSession:
    device = SimpleNamespace(
        key="dev-1",
        name="dev-1",
        host="127.0.0.1",
        port=18862,
        parameters={},
    )
    return DeviceSession(device, WebsocketManager())


def test_reset_connection_state_is_reentrant_under_lock():
    """connect() holds _lock across its whole body; its failure path re-enters
    _lock via _reset_connection_state(). With a plain (non-reentrant) Lock this
    deadlocks — _lock must be an RLock. Run in a worker so a regression fails
    fast (timeout) instead of hanging the suite."""
    session = _make_session()
    session.connected = True
    session.connecting = True
    session.client = SimpleNamespace(disconnect=lambda: None)
    session.control = object()
    session.parameters = object()

    # A previous poll thread that has already finished — _reset should clear it.
    finished = threading.Thread(target=lambda: None)
    finished.start()
    finished.join()
    session._poll_thread = finished

    done = threading.Event()

    def worker() -> None:
        with session._lock:  # mimic connect() holding _lock ...
            session._reset_connection_state(last_error="boom")  # ... and re-entering it
        done.set()

    threading.Thread(target=worker, daemon=True).start()

    assert done.wait(timeout=5.0), "reset under a held _lock deadlocked (need an RLock)"
    assert session.connected is False
    assert session.connecting is False
    assert session.client is None
    assert session.control is None
    assert session.parameters is None
    assert session.last_error == "boom"
    assert session._poll_thread is None
    assert session._stop_event.is_set()


def test_reset_from_inside_poll_thread_does_not_self_join():
    """_reset_connection_state can be reached from the poll thread itself via
    _handle_poll_failure(); it must not try to join the current thread."""
    session = _make_session()
    completed = threading.Event()

    def poll_like() -> None:
        # Pretend to be the running poll thread tearing itself down.
        session._poll_thread = threading.current_thread()
        session._reset_connection_state(last_error="poll boom", request_diagnosis=True)
        completed.set()

    t = threading.Thread(target=poll_like, daemon=True)
    t.start()

    assert completed.wait(timeout=5.0), "self-join or deadlock in poll-thread reset path"
    assert session.connected is False
