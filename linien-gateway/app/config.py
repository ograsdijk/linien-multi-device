from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .path_utils import find_repo_root

DEFAULT_API_PORT = 8000
DEFAULT_API_HOST = "127.0.0.1"


def _repo_root() -> Path:
    return find_repo_root(Path(__file__)) or Path(__file__).resolve().parents[2]


def _load_config() -> dict[str, Any]:
    config_path = _repo_root() / "config.json"
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def get_api_port() -> int:
    config = _load_config()
    value = config.get("apiPort")
    if isinstance(value, int) and value > 0:
        return value
    return DEFAULT_API_PORT


def get_api_host() -> str:
    config = _load_config()
    value = config.get("apiHost")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_API_HOST
