from fastapi.testclient import TestClient

import app.main as main


def test_range_selection_rejects_out_of_bounds():
    client = TestClient(main.app)

    response = client.post(
        "/api/devices/test-device/control/start_autolock",
        json={"x0": -1, "x1": 1024},
    )

    assert response.status_code == 422


def test_lock_indicator_rejects_invalid_std_bounds():
    client = TestClient(main.app)

    payload = {
        "enabled": True,
        "bad_hold_s": 1.0,
        "good_hold_s": 1.0,
        "use_control": True,
        "control_stuck_delta_counts": 0,
        "control_stuck_time_s": 1.0,
        "control_rail_threshold_v": 0.9,
        "control_rail_hold_s": 1.0,
        "use_error": True,
        "error_mean_abs_max_v": 0.2,
        "error_std_min_v": 0.5,
        "error_std_max_v": 0.1,
        "use_monitor": False,
        "monitor_mode": "locked_above",
        "monitor_threshold_v": 0.0,
    }
    response = client.put("/api/devices/test-device/lock-indicator", json=payload)

    assert response.status_code == 422


def test_postgres_config_rejects_invalid_sslmode():
    client = TestClient(main.app)

    payload = {
        "enabled": False,
        "host": "127.0.0.1",
        "port": 5432,
        "database": "experiment_db",
        "user": "admin",
        "password": "adminpassword",
        "sslmode": "invalid-mode",
        "connect_timeout_s": 3.0,
    }
    response = client.put("/api/postgres/manual-lock", json=payload)

    assert response.status_code == 422
