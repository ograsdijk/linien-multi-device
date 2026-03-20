from pathlib import Path

import app.manual_lock_postgres as mlp
from app.manual_lock_postgres import (
    LockResultPostgresConfig,
    LockResultPostgresService,
    load_lock_result_postgres_config,
    save_lock_result_postgres_config,
)


class FakeCursor:
    def __init__(self, calls: list[tuple[str, object | None]]) -> None:
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params=None) -> None:
        self.calls.append((sql.strip(), params))


class FakeConnection:
    def __init__(self, calls: list[tuple[str, object | None]], kwargs: dict) -> None:
        self.calls = calls
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.calls)

    def commit(self) -> None:
        self.calls.append(("COMMIT", None))


class FakePsycopg:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object | None]] = []
        self.connect_kwargs: list[dict] = []

    def connect(self, **kwargs):
        self.connect_kwargs.append(kwargs)
        return FakeConnection(self.calls, kwargs)


def test_manual_lock_postgres_config_roundtrip(tmp_path: Path):
    config_path = tmp_path / "manual_lock_postgres.json"
    config = LockResultPostgresConfig(
        enabled=True,
        host="10.0.0.2",
        port=5433,
        database="locks",
        user="writer",
        password="secret",
        sslmode="require",
        connect_timeout_s=5,
    )
    save_lock_result_postgres_config(config, config_path)
    loaded = load_lock_result_postgres_config(config_path)
    assert loaded == config


def test_service_test_connection_and_write_row(monkeypatch, tmp_path: Path):
    fake_driver = FakePsycopg()
    monkeypatch.setattr(mlp, "psycopg", fake_driver)
    service = LockResultPostgresService(config_path=tmp_path / "cfg.json")

    state = service.update_config(
        {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 5432,
            "database": "experiment_db",
            "user": "admin",
            "password": "adminpassword",
            "sslmode": "prefer",
            "connect_timeout_s": 3,
        }
    )
    assert state["config"]["enabled"] is True
    ok, _detail = service.test_connection()
    assert ok is True
    assert any("CREATE TABLE IF NOT EXISTS pdh_lock_results" in sql for sql, _ in fake_driver.calls)

    service._write_row(  # noqa: SLF001 - explicit unit-level check of writer behavior
        {
            "laser_name": "L1",
            "lock_source": "manual_lock",
            "success": True,
            "modulation_frequency_hz": 1.0,
            "demod_phase_deg": 10.0,
            "signal_offset_volts": 0.1,
            "modulation_amplitude": 0.2,
            "pid_p": 1.0,
            "pid_i": 2.0,
            "pid_d": 3.0,
            "trace_x": [0.0, 1.0],
            "trace_y": [0.0, 1.0],
            "monitor_trace_y": [0.25, 0.5],
            "trace_x_units": "V",
            "trace_y_units": "V",
            "monitor_trace_y_units": "V",
        }
    )
    assert any("INSERT INTO pdh_lock_results" in sql for sql, _ in fake_driver.calls)
    insert_calls = [
        (sql, params)
        for sql, params in fake_driver.calls
        if "INSERT INTO pdh_lock_results" in sql
    ]
    assert insert_calls
    assert insert_calls[-1][1]["lock_source"] == "manual_lock"
    assert insert_calls[-1][1]["monitor_trace_y"] == [0.25, 0.5]
