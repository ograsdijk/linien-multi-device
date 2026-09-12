import time

from app.board_event_store import (
    KIND_DISCONNECTED,
    KIND_REBOOT_DETECTED,
    BoardEventStore,
)


def make_store(tmp_path, **kwargs):
    kwargs.setdefault("flush_interval_s", 0.0)
    return BoardEventStore(tmp_path / "board_events.json", **kwargs)


def test_events_come_back_newest_first(tmp_path):
    """The UI renders a timeline top-down and must not have to reverse it."""
    store = make_store(tmp_path)
    now = time.time()
    store.record("dev-1", KIND_DISCONNECTED, detail="first", ts=now - 10.0)
    store.record("dev-1", KIND_DISCONNECTED, detail="second", ts=now)

    events = store.events("dev-1")

    assert [event["detail"] for event in events] == ["second", "first"]


def test_events_are_kept_per_device(tmp_path):
    store = make_store(tmp_path)
    store.record("dev-1", KIND_DISCONNECTED, detail="a")
    store.record("dev-2", KIND_DISCONNECTED, detail="b")

    assert [event["detail"] for event in store.events("dev-1")] == ["a"]
    assert [event["detail"] for event in store.events("dev-2")] == ["b"]
    assert store.events("never-seen") == []


def test_the_ring_is_bounded_per_device(tmp_path):
    store = make_store(tmp_path, max_per_device=3)
    now = time.time()
    for index in range(10):
        store.record("dev-1", KIND_DISCONNECTED, detail=str(index), ts=now + index)

    events = store.events("dev-1")

    assert [event["detail"] for event in events] == ["9", "8", "7"]


def test_events_older_than_the_window_are_dropped(tmp_path):
    store = make_store(tmp_path, max_age_s=60.0)
    now = time.time()
    store.record("dev-1", KIND_DISCONNECTED, detail="ancient", ts=now - 3600.0)
    store.record("dev-1", KIND_DISCONNECTED, detail="recent", ts=now)

    assert [event["detail"] for event in store.events("dev-1")] == ["recent"]


def test_the_timeline_survives_a_gateway_restart(tmp_path):
    """The restart is often part of the incident; the history must outlive it."""
    store = make_store(tmp_path)
    store.record("dev-1", KIND_DISCONNECTED, detail="the night before")
    store.flush(block=True)

    reloaded = make_store(tmp_path)

    assert [event["detail"] for event in reloaded.events("dev-1")] == [
        "the night before"
    ]


def test_a_corrupt_file_is_ignored_rather_than_fatal(tmp_path):
    path = tmp_path / "board_events.json"
    path.write_text("{not json", encoding="utf-8")

    store = BoardEventStore(path)

    assert store.events("dev-1") == []


def test_the_first_boot_id_is_not_a_reboot(tmp_path):
    """We had nothing to compare against, which is not evidence of a restart."""
    store = make_store(tmp_path)

    assert store.note_boot_id("dev-1", "boot-a") is False
    assert store.events("dev-1") == []
    assert store.last_boot_id("dev-1") == "boot-a"


def test_an_unchanged_boot_id_is_not_a_reboot(tmp_path):
    store = make_store(tmp_path)
    store.note_boot_id("dev-1", "boot-a")

    assert store.note_boot_id("dev-1", "boot-a") is False
    assert store.events("dev-1") == []


def test_a_changed_boot_id_records_a_reboot(tmp_path):
    store = make_store(tmp_path)
    store.note_boot_id("dev-1", "boot-a")

    assert store.note_boot_id("dev-1", "boot-b") is True

    events = store.events("dev-1")
    assert len(events) == 1
    assert events[0]["kind"] == KIND_REBOOT_DETECTED
    assert events[0]["boot_id"] == "boot-b"
    assert store.last_boot_id("dev-1") == "boot-b"


def test_the_remembered_boot_id_survives_a_restart(tmp_path):
    """Otherwise every gateway restart would re-arm a false reboot report."""
    store = make_store(tmp_path)
    store.note_boot_id("dev-1", "boot-a")
    store.record("dev-1", KIND_DISCONNECTED, detail="x")
    store.flush(block=True)

    reloaded = make_store(tmp_path)

    assert reloaded.last_boot_id("dev-1") == "boot-a"
    assert reloaded.note_boot_id("dev-1", "boot-a") is False


def test_forgetting_a_device_clears_both_history_and_boot_id(tmp_path):
    store = make_store(tmp_path)
    store.note_boot_id("dev-1", "boot-a")
    store.record("dev-1", KIND_DISCONNECTED, detail="x")

    store.forget("dev-1")

    assert store.events("dev-1") == []
    assert store.last_boot_id("dev-1") is None


def test_an_unserialisable_detail_does_not_poison_the_file(tmp_path):
    """One bad `details` value must not cost every later write.

    `data` comes from free-form log-event details, so the store coerces at the
    door rather than discovering the problem at json.dumps time -- where the
    failure would be the whole file, not the one field.
    """
    store = make_store(tmp_path)

    assert store.record("dev-1", KIND_DISCONNECTED, data={"obj": object()}) is not None
    store.record("dev-1", KIND_DISCONNECTED, detail="after the bad one")
    store.flush(block=True)

    reloaded = make_store(tmp_path)
    details = [event["detail"] for event in reloaded.events("dev-1")]
    assert "after the bad one" in details


def test_an_unwritable_path_does_not_break_recording(tmp_path):
    store = BoardEventStore(tmp_path / "nope" / "x" / "board_events.json")
    store.record("dev-1", KIND_DISCONNECTED, detail="x")
    store.flush(block=True)

    assert [event["detail"] for event in store.events("dev-1")] == ["x"]


def test_a_backgrounded_flush_still_lands(tmp_path):
    """The default path is async so it never blocks the event loop."""
    store = make_store(tmp_path)
    store.record("dev-1", KIND_DISCONNECTED, detail="async")
    store.close()

    reloaded = make_store(tmp_path)
    assert [event["detail"] for event in reloaded.events("dev-1")] == ["async"]


def test_a_failed_write_leaves_the_events_pending_not_lost(tmp_path):
    """A full disk must not silently discard the history.

    Clearing the dirty flag before the write meant one transient failure made
    every later flush -- including the one at shutdown -- believe there was
    nothing to write.
    """
    path = tmp_path / "board_events.json"
    store = BoardEventStore(path, flush_interval_s=0.0)
    store.record("dev-1", KIND_DISCONNECTED, detail="precious")

    # Make the write fail (the parent is a file, so mkdir cannot succeed),
    # then let it succeed.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    store._path = blocker / "board_events.json"
    store.flush(block=True)
    assert store._dirty is True

    store._path = path
    store.flush(block=True)

    reloaded = make_store(tmp_path)
    assert [event["detail"] for event in reloaded.events("dev-1")] == ["precious"]


def test_concurrent_recording_never_publishes_a_torn_file(tmp_path):
    """Two writers sharing one temp path could publish half a file, and a torn
    file is read back as corrupt -- losing the whole retained history."""
    import threading

    store = make_store(tmp_path)

    def worker(index):
        for n in range(20):
            store.record(f"dev-{index}", KIND_DISCONNECTED, detail=f"{index}-{n}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    store.close()

    reloaded = make_store(tmp_path)
    # A corrupt file would come back empty for every device.
    assert sum(len(reloaded.events(f"dev-{i}")) for i in range(4)) == 80

