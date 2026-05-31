from fastapi.testclient import TestClient

import app.main as main
from app.group_store import Group


def test_reorder_groups_endpoint(monkeypatch):
    groups = [
        Group(key="a", name="A", device_keys=[]),
        Group(key="b", name="B", device_keys=[]),
        Group(key="c", name="C", device_keys=[]),
    ]

    def fake_reorder(keys):
        by_key = {group.key: group for group in groups}
        ordered = [by_key[key] for key in keys if key in by_key]
        ordered.extend(group for group in groups if group.key not in keys)
        return ordered

    monkeypatch.setattr(main.group_store, "reorder_groups", fake_reorder)
    client = TestClient(main.app)

    response = client.put("/api/groups/order", json={"keys": ["c", "a"]})

    assert response.status_code == 200
    assert [group["key"] for group in response.json()] == ["c", "a", "b"]
