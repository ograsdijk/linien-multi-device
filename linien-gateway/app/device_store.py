from __future__ import annotations

import copy
import threading
from typing import List

from linien_client.device import (
    Device,
    add_device,
    delete_device,
    load_device_list,
    update_device,
)

# In-memory cache of the device list. linien_client's `load_device_list`
# reads + JSON-parses `devices.json` on every call; with 12 devices and
# constant API traffic (every /api/devices/{key}/* endpoint, every WS
# lifecycle event) the disk-read storm shows up under profiling as a
# meaningful chunk of request latency. The cache is invalidated by the
# only writers (`add_device`/`update_device`/`delete_device`) so it
# stays authoritative as long as nothing else mutates the file out of
# band.
_cache_lock = threading.RLock()
_cache: List[Device] | None = None


def _invalidate_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def _get_cached_list() -> List[Device]:
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = load_device_list()
        # Return a shallow copy so callers can iterate / filter without
        # racing concurrent invalidation. Device objects themselves are
        # treated as immutable for the duration of one request.
        return list(_cache)


def _find_device(devices: List[Device], key: str) -> Device | None:
    for device in devices:
        if device.key == key:
            return device
    return None


def list_devices() -> List[Device]:
    return _get_cached_list()


def get_device(key: str) -> Device | None:
    found = _find_device(_get_cached_list(), key)
    if found is None:
        return None
    # Hand callers a deep copy so mutations they make to `device.parameters`
    # in flight (e.g. `_session_for_device` writing back synced configs)
    # don't silently propagate to the cached entry without going through
    # `save_device`, which is the only legitimate persistence path.
    return copy.deepcopy(found)


def save_device(device: Device) -> None:
    existing = _find_device(load_device_list(), device.key)
    if existing is None:
        add_device(device)
    else:
        update_device(device)
    _invalidate_cache()


def remove_device(device: Device) -> None:
    delete_device(device)
    _invalidate_cache()
