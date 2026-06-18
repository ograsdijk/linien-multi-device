from pathlib import Path
import threading

from app.group_store import (
    Group,
    add_device_to_auto_groups,
    create_group,
    delete_group,
    list_groups,
    load_groups,
    remove_device_from_groups,
    reorder_groups,
    save_groups,
    update_group,
)


def test_load_groups_malformed_json_returns_empty(tmp_path: Path):
    path = tmp_path / "groups.json"
    path.write_text("{invalid json", encoding="utf-8")

    groups = load_groups(path=path)

    assert groups == []


def test_load_groups_skips_invalid_entries(tmp_path: Path):
    path = tmp_path / "groups.json"
    path.write_text(
        """
        {
          "0": {"key": "abc", "name": "valid", "device_keys": ["d1"], "auto_include": true},
          "1": {"name": "missing key"},
          "2": "bad"
        }
        """,
        encoding="utf-8",
    )

    groups = load_groups(path=path)

    assert len(groups) == 1
    assert groups[0].key == "abc"
    assert groups[0].name == "valid"


def test_load_groups_keeps_group_with_unknown_keys(tmp_path: Path):
    # An extra/unknown field must not drop the whole group (it used to make
    # Group(**item) raise TypeError).
    path = tmp_path / "groups.json"
    path.write_text(
        """
        {
          "0": {"key": "abc", "name": "valid", "device_keys": ["d1"],
                "auto_include": true, "future_field": 123}
        }
        """,
        encoding="utf-8",
    )

    groups = load_groups(path=path)

    assert len(groups) == 1
    assert groups[0].key == "abc"
    assert groups[0].device_keys == ["d1"]
    assert groups[0].auto_include is True


def test_load_groups_coerces_bad_value_types(tmp_path: Path):
    path = tmp_path / "groups.json"
    path.write_text(
        """
        {
          "0": {"key": "abc", "name": "valid", "device_keys": "not-a-list",
                "auto_include": "yes"},
          "1": {"key": "def", "name": "mixed", "device_keys": ["d1", 5, "d2"]}
        }
        """,
        encoding="utf-8",
    )

    groups = load_groups(path=path)

    assert len(groups) == 2
    assert groups[0].device_keys == []  # non-list coerced to empty
    assert groups[0].auto_include is True  # truthy string -> True
    assert groups[1].device_keys == ["d1", "d2"]  # non-str entries dropped


def test_save_groups_roundtrip(tmp_path: Path):
    path = tmp_path / "groups.json"
    source = [Group(key="a1", name="All", device_keys=["d1"], auto_include=True)]

    save_groups(source, path=path)
    loaded = load_groups(path=path)

    assert len(loaded) == 1
    assert loaded[0].key == "a1"
    assert loaded[0].auto_include is True


def test_list_groups_auto_include_preserves_existing_order_and_appends_missing(
    monkeypatch,
):
    groups = [
        Group(
            key="all",
            name="All devices",
            device_keys=["d2", "d1"],
            auto_include=True,
        )
    ]
    saved_groups = []

    monkeypatch.setattr("app.group_store.load_groups", lambda path=None: groups)
    monkeypatch.setattr(
        "app.group_store.save_groups",
        lambda payload, path=None: saved_groups.append(payload),
    )

    listed = list_groups(["d1", "d2", "d3"])

    assert listed[0].device_keys == ["d2", "d1", "d3"]
    assert saved_groups


def test_group_crud_helpers_roundtrip_with_explicit_path(tmp_path: Path):
    path = tmp_path / "groups.json"

    created = create_group("Lab", ["dev-a"], auto_include=False, path=path)
    updated = update_group(
        created.key,
        name="Main lab",
        device_keys=["dev-a", "dev-b"],
        auto_include=True,
        path=path,
    )
    add_device_to_auto_groups("dev-c", path=path)
    remove_device_from_groups("dev-a", path=path)

    loaded = load_groups(path=path)

    assert updated.key == created.key
    assert len(loaded) == 1
    assert loaded[0].name == "Main lab"
    assert loaded[0].device_keys == ["dev-b", "dev-c"]
    assert loaded[0].auto_include is True

    delete_group(created.key, path=path)

    assert load_groups(path=path) == []


def test_reorder_groups_preserves_omitted_and_ignores_unknown(tmp_path: Path):
    path = tmp_path / "groups.json"
    save_groups(
        [
            Group(key="a", name="A"),
            Group(key="b", name="B"),
            Group(key="c", name="C"),
        ],
        path=path,
    )

    reordered = reorder_groups(["c", "missing", "a", "c"], path=path)

    assert [group.key for group in reordered] == ["c", "a", "b"]
    assert [group.key for group in load_groups(path=path)] == ["c", "a", "b"]


def test_concurrent_auto_group_writes_do_not_corrupt_file(tmp_path: Path):
    path = tmp_path / "groups.json"
    save_groups(
        [Group(key="all", name="All devices", device_keys=[], auto_include=True)],
        path=path,
    )

    threads = [
        threading.Thread(target=add_device_to_auto_groups, args=(f"dev-{idx}", path))
        for idx in range(20)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2.0)

    loaded = load_groups(path=path)

    assert len(loaded) == 1
    assert sorted(loaded[0].device_keys) == sorted(f"dev-{idx}" for idx in range(20))
    assert list(tmp_path.glob("*.tmp")) == []
