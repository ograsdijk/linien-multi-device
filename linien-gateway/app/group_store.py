from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import List
from uuid import uuid4

from linien_common.config import USER_DATA_PATH

GROUPS_PATH = USER_DATA_PATH / "groups.json"


def _generate_key() -> str:
    return uuid4().hex[:10]


@dataclass
class Group:
    key: str = field(default_factory=_generate_key)
    name: str = field(default_factory=str)
    device_keys: List[str] = field(default_factory=list)
    auto_include: bool = False


def load_groups(path=GROUPS_PATH) -> List[Group]:
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return [Group(**value) for _, value in data.items()]
    except FileNotFoundError:
        return []


def save_groups(groups: List[Group], path=GROUPS_PATH) -> None:
    with open(path, "w") as f:
        json.dump({i: asdict(group) for i, group in enumerate(groups)}, f, indent=2)


def list_groups(device_keys: List[str]) -> List[Group]:
    groups = load_groups()
    changed = False

    for group in groups:
        filtered = [key for key in group.device_keys if key in device_keys]
        if filtered != group.device_keys:
            group.device_keys = filtered
            changed = True

    if not groups:
        groups = [
            Group(
                name="All devices",
                device_keys=list(device_keys),
                auto_include=True,
            )
        ]
        changed = True
    else:
        for group in groups:
            if not group.auto_include:
                continue
            for key in device_keys:
                if key not in group.device_keys:
                    group.device_keys.append(key)
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
        if device_key not in group.device_keys:
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
