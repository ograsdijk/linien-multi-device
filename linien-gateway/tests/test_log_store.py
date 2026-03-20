import logging
import time

from app.log_store import LogStore


def test_log_store_prunes_by_count():
    store = LogStore(max_entries=3, max_age_s=3600.0)
    store.emit(level=logging.INFO, source="test", code="a", message="1")
    store.emit(level=logging.INFO, source="test", code="b", message="2")
    store.emit(level=logging.INFO, source="test", code="c", message="3")
    store.emit(level=logging.INFO, source="test", code="d", message="4")

    entries = store.tail(limit=10)
    assert len(entries) == 3
    assert [item["message"] for item in entries] == ["2", "3", "4"]


def test_log_store_prunes_by_age():
    now = time.time()
    store = LogStore(max_entries=10, max_age_s=120.0)
    store.emit(
        level=logging.WARNING,
        source="test",
        code="old",
        message="old",
        ts=now - 3600.0,
    )
    store.emit(level=logging.ERROR, source="test", code="new", message="new", ts=now)

    entries = store.tail(limit=10)
    assert len(entries) == 1
    assert entries[0]["message"] == "new"
    assert entries[0]["level"] == logging.ERROR
    assert entries[0]["level_name"] == "error"
