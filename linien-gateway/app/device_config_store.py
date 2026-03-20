from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

from linien_client.device import Device

from .path_utils import resolve_repo_path

CONFIG_AUTO_LOCK_SCAN = "auto_lock_scan_settings"
CONFIG_LOCK_INDICATOR = "lock_indicator_config"
CONFIG_AUTO_RELOCK = "auto_relock_config"

KNOWN_CONFIG_NAMES = (
    CONFIG_AUTO_LOCK_SCAN,
    CONFIG_LOCK_INDICATOR,
    CONFIG_AUTO_RELOCK,
)


def _resolve_default_config_path() -> Path:
    return resolve_repo_path("device_settings.json", Path.cwd().resolve())


class DeviceConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _resolve_default_config_path()
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = self._load_from_disk()

    @property
    def path(self) -> Path:
        return self._path

    def _load_from_disk(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for raw_key, raw_value in payload.items():
            if not isinstance(raw_key, str) or not isinstance(raw_value, dict):
                continue
            item: dict[str, Any] = {}
            for name in KNOWN_CONFIG_NAMES:
                if name in raw_value:
                    item[name] = copy.deepcopy(raw_value[name])
            if item:
                result[raw_key] = item
        return result

    def _write_to_disk_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._path.with_suffix(f"{self._path.suffix}.tmp")
        payload = json.dumps(self._data, indent=2, sort_keys=True)
        temp_path.write_text(payload, encoding="utf-8")
        temp_path.replace(self._path)

    def get_device_configs(self, device_key: str) -> dict[str, Any]:
        with self._lock:
            payload = self._data.get(device_key)
            if not isinstance(payload, dict):
                return {}
            return copy.deepcopy(payload)

    def set_config(self, device_key: str, config_name: str, value: Any) -> dict[str, Any]:
        if config_name not in KNOWN_CONFIG_NAMES:
            raise KeyError(f"Unknown config name: {config_name}")
        with self._lock:
            device_payload = self._data.get(device_key)
            if not isinstance(device_payload, dict):
                device_payload = {}
            device_payload[config_name] = copy.deepcopy(value)
            self._data[device_key] = device_payload
            self._write_to_disk_locked()
            return copy.deepcopy(device_payload)

    def remove_device(self, device_key: str) -> None:
        with self._lock:
            if device_key not in self._data:
                return
            self._data.pop(device_key, None)
            self._write_to_disk_locked()

    def apply_configs_to_device(self, device: Device) -> bool:
        payload = self.get_device_configs(device.key)
        if not payload:
            return False
        parameters = device.parameters if isinstance(device.parameters, dict) else {}
        changed = False
        for name in KNOWN_CONFIG_NAMES:
            if name not in payload:
                continue
            value = payload[name]
            if parameters.get(name) != value:
                parameters[name] = copy.deepcopy(value)
                changed = True
        if changed:
            device.parameters = parameters
        return changed
