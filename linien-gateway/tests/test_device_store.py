from types import SimpleNamespace

from app import device_store


def test_save_device_loads_device_list_once(monkeypatch):
    tracked_calls = {"load": 0, "add": 0, "update": 0}
    device = SimpleNamespace(key="dev-1")

    def fake_load_device_list():
        tracked_calls["load"] += 1
        return [device]

    monkeypatch.setattr(device_store, 'load_device_list', fake_load_device_list)
    monkeypatch.setattr(device_store, 'add_device', lambda _device: tracked_calls.__setitem__('add', tracked_calls['add'] + 1))
    monkeypatch.setattr(device_store, 'update_device', lambda _device: tracked_calls.__setitem__('update', tracked_calls['update'] + 1))

    device_store.save_device(device)

    assert tracked_calls == {"load": 1, "add": 0, "update": 1}