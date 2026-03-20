import logging

from app.session import DeviceSession


class _DummyDevice:
    key = "dev-a"
    name = "Device A"
    parameters = {}


class _DummyManager:
    def publish(self, *_args, **_kwargs):
        return None


def _build_session(postgres_service):
    events = []

    def _capture(level, source, code, message, device_key=None, details=None):
        events.append(
            {
                "level": int(level),
                "source": source,
                "code": code,
                "message": message,
                "device_key": device_key,
                "details": details or {},
            }
        )

    session = DeviceSession(
        _DummyDevice(),
        _DummyManager(),
        lock_result_postgres_service=postgres_service,
        log_event_callback=_capture,
    )
    return session, events


def test_write_lock_result_logs_enqueued():
    class _Postgres:
        def __init__(self):
            self.rows = []

        def enqueue_lock_result(self, row):
            self.rows.append(row)
            return True

        def get_state(self):
            return {
                "config": {"enabled": True},
                "status": {"active": True, "last_error": None},
            }

    postgres = _Postgres()
    session, events = _build_session(postgres)

    session._write_lock_result_to_postgres(
        lock_source="auto_relock",
        event_source="auto_relock",
    )

    assert len(postgres.rows) == 1
    assert any(entry["code"] == "lock_result_postgres_enqueued" for entry in events)


def test_write_lock_result_logs_disabled_skip():
    class _Postgres:
        def enqueue_lock_result(self, _row):
            return False

        def get_state(self):
            return {
                "config": {"enabled": False},
                "status": {"active": False, "last_error": None},
            }

    session, events = _build_session(_Postgres())

    session._write_lock_result_to_postgres(
        lock_source="auto_relock",
        event_source="auto_relock",
    )

    matching = [entry for entry in events if entry["code"] == "lock_result_postgres_skipped_disabled"]
    assert len(matching) == 1
    assert matching[0]["level"] == logging.INFO


def test_write_lock_result_logs_rejected_as_error():
    class _Postgres:
        def enqueue_lock_result(self, _row):
            return False

        def get_state(self):
            return {
                "config": {"enabled": True},
                "status": {"active": False, "last_error": "connect_failed"},
            }

    session, events = _build_session(_Postgres())

    session._write_lock_result_to_postgres(
        lock_source="auto_relock",
        event_source="auto_relock",
    )

    matching = [entry for entry in events if entry["code"] == "lock_result_postgres_enqueue_rejected"]
    assert len(matching) == 1
    assert matching[0]["level"] == logging.ERROR
    assert matching[0]["details"].get("lock_source") == "auto_relock"


def test_lock_indicator_transition_emits_lost_and_acquired():
    session, events = _build_session(postgres_service=None)
    snapshot_lost = {
        "state": "lost",
        "reasons": ["error_std_too_low"],
        "metrics": {"error_std_v": 1e-4, "control_mean_v": 0.2},
    }
    snapshot_locked = {
        "state": "locked",
        "reasons": [],
        "metrics": {"error_std_v": 2e-3, "control_mean_v": 0.01},
    }

    session._emit_lock_transition_log(
        lock_enabled=True,
        indicator_state="marginal",
        indicator_snapshot={"state": "marginal", "reasons": [], "metrics": {}},
    )
    session._emit_lock_transition_log(
        lock_enabled=True,
        indicator_state="lost",
        indicator_snapshot=snapshot_lost,
    )
    session._emit_lock_transition_log(
        lock_enabled=True,
        indicator_state="lost",
        indicator_snapshot=snapshot_lost,
    )
    session._emit_lock_transition_log(
        lock_enabled=True,
        indicator_state="locked",
        indicator_snapshot=snapshot_locked,
    )

    lost = [entry for entry in events if entry["code"] == "lock_lost"]
    acquired = [entry for entry in events if entry["code"] == "lock_acquired"]
    assert len(lost) == 1
    assert len(acquired) == 1
    assert lost[0]["level"] == logging.ERROR
    assert acquired[0]["level"] == logging.INFO


def test_auto_relock_state_transition_logs_key_stages_once():
    session, events = _build_session(postgres_service=None)

    session._emit_auto_relock_state_transition_log(
        {"enabled": True, "state": "idle", "attempts": 0, "max_attempts": 2}
    )
    session._emit_auto_relock_state_transition_log(
        {"enabled": True, "state": "lost_pending", "attempts": 0, "max_attempts": 2}
    )
    session._emit_auto_relock_state_transition_log(
        {"enabled": True, "state": "lost_pending", "attempts": 0, "max_attempts": 2}
    )
    session._emit_auto_relock_state_transition_log(
        {
            "enabled": True,
            "state": "waiting_unlocked_trace",
            "attempts": 1,
            "max_attempts": 2,
        }
    )

    lost_pending = [entry for entry in events if entry["code"] == "auto_relock_lost_pending"]
    waiting = [
        entry
        for entry in events
        if entry["code"] == "auto_relock_waiting_unlocked_trace"
    ]
    assert len(lost_pending) == 1
    assert len(waiting) == 1
