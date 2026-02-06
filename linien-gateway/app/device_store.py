from __future__ import annotations

from typing import List

from linien_client.device import Device, add_device, delete_device, load_device_list, update_device


def list_devices() -> List[Device]:
    return load_device_list()


def get_device(key: str) -> Device | None:
    devices = load_device_list()
    for device in devices:
        if device.key == key:
            return device
    return None


def save_device(device: Device) -> None:
    existing = get_device(device.key)
    if existing is None:
        add_device(device)
    else:
        update_device(device)


def remove_device(device: Device) -> None:
    delete_device(device)

