from __future__ import annotations

from enum import Enum, IntEnum
from typing import Any

import numpy as np

UNSERIALIZABLE = object()


def to_jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, IntEnum):
        return int(value)
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, bytes):
        return UNSERIALIZABLE
    if isinstance(value, (list, tuple)):
        items = []
        for item in value:
            encoded = to_jsonable(item)
            if encoded is UNSERIALIZABLE:
                return UNSERIALIZABLE
            items.append(encoded)
        return items
    if isinstance(value, dict):
        encoded_dict = {}
        for key, item in value.items():
            encoded = to_jsonable(item)
            if encoded is UNSERIALIZABLE:
                return UNSERIALIZABLE
            encoded_dict[str(key)] = encoded
        return encoded_dict
    return UNSERIALIZABLE

