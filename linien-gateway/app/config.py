from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .path_utils import find_repo_root

DEFAULT_API_PORT = 8000
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_PLOT_STREAM_DEFAULT_FPS = 60.0
DEFAULT_PLOT_STREAM_MAX_FPS_CAP = 60.0
DEFAULT_PLOT_STREAM_DROP_OLD_FRAMES = True


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


def _get_positive_float(config: dict[str, Any], key: str, fallback: float) -> float:
    value = config.get(key)
    if isinstance(value, (int, float)) and float(value) > 0:
        return float(value)
    return fallback


def get_plot_stream_default_fps() -> float:
    config = _load_config()
    return _get_positive_float(
        config, "plotStreamDefaultFps", DEFAULT_PLOT_STREAM_DEFAULT_FPS
    )


def get_plot_stream_max_fps_cap() -> float:
    config = _load_config()
    return _get_positive_float(
        config, "plotStreamMaxFpsCap", DEFAULT_PLOT_STREAM_MAX_FPS_CAP
    )


def get_plot_stream_drop_old_frames() -> bool:
    config = _load_config()
    value = config.get("plotStreamDropOldFrames")
    if isinstance(value, bool):
        return value
    return DEFAULT_PLOT_STREAM_DROP_OLD_FRAMES
