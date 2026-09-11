from __future__ import annotations

import asyncio
import copy
import threading
import time
from types import SimpleNamespace

import pytest

from app import rp_telemetry as rpt
from app.influx_writer import InfluxDestination, InfluxWriteError


def make_device(key="dev-1", host="10.0.0.1", parameters=None, username="root"):
    return SimpleNamespace(
        key=key,
        host=host,
        port=18862,
        username=username,
        password="root",
        name=key,
        parameters={} if parameters is None else parameters,
    )


def make_manager(devices, **kwargs):
    saved: list = []
    published: list[str] = []
    logs: list[dict] = []

    kwargs.setdefault("save_device", saved.append)
    kwargs.setdefault("status_publisher", published.append)
    kwargs.setdefault("log_callback", lambda **entry: logs.append(entry))
    manager = rpt.RpTelemetryManager(
        device_provider=lambda: devices,
        **kwargs,
    )
    return manager, saved, published, logs


def reading_fn(*readings):
    """Async read_fn returning the given readings in order, then repeating."""
    queue = list(readings)

    async def _read(host, port, **_kwargs):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return _read


async def _no_version(*_args, **_kwargs):
    return None


# --- caching / state ----------------------------------------------------


def test_successful_poll_caches_the_temperature():
    device = make_device()
    manager, _saved, published, _logs = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=57.3)),
        version_fn=_no_version,
    )
    asyncio.run(manager.poll_once())

    fields = manager.status_fields("dev-1")
    assert fields["rp_temperature_c"] == 57.3
    assert fields["rp_telemetry"]["state"] == rpt.STATE_RUNNING
    assert fields["rp_telemetry"]["error"] is None
    assert isinstance(fields["rp_temperature_sampled_at"], float)
    assert published == ["dev-1"]


def test_status_fields_for_an_unknown_device_are_inert():
    manager, *_ = make_manager([])
    fields = manager.status_fields("nope")
    assert fields["rp_temperature_c"] is None
    assert fields["rp_telemetry"]["state"] == rpt.STATE_UNKNOWN


def test_unchanged_reading_does_not_republish():
    device = make_device()
    manager, _saved, published, _logs = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=57.30)),
        version_fn=_no_version,
    )
    asyncio.run(manager.poll_once())
    asyncio.run(manager.poll_once())
    asyncio.run(manager.poll_once())
    # Rounded to 0.1 C, so a settled board publishes once, not once per cycle.
    assert published == ["dev-1"]


def test_temperature_change_republishes():
    device = make_device()
    readings = [
        rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=57.3),
        rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=59.9),
    ]
    manager, _saved, published, _logs = make_manager(
        [device], read_fn=reading_fn(*readings), version_fn=_no_version
    )
    asyncio.run(manager.poll_once())
    asyncio.run(manager.poll_once())
    assert published == ["dev-1", "dev-1"]


def test_stale_detection_hides_an_old_reading():
    device = make_device()
    manager, *_ = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=57.3)),
        version_fn=_no_version,
        stale_after_s=90.0,
    )
    asyncio.run(manager.poll_once())
    assert manager.status_fields("dev-1")["rp_telemetry"]["state"] == rpt.STATE_RUNNING

    entry = manager._entry("dev-1")
    entry.sampled_at = time.time() - 120.0
    fields = manager.status_fields("dev-1")
    assert fields["rp_telemetry"]["state"] == rpt.STATE_STALE
    # The last value is still carried for context; the UI keys off the state.
    assert fields["rp_temperature_c"] == 57.3


def test_refused_connection_on_an_uninstalled_board_reads_not_installed():
    device = make_device()
    manager, *_ = make_manager(
        [device],
        read_fn=reading_fn(
            rpt.TelemetryReading(rpt.STATE_STOPPED, error="connection refused")
        ),
        version_fn=_no_version,
    )
    asyncio.run(manager.poll_once())
    assert manager.status_fields("dev-1")["rp_telemetry"]["state"] == (
        rpt.STATE_NOT_INSTALLED
    )


def test_refused_connection_on_an_installed_board_reads_stopped():
    device = make_device(
        parameters={rpt.DEVICE_PARAM_KEY: {"installed": True, "version": "1.0.0"}}
    )
    manager, *_ = make_manager(
        [device],
        read_fn=reading_fn(
            rpt.TelemetryReading(rpt.STATE_STOPPED, error="connection refused")
        ),
        version_fn=_no_version,
    )
    asyncio.run(manager.poll_once())
    telemetry = manager.status_fields("dev-1")["rp_telemetry"]
    assert telemetry["state"] == rpt.STATE_STOPPED
    assert telemetry["installed"] is True


def test_version_is_only_requested_once_per_daemon_lifetime():
    device = make_device()
    calls: list[str] = []

    async def version_fn(host, port, **_kwargs):
        calls.append(host)
        return "1.0.0"

    manager, *_ = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)),
        version_fn=version_fn,
    )
    asyncio.run(manager.poll_once())
    asyncio.run(manager.poll_once())
    asyncio.run(manager.poll_once())
    assert calls == ["10.0.0.1"]
    assert manager.status_fields("dev-1")["rp_telemetry"]["version"] == "1.0.0"


def test_installed_board_is_still_asked_for_its_running_version():
    """The install record must not suppress the probe.

    The record says what the gateway installed, which is exactly the thing that
    can be wrong (an out-of-band reflash, a record written by another gateway).
    Version/update detection is worthless if it only ever reads back the record.
    """
    device = make_device(
        parameters={rpt.DEVICE_PARAM_KEY: {"installed": True, "version": "1.0.0"}}
    )
    calls: list[str] = []

    async def version_fn(host, port, **_kwargs):
        calls.append(host)
        return "0.9.0"  # what is actually running, contradicting the record

    manager, *_ = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)),
        version_fn=version_fn,
    )
    asyncio.run(manager.poll_once())

    assert calls == ["10.0.0.1"]
    telemetry = manager.status_fields("dev-1")["rp_telemetry"]
    assert telemetry["version"] == "0.9.0"
    assert telemetry["update_available"] is True


def test_version_is_re_probed_after_the_daemon_goes_away():
    device = make_device()
    versions = iter(["1.0.0", "2.0.0"])
    calls: list[str] = []

    async def version_fn(host, port, **_kwargs):
        calls.append(host)
        return next(versions)

    states = [
        rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0),
        rpt.TelemetryReading(rpt.STATE_OFFLINE, error="timed out"),
        rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0),
    ]

    async def read_fn(*_args, **_kwargs):
        return states.pop(0)

    manager, *_ = make_manager([device], read_fn=read_fn, version_fn=version_fn)
    for _ in range(3):
        asyncio.run(manager.poll_once())

    # Whatever came back may be a different build, so it is asked again.
    assert len(calls) == 2
    assert manager.status_fields("dev-1")["rp_telemetry"]["version"] == "2.0.0"


def test_a_daemon_that_cannot_answer_version_is_not_re_asked_every_cycle():
    device = make_device()
    calls: list[str] = []

    async def version_fn(host, port, **_kwargs):
        calls.append(host)
        return None

    manager, *_ = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)),
        version_fn=version_fn,
    )
    for _ in range(4):
        asyncio.run(manager.poll_once())
    assert len(calls) == 1


def test_poll_loop_never_writes_to_the_device_store():
    """The poll loop gets the *cached* Device objects from list_devices().

    device_store only deep-copies in get_device(), so any mutation here would
    poison the cache, and save_device() would rewrite devices.json from the
    event loop and could clobber a concurrent edit.
    """
    parameters = {rpt.DEVICE_PARAM_KEY: {"installed": True, "version": "0.9.0"}}
    device = make_device(parameters=parameters)
    before = copy.deepcopy(parameters)

    async def version_fn(*_args, **_kwargs):
        return "0.9.0"  # mismatched, which is what used to trigger a write

    manager, saved, _published, logs = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)),
        version_fn=version_fn,
    )
    for _ in range(3):
        asyncio.run(manager.poll_once())

    assert saved == [], "the poll loop must not persist devices"
    assert device.parameters == before, "the poll loop must not mutate the device"
    # The warning still fires, just once, tracked in the in-memory cache.
    mismatches = [e for e in logs if e["code"] == "rp_telemetry_version_mismatch"]
    assert len(mismatches) == 1


def test_credential_lookup_runs_off_the_event_loop():
    """A blocking credential fetch must not freeze the loop.

    _resolve_credentials can fall back to an RPyC round trip under the session's
    _rpyc_lock. On a wedged device that takes seconds, and if it ran inline in
    the poll coroutine it would stall every websocket and HTTP handler.
    """
    device = _influx_device()
    writer = RecordingWriter()
    fetch_threads: list[int] = []

    def fetcher(key):
        fetch_threads.append(threading.get_ident())
        time.sleep(0.2)  # stand-in for a blocking RPyC call
        return FakeCredentials()

    manager, *_ = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)),
        version_fn=_no_version,
        influx_writer=writer,
        credentials_fetcher=fetcher,
    )

    async def run():
        loop_thread = threading.get_ident()
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        await manager.poll_once()
        beat.cancel()
        return loop_thread, ticks

    loop_thread, ticks = asyncio.run(run())

    assert fetch_threads and fetch_threads[0] != loop_thread, (
        "the blocking credential fetch ran on the event loop"
    )
    # The loop kept running during the 0.2 s blocking fetch.
    assert ticks >= 4
    assert len(writer.calls) == 1


def test_update_available_when_the_board_runs_an_older_build():
    device = make_device(
        parameters={rpt.DEVICE_PARAM_KEY: {"installed": True, "version": "0.9.0"}}
    )

    async def version_fn(*_args, **_kwargs):
        return "0.9.0"

    manager, _saved, _published, logs = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)),
        version_fn=version_fn,
    )
    asyncio.run(manager.poll_once())
    telemetry = manager.status_fields("dev-1")["rp_telemetry"]
    assert telemetry["update_available"] is True
    assert telemetry["bundled_version"] == rpt.BUNDLED_VERSION
    assert any(entry["code"] == "rp_telemetry_version_mismatch" for entry in logs)


def test_sustained_loss_is_logged_once_then_recovery_is_logged():
    device = make_device(
        parameters={rpt.DEVICE_PARAM_KEY: {"installed": True, "version": "1.0.0"}}
    )
    offline = rpt.TelemetryReading(rpt.STATE_OFFLINE, error="timed out")
    good = rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)
    queue = [offline, offline, offline, offline, offline, good]

    async def read_fn(*_args, **_kwargs):
        return queue.pop(0)

    manager, _saved, _published, logs = make_manager(
        [device], read_fn=read_fn, version_fn=_no_version
    )
    for _ in range(6):
        asyncio.run(manager.poll_once())

    unavailable = [e for e in logs if e["code"] == "rp_telemetry_unavailable"]
    recovered = [e for e in logs if e["code"] == "rp_telemetry_recovered"]
    assert len(unavailable) == 1, "an offline board must not warn every cycle"
    assert len(recovered) == 1


def test_one_unreachable_device_does_not_block_the_others():
    devices = [make_device("slow", "10.0.0.1"), make_device("fast", "10.0.0.2")]

    async def read_fn(host, port, **_kwargs):
        if host == "10.0.0.1":
            await asyncio.sleep(0.3)
            return rpt.TelemetryReading(rpt.STATE_OFFLINE, error="timed out")
        return rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=44.0)

    manager, *_ = make_manager([devices[0], devices[1]], read_fn=read_fn, version_fn=_no_version)

    started = time.monotonic()
    asyncio.run(manager.poll_once())
    elapsed = time.monotonic() - started

    # Concurrent, so the whole cycle costs about one slow device, not the sum.
    assert elapsed < 0.6
    assert manager.status_fields("fast")["rp_temperature_c"] == 44.0
    assert manager.status_fields("slow")["rp_telemetry"]["state"] == rpt.STATE_OFFLINE


def test_a_raising_read_does_not_break_the_cycle():
    devices = [make_device("boom", "10.0.0.1"), make_device("ok", "10.0.0.2")]

    async def read_fn(host, port, **_kwargs):
        if host == "10.0.0.1":
            raise RuntimeError("unexpected")
        return rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=44.0)

    manager, *_ = make_manager(devices, read_fn=read_fn, version_fn=_no_version)
    asyncio.run(manager.poll_once())
    assert manager.status_fields("ok")["rp_temperature_c"] == 44.0


def test_forget_drops_the_cache_entry():
    device = make_device()
    manager, *_ = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)),
        version_fn=_no_version,
    )
    asyncio.run(manager.poll_once())
    manager.forget("dev-1")
    assert manager.status_fields("dev-1")["rp_temperature_c"] is None


# --- InfluxDB -----------------------------------------------------------


class FakeCredentials:
    def __init__(self, measurement="linien"):
        self.url = "http://influx.example:8086"
        self.org = "lab"
        self.token = "secret"
        self.bucket = "linien"
        self.measurement = measurement


class RecordingWriter:
    def __init__(self, error: Exception | None = None):
        self.calls: list[tuple[InfluxDestination, list[str]]] = []
        self.error = error

    def write(self, destination, lines):
        self.calls.append((destination, list(lines)))
        if self.error is not None:
            raise self.error

    def close(self):
        pass


def _influx_device(key="dev-1", host="10.0.0.1"):
    return make_device(
        key,
        host,
        parameters={"influx_logging_state": {"enabled": True, "interval_s": 1.0}},
    )


class NetrefCredentials:
    """Stands in for what RPyC actually returns: a remote reference.

    `InfluxDBCredentials` is a dataclass, so it is not brine-serializable and
    comes back as a netref -- every attribute read is a synchronous round trip
    to the Red Pitaya, and every read raises once the connection is replaced.
    """

    def __init__(self, *, dead=False, reader=None):
        self._dead = dead
        self._reader = reader

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if self._reader is not None:
            self._reader(name)
        if self._dead:
            raise EOFError("stream has been closed")
        return {
            "url": "http://influx.example:8086",
            "org": "lab",
            "token": "secret",
            "bucket": "linien",
            "measurement": "linien",
        }[name]


def test_credentials_are_snapshotted_off_the_event_loop():
    """No RPyC attribute read may happen in the poll coroutine.

    Reading a netref is a round trip to the very board this feature exists to
    keep idle, and it bypasses DeviceSession._rpyc_lock.
    """
    device = _influx_device()
    writer = RecordingWriter()
    read_threads: list[int] = []

    def reader(_name):
        read_threads.append(threading.get_ident())

    manager, *_ = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)),
        version_fn=_no_version,
        influx_writer=writer,
        credentials_fetcher=lambda key: NetrefCredentials(reader=reader),
    )

    async def run():
        await manager.poll_once()
        return threading.get_ident()

    loop_thread = asyncio.run(run())

    assert read_threads, "the credentials were never read"
    assert all(t != loop_thread for t in read_threads)
    assert len(writer.calls) == 1

    # Subsequent cycles must not touch the netref again at all.
    read_threads.clear()
    asyncio.run(manager.poll_once())
    assert read_threads == []


def test_a_dead_netref_does_not_break_other_devices():
    """A reconnect replaces the connection; the old netref then raises EOFError.

    That must not abort the whole cycle's Influx batch, nor repeat forever.
    """
    dead = _influx_device("dead", "10.0.0.1")
    healthy = _influx_device("healthy", "10.0.0.2")
    writer = RecordingWriter()
    manager, *_ = make_manager(
        [dead, healthy],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)),
        version_fn=_no_version,
        influx_writer=writer,
        credentials_fetcher=lambda key: NetrefCredentials(dead=True),
    )
    manager.set_influx_credentials("healthy", FakeCredentials())

    asyncio.run(manager.poll_once())

    # The healthy device still got its point.
    assert len(writer.calls) == 1
    assert len(writer.calls[0][1]) == 1
    # And nothing unusable was cached for the broken one.
    assert manager.get_influx_credentials("dead") is None


def test_setting_credentials_stores_a_local_snapshot():
    manager, *_ = make_manager([])
    manager.set_influx_credentials("dev-1", NetrefCredentials())
    cached = manager.get_influx_credentials("dev-1")

    assert isinstance(cached, rpt.InfluxCredentialSnapshot)
    assert cached.measurement == "linien"
    assert cached.is_usable()


def test_temperature_is_written_to_the_configured_measurement():
    device = _influx_device()
    writer = RecordingWriter()
    manager, *_ = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=57.25)),
        version_fn=_no_version,
        influx_writer=writer,
    )
    manager.set_influx_credentials("dev-1", FakeCredentials())
    asyncio.run(manager.poll_once())

    assert len(writer.calls) == 1
    destination, lines = writer.calls[0]
    assert destination.bucket == "linien"
    assert len(lines) == 1
    # Tagged with the device key: points for devices sharing a destination are
    # batched into one request, and nothing else in the point tells them apart.
    assert lines[0].startswith("linien,device=dev-1 rp_temperature_c=57.25 ")


def test_no_influx_write_without_credentials():
    device = _influx_device()
    writer = RecordingWriter()
    manager, *_ = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)),
        version_fn=_no_version,
        influx_writer=writer,
    )
    asyncio.run(manager.poll_once())
    assert writer.calls == []


def test_no_influx_write_when_device_logging_is_disabled():
    device = make_device(parameters={"influx_logging_state": {"enabled": False}})
    writer = RecordingWriter()
    manager, *_ = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)),
        version_fn=_no_version,
        influx_writer=writer,
    )
    manager.set_influx_credentials("dev-1", FakeCredentials())
    asyncio.run(manager.poll_once())
    assert writer.calls == []


def test_failed_influx_write_does_not_break_telemetry():
    device = _influx_device()
    writer = RecordingWriter(error=InfluxWriteError("HTTP 503"))
    manager, _saved, _published, logs = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)),
        version_fn=_no_version,
        influx_writer=writer,
    )
    manager.set_influx_credentials("dev-1", FakeCredentials())
    asyncio.run(manager.poll_once())
    asyncio.run(manager.poll_once())
    asyncio.run(manager.poll_once())

    # Telemetry itself is unaffected...
    assert manager.status_fields("dev-1")["rp_temperature_c"] == 50.0
    assert manager.status_fields("dev-1")["rp_telemetry"]["state"] == rpt.STATE_RUNNING
    # ...and a persistent Influx outage is reported once, not per cycle.
    failures = [e for e in logs if e["code"] == "rp_telemetry_influx_write_failed"]
    assert len(failures) == 1


def test_influx_recovery_is_reported():
    device = _influx_device()
    writer = RecordingWriter(error=InfluxWriteError("HTTP 503"))
    manager, _saved, _published, logs = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)),
        version_fn=_no_version,
        influx_writer=writer,
    )
    manager.set_influx_credentials("dev-1", FakeCredentials())
    asyncio.run(manager.poll_once())
    writer.error = None
    asyncio.run(manager.poll_once())
    assert any(
        e["code"] == "rp_telemetry_influx_write_recovered" for e in logs
    )


def test_devices_sharing_a_destination_are_batched_into_one_request():
    devices = [_influx_device("a", "10.0.0.1"), _influx_device("b", "10.0.0.2")]
    writer = RecordingWriter()
    manager, *_ = make_manager(
        devices,
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)),
        version_fn=_no_version,
        influx_writer=writer,
    )
    manager.set_influx_credentials("a", FakeCredentials())
    manager.set_influx_credentials("b", FakeCredentials())
    asyncio.run(manager.poll_once())

    assert len(writer.calls) == 1
    assert len(writer.calls[0][1]) == 2


def test_credentials_are_fetched_at_most_once_per_retry_window():
    device = _influx_device()
    writer = RecordingWriter()
    calls: list[str] = []

    def fetcher(key):
        calls.append(key)
        return None

    manager, *_ = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)),
        version_fn=_no_version,
        influx_writer=writer,
        credentials_fetcher=fetcher,
    )
    for _ in range(4):
        asyncio.run(manager.poll_once())
    assert calls == ["dev-1"]


# --- SSH management -----------------------------------------------------


class FakeResult:
    def __init__(self, stdout="", exited=0, stderr=""):
        self.stdout = stdout
        self.stderr = stderr
        self.exited = exited


class FakeConnection:
    """Stands in for a fabric Connection; records the commands issued."""

    def __init__(self, responses=None, put_error=None):
        self.commands: list[str] = []
        self.puts: list[tuple[str, str]] = []
        self.responses = responses or {}
        self.put_error = put_error

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def put(self, local, remote=None):
        if self.put_error is not None:
            raise self.put_error
        self.puts.append((local, remote))

    def run(self, command, **_kwargs):
        self.commands.append(command)
        for needle, result in self.responses.items():
            if needle in command:
                return result
        return FakeResult()


@pytest.fixture
def bundled_binary(tmp_path, monkeypatch):
    binary = tmp_path / "rp-telemetry-armv7"
    binary.write_bytes(b"\x7fELF fake arm binary")
    monkeypatch.setattr(rpt, "BUNDLED_BINARY_PATH", binary)
    return binary


def _install_manager(device, connection, **kwargs):
    manager, saved, published, logs = make_manager(
        [device], connection_factory=lambda *a, **k: connection, **kwargs
    )
    return manager, saved, published, logs


def _sha_response(binary):
    import hashlib

    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    return FakeResult(stdout=f"{digest}  /tmp/rp-telemetry.upload\n")


def test_install_requires_a_bundled_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(rpt, "BUNDLED_BINARY_PATH", tmp_path / "missing")
    manager, *_ = make_manager([make_device()])
    with pytest.raises(RuntimeError, match="No bundled rp-telemetry binary"):
        manager.install(make_device())


def test_install_runs_the_full_flow_and_verifies(bundled_binary, monkeypatch):
    device = make_device()
    connection = FakeConnection(
        {
            "sha256sum": _sha_response(bundled_binary),
            "is-active": FakeResult(stdout="active\n"),
        }
    )
    manager, saved, published, logs = _install_manager(device, connection)
    monkeypatch.setattr(
        rpt,
        "read_telemetry_sync",
        lambda *a, **k: rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=48.0),
    )

    result = manager.install(device)

    assert result == {
        "ok": True,
        "version": rpt.BUNDLED_VERSION,
        "temperature_c": 48.0,
    }
    joined = "\n".join(connection.commands)
    assert connection.puts == [(str(bundled_binary), rpt.REMOTE_UPLOAD_PATH)]
    assert "sha256sum" in joined
    assert f"install -m 0755 {rpt.REMOTE_UPLOAD_PATH} {rpt.REMOTE_STAGE_PATH}" in joined
    # Atomic replacement: staged next to the target, then renamed onto it.
    assert f"mv -f {rpt.REMOTE_STAGE_PATH} {rpt.REMOTE_BINARY_PATH}" in joined
    unit_write = next(c for c in connection.commands if rpt.SERVICE_UNIT_PATH in c)
    assert "Description=Red Pitaya telemetry" in unit_write
    assert f"ExecStart={rpt.REMOTE_BINARY_PATH} --port" in unit_write
    assert "systemctl daemon-reload" in joined
    assert f"systemctl enable {rpt.SERVICE_NAME}" in joined
    assert f"systemctl restart {rpt.SERVICE_NAME}" in joined
    assert f"systemctl is-active {rpt.SERVICE_NAME}" in joined
    # Install record persisted so the poller can tell stopped from never-installed.
    assert device.parameters[rpt.DEVICE_PARAM_KEY]["installed"] is True
    assert saved == [device]
    assert published == ["dev-1"]
    assert any(e["code"] == "rp_telemetry_installed" for e in logs)
    assert manager.status_fields("dev-1")["rp_temperature_c"] == 48.0


def test_install_is_idempotent(bundled_binary, monkeypatch):
    device = make_device()
    connection = FakeConnection(
        {
            "sha256sum": _sha_response(bundled_binary),
            "is-active": FakeResult(stdout="active\n"),
        }
    )
    manager, *_ = _install_manager(device, connection)
    monkeypatch.setattr(
        rpt,
        "read_telemetry_sync",
        lambda *a, **k: rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=48.0),
    )
    first = manager.install(device)
    commands_after_first = len(connection.commands)
    second = manager.install(device)

    assert first == second
    # Same command sequence the second time; nothing accumulates.
    assert len(connection.commands) == commands_after_first * 2
    assert device.parameters[rpt.DEVICE_PARAM_KEY]["installed"] is True


def test_install_rejects_a_corrupted_upload(bundled_binary):
    device = make_device()
    connection = FakeConnection(
        {"sha256sum": FakeResult(stdout="deadbeef  /tmp/rp-telemetry.upload\n")}
    )
    manager, saved, _published, logs = _install_manager(device, connection)

    with pytest.raises(RuntimeError, match="checksum does not match"):
        manager.install(device)

    joined = "\n".join(connection.commands)
    # The bad upload never reaches the installed path.
    assert f"mv -f {rpt.REMOTE_STAGE_PATH}" not in joined
    assert saved == []
    assert any(e["code"] == "rp_telemetry_install_failed" for e in logs)


def test_install_falls_back_to_a_size_check_without_sha256sum(
    bundled_binary, monkeypatch
):
    device = make_device()
    size = len(bundled_binary.read_bytes())
    connection = FakeConnection(
        {
            "sha256sum": FakeResult(exited=127, stderr="sha256sum: not found"),
            "wc -c": FakeResult(stdout=f"{size}\n"),
            "is-active": FakeResult(stdout="active\n"),
        }
    )
    manager, *_ = _install_manager(device, connection)
    monkeypatch.setattr(
        rpt,
        "read_telemetry_sync",
        lambda *a, **k: rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=48.0),
    )
    assert manager.install(device)["ok"] is True


def test_install_detects_a_truncated_upload(bundled_binary):
    device = make_device()
    connection = FakeConnection(
        {
            "sha256sum": FakeResult(exited=127),
            "wc -c": FakeResult(stdout="3\n"),
        }
    )
    manager, *_ = _install_manager(device, connection)
    with pytest.raises(RuntimeError, match="truncated"):
        manager.install(device)


def test_install_fails_when_the_service_does_not_come_up(bundled_binary):
    device = make_device()
    connection = FakeConnection(
        {
            "sha256sum": _sha_response(bundled_binary),
            "is-active": FakeResult(stdout="failed\n", exited=3),
        }
    )
    manager, saved, _published, logs = _install_manager(device, connection)
    with pytest.raises(RuntimeError, match="did not become active"):
        manager.install(device)
    assert saved == []
    assert any(e["code"] == "rp_telemetry_install_failed" for e in logs)


def test_install_fails_when_the_daemon_does_not_answer(bundled_binary, monkeypatch):
    device = make_device()
    connection = FakeConnection(
        {
            "sha256sum": _sha_response(bundled_binary),
            "is-active": FakeResult(stdout="active\n"),
        }
    )
    manager, saved, _published, _logs = _install_manager(device, connection)
    monkeypatch.setattr(
        rpt,
        "read_telemetry_sync",
        lambda *a, **k: rpt.TelemetryReading(rpt.STATE_OFFLINE, error="timed out"),
    )
    with pytest.raises(RuntimeError, match="did not return a temperature"):
        manager.install(device)

    # The binary is in place and the unit is enabled and active, so the board is
    # installed even though the protocol check failed -- recording otherwise
    # would make the UI offer "Install" for a service that starts on every boot.
    assert device.parameters[rpt.DEVICE_PARAM_KEY]["installed"] is True
    assert saved == [device]
    telemetry = manager.status_fields("dev-1")["rp_telemetry"]
    assert telemetry["installed"] is True
    # ...and the state reports the failure rather than claiming it is running.
    assert telemetry["state"] == rpt.STATE_OFFLINE
    assert telemetry["error"] == "timed out"
    assert manager.status_fields("dev-1")["rp_temperature_c"] is None


def test_install_uses_sudo_for_a_non_root_user(bundled_binary, monkeypatch):
    device = make_device(username="pitaya")
    connection = FakeConnection(
        {
            "sha256sum": _sha_response(bundled_binary),
            "is-active": FakeResult(stdout="active\n"),
        }
    )
    manager, *_ = _install_manager(device, connection)
    monkeypatch.setattr(
        rpt,
        "read_telemetry_sync",
        lambda *a, **k: rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=48.0),
    )
    manager.install(device)

    privileged = [c for c in connection.commands if "systemctl" in c]
    assert privileged and all(c.startswith("sudo -n ") for c in privileged)
    # The checksum read needs no privileges and must not ask for them.
    assert any(c.startswith("sha256sum") for c in connection.commands)
    # `sudo -n a && b` would elevate only `a`, so every privileged step must be
    # its own command.
    assert not any("sudo -n" in c and "&&" in c for c in connection.commands)
    assert any(
        c.startswith(f"sudo -n install -m 0755 {rpt.REMOTE_UPLOAD_PATH}")
        for c in connection.commands
    )
    # A shell redirect runs as the calling (unprivileged) user, so the unit file
    # has to be piped through a privileged `tee`, not `sudo -n cat > path`.
    unit_write = next(c for c in connection.commands if rpt.SERVICE_UNIT_PATH in c)
    assert f"sudo -n tee {rpt.SERVICE_UNIT_PATH}" in unit_write
    assert f"> {rpt.SERVICE_UNIT_PATH}" not in unit_write


def test_install_surfaces_an_ssh_failure(bundled_binary):
    device = make_device()
    connection = FakeConnection(put_error=OSError("No route to host"))
    manager, saved, _published, logs = _install_manager(device, connection)
    with pytest.raises(RuntimeError, match="No route to host"):
        manager.install(device)
    assert saved == []
    assert any(e["code"] == "rp_telemetry_install_failed" for e in logs)


def test_uninstall_removes_unit_and_binary_and_clears_the_record():
    device = make_device(
        parameters={rpt.DEVICE_PARAM_KEY: {"installed": True, "version": "1.0.0"}}
    )
    connection = FakeConnection()
    manager, saved, published, logs = _install_manager(device, connection)
    manager._entry("dev-1").installed = True

    assert manager.uninstall(device) == {"ok": True}

    joined = "\n".join(connection.commands)
    assert f"systemctl stop {rpt.SERVICE_NAME}" in joined
    assert f"systemctl disable {rpt.SERVICE_NAME}" in joined
    assert f"rm -f {rpt.SERVICE_UNIT_PATH}" in joined
    assert f"rm -f {rpt.REMOTE_BINARY_PATH}" in joined
    # Leftovers from a half-finished install, ~400 KB each.
    assert f"rm -f {rpt.REMOTE_STAGE_PATH}" in joined
    assert f"rm -f {rpt.REMOTE_UPLOAD_PATH}" in joined
    assert rpt.DEVICE_PARAM_KEY not in device.parameters
    assert saved == [device]
    assert manager.status_fields("dev-1")["rp_telemetry"]["state"] == (
        rpt.STATE_NOT_INSTALLED
    )
    assert any(e["code"] == "rp_telemetry_uninstalled" for e in logs)


@pytest.mark.parametrize(
    "method,action",
    [("start_service", "start"), ("stop_service", "stop"), ("restart_service", "restart")],
)
def test_service_actions(method, action):
    device = make_device()
    connection = FakeConnection({"is-active": FakeResult(stdout="active\n")})
    manager, *_ = _install_manager(device, connection)

    result = getattr(manager, method)(device)

    assert result["ok"] is True
    assert result["active"] is True
    assert f"systemctl {action} {rpt.SERVICE_NAME}" in "\n".join(connection.commands)


def test_start_and_restart_refresh_the_cached_state():
    """The UI refetches the status right after the action.

    Leaving the previous stopped/offline state cached would make a successful
    Start still render as "Telemetry service stopped" with a Start button for
    up to a full poll interval.
    """
    for action, method in (("start", "start_service"), ("restart", "restart_service")):
        device = make_device()
        connection = FakeConnection({"is-active": FakeResult(stdout="active\n")})
        manager, _saved, published, _logs = _install_manager(device, connection)
        entry = manager._entry("dev-1")
        entry.state = rpt.STATE_STOPPED
        entry.error = "connection refused"

        result = getattr(manager, method)(device)

        assert result["active"] is True, action
        telemetry = manager.status_fields("dev-1")["rp_telemetry"]
        assert telemetry["state"] == rpt.STATE_RUNNING, action
        assert telemetry["error"] is None, action
        assert published == ["dev-1"], action


def test_a_failed_start_does_not_claim_the_service_is_running():
    device = make_device()
    connection = FakeConnection({"is-active": FakeResult(stdout="inactive\n", exited=3)})
    manager, *_ = _install_manager(device, connection)
    manager._entry("dev-1").state = rpt.STATE_STOPPED

    result = manager.start_service(device)

    assert result["active"] is False
    assert manager.status_fields("dev-1")["rp_telemetry"]["state"] == rpt.STATE_STOPPED


def test_a_successful_restart_does_not_present_a_stale_reading():
    """The last reading predates the restart.

    Keeping it means `_effective_state` immediately downgrades a just-restarted
    service to `stale` and offers a Restart button for the restart that worked.
    """
    device = make_device()
    connection = FakeConnection({"is-active": FakeResult(stdout="active\n")})
    manager, *_ = _install_manager(device, connection)
    entry = manager._entry("dev-1")
    entry.state = rpt.STATE_STOPPED
    entry.temperature_c = 57.3
    entry.sampled_at = time.time() - 600.0  # long stale

    manager.restart_service(device)

    fields = manager.status_fields("dev-1")
    assert fields["rp_telemetry"]["state"] == rpt.STATE_RUNNING
    assert fields["rp_temperature_c"] is None
    assert fields["rp_temperature_sampled_at"] is None


def test_install_persists_onto_a_freshly_read_device(bundled_binary, monkeypatch):
    """An install holds its device snapshot across the whole SSH sequence.

    Saving that stale snapshot would revert any parameter written meanwhile.
    """
    device = make_device(parameters={"influx_logging_state": {"enabled": False}})
    connection = FakeConnection(
        {
            "sha256sum": _sha_response(bundled_binary),
            "is-active": FakeResult(stdout="active\n"),
        }
    )
    monkeypatch.setattr(
        rpt,
        "read_telemetry_sync",
        lambda *a, **k: rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=48.0),
    )
    saved: list = []
    # What the store holds by the time the install finishes: someone enabled
    # Influx logging while the SSH work was in flight.
    current = make_device(parameters={"influx_logging_state": {"enabled": True}})

    manager, _saved, _published, _logs = make_manager(
        [device],
        save_device=saved.append,
        connection_factory=lambda *a, **k: connection,
        reload_device=lambda key: current,
    )
    manager.install(device)

    assert saved == [current]
    # The concurrent edit survives...
    assert saved[0].parameters["influx_logging_state"]["enabled"] is True
    # ...alongside the new install record.
    assert saved[0].parameters[rpt.DEVICE_PARAM_KEY]["installed"] is True


def test_stop_marks_the_cached_state_stopped():
    device = make_device()
    connection = FakeConnection({"is-active": FakeResult(stdout="inactive\n", exited=3)})
    manager, *_ = _install_manager(device, connection)
    manager.stop_service(device)
    assert manager.status_fields("dev-1")["rp_telemetry"]["state"] == rpt.STATE_STOPPED


def test_restart_failure_raises_and_is_logged():
    device = make_device()
    connection = FakeConnection(
        {f"systemctl restart {rpt.SERVICE_NAME}": FakeResult(exited=5, stderr="nope")}
    )
    manager, _saved, _published, logs = _install_manager(device, connection)
    with pytest.raises(RuntimeError, match="systemctl restart failed"):
        manager.restart_service(device)
    assert any(e["code"] == "rp_telemetry_service_action_failed" for e in logs)


def test_service_status_reports_installed_version():
    device = make_device()
    connection = FakeConnection(
        {
            "is-active": FakeResult(stdout="active\n"),
            "is-enabled": FakeResult(stdout="enabled\n"),
            "--version": FakeResult(stdout="1.0.0\n"),
        }
    )
    manager, *_ = _install_manager(device, connection)

    status = manager.service_status(device)

    assert status["installed"] is True
    assert status["active"] is True
    assert status["enabled_state"] == "enabled"
    assert status["version"] == "1.0.0"
    assert status["bundled_version"] == rpt.BUNDLED_VERSION


def test_service_status_does_not_suppress_the_live_version_probe():
    """`--version` reads the binary on DISK, not the running process.

    A manual scp without a restart (or an install that died between the mv and
    the restart) leaves them different, so marking the version "probed" here
    would pin the cache to the on-disk build for the gateway's lifetime.
    """
    device = make_device(
        parameters={rpt.DEVICE_PARAM_KEY: {"installed": True, "version": "0.9.0"}}
    )
    connection = FakeConnection(
        {
            "is-active": FakeResult(stdout="active\n"),
            "is-enabled": FakeResult(stdout="enabled\n"),
            "--version": FakeResult(stdout="1.0.0\n"),
        }
    )
    probes: list[str] = []

    async def version_fn(host, port, **_kwargs):
        probes.append(host)
        return "0.9.0"  # what is actually running

    manager, *_ = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)),
        version_fn=version_fn,
        connection_factory=lambda *a, **k: connection,
    )

    manager.service_status(device)
    assert manager._entry("dev-1").version == "1.0.0"

    # The next poll still asks the live daemon, and that answer wins.
    asyncio.run(manager.poll_once())

    assert probes == ["10.0.0.1"]
    telemetry = manager.status_fields("dev-1")["rp_telemetry"]
    assert telemetry["version"] == "0.9.0"
    assert telemetry["update_available"] is True


def test_service_status_reports_a_missing_binary():
    device = make_device()
    connection = FakeConnection(
        {
            "is-active": FakeResult(stdout="inactive\n", exited=3),
            "is-enabled": FakeResult(stdout="disabled\n", exited=1),
            "--version": FakeResult(exited=127, stderr="not found"),
        }
    )
    manager, *_ = _install_manager(device, connection)
    status = manager.service_status(device)
    assert status["installed"] is False
    assert status["version"] is None


def test_install_retries_the_protocol_check_before_giving_up(
    bundled_binary, monkeypatch
):
    """systemd reports a Type=simple unit active before it has bound its socket.

    A single immediate check would report a false failure for a good install.
    """
    device = make_device()
    connection = FakeConnection(
        {
            "sha256sum": _sha_response(bundled_binary),
            "is-active": FakeResult(stdout="active\n"),
        }
    )
    manager, *_ = _install_manager(device, connection)
    monkeypatch.setattr(rpt, "INSTALL_VERIFY_INTERVAL_S", 0.0)
    attempts: list[int] = []

    def flaky(*_args, **_kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            return rpt.TelemetryReading(rpt.STATE_STOPPED, error="connection refused")
        return rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=48.0)

    monkeypatch.setattr(rpt, "read_telemetry_sync", flaky)

    assert manager.install(device)["ok"] is True
    assert len(attempts) == 3


def test_install_still_fails_when_the_daemon_never_answers(
    bundled_binary, monkeypatch
):
    device = make_device()
    connection = FakeConnection(
        {
            "sha256sum": _sha_response(bundled_binary),
            "is-active": FakeResult(stdout="active\n"),
        }
    )
    manager, *_ = _install_manager(device, connection)
    monkeypatch.setattr(rpt, "INSTALL_VERIFY_INTERVAL_S", 0.0)
    calls: list[int] = []

    def never(*_args, **_kwargs):
        calls.append(1)
        return rpt.TelemetryReading(rpt.STATE_OFFLINE, error="timed out")

    monkeypatch.setattr(rpt, "read_telemetry_sync", never)

    with pytest.raises(RuntimeError, match="did not return a temperature"):
        manager.install(device)
    assert len(calls) == rpt.INSTALL_VERIFY_ATTEMPTS


def test_an_operator_action_is_not_undone_by_an_in_flight_poll():
    """A poll that started before the action must not overwrite its result."""
    device = make_device()
    release = threading.Event()

    async def slow_read(*_args, **_kwargs):
        # Stand-in for a read that is still in flight when the operator acts.
        await asyncio.get_running_loop().run_in_executor(None, release.wait, 1.0)
        return rpt.TelemetryReading(rpt.STATE_OFFLINE, error="timed out")

    connection = FakeConnection({"is-active": FakeResult(stdout="active\n")})
    manager, *_ = make_manager(
        [device],
        read_fn=slow_read,
        version_fn=_no_version,
        connection_factory=lambda *a, **k: connection,
    )

    async def run():
        poll = asyncio.create_task(manager.poll_once())
        await asyncio.sleep(0.05)
        # Operator starts the service while the poll is still awaiting its read.
        await asyncio.to_thread(manager.start_service, device)
        release.set()
        await poll

    asyncio.run(run())

    telemetry = manager.status_fields("dev-1")["rp_telemetry"]
    assert telemetry["state"] == rpt.STATE_RUNNING
    assert telemetry["error"] is None


def test_an_in_flight_poll_cannot_undo_a_real_install(bundled_binary, monkeypatch):
    """Drives the actual install(), not a stand-in that fakes the seq bump.

    Two holes lived here: `installed` was written outside the guard, and
    install()'s own first write block did not bump the sequence before its
    (multi-second) verification step -- so a poll whose VERSION probe was in
    flight could land afterwards and write back `installed=False` plus the
    pre-install version, leaving the board permanently reported as needing the
    update it had just received.
    """
    device = make_device(
        parameters={rpt.DEVICE_PARAM_KEY: {"installed": True, "version": "0.9.0"}}
    )
    release = threading.Event()

    async def read_fn(*_args, **_kwargs):
        return rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)

    async def slow_version(*_args, **_kwargs):
        # The probe is still in flight when the install lands.
        await asyncio.get_running_loop().run_in_executor(None, release.wait, 2.0)
        return "0.9.0"

    connection = FakeConnection(
        {
            "sha256sum": _sha_response(bundled_binary),
            "is-active": FakeResult(stdout="active\n"),
        }
    )
    monkeypatch.setattr(rpt, "INSTALL_VERIFY_INTERVAL_S", 0.0)
    monkeypatch.setattr(
        rpt,
        "read_telemetry_sync",
        lambda *a, **k: rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=48.0),
    )
    manager, *_ = make_manager(
        [device],
        read_fn=read_fn,
        version_fn=slow_version,
        connection_factory=lambda *a, **k: connection,
    )

    async def run():
        poll = asyncio.create_task(manager.poll_once())
        await asyncio.sleep(0.05)
        await asyncio.to_thread(manager.install, device)
        release.set()
        await poll

    asyncio.run(run())

    telemetry = manager.status_fields("dev-1")["rp_telemetry"]
    assert telemetry["installed"] is True
    assert telemetry["version"] == rpt.BUNDLED_VERSION
    assert telemetry["update_available"] is False


def test_a_restart_during_a_version_probe_still_gets_re_probed():
    """`version_probed` is written under the guard, not next to the probe.

    A Restart asks for a fresh probe (the new process may be a different
    build); a poll whose probe was already in flight must not cancel that.
    """
    device = make_device()
    release = threading.Event()
    probes: list[str] = []

    async def slow_version(host, port, **_kwargs):
        probes.append(host)
        await asyncio.get_running_loop().run_in_executor(None, release.wait, 1.0)
        return "1.0.0"

    connection = FakeConnection({"is-active": FakeResult(stdout="active\n")})
    manager, *_ = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)),
        version_fn=slow_version,
        connection_factory=lambda *a, **k: connection,
    )

    async def run():
        poll = asyncio.create_task(manager.poll_once())
        await asyncio.sleep(0.05)
        await asyncio.to_thread(manager.restart_service, device)
        release.set()
        await poll

    asyncio.run(run())

    assert len(probes) == 1
    # The restart's request for a fresh probe survived the returning poll.
    assert manager._entry("dev-1").version_probed is False


def test_an_in_flight_on_demand_read_cannot_undo_a_stop():
    """The on-demand read is a cache writer like any other: same guard."""
    device = make_device()
    release = threading.Event()

    async def slow_read(*_args, **_kwargs):
        await asyncio.get_running_loop().run_in_executor(None, release.wait, 1.0)
        return rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)

    connection = FakeConnection({"is-active": FakeResult(stdout="inactive\n", exited=3)})
    manager, *_ = make_manager(
        [device],
        read_fn=slow_read,
        version_fn=_no_version,
        connection_factory=lambda *a, **k: connection,
    )

    async def run():
        read = asyncio.create_task(manager.read_temperature(device))
        await asyncio.sleep(0.05)
        await asyncio.to_thread(manager.stop_service, device)
        release.set()
        await read

    asyncio.run(run())

    # The stop wins: the read's result predates it.
    fields = manager.status_fields("dev-1")
    assert fields["rp_telemetry"]["state"] == rpt.STATE_STOPPED
    assert fields["rp_temperature_c"] is None


def test_service_status_wins_over_an_in_flight_version_probe():
    """Read straight off the board, so it outranks a poll's older probe."""
    device = make_device(
        parameters={rpt.DEVICE_PARAM_KEY: {"installed": True, "version": "0.9.0"}}
    )
    release = threading.Event()

    async def slow_version(*_args, **_kwargs):
        await asyncio.get_running_loop().run_in_executor(None, release.wait, 1.0)
        return "0.9.0"

    connection = FakeConnection(
        {
            "is-active": FakeResult(stdout="active\n"),
            "is-enabled": FakeResult(stdout="enabled\n"),
            "--version": FakeResult(stdout="1.0.0\n"),
        }
    )
    manager, *_ = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)),
        version_fn=slow_version,
        connection_factory=lambda *a, **k: connection,
    )

    async def run():
        poll = asyncio.create_task(manager.poll_once())
        await asyncio.sleep(0.05)
        await asyncio.to_thread(manager.service_status, device)
        release.set()
        await poll

    asyncio.run(run())

    assert manager.status_fields("dev-1")["rp_telemetry"]["version"] == "1.0.0"


def test_on_demand_reads_do_not_move_the_sustained_loss_counters():
    """The loss counters are calibrated against the 30 s poll cadence.

    A few manual clicks on an offline board must not fabricate an outage
    warning, nor clear one that the poll loop legitimately raised.
    """
    device = make_device(
        parameters={rpt.DEVICE_PARAM_KEY: {"installed": True, "version": "1.0.0"}}
    )
    manager, _saved, _published, logs = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_OFFLINE, error="timed out")),
        version_fn=_no_version,
    )

    for _ in range(5):
        asyncio.run(manager.read_temperature(device))

    assert [e for e in logs if e["code"] == "rp_telemetry_unavailable"] == []
    assert manager._entry("dev-1").consecutive_failures == 0
    # The cache is still refreshed -- that is what the endpoint is for.
    assert manager.status_fields("dev-1")["rp_telemetry"]["state"] == rpt.STATE_OFFLINE


def test_unusable_cached_credentials_are_refetched():
    """A blank/rotated credential must not be cached for the process lifetime."""
    device = _influx_device()
    writer = RecordingWriter()
    fetched: list[str] = []

    class Blank:
        url = ""
        org = ""
        token = ""
        bucket = ""
        measurement = ""

    def fetcher(key):
        fetched.append(key)
        return FakeCredentials()

    manager, *_ = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)),
        version_fn=_no_version,
        influx_writer=writer,
        credentials_fetcher=fetcher,
    )
    manager.set_influx_credentials("dev-1", Blank())

    # Still inside the retry window: unusable, but not hammered either.
    asyncio.run(manager.poll_once())
    assert fetched == []
    assert writer.calls == []

    # Once the window has elapsed the bad value is replaced rather than kept
    # for the lifetime of the process.
    manager._entry("dev-1").influx_credentials_checked_at = (
        time.time() - rpt.INFLUX_CREDENTIAL_RETRY_S - 1
    )
    asyncio.run(manager.poll_once())

    assert fetched == ["dev-1"]
    assert len(writer.calls) == 1


def test_rejected_credentials_are_dropped_so_the_next_cycle_refetches():
    device = _influx_device()
    writer = RecordingWriter(error=InfluxWriteError("HTTP 401: unauthorized"))
    manager, *_ = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)),
        version_fn=_no_version,
        influx_writer=writer,
    )
    manager.set_influx_credentials("dev-1", FakeCredentials())

    asyncio.run(manager.poll_once())

    # A rotated token stays rejected forever if the bad value is kept.
    assert manager.get_influx_credentials("dev-1") is None


def test_rejected_credentials_still_honour_the_retry_window():
    """Dropping the value must not also drop the rate limit.

    The refetch is a blocking RPyC call; a permanently-wrong token would
    otherwise put one round trip per 30 s cycle back on the poll path.
    """
    device = _influx_device()
    writer = RecordingWriter(error=InfluxWriteError("HTTP 401: unauthorized"))
    fetches: list[str] = []

    manager, *_ = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)),
        version_fn=_no_version,
        influx_writer=writer,
        credentials_fetcher=lambda key: fetches.append(key) or FakeCredentials(),
    )
    manager.set_influx_credentials("dev-1", FakeCredentials())

    for _ in range(4):
        asyncio.run(manager.poll_once())

    # The bad value is dropped, but not re-fetched on every single cycle.
    assert manager.get_influx_credentials("dev-1") is None
    assert fetches == []


def test_devices_sharing_a_destination_stay_distinguishable():
    """Batched points share a request; only the tag tells the boards apart."""
    devices = [_influx_device("laser-a", "10.0.0.1"), _influx_device("laser-b", "10.0.0.2")]
    writer = RecordingWriter()
    manager, *_ = make_manager(
        devices,
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)),
        version_fn=_no_version,
        influx_writer=writer,
    )
    for device in devices:
        manager.set_influx_credentials(device.key, FakeCredentials())

    asyncio.run(manager.poll_once())

    lines = writer.calls[0][1]
    assert len(lines) == 2
    assert any("device=laser-a" in line for line in lines)
    assert any("device=laser-b" in line for line in lines)


def test_publishing_after_stop_does_not_leak_a_thread():
    device = make_device()
    published: list[str] = []
    manager, *_ = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)),
        version_fn=_no_version,
        status_publisher=published.append,
    )

    async def run():
        manager.start()
        await manager.stop()
        # On the loop, after stop(): must not spin up a worker nobody joins --
        # and must not fall back to publishing inline either, since that is the
        # blocking call on the event loop the worker hop exists to avoid.
        manager._publish("dev-1")

    asyncio.run(run())

    assert published == []
    assert manager._publish_executor is None


def test_publishing_off_the_loop_after_stop_still_works():
    """Management actions run on a worker thread; there is no loop to protect."""
    device = make_device()
    published: list[str] = []
    manager, *_ = make_manager([device], status_publisher=published.append)

    async def run():
        manager.start()
        await manager.stop()

    asyncio.run(run())
    manager._publish("dev-1")

    assert published == ["dev-1"]


def test_a_plain_write_failure_keeps_the_credentials():
    device = _influx_device()
    writer = RecordingWriter(error=InfluxWriteError("HTTP 503: unavailable"))
    manager, *_ = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)),
        version_fn=_no_version,
        influx_writer=writer,
    )
    manager.set_influx_credentials("dev-1", FakeCredentials())

    asyncio.run(manager.poll_once())

    assert manager.get_influx_credentials("dev-1") is not None


def test_status_publishing_never_runs_on_the_event_loop():
    """status() can fall back to a locked RPyC read; keep it off the loop."""
    device = make_device()
    publish_threads: list[int] = []
    done = threading.Event()

    def publisher(key):
        publish_threads.append(threading.get_ident())
        done.set()

    manager, *_ = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)),
        version_fn=_no_version,
        status_publisher=publisher,
    )

    async def run():
        await manager.poll_once()
        await asyncio.to_thread(done.wait, 2.0)
        return threading.get_ident()

    loop_thread = asyncio.run(run())

    assert publish_threads, "the status publisher never ran"
    assert publish_threads[0] != loop_thread


def test_management_actions_publish_inline():
    """Off a worker thread there is no loop to protect; publish directly."""
    device = make_device()
    connection = FakeConnection({"is-active": FakeResult(stdout="active\n")})
    published: list[str] = []
    manager, *_ = make_manager(
        [device],
        status_publisher=published.append,
        connection_factory=lambda *a, **k: connection,
    )

    manager.start_service(device)

    assert published == ["dev-1"]


def test_on_demand_read_refreshes_the_cache():
    device = make_device()
    manager, *_ = make_manager(
        [device],
        read_fn=reading_fn(rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=61.5)),
        version_fn=_no_version,
    )
    fields = asyncio.run(manager.read_temperature(device))
    assert fields["rp_temperature_c"] == 61.5
    assert manager.status_fields("dev-1")["rp_temperature_c"] == 61.5


def test_poll_loop_starts_and_stops_cleanly():
    device = make_device()
    cycles = 0

    async def read_fn(*_args, **_kwargs):
        nonlocal cycles
        cycles += 1
        return rpt.TelemetryReading(rpt.STATE_RUNNING, temperature_c=50.0)

    manager, *_ = make_manager(
        [device], read_fn=read_fn, version_fn=_no_version, poll_interval_s=0.05
    )

    async def run():
        manager.start()
        await asyncio.sleep(0.2)
        await manager.stop()

    asyncio.run(run())
    assert cycles >= 2
