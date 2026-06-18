import json
import threading
import time
from types import SimpleNamespace

import pytest

from app import device_store


@pytest.fixture(autouse=True)
def _reset_cache():
    device_store._invalidate_cache()
    yield
    device_store._invalidate_cache()


def test_save_device_loads_device_list_once(monkeypatch):
    tracked_calls = {"load": 0, "add": 0, "update": 0}
    device = SimpleNamespace(key="dev-1")

    def fake_load_device_list():
        tracked_calls["load"] += 1
        return [device]

    monkeypatch.setattr(device_store, "load_device_list", fake_load_device_list)
    monkeypatch.setattr(
        device_store,
        "add_device",
        lambda _device: tracked_calls.__setitem__("add", tracked_calls["add"] + 1),
    )
    monkeypatch.setattr(
        device_store,
        "update_device",
        lambda _device: tracked_calls.__setitem__(
            "update", tracked_calls["update"] + 1
        ),
    )

    device_store.save_device(device)

    assert tracked_calls == {"load": 1, "add": 0, "update": 1}


def test_concurrent_saves_do_not_lose_updates(monkeypatch):
    # Simulate linien_client's non-atomic read-modify-write of the whole file
    # with a widened window so an unserialized caller would lose updates.
    state: list = []

    def fake_load():
        return list(state)

    def fake_add(device):
        snapshot = list(state)
        time.sleep(0.001)
        snapshot.append(device)
        state[:] = snapshot

    def fake_update(device):
        snapshot = list(state)
        time.sleep(0.001)
        state[:] = [device if d.key == device.key else d for d in snapshot]

    monkeypatch.setattr(device_store, "load_device_list", fake_load)
    monkeypatch.setattr(device_store, "add_device", fake_add)
    monkeypatch.setattr(device_store, "update_device", fake_update)

    n = 16
    threads = [
        threading.Thread(
            target=device_store.save_device,
            args=(SimpleNamespace(key=f"dev-{i}"),),
        )
        for i in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # With the store lock, every distinct device survives (no last-writer-wins
    # over the whole file).
    assert sorted(d.key for d in state) == sorted(f"dev-{i}" for i in range(n))


def test_corrupt_device_list_is_tolerated_on_read(monkeypatch):
    def boom():
        raise json.JSONDecodeError("bad", "", 0)

    monkeypatch.setattr(device_store, "load_device_list", boom)

    # A corrupt/truncated devices.json must not take down list_devices().
    assert device_store.list_devices() == []


def test_save_device_tolerates_corrupt_list(monkeypatch):
    added: list = []

    def boom():
        raise json.JSONDecodeError("bad", "", 0)

    monkeypatch.setattr(device_store, "load_device_list", boom)
    monkeypatch.setattr(device_store, "add_device", lambda d: added.append(d))
    monkeypatch.setattr(device_store, "update_device", lambda _d: None)

    # Should not raise even when the on-disk list is unreadable.
    device_store.save_device(SimpleNamespace(key="dev-x"))
    assert [d.key for d in added] == ["dev-x"]
