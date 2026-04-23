from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List
from uuid import uuid4

from linien_common.config import USER_DATA_PATH

GROUPS_PATH = USER_DATA_PATH / "groups.json"
logger = logging.getLogger(__name__)


def _generate_key() -> str:
    return uuid4().hex[:10]


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temp_path.replace(path)


@dataclass
class Group:
    key: str = field(default_factory=_generate_key)
    name: str = field(default_factory=str)
    device_keys: List[str] = field(default_factory=list)
    auto_include: bool = False


def load_groups(path=GROUPS_PATH) -> List[Group]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError:
        logger.warning("Failed reading groups file at %s", path, exc_info=True)
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Malformed groups file at %s; ignoring.", path, exc_info=True)
        return []

    if isinstance(data, list):
        candidates = data
    elif isinstance(data, dict):
        candidates = list(data.values())
    else:
        logger.warning("Invalid groups payload type at %s: %s", path, type(data).__name__)
        return []

    groups: list[Group] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        if not isinstance(item.get("key"), str):
            logger.warning("Skipping group entry without valid key in %s: %r", path, item)
            continue
        if not isinstance(item.get("name"), str):
            logger.warning("Skipping group entry without valid name in %s: %r", path, item)
            continue
        try:
            groups.append(Group(**item))
        except TypeError:
            logger.warning("Skipping invalid group entry in %s: %r", path, item)
    return groups


def save_groups(groups: List[Group], path=GROUPS_PATH) -> None:
    payload = {i: asdict(group) for i, group in enumerate(groups)}
    _atomic_write_json(Path(path), payload)


def list_groups(device_keys: List[str]) -> List[Group]:
    groups = load_groups()
    changed = False
    device_key_list = list(device_keys)
    device_key_set = set(device_key_list)

    for group in groups:
        filtered = [key for key in group.device_keys if key in device_key_set]
        if filtered != group.device_keys:
            group.device_keys = filtered
            changed = True

    if not groups:
        groups = [
            Group(
                name="All devices",
                device_keys=device_key_list,
                auto_include=True,
            )
        ]
        changed = True
    else:
        for group in groups:
            if not group.auto_include:
                continue
            existing_keys = set(group.device_keys)
            missing_keys = [key for key in device_key_list if key not in existing_keys]
            if missing_keys:
                group.device_keys.extend(missing_keys)
                changed = True

    if changed:
        save_groups(groups)

    return groups


def create_group(name: str, device_keys: List[str], auto_include: bool = False) -> Group:
    group = Group(name=name, device_keys=list(device_keys), auto_include=auto_include)
    groups = load_groups()
    groups.append(group)
    save_groups(groups)
    return group


def update_group(
    key: str,
    name: str | None = None,
    device_keys: List[str] | None = None,
    auto_include: bool | None = None,
) -> Group:
    groups = load_groups()
    for group in groups:
        if group.key != key:
            continue
        if name is not None:
            group.name = name
        if device_keys is not None:
            group.device_keys = list(device_keys)
        if auto_include is not None:
            group.auto_include = auto_include
        save_groups(groups)
        return group
    raise KeyError("Group not found")


def delete_group(key: str) -> None:
    groups = load_groups()
    groups = [group for group in groups if group.key != key]
    save_groups(groups)


def add_device_to_auto_groups(device_key: str) -> None:
    groups = load_groups()
    changed = False
    for group in groups:
        if not group.auto_include:
            continue
        existing_keys = set(group.device_keys)
        if device_key not in existing_keys:
            group.device_keys.append(device_key)
            changed = True
    if changed:
        save_groups(groups)


def remove_device_from_groups(device_key: str) -> None:
    groups = load_groups()
    changed = False
    for group in groups:
        if device_key in group.device_keys:
            group.device_keys = [key for key in group.device_keys if key != device_key]
            changed = True
    if changed:
        save_groups(groups)
