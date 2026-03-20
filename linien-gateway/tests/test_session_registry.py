import threading

from app.session_registry import SessionRegistry


def test_session_registry_get_or_create_is_singleton_under_concurrency():
    registry = SessionRegistry()
    created = []

    def factory():
        created.append(object())
        return created[-1]

    results = []

    def worker():
        results.append(registry.get_or_create("device-a", factory))

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(created) == 1
    assert len(results) == 12
    assert all(item is created[0] for item in results)


def test_session_registry_remove_returns_session_once():
    registry = SessionRegistry()
    session = object()
    registry.get_or_create("device-a", lambda: session)

    first = registry.remove("device-a")
    second = registry.remove("device-a")

    assert first is session
    assert second is None
