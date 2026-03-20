from pathlib import Path

from app.group_store import Group, load_groups, save_groups


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


def test_save_groups_roundtrip(tmp_path: Path):
    path = tmp_path / "groups.json"
    source = [Group(key="a1", name="All", device_keys=["d1"], auto_include=True)]

    save_groups(source, path=path)
    loaded = load_groups(path=path)

    assert len(loaded) == 1
    assert loaded[0].key == "a1"
    assert loaded[0].auto_include is True
