from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import rp_telemetry as rpt


def make_device(key="dev-1"):
    return SimpleNamespace(
        key=key,
        name=key,
        host="10.0.0.1",
        port=18862,
        username="root",
        password="root",
        parameters={},
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main.device_store, "get_device", lambda key: make_device(key))
    return TestClient(main.app)


def test_get_telemetry_is_cache_only(client, monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("the status endpoint must not do I/O")

    monkeypatch.setattr(rpt, "read_telemetry_sync", boom)
    monkeypatch.setattr(main.telemetry_manager, "_open_ssh", boom)

    response = client.get("/api/devices/dev-1/telemetry")

    assert response.status_code == 200
    payload = response.json()
    assert payload["rp_temperature_c"] is None
    assert payload["rp_telemetry"]["state"] == rpt.STATE_UNKNOWN
    assert payload["rp_telemetry"]["bundled_version"] == rpt.BUNDLED_VERSION


def test_get_telemetry_404s_for_an_unknown_device(monkeypatch):
    monkeypatch.setattr(main.device_store, "get_device", lambda key: None)
    client = TestClient(main.app)
    assert client.get("/api/devices/nope/telemetry").status_code == 404


@pytest.mark.parametrize(
    "path,method_name",
    [
        ("install", "install"),
        ("uninstall", "uninstall"),
        ("start", "start_service"),
        ("stop", "stop_service"),
        ("restart", "restart_service"),
    ],
)
def test_management_endpoints_call_the_manager(client, monkeypatch, path, method_name):
    seen: list = []

    def action(device):
        seen.append(device.key)
        return {"ok": True}

    monkeypatch.setattr(main.telemetry_manager, method_name, action)
    response = client.post(f"/api/devices/dev-1/telemetry/{path}")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert seen == ["dev-1"]


def test_single_device_actions_avoid_the_default_executor(client, monkeypatch):
    """Telemetry SSH work shares one bounded pool of its own.

    On the loop's default executor these multi-second SSH sequences would queue
    ahead of /api/devices/statuses and every other asyncio.to_thread() caller.
    """
    import threading as _threading

    seen: list[str] = []

    def action(_device):
        seen.append(_threading.current_thread().name)
        return {"ok": True}

    monkeypatch.setattr(main.telemetry_manager, "install", action)
    assert client.post("/api/devices/dev-1/telemetry/install").status_code == 200

    assert seen and seen[0].startswith("rp-telemetry-ssh")


def test_the_ssh_executor_survives_a_shutdown_and_restart(client, monkeypatch):
    """A disposed pool must be recreated, not reused after shutdown."""
    monkeypatch.setattr(main.telemetry_manager, "install", lambda _device: {"ok": True})

    assert client.post("/api/devices/dev-1/telemetry/install").status_code == 200
    main._shutdown_telemetry_ssh_executor()
    # A second client re-runs the lifespan, as a redeploy or another test would.
    with TestClient(main.app) as restarted:
        assert restarted.post("/api/devices/dev-1/telemetry/install").status_code == 200


def test_management_failures_return_409(client, monkeypatch):
    def action(_device):
        raise RuntimeError("No bundled rp-telemetry binary")

    monkeypatch.setattr(main.telemetry_manager, "install", action)
    response = client.post("/api/devices/dev-1/telemetry/install")

    assert response.status_code == 409
    assert "No bundled" in response.json()["detail"]


def test_service_status_endpoint(client, monkeypatch):
    monkeypatch.setattr(
        main.telemetry_manager,
        "service_status",
        lambda _device: {"installed": True, "active": True, "version": "1.0.0"},
    )
    response = client.get("/api/devices/dev-1/telemetry/service")
    assert response.status_code == 200
    assert response.json()["version"] == "1.0.0"


def test_read_endpoint_refreshes_the_cache(client, monkeypatch):
    async def read_temperature(device):
        return {"rp_temperature_c": 51.0, "rp_telemetry": {"state": "running"}}

    monkeypatch.setattr(main.telemetry_manager, "read_temperature", read_temperature)
    response = client.post("/api/devices/dev-1/telemetry/read")
    assert response.status_code == 200
    assert response.json()["rp_temperature_c"] == 51.0


def test_bulk_install_reports_per_device_outcomes(monkeypatch):
    devices = {key: make_device(key) for key in ("a", "b")}
    monkeypatch.setattr(main.device_store, "get_device", lambda key: devices.get(key))

    def install(device):
        if device.key == "b":
            raise RuntimeError("ssh timed out")
        return {"ok": True}

    monkeypatch.setattr(main.telemetry_manager, "install", install)
    client = TestClient(main.app)

    response = client.post(
        "/api/telemetry/install", json={"device_keys": ["a", "b", "missing"]}
    )

    assert response.status_code == 200
    payload = response.json()
    # One failing board must not fail the batch.
    assert payload["installed"] == ["a"]
    assert payload["failed"]["b"] == "ssh timed out"
    assert payload["failed"]["missing"] == "Device not found"


def test_bulk_start_reports_per_device_outcomes(monkeypatch):
    devices = {key: make_device(key) for key in ("a", "b")}
    monkeypatch.setattr(main.device_store, "get_device", lambda key: devices.get(key))

    def start_service(device):
        if device.key == "b":
            raise RuntimeError("ssh timed out")
        return {"ok": True, "active": True, "state": "active"}

    monkeypatch.setattr(main.telemetry_manager, "start_service", start_service)
    client = TestClient(main.app)

    response = client.post(
        "/api/telemetry/start", json={"device_keys": ["a", "b", "missing"]}
    )

    assert response.status_code == 200
    payload = response.json()
    # One failing board must not fail the batch.
    assert payload["started"] == ["a"]
    assert payload["failed"]["b"] == "ssh timed out"
    assert payload["failed"]["missing"] == "Device not found"


def test_bulk_start_does_not_count_a_unit_that_died_immediately(monkeypatch):
    """`systemctl start` succeeds for a unit that exits straight afterwards.

    Reporting that board as started would be a green summary line for a service
    that is not running -- exactly the failure the single-device UI already
    refuses to paper over.
    """
    devices = {key: make_device(key) for key in ("a",)}
    monkeypatch.setattr(main.device_store, "get_device", lambda key: devices.get(key))
    monkeypatch.setattr(
        main.telemetry_manager,
        "start_service",
        lambda device: {"ok": True, "active": False, "state": "failed"},
    )
    client = TestClient(main.app)

    payload = client.post("/api/telemetry/start", json={"device_keys": ["a"]}).json()

    assert payload["started"] == []
    assert "failed" in payload["failed"]["a"]


def test_status_payload_carries_the_telemetry_fields(monkeypatch):
    """DeviceSession.status() merges the cached telemetry, without any I/O."""

    class Session:
        def status(self):
            base = {"connected": True, "connecting": False}
            base.update(main.telemetry_manager.status_fields("dev-1"))
            return base

    main.telemetry_manager._entry("dev-1").state = rpt.STATE_RUNNING
    main.telemetry_manager._entry("dev-1").temperature_c = 57.3
    try:
        monkeypatch.setattr(main, "_get_session", lambda _key: Session())
        monkeypatch.setattr(main.device_store, "get_device", lambda key: make_device(key))
        client = TestClient(main.app)

        payload = client.get("/api/devices/dev-1/status").json()

        assert payload["rp_temperature_c"] == 57.3
        assert payload["rp_telemetry"]["state"] == rpt.STATE_RUNNING
    finally:
        main.telemetry_manager.forget("dev-1")


def test_statuses_endpoint_stays_cache_only(monkeypatch):
    devices = [make_device("a"), make_device("b")]
    monkeypatch.setattr(main.device_store, "list_devices", lambda: devices)

    class Session:
        def __init__(self, key):
            self.key = key

        def status(self):
            return {
                "connected": False,
                "connecting": False,
                **main.telemetry_manager.status_fields(self.key),
            }

    monkeypatch.setattr(main, "_session_for_device", lambda device: Session(device.key))

    def boom(*_args, **_kwargs):
        raise AssertionError("/statuses must not open sockets or SSH")

    monkeypatch.setattr(rpt, "read_telemetry_sync", boom)
    monkeypatch.setattr(main.telemetry_manager, "_open_ssh", boom)

    client = TestClient(main.app)
    payload = client.get("/api/devices/statuses").json()

    assert set(payload) == {"a", "b"}
    assert payload["a"]["rp_telemetry"]["state"] == rpt.STATE_UNKNOWN


def test_credentials_endpoints_cache_the_influx_destination(monkeypatch):
    from linien_common.influxdb import InfluxDBCredentials

    credentials = InfluxDBCredentials(
        url="http://influx:8086",
        org="lab",
        token="tok",
        bucket="b",
        measurement="m",
    )

    class Session:
        def logging_get_credentials(self):
            return credentials

        def logging_update_credentials(self, _credentials):
            return True, "ok"

    monkeypatch.setattr(main, "_get_session", lambda _key: Session())
    client = TestClient(main.app)
    try:
        assert client.get("/api/devices/dev-1/logging/credentials").status_code == 200
        cached = main.telemetry_manager.get_influx_credentials("dev-1")
        assert cached is not None and cached.bucket == "b"

        main.telemetry_manager.forget("dev-1")
        response = client.put(
            "/api/devices/dev-1/logging/credentials",
            json={
                "url": "http://influx:8086",
                "org": "lab",
                "token": "tok",
                "bucket": "b2",
                "measurement": "m",
            },
        )
        assert response.status_code == 200
        cached = main.telemetry_manager.get_influx_credentials("dev-1")
        assert cached is not None and cached.bucket == "b2"
    finally:
        main.telemetry_manager.forget("dev-1")


def test_failed_credential_update_is_not_cached(monkeypatch):
    class Session:
        def logging_update_credentials(self, _credentials):
            return False, "connection failed"

    monkeypatch.setattr(main, "_get_session", lambda _key: Session())
    client = TestClient(main.app)
    try:
        response = client.put(
            "/api/devices/dev-1/logging/credentials",
            json={
                "url": "http://influx:8086",
                "org": "lab",
                "token": "tok",
                "bucket": "b",
                "measurement": "m",
            },
        )
        assert response.json()["success"] is False
        assert main.telemetry_manager.get_influx_credentials("dev-1") is None
    finally:
        main.telemetry_manager.forget("dev-1")
