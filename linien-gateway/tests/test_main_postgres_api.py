from fastapi.testclient import TestClient

import app.main as main


class FakePostgresService:
    def __init__(self) -> None:
        self.state = {
            "config": {
                "enabled": False,
                "host": "127.0.0.1",
                "port": 5432,
                "database": "experiment_db",
                "user": "admin",
                "password": "adminpassword",
                "sslmode": "prefer",
                "connect_timeout_s": 3.0,
            },
            "status": {
                "active": False,
                "last_test_ok": None,
                "last_test_at": None,
                "last_write_ok": None,
                "last_write_at": None,
                "last_error": None,
                "enqueued_count": 0,
                "write_ok_count": 0,
                "write_error_count": 0,
                "dropped_count": 0,
                "queue_size": 0,
            },
        }

    def start(self) -> None:
        return

    def stop(self) -> None:
        return

    def set_event_callback(self, _callback) -> None:
        return

    def get_state(self):
        return self.state

    def update_config(self, payload):
        self.state["config"] = {**self.state["config"], **payload}
        return self.state

    def test_connection(self):
        self.state["status"]["last_test_ok"] = True
        self.state["status"]["active"] = bool(self.state["config"]["enabled"])
        return True, "ok"

    def enqueue_lock_result(self, _row):
        return True


def test_postgres_endpoints_contract(monkeypatch):
    fake = FakePostgresService()
    monkeypatch.setattr(main, "lock_result_postgres", fake)
    client = TestClient(main.app)

    get_res = client.get("/api/postgres/manual-lock")
    assert get_res.status_code == 200
    assert "config" in get_res.json()
    assert "status" in get_res.json()

    put_res = client.put(
        "/api/postgres/manual-lock",
        json={**fake.state["config"], "enabled": True, "host": "10.0.0.2"},
    )
    assert put_res.status_code == 200
    assert put_res.json()["config"]["enabled"] is True
    assert put_res.json()["config"]["host"] == "10.0.0.2"

    post_res = client.post("/api/postgres/manual-lock/test")
    assert post_res.status_code == 200
    payload = post_res.json()
    assert payload["ok"] is True
    assert "state" in payload
    assert payload["state"]["status"]["last_test_ok"] is True


def test_start_lock_does_not_fail_when_enqueue_raises(monkeypatch):
    class DummySession:
        def start_lock(self):
            return

        def build_manual_lock_row(self, **_kwargs):
            return {
                "laser_name": "dummy",
                "success": True,
                "trace_x": [0.0],
                "trace_y": [0.0],
                "monitor_trace_y": [0.0],
                "trace_x_units": "V",
                "trace_y_units": "V",
                "monitor_trace_y_units": "V",
            }

    class DummyDevice:
        name = "dummy"

    class RaisingPostgres:
        def set_event_callback(self, _callback):
            return

        def enqueue_lock_result(self, _row):
            raise RuntimeError("queue failure")

    monkeypatch.setattr(main, "_get_session", lambda _key: DummySession())
    monkeypatch.setattr(main.device_store, "get_device", lambda _key: DummyDevice())
    monkeypatch.setattr(main, "lock_result_postgres", RaisingPostgres())

    result = main.start_lock("device-key")
    assert result == {"ok": True}


def test_start_lock_enqueues_manual_lock_source(monkeypatch):
    class DummySession:
        def __init__(self) -> None:
            self.last_build_kwargs = None

        def start_lock(self):
            return

        def build_manual_lock_row(self, **kwargs):
            self.last_build_kwargs = kwargs
            return {"lock_source": kwargs.get("lock_source")}

    class DummyDevice:
        name = "dummy"

    class CapturingPostgres:
        def __init__(self) -> None:
            self.rows = []

        def set_event_callback(self, _callback):
            return

        def enqueue_lock_result(self, row):
            self.rows.append(row)
            return True

    session = DummySession()
    postgres = CapturingPostgres()
    monkeypatch.setattr(main, "_get_session", lambda _key: session)
    monkeypatch.setattr(main.device_store, "get_device", lambda _key: DummyDevice())
    monkeypatch.setattr(main, "lock_result_postgres", postgres)

    result = main.start_lock("device-key")
    assert result == {"ok": True}
    assert session.last_build_kwargs is not None
    assert session.last_build_kwargs["lock_source"] == "manual_lock"
    assert postgres.rows
    assert postgres.rows[-1]["lock_source"] == "manual_lock"
