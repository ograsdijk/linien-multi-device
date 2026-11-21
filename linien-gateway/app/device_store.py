from __future__ import annotations

import copy
import json
import logging
import threading
from collections.abc import Callable
from typing import Any, List

from linien_client.device import (
    Device,
    add_device,
    delete_device,
    load_device_list,
    update_device,
)

logger = logging.getLogger(__name__)

# The device's saved restorable Linien settings, replayed onto the board on
# every connect (see DeviceSession._seed_or_replay_persistent_settings_locked).
SETTINGS_SNAPSHOT_KEY = "linien_settings_snapshot"

# Single process-wide lock serializing ALL access to devices.json — both the
# in-memory cache and the on-disk read-modify-write.
#
# linien_client's `load_device_list`/`save_device_list` are unlocked and
# non-atomic (`save_device_list` truncates with `open(path, "w")`), and
# `save_device` is called concurrently from request threads (every
# /api/devices/{key}/* route, the /devices/statuses fan-out) and from
# background session/poll threads. Without a lock spanning the whole
# load->mutate->write, two writers interleave and the whole-file rewrite drops
# one update (last-writer-wins), and a reader can observe a half-written file.
# A single lock around every read and every write closes both windows.
#
# NOTE: the actual write still goes through linien_client's non-atomic
# `save_device_list`, so a process crash *exactly* mid-write can still leave a
# truncated devices.json. Reads tolerate that — see `_safe_load_device_list`.
#
# The cache exists because `load_device_list` re-reads + JSON-parses
# `devices.json` on every call; with constant API traffic that disk-read storm
# is a measurable chunk of request latency.
_store_lock = threading.RLock()
_cache: List[Device] | None = None


def _safe_load_device_list() -> List[Device]:
    """Load devices.json, tolerating a corrupt/unreadable file.

    `linien_client.load_device_list` only guards `FileNotFoundError`, so a
    truncated file (e.g. from a crash mid-write) raises `JSONDecodeError` and
    would otherwise take down every /api/devices* endpoint until the file is
    manually repaired. Fall back to the last-good cache, or an empty list,
    instead of propagating. Must be called with `_store_lock` held.
    """
    try:
        return load_device_list()
    except (json.JSONDecodeError, OSError):
        logger.warning(
            "devices.json is unreadable; falling back to last-known device list",
            exc_info=True,
        )
        return list(_cache) if _cache is not None else []


def _invalidate_cache() -> None:
    global _cache
    with _store_lock:
        _cache = None


def _get_cached_list() -> List[Device]:
    global _cache
    with _store_lock:
        if _cache is None:
            _cache = _safe_load_device_list()
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
    """Write `device` to devices.json, except its Linien settings snapshot.

    Callers save whole records from copies they took earlier (the last-healthy
    and recovery state, influx logging, config sync, a device edit), and the
    gateway replays the snapshot onto the board on every connect. Saving a
    stale copy used to write an old snapshot back, so the next restart put old
    demodulation, filter and PID settings onto the board. The snapshot is
    therefore owned by `update_settings_snapshot`: this keeps whatever is on
    disk, and refreshes the caller's copy to match.
    """
    # Hold the store lock across the existence check AND the underlying
    # linien_client write so concurrent saves can't interleave and clobber
    # each other (each rewrites the entire devices.json).
    with _store_lock:
        existing = _find_device(_safe_load_device_list(), device.key)
        if existing is None:
            add_device(device)
        else:
            _keep_stored_snapshot(existing, device)
            update_device(device)
        _invalidate_cache()


def _keep_stored_snapshot(stored: Device, device: Device) -> None:
    stored_params = getattr(stored, "parameters", None)
    if not isinstance(stored_params, dict) or SETTINGS_SNAPSHOT_KEY not in stored_params:
        return
    params = getattr(device, "parameters", None)
    params = params if isinstance(params, dict) else {}
    stored_snapshot = stored_params[SETTINGS_SNAPSHOT_KEY]
    if params.get(SETTINGS_SNAPSHOT_KEY) == stored_snapshot:
        return
    params[SETTINGS_SNAPSHOT_KEY] = copy.deepcopy(stored_snapshot)
    device.parameters = params


def get_settings_snapshot(key: str) -> tuple[bool, dict[str, Any] | None]:
    """(device is stored, its snapshot as on disk). Reads disk, not the cache."""
    with _store_lock:
        device = _find_device(_safe_load_device_list(), key)
        if device is None:
            return False, None
        params = device.parameters if isinstance(device.parameters, dict) else {}
        snapshot = params.get(SETTINGS_SNAPSHOT_KEY)
        return True, copy.deepcopy(snapshot) if isinstance(snapshot, dict) else None


def update_settings_snapshot(
    key: str,
    mutate: Callable[[dict[str, Any] | None], dict[str, Any] | None],
) -> tuple[bool, dict[str, Any] | None]:
    """Change one device's settings snapshot: read, mutate, write, under one lock.

    `mutate` gets a copy of the snapshot on disk (None if there is none) and
    returns the new snapshot, or None to leave it unchanged. Returns
    (device is stored, the snapshot now on disk). Nothing is written when the
    device is not stored.
    """
    with _store_lock:
        devices = _safe_load_device_list()
        device = _find_device(devices, key)
        if device is None:
            return False, None
        params = device.parameters if isinstance(device.parameters, dict) else {}
        current = params.get(SETTINGS_SNAPSHOT_KEY)
        current = copy.deepcopy(current) if isinstance(current, dict) else None
        updated = mutate(copy.deepcopy(current))
        if updated is None:
            return True, current
        params[SETTINGS_SNAPSHOT_KEY] = copy.deepcopy(updated)
        device.parameters = params
        update_device(device)
        _invalidate_cache()
        return True, copy.deepcopy(updated)


def remove_device(device: Device) -> None:
    with _store_lock:
        delete_device(device)
        _invalidate_cache()
