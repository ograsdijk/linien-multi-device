"""Red Pitaya die-temperature telemetry: deployment, polling, and caching.

The board-side half of this feature is `rp-telemetry` (see `rp-telemetry/` in
the repo root), a tiny C daemon that sits blocked in `accept()` and answers a
one-line TCP request with the Zynq XADC temperature. This module is the
gateway-side counterpart:

* **Management** (install / uninstall / start / stop / restart / service
  status) goes over SSH, reusing `app.ssh.open_ssh_connection`. These are
  explicit operator actions only — nothing here installs the daemon just
  because a device exists.
* **Runtime monitoring** is TCP only. A background asyncio task polls every
  installed device concurrently (default 30 s) and caches the result. SSH is
  never used on that path.
* **Status** is served from the cache. `DeviceSession.status()` and
  `/api/devices/statuses` read `status_fields()`, which does no I/O at all.
* **InfluxDB** writes happen here, in the gateway — the daemon never touches
  the network beyond answering the request. The Linien parameter logging that
  runs on the Red Pitaya itself is untouched. The write, and the credential
  lookup that can precede it, both run on worker threads: nothing on the poll
  path is allowed to block the event loop.

Protocol: see `rp-telemetry/src/rp_telemetry.c`. `BUNDLED_VERSION` below must
match `RPT_VERSION` there; `tests/test_rp_telemetry_protocol.py` asserts it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Sequence

from .influx_writer import (
    InfluxDestination,
    InfluxLineWriter,
    InfluxWriteError,
    format_point,
)
from .ssh import open_ssh_connection

logger = logging.getLogger(__name__)

# --- protocol / deployment constants ------------------------------------

PROTOCOL_ID = "RPT1"
# Must match RPT_VERSION in rp-telemetry/src/rp_telemetry.c.
BUNDLED_VERSION = "1.0.0"

DEFAULT_TELEMETRY_PORT = 18864
CONNECT_TIMEOUT_S = 1.0
READ_TIMEOUT_S = 1.0
# A response is "RPT1 VERSION 1.0.0\n" at the longest. Anything past this is a
# broken or hostile peer; cap the read so a chatty endpoint cannot make the
# gateway buffer without bound.
MAX_RESPONSE_BYTES = 128

POLL_INTERVAL_S = 30.0
# Three missed polls. Below this a cached reading is presented as current;
# above it the UI must not show the number as live.
STALE_AFTER_S = 90.0
# Consecutive failed polls before a device is reported as a sustained loss in
# the gateway log (once — not once per cycle).
SUSTAINED_LOSS_POLLS = 3
# How long to wait before re-asking a connected device for its InfluxDB
# credentials after a failed/absent attempt.
INFLUX_CREDENTIAL_RETRY_S = 300.0
# Field name written to the device's existing InfluxDB measurement.
INFLUX_TEMPERATURE_FIELD = "rp_temperature_c"
# Tag identifying which board a temperature came from. Points for devices that
# share a destination are batched into one request, and nothing else in the
# point distinguishes them -- without this tag two boards configured with the
# same url/org/bucket/measurement would write into one indistinguishable
# series and same-timestamp points would overwrite each other.
INFLUX_DEVICE_TAG = "device"

# Reasons a sampled temperature is not written, surfaced to the operator.
INFLUX_SKIP_DISABLED = "influx_logging_disabled"
INFLUX_SKIP_NO_CREDENTIALS = "no_influx_credentials"
INFLUX_SKIP_REASON_TEXT = {
    INFLUX_SKIP_DISABLED: (
        "Red Pitaya temperature is not being written to InfluxDB: InfluxDB "
        "logging is not enabled for this device. Start it from the InfluxDB "
        "panel and the temperature follows the same destination."
    ),
    INFLUX_SKIP_NO_CREDENTIALS: (
        "Red Pitaya temperature is not being written to InfluxDB: no usable "
        "credentials for this device yet. Open the InfluxDB panel for it once "
        "(the gateway caches them from there), or connect the device so they "
        "can be read from the board."
    ),
}

REMOTE_BINARY_PATH = "/usr/local/bin/rp-telemetry"
REMOTE_STAGE_PATH = "/usr/local/bin/.rp-telemetry.new"
REMOTE_UPLOAD_PATH = "/tmp/rp-telemetry.upload"
SERVICE_NAME = "rp-telemetry.service"
SERVICE_UNIT_PATH = f"/etc/systemd/system/{SERVICE_NAME}"
SERVICE_UNIT_STAGE_PATH = f"{SERVICE_UNIT_PATH}.new"
SSH_COMMAND_TIMEOUT_S = 20.0
# `Type=simple` means systemd reports the unit active as soon as the process is
# forked -- before it has bind()/listen()ed. The post-install protocol check is
# therefore retried briefly rather than being taken as a failure on the first
# refused connection.
INSTALL_VERIFY_ATTEMPTS = 6
INSTALL_VERIFY_INTERVAL_S = 0.5
# Journal lines pulled from the board when a start/verify step fails. The
# daemon's own stderr goes to the board's journal, so without this the gateway
# can only say "the service did not become active" and the operator has to SSH
# in to find out why (a wrong-architecture binary, a missing loader, a port
# already in use). Bounded so one failure cannot dump a log into the UI.
SERVICE_JOURNAL_LINES = 20
SERVICE_JOURNAL_MESSAGE_CHARS = 400

# Plausible Zynq die temperatures. A reading outside this is a broken sysfs
# value, not a hot board, and is reported as an error rather than displayed.
MIN_PLAUSIBLE_TEMPERATURE_C = -40.0
MAX_PLAUSIBLE_TEMPERATURE_C = 150.0

BUNDLED_BINARY_PATH = Path(__file__).resolve().parent / "assets" / "rp-telemetry-armv7"

# Key under `device.parameters` recording that an operator installed the
# daemon on this board. Lets the poller tell "installed but not running"
# (connection refused on a board we deployed to) from "never installed",
# without an SSH round trip every 30 s.
DEVICE_PARAM_KEY = "rp_telemetry_install"

# --- telemetry states ----------------------------------------------------

STATE_UNKNOWN = "unknown"  # not polled yet
STATE_NOT_INSTALLED = "not_installed"
STATE_RUNNING = "running"
STATE_STOPPED = "stopped"  # port refused on a board we know has it installed
STATE_OFFLINE = "offline"  # host unreachable / timed out
STATE_STALE = "stale"  # last good sample is too old to present as current
STATE_ERROR = "error"  # daemon answered, but not with a temperature
STATE_VERSION_MISMATCH = "version_mismatch"  # unrecognised protocol id


@dataclass(frozen=True)
class InfluxCredentialSnapshot:
    """A local, plain-Python copy of a device's InfluxDB credentials.

    `DeviceSession.logging_get_credentials()` returns whatever RPyC hands back.
    `InfluxDBCredentials` is a dataclass, which is not brine-serializable, so
    that is a **netref**: every attribute read is a synchronous round trip to
    the Red Pitaya, outside `_rpyc_lock`, and every read raises once the
    connection is replaced. Caching one would put RPyC traffic on the poll path
    (the thing this module exists to avoid) and would break permanently after a
    reconnect. So the five fields are copied out once, on a worker thread, and
    only this snapshot is ever cached or read afterwards.
    """

    url: str = ""
    org: str = ""
    token: str = ""
    bucket: str = ""
    measurement: str = ""

    @classmethod
    def from_credentials(cls, credentials: Any) -> "InfluxCredentialSnapshot | None":
        """Materialize a snapshot. Returns None if the source cannot be read."""
        if credentials is None:
            return None
        try:
            return cls(
                url=str(getattr(credentials, "url", "") or ""),
                org=str(getattr(credentials, "org", "") or ""),
                token=str(getattr(credentials, "token", "") or ""),
                bucket=str(getattr(credentials, "bucket", "") or ""),
                measurement=str(getattr(credentials, "measurement", "") or ""),
            )
        except Exception:  # noqa: BLE001 - a dead netref raises EOFError here
            logger.debug("Could not read InfluxDB credentials", exc_info=True)
            return None

    def is_usable(self) -> bool:
        return bool(
            self.url and self.org and self.token and self.bucket and self.measurement
        )


@dataclass(frozen=True)
class TelemetryReading:
    """Outcome of one telemetry request. Never carries an exception."""

    state: str
    temperature_c: float | None = None
    version: str | None = None
    error: str | None = None


# --- protocol parsing ----------------------------------------------------


def parse_status_line(line: str) -> TelemetryReading:
    """Parse a `STATUS` response into a reading. Never raises."""
    text = (line or "").strip()
    if not text:
        return TelemetryReading(STATE_ERROR, error="empty response")
    parts = text.split(None, 2)
    if parts[0] != PROTOCOL_ID:
        return TelemetryReading(
            STATE_VERSION_MISMATCH,
            error=f"unexpected protocol id {parts[0]!r}",
        )
    if len(parts) < 2:
        return TelemetryReading(STATE_ERROR, error="truncated response")
    if parts[1] == "ERR":
        code = parts[2].strip() if len(parts) > 2 else "UNKNOWN"
        return TelemetryReading(STATE_ERROR, error=f"daemon error: {code}")
    try:
        temperature = float(parts[1])
    except ValueError:
        return TelemetryReading(
            STATE_ERROR, error=f"unparsable temperature {parts[1]!r}"
        )
    if not math.isfinite(temperature):
        return TelemetryReading(STATE_ERROR, error="non-finite temperature")
    if not (
        MIN_PLAUSIBLE_TEMPERATURE_C <= temperature <= MAX_PLAUSIBLE_TEMPERATURE_C
    ):
        return TelemetryReading(
            STATE_ERROR, error=f"implausible temperature {temperature:.2f} C"
        )
    return TelemetryReading(STATE_RUNNING, temperature_c=temperature)


def parse_version_line(line: str) -> str | None:
    """Parse a `VERSION` response, or None if it isn't one."""
    parts = (line or "").strip().split()
    if len(parts) == 3 and parts[0] == PROTOCOL_ID and parts[1] == "VERSION":
        return parts[2]
    return None


def _classify_exception(exc: BaseException) -> TelemetryReading:
    if isinstance(exc, ConnectionRefusedError):
        # Host is up, nothing listening: the daemon is not running (or not
        # installed). The caller decides which, from the install record.
        return TelemetryReading(STATE_STOPPED, error="connection refused")
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return TelemetryReading(STATE_OFFLINE, error="timed out")
    if isinstance(exc, (ValueError, asyncio.LimitOverrunError)):
        # StreamReader hit the response-size limit: the peer is talking, but
        # not this protocol.
        return TelemetryReading(STATE_ERROR, error="oversized response")
    if isinstance(exc, OSError):
        return TelemetryReading(STATE_OFFLINE, error=str(exc) or exc.__class__.__name__)
    return TelemetryReading(STATE_OFFLINE, error=str(exc) or exc.__class__.__name__)


# --- TCP transport -------------------------------------------------------


async def _request(
    host: str,
    port: int,
    command: bytes,
    *,
    connect_timeout: float,
    read_timeout: float,
) -> str:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, limit=MAX_RESPONSE_BYTES),
        timeout=connect_timeout,
    )
    try:
        writer.write(command)
        await asyncio.wait_for(writer.drain(), timeout=read_timeout)
        raw = await asyncio.wait_for(
            reader.readline(), timeout=read_timeout
        )
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, asyncio.TimeoutError):
            # The peer closing first is the normal case for this protocol.
            pass
    return raw[:MAX_RESPONSE_BYTES].decode("ascii", "replace")


async def read_telemetry(
    host: str,
    port: int = DEFAULT_TELEMETRY_PORT,
    *,
    connect_timeout: float = CONNECT_TIMEOUT_S,
    read_timeout: float = READ_TIMEOUT_S,
) -> TelemetryReading:
    """One `STATUS` round trip. Never raises."""
    if not host:
        return TelemetryReading(STATE_OFFLINE, error="device has no host")
    try:
        line = await _request(
            host,
            port,
            b"STATUS\n",
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        )
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001 - a poll must never raise
        return _classify_exception(exc)
    return parse_status_line(line)


async def read_version(
    host: str,
    port: int = DEFAULT_TELEMETRY_PORT,
    *,
    connect_timeout: float = CONNECT_TIMEOUT_S,
    read_timeout: float = READ_TIMEOUT_S,
) -> str | None:
    """One `VERSION` round trip. Never raises; None when unavailable."""
    if not host:
        return None
    try:
        line = await _request(
            host,
            port,
            b"VERSION\n",
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        )
    except asyncio.CancelledError:
        raise
    except BaseException:  # noqa: BLE001 - version is informational
        return None
    return parse_version_line(line)


def read_telemetry_sync(
    host: str,
    port: int = DEFAULT_TELEMETRY_PORT,
    *,
    connect_timeout: float = CONNECT_TIMEOUT_S,
    read_timeout: float = READ_TIMEOUT_S,
) -> TelemetryReading:
    """Blocking `STATUS` round trip, for the post-install verification step.

    The install path already runs on a worker thread (SSH is blocking), so it
    verifies with a plain socket instead of borrowing an event loop.
    """
    if not host:
        return TelemetryReading(STATE_OFFLINE, error="device has no host")
    try:
        with socket.create_connection((host, port), timeout=connect_timeout) as sock:
            sock.settimeout(read_timeout)
            sock.sendall(b"STATUS\n")
            chunks: list[bytes] = []
            total = 0
            while total < MAX_RESPONSE_BYTES:
                chunk = sock.recv(MAX_RESPONSE_BYTES - total)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if b"\n" in chunk:
                    break
        return parse_status_line(b"".join(chunks).decode("ascii", "replace"))
    except BaseException as exc:  # noqa: BLE001 - verification must not raise
        return _classify_exception(exc)


# --- systemd unit --------------------------------------------------------


def _shell_single_quote(value: str) -> str:
    """Make `value` safe inside single quotes in a POSIX shell command.

    The text is embedded in the command that crosses SSH, so anything the
    remote shell would reinterpret has to be neutralised. Quoting only -- the
    content is normalised separately (see `_normalize_unit`) so that what is
    sent and what is verified afterwards are the same bytes.
    """
    return value.replace("'", "'\"'\"'")


def _normalize_unit(unit: str) -> str:
    """LF-only unit text.

    A carriage return makes every value invalid ("Type=simple\r"), which
    systemd reports only as "bad unit file setting". Applied once, before both
    the write and the read-back comparison.
    """
    return unit.replace("\r\n", "\n").replace("\r", "\n")


def render_service_unit(port: int = DEFAULT_TELEMETRY_PORT) -> str:
    return f"""[Unit]
Description=Red Pitaya telemetry (Zynq die temperature)
After=network.target

[Service]
Type=simple
ExecStart={REMOTE_BINARY_PATH} --port {port}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
"""


# --- cache ---------------------------------------------------------------


@dataclass
class TelemetryEntry:
    """Cached per-device telemetry state. Guarded by the manager's lock."""

    state: str = STATE_UNKNOWN
    temperature_c: float | None = None
    sampled_at: float | None = None
    version: str | None = None
    error: str | None = None
    installed: bool = False
    # True once the running daemon has actually been asked for its version.
    # `version` may be pre-filled from the persisted install record for display,
    # so a separate flag is needed or the probe would never run on exactly the
    # boards where a mismatch matters. Reset whenever the daemon goes away, so a
    # restart or an out-of-band reflash is picked up.
    version_probed: bool = False
    # Version the mismatch warning was last emitted for. In-memory, so the
    # warning repeats at most once per gateway run rather than being persisted
    # into the device record from the poll loop.
    logged_version_mismatch: str | None = None
    # The cache's write discipline, in two halves:
    #   * every writer that sets entry state directly (the operator actions:
    #     install, uninstall, start/stop/restart, service_status) bumps this
    #     while holding the lock, in the SAME block as its writes;
    #   * every writer whose data came from a read that happened earlier
    #     (_poll_device, read_temperature) captures the value beforehand and
    #     passes it to _apply_reading as `expect_seq`, which drops the write if
    #     it no longer matches.
    # A writer that does neither can silently undo an operator action with a
    # view of the world captured seconds earlier.
    mutation_seq: int = 0
    consecutive_failures: int = 0
    last_success_at: float | None = None
    loss_reported: bool = False
    influx_credentials: InfluxCredentialSnapshot | None = None
    influx_credentials_checked_at: float | None = None
    influx_error_reported: bool = False
    # Why this device's temperature is not reaching InfluxDB, or None while it
    # is. Reported once per change rather than once per cycle.
    influx_skip_reason: str | None = None


def _material_signature(entry: TelemetryEntry, state: str) -> tuple:
    """What counts as a change worth pushing to connected clients.

    Temperature is rounded to 0.1 °C so ordinary jitter on a settled board
    doesn't generate a websocket message every 30 s.
    """
    temperature = (
        None if entry.temperature_c is None else round(entry.temperature_c, 1)
    )
    return (state, temperature, entry.version, entry.error, entry.installed)


class RpTelemetryManager:
    """Owns telemetry deployment, polling, caching, and Influx forwarding."""

    def __init__(
        self,
        *,
        device_provider: Callable[[], Sequence[Any]],
        save_device: Callable[[Any], None] | None = None,
        status_publisher: Callable[[str], None] | None = None,
        log_callback: Callable[..., None] | None = None,
        credentials_fetcher: Callable[[str], Any] | None = None,
        reload_device: Callable[[str], Any] | None = None,
        influx_writer: InfluxLineWriter | None = None,
        read_fn: Callable[..., Awaitable[TelemetryReading]] = read_telemetry,
        version_fn: Callable[..., Awaitable[str | None]] = read_version,
        connection_factory: Any | None = None,
        port: int = DEFAULT_TELEMETRY_PORT,
        poll_interval_s: float = POLL_INTERVAL_S,
        stale_after_s: float = STALE_AFTER_S,
    ) -> None:
        self._device_provider = device_provider
        self._save_device = save_device
        self._status_publisher = status_publisher
        self._log_callback = log_callback
        self._credentials_fetcher = credentials_fetcher
        self._reload_device = reload_device
        self._influx_writer = influx_writer
        self._read_fn = read_fn
        self._version_fn = version_fn
        self._connection_factory = connection_factory
        self._port = port
        self._poll_interval_s = poll_interval_s
        self._stale_after_s = stale_after_s

        self._lock = threading.RLock()
        self._entries: dict[str, TelemetryEntry] = {}
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        # Dedicated single thread for status pushes. The loop's default
        # executor is shared with `asyncio.to_thread`, and a bulk install fills
        # it with multi-second SSH jobs -- websocket status frames must not
        # queue behind those.
        self._publish_executor: ThreadPoolExecutor | None = None
        self._stopped = False

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Start the background poll task on the running event loop."""
        if self._task is not None and not self._task.done():
            return
        self._stopped = False
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="rp-telemetry-poll")

    async def stop(self) -> None:
        self._stopped = True
        if self._stop_event is not None:
            self._stop_event.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._influx_writer is not None:
            self._influx_writer.close()
        executor = self._publish_executor
        self._publish_executor = None
        if executor is not None:
            executor.shutdown(wait=False)

    async def _run(self) -> None:
        stop_event = self._stop_event
        assert stop_event is not None
        while not stop_event.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad cycle must not end polling
                logger.debug("rp-telemetry poll cycle failed", exc_info=True)
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self._poll_interval_s
                )
            except asyncio.TimeoutError:
                continue

    # --- cache access ----------------------------------------------------

    def _entry(self, key: str) -> TelemetryEntry:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = TelemetryEntry()
                self._entries[key] = entry
            return entry

    def _effective_state(self, entry: TelemetryEntry, now: float) -> str:
        if entry.state != STATE_RUNNING:
            return entry.state
        if (
            entry.sampled_at is not None
            and now - entry.sampled_at > self._stale_after_s
        ):
            return STATE_STALE
        return entry.state

    def status_fields(self, key: str) -> dict[str, Any]:
        """Status payload fragment for one device. Pure cache read, no I/O."""
        now = time.time()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = TelemetryEntry()
            state = self._effective_state(entry, now)
            version = entry.version
            error = entry.error
            temperature = entry.temperature_c
            sampled_at = entry.sampled_at
            installed = entry.installed
            influx_skip_reason = entry.influx_skip_reason
        return {
            "rp_temperature_c": temperature,
            "rp_temperature_sampled_at": sampled_at,
            "rp_telemetry": {
                "state": state,
                "version": version,
                "bundled_version": BUNDLED_VERSION,
                "update_available": bool(
                    installed and version is not None and version != BUNDLED_VERSION
                ),
                "installed": installed,
                "port": self._port,
                "error": error,
                # None while the temperature is reaching InfluxDB; otherwise
                # why it is not (see INFLUX_SKIP_* above).
                "influx_skip_reason": influx_skip_reason,
            },
        }

    def forget(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def set_influx_credentials(self, key: str, credentials: Any) -> None:
        """Cache the InfluxDB destination the gateway should write to.

        Called when the credentials endpoints are used, so the poll loop never
        has to make an RPyC call of its own on the hot path. The value is
        snapshotted here (see InfluxCredentialSnapshot) so what gets cached is
        local data, never a live RPyC reference.
        """
        snapshot = InfluxCredentialSnapshot.from_credentials(credentials)
        with self._lock:
            entry = self._entry(key)
            entry.influx_credentials = snapshot
            entry.influx_credentials_checked_at = time.time()

    def get_influx_credentials(self, key: str) -> Any | None:
        with self._lock:
            entry = self._entries.get(key)
            return entry.influx_credentials if entry is not None else None

    # --- install-record bookkeeping --------------------------------------

    @staticmethod
    def _install_record(device: Any) -> dict[str, Any]:
        parameters = getattr(device, "parameters", None)
        if not isinstance(parameters, dict):
            return {}
        record = parameters.get(DEVICE_PARAM_KEY)
        return record if isinstance(record, dict) else {}

    @staticmethod
    def _apply_record(device: Any, record: dict[str, Any] | None) -> None:
        parameters = getattr(device, "parameters", None)
        if not isinstance(parameters, dict):
            parameters = {}
        if record is None:
            parameters.pop(DEVICE_PARAM_KEY, None)
        else:
            parameters[DEVICE_PARAM_KEY] = record
        device.parameters = parameters

    def _persist_install_record(self, device: Any, record: dict[str, Any] | None) -> None:
        """Write the install record onto `device` and persist it.

        MUTATES `device` and rewrites devices.json, so it must only be called
        with a device the caller owns -- i.e. one from `device_store.get_device`,
        which deep-copies. `device_store.list_devices` hands out the *cached*
        instances, so the poll loop must never route through here.

        The device is re-read immediately before saving when a reloader is
        configured. `save_device` rewrites the whole record, and an install
        holds its snapshot across the entire SSH sequence -- tens of seconds,
        and for every board at once during a bulk install. Saving that stale
        snapshot would silently revert any parameter written meanwhile (an
        influx_logging_state toggle, a config sync).
        """
        self._apply_record(device, record)
        if self._save_device is None:
            return
        target = device
        if self._reload_device is not None:
            try:
                fresh = self._reload_device(getattr(device, "key", ""))
            except Exception:  # noqa: BLE001 - fall back to what we hold
                logger.debug("rp-telemetry device reload failed", exc_info=True)
                fresh = None
            if fresh is not None:
                self._apply_record(fresh, record)
                target = fresh
        try:
            self._save_device(target)
        except Exception:  # noqa: BLE001 - persistence is best effort
            logger.warning(
                "Failed persisting rp-telemetry install record for device=%s",
                getattr(device, "key", "?"),
                exc_info=True,
            )

    def _installed_from_record(self, device: Any) -> bool:
        """Read the install record. Pure -- writes nothing to the cache.

        The poll path must not touch the entry outside `_apply_reading`'s
        `mutation_seq` guard: a poll that started before an operator action
        would otherwise clobber the fresher state with its own stale view.
        """
        return bool(self._install_record(device).get("installed", False))

    # --- polling ---------------------------------------------------------

    async def poll_once(self) -> None:
        """One concurrent poll pass over all devices."""
        try:
            devices = list(self._device_provider())
        except Exception:  # noqa: BLE001 - a broken device store must not stop polling
            logger.debug("rp-telemetry device lookup failed", exc_info=True)
            return
        if not devices:
            return
        # Each device gets its own task, so one unreachable board waits out its
        # own timeout without holding up the rest.
        results = await asyncio.gather(
            *(self._poll_device(device) for device in devices),
            return_exceptions=True,
        )
        samples: list[tuple[Any, float, float]] = []
        for device, result in zip(devices, results):
            if isinstance(result, BaseException):
                logger.debug(
                    "rp-telemetry poll failed for device=%s",
                    getattr(device, "key", "?"),
                    exc_info=result,
                )
                continue
            if result is not None:
                samples.append((device, result[0], result[1]))
        if samples:
            await self._write_influx(samples)

    async def _poll_device(self, device: Any) -> tuple[float, float] | None:
        key = getattr(device, "key", "")
        host = getattr(device, "host", "") or ""
        if not key:
            return None
        installed = self._installed_from_record(device)
        # Captured before the reads: they can take a couple of seconds, and an
        # operator action landing in that window must win over everything this
        # cycle later computes -- the reading, `installed`, and `version_probed`.
        with self._lock:
            expect_seq = self._entry(key).mutation_seq
        reading = await self._read_fn(
            host,
            self._port,
            connect_timeout=CONNECT_TIMEOUT_S,
            read_timeout=READ_TIMEOUT_S,
        )
        if reading.state == STATE_STOPPED and not installed:
            reading = TelemetryReading(
                STATE_NOT_INSTALLED, error="no telemetry service on this board"
            )

        version: str | None = None
        probed = False
        if reading.state == STATE_RUNNING:
            with self._lock:
                needs_version = not self._entry(key).version_probed
            if needs_version:
                # Ask the daemon itself rather than trusting the install record,
                # so an out-of-band reflash or a stale record is still caught.
                # Once per gateway run per daemon lifetime -- the flag is reset
                # when the daemon goes away -- not once per poll.
                version = await self._version_fn(
                    host,
                    self._port,
                    connect_timeout=CONNECT_TIMEOUT_S,
                    read_timeout=READ_TIMEOUT_S,
                )
                # Recorded (either way -- a daemon too old to answer VERSION
                # must not be re-asked every cycle) by _apply_reading, so that
                # it lands under the same staleness guard as everything else.
                # Setting it here would let a poll that overlapped a Restart
                # undo that restart's request for a fresh probe.
                probed = True

        return self._apply_reading(
            device,
            reading,
            version,
            expect_seq=expect_seq,
            installed=installed,
            version_probed=probed,
        )

    def _apply_reading(
        self,
        device: Any,
        reading: TelemetryReading,
        version: str | None,
        *,
        expect_seq: int | None = None,
        track_health: bool = True,
        installed: bool | None = None,
        version_probed: bool = False,
    ) -> tuple[float, float] | None:
        """Fold a reading into the cache. Returns (temperature, ts) to log.

        `expect_seq` guards against a poll that started before an operator
        action landed: if the entry was mutated in the meantime, this reading is
        stale and is dropped rather than overwriting the fresher state.

        `track_health` is False for on-demand reads, which must refresh the
        cache without moving the sustained-loss counters -- those are calibrated
        against the 30 s poll cadence, and a few manual clicks should not
        trigger (or clear) an outage warning.
        """
        key = getattr(device, "key", "")
        now = time.time()
        sample: tuple[float, float] | None = None
        with self._lock:
            entry = self._entry(key)
            if expect_seq is not None and entry.mutation_seq != expect_seq:
                return None
            previous = _material_signature(entry, self._effective_state(entry, now))
            if installed is not None:
                entry.installed = installed
                if installed and entry.version is None:
                    # Display fallback only, until the daemon is actually
                    # probed -- see TelemetryEntry.version_probed.
                    recorded = self._install_record(device).get("version")
                    if isinstance(recorded, str):
                        entry.version = recorded
            if version_probed:
                entry.version_probed = True
            previous_state = entry.state
            if version is not None:
                entry.version = version
            if reading.state == STATE_RUNNING and reading.temperature_c is not None:
                entry.temperature_c = reading.temperature_c
                entry.sampled_at = now
                entry.last_success_at = now
                entry.error = None
                if track_health:
                    entry.consecutive_failures = 0
                sample = (reading.temperature_c, now)
            else:
                entry.error = reading.error
                if track_health:
                    entry.consecutive_failures += 1
                # The daemon we probed is gone; whatever comes back may be a
                # different build (a restart, a reinstall, a reflash).
                entry.version_probed = False
            entry.state = reading.state
            current = _material_signature(entry, self._effective_state(entry, now))
            failures = entry.consecutive_failures
            loss_reported = entry.loss_reported
            entry_version = entry.version
            installed = entry.installed

        if track_health:
            self._log_transitions(
                device=device,
                key=key,
                previous_state=previous_state,
                reading=reading,
                failures=failures,
                loss_reported=loss_reported,
                version=entry_version,
                installed=installed,
            )
        if current != previous:
            self._publish(key)
        return sample

    def _log_transitions(
        self,
        *,
        device: Any,
        key: str,
        previous_state: str,
        reading: TelemetryReading,
        failures: int,
        loss_reported: bool,
        version: str | None,
        installed: bool,
    ) -> None:
        if reading.state == STATE_RUNNING:
            if loss_reported:
                with self._lock:
                    self._entry(key).loss_reported = False
                self._emit_log(
                    logging.INFO,
                    "rp_telemetry_recovered",
                    "Red Pitaya telemetry recovered.",
                    key,
                    {"previous_state": previous_state},
                )
            if installed and version is not None and version != BUNDLED_VERSION:
                # Remembered in the cache entry, not the device record: the poll
                # loop must not mutate the shared cached Device (device_store
                # hands out the cached instances, uncopied) nor rewrite
                # devices.json from the event loop. The cost is that the warning
                # repeats once per gateway run, which is the right cadence for
                # an operational notice anyway.
                with self._lock:
                    entry = self._entry(key)
                    already_logged = entry.logged_version_mismatch == version
                    entry.logged_version_mismatch = version
                if not already_logged:
                    self._emit_log(
                        logging.WARNING,
                        "rp_telemetry_version_mismatch",
                        (
                            f"Red Pitaya telemetry runs {version}; the gateway "
                            f"bundles {BUNDLED_VERSION}. Reinstall to update."
                        ),
                        key,
                        {
                            "installed_version": version,
                            "bundled_version": BUNDLED_VERSION,
                        },
                    )
            return

        if failures == SUSTAINED_LOSS_POLLS and not loss_reported:
            with self._lock:
                self._entry(key).loss_reported = True
            level = (
                logging.INFO
                if reading.state == STATE_NOT_INSTALLED
                else logging.WARNING
            )
            self._emit_log(
                level,
                "rp_telemetry_unavailable",
                f"Red Pitaya telemetry unavailable ({reading.state}).",
                key,
                {"state": reading.state, "error": reading.error},
            )

    def _publish(self, key: str) -> None:
        """Publish a status frame for `key`, never on the event loop.

        The publisher builds a full `DeviceSession.status()` snapshot. That is
        normally a pure cache read, but `_read_param_fast` falls back to a
        locked RPyC read when the linien-client cache has no value yet (e.g. the
        first frames after a reconnect), which can block on `_rpyc_lock` while
        the poll thread holds it. Since `_publish` is reached from the poll
        coroutine, do that work on a worker thread; management actions already
        run on one and publish inline.
        """
        publisher = self._status_publisher
        if publisher is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            self._publish_blocking(key)
            return
        # Fire and forget: _publish_blocking swallows its own errors, so the
        # future never carries an exception for anyone to retrieve.
        executor = self._publish_executor
        if executor is None:
            if self._stopped:
                # Shutting down: don't create a worker thread nobody will join,
                # and don't fall back to publishing inline either -- that is the
                # blocking call on the event loop this hop exists to avoid, and
                # a status frame during shutdown is worth nothing.
                logger.debug("rp-telemetry publish skipped during shutdown key=%s", key)
                return
            executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="rp-telemetry-publish"
            )
            self._publish_executor = executor
        try:
            loop.run_in_executor(executor, self._publish_blocking, key)
        except RuntimeError:
            # Executor shut down underneath us; same reasoning as above.
            logger.debug("rp-telemetry publish skipped after shutdown key=%s", key)

    def _publish_blocking(self, key: str) -> None:
        publisher = self._status_publisher
        if publisher is None:
            return
        try:
            publisher(key)
        except Exception:  # noqa: BLE001 - publishing must not break polling
            logger.debug("rp-telemetry status publish failed key=%s", key, exc_info=True)

    def _emit_log(
        self,
        level: int,
        code: str,
        message: str,
        device_key: str | None,
        details: dict[str, Any] | None = None,
    ) -> None:
        callback = self._log_callback
        if callback is None:
            return
        try:
            callback(
                level=level,
                source="rp_telemetry",
                code=code,
                message=message,
                device_key=device_key,
                details=details or {},
            )
        except Exception:  # noqa: BLE001 - logging must never break telemetry
            logger.debug("rp-telemetry log emit failed", exc_info=True)

    # --- InfluxDB --------------------------------------------------------

    @staticmethod
    def _influx_logging_enabled(device: Any) -> bool:
        parameters = getattr(device, "parameters", None)
        if not isinstance(parameters, dict):
            return False
        state = parameters.get("influx_logging_state")
        return bool(isinstance(state, dict) and state.get("enabled", False))

    def _resolve_credentials(self, device: Any) -> Any | None:
        """Return the device's InfluxDB credentials, fetching them if needed.

        BLOCKING: the fallback fetcher does an RPyC round trip under the
        session's `_rpyc_lock`, which on a wedged device can take seconds.
        Callers on the event loop must run this via `asyncio.to_thread`.
        """
        key = getattr(device, "key", "")
        now = time.time()
        with self._lock:
            entry = self._entry(key)
            credentials = entry.influx_credentials
            checked_at = entry.influx_credentials_checked_at
        if credentials is not None and credentials.is_usable():
            return credentials
        # Anything else -- never fetched, or cached-but-unusable (blank token or
        # bucket, a bucket that was renamed, a token rotated on the board) --
        # falls through to a refetch, rate-limited by the retry window. Without
        # this a bad credential object would be kept for the process lifetime
        # and every temperature write silently skipped.
        if self._credentials_fetcher is None:
            return None
        if checked_at is not None and now - checked_at < INFLUX_CREDENTIAL_RETRY_S:
            return None
        with self._lock:
            self._entry(key).influx_credentials_checked_at = now
        try:
            fetched = self._credentials_fetcher(key)
        except Exception:  # noqa: BLE001 - credential lookup is best effort
            logger.debug(
                "rp-telemetry credential fetch failed key=%s", key, exc_info=True
            )
            return None
        # Snapshotted on this worker thread, so the netref's attribute reads
        # happen here and not in the poll coroutine.
        snapshot = InfluxCredentialSnapshot.from_credentials(fetched)
        if snapshot is None:
            return None
        with self._lock:
            self._entry(key).influx_credentials = snapshot
        return snapshot

    async def _write_influx(self, samples: Iterable[tuple[Any, float, float]]) -> None:
        writer = self._influx_writer
        if writer is None:
            return
        batches: dict[tuple[InfluxDestination, str], list[str]] = {}
        keys_by_batch: dict[tuple[InfluxDestination, str], list[str]] = {}
        for device, temperature, sampled_at in samples:
            if not self._influx_logging_enabled(device):
                self._note_influx_skip(device, INFLUX_SKIP_DISABLED)
                continue
            # Off-loop: _resolve_credentials can fall back to a blocking RPyC
            # call, and a wedged device would otherwise freeze every websocket
            # and HTTP handler for the duration of the poll cycle.
            credentials = await asyncio.to_thread(self._resolve_credentials, device)
            if credentials is None or not credentials.is_usable():
                self._note_influx_skip(device, INFLUX_SKIP_NO_CREDENTIALS)
                continue
            # Plain local reads: `credentials` is an InfluxCredentialSnapshot,
            # never the RPyC netref the session hands back.
            destination = InfluxDestination.from_credentials(credentials)
            measurement = credentials.measurement
            line = format_point(
                measurement,
                {INFLUX_TEMPERATURE_FIELD: temperature},
                int(sampled_at * 1_000_000_000),
                tags={INFLUX_DEVICE_TAG: str(getattr(device, "key", "") or "unknown")},
            )
            self._note_influx_skip(device, None)
            batch_key = (destination, measurement)
            batches.setdefault(batch_key, []).append(line)
            keys_by_batch.setdefault(batch_key, []).append(getattr(device, "key", ""))
        if not batches:
            return
        for (destination, _measurement), lines in batches.items():
            device_keys = keys_by_batch[(destination, _measurement)]
            try:
                await asyncio.to_thread(writer.write, destination, lines)
            except InfluxWriteError as exc:
                self._report_influx_failure(device_keys, str(exc))
            except Exception as exc:  # noqa: BLE001 - never break the poll loop
                self._report_influx_failure(device_keys, str(exc))
            else:
                self._report_influx_success(device_keys)

    def _note_influx_skip(self, device: Any, reason: str | None) -> None:
        """Record (and report once) why a temperature is not being written.

        Both skips are silent `continue`s on the hot path, which left an
        operator watching a working temperature reading and an empty InfluxDB
        with nothing to go on. Emitted on change only -- never once per cycle.
        """
        key = getattr(device, "key", "")
        with self._lock:
            entry = self._entry(key)
            if entry.influx_skip_reason == reason:
                return
            previous = entry.influx_skip_reason
            entry.influx_skip_reason = reason
        if reason is None:
            if previous is not None:
                self._emit_log(
                    logging.INFO,
                    "rp_telemetry_influx_writing",
                    "Red Pitaya temperature is now being written to InfluxDB.",
                    key,
                    {"previous_reason": previous},
                )
            return
        self._emit_log(
            logging.WARNING,
            "rp_telemetry_influx_skipped",
            INFLUX_SKIP_REASON_TEXT.get(
                reason, "Red Pitaya temperature is not being written to InfluxDB."
            ),
            key,
            {"reason": reason},
        )

    def influx_skip_reason(self, key: str) -> str | None:
        """Why this device's temperature is not reaching InfluxDB, if it isn't."""
        with self._lock:
            entry = self._entries.get(key)
            return entry.influx_skip_reason if entry is not None else None

    def _report_influx_failure(self, device_keys: Sequence[str], error: str) -> None:
        # An auth rejection means the cached token/org is stale (rotated on the
        # board, say). Drop it so the next cycle re-reads it instead of
        # retrying the same rejected value until the gateway restarts.
        rejected = "HTTP 401" in error or "HTTP 403" in error
        for key in device_keys:
            with self._lock:
                entry = self._entry(key)
                already = entry.influx_error_reported
                entry.influx_error_reported = True
                if rejected:
                    # Drop the value but KEEP `checked_at`: it is the retry
                    # window. Clearing it would make the next cycle refetch
                    # immediately, and since the refetch is a blocking RPyC call
                    # a permanently-wrong token would put one round trip per
                    # 30 s on the poll path -- exactly what this module exists
                    # to avoid.
                    entry.influx_credentials = None
            if already:
                # Transient Influx trouble must not produce a warning every
                # 30 s for as long as it lasts.
                continue
            self._emit_log(
                logging.WARNING,
                "rp_telemetry_influx_write_failed",
                "Red Pitaya temperature could not be written to InfluxDB.",
                key,
                {"error": error},
            )

    def _report_influx_success(self, device_keys: Sequence[str]) -> None:
        for key in device_keys:
            with self._lock:
                entry = self._entry(key)
                recovered = entry.influx_error_reported
                entry.influx_error_reported = False
            if recovered:
                self._emit_log(
                    logging.INFO,
                    "rp_telemetry_influx_write_recovered",
                    "Red Pitaya temperature InfluxDB writes recovered.",
                    key,
                    None,
                )

    # --- SSH management --------------------------------------------------

    def _open_ssh(self, device: Any):
        if self._connection_factory is not None:
            return open_ssh_connection(device, self._connection_factory)
        return open_ssh_connection(device)

    @staticmethod
    def _privileged(device: Any, command: str) -> str:
        username = getattr(device, "username", "root") or "root"
        return command if username == "root" else f"sudo -n {command}"

    def _ssh_run(self, conn: Any, device: Any, command: str, *, privileged: bool = True):
        full = self._privileged(device, command) if privileged else command
        return conn.run(
            full, hide=True, warn=True, timeout=SSH_COMMAND_TIMEOUT_S
        )

    @staticmethod
    def _failed(result: Any) -> bool:
        return getattr(result, "exited", 1) != 0

    @staticmethod
    def _detail(result: Any) -> str:
        stderr = (getattr(result, "stderr", "") or "").strip()
        stdout = (getattr(result, "stdout", "") or "").strip()
        return (stderr or stdout)[:300]

    def bundled_binary_path(self) -> Path:
        return BUNDLED_BINARY_PATH

    def install(self, device: Any) -> dict[str, Any]:
        """Install/update the daemon, enable it at boot, start, and verify.

        Idempotent: running it again on an already-installed board replaces the
        binary and restarts the service. The executable is put in place with an
        atomic rename, so a failed upload can never leave a truncated binary at
        `/usr/local/bin/rp-telemetry`.
        """
        key = getattr(device, "key", "")
        binary = BUNDLED_BINARY_PATH
        if not binary.exists():
            raise RuntimeError(
                "No bundled rp-telemetry binary. Build it with "
                "`rp-telemetry/build-arm.sh` (see the README) so it lands at "
                f"{binary}."
            )
        try:
            payload = binary.read_bytes()
        except OSError as exc:
            raise RuntimeError(
                f"Could not read the bundled rp-telemetry binary at {binary}: {exc}"
            ) from exc
        digest = hashlib.sha256(payload).hexdigest()
        unit = _normalize_unit(render_service_unit(self._port))

        try:
            with self._open_ssh(device) as conn:
                # Skip the transfer when the board already has this exact
                # binary. An update on an up-to-date board is then a few
                # systemctl calls instead of a 400 KB SFTP transfer followed by
                # a copy and an fsync -- by far the heaviest I/O the gateway
                # ever asks of a Red Pitaya, and worth not repeating for
                # nothing.
                if self._remote_binary_matches(conn, device, digest):
                    logger.info(
                        "rp-telemetry binary already current on device=%s; "
                        "skipping upload",
                        key,
                    )
                else:
                    conn.put(str(binary), remote=REMOTE_UPLOAD_PATH)

                    verified = self._verify_upload(conn, device, digest, len(payload))
                    if verified is not None:
                        raise RuntimeError(verified)

                    # Each privileged step is its own command: `sudo -n a && b`
                    # would only elevate `a`, leaving `b` unprivileged.
                    self._ssh_run(conn, device, "mkdir -p /usr/local/bin")
                    staged = self._ssh_run(
                        conn,
                        device,
                        f"install -m 0755 {REMOTE_UPLOAD_PATH} {REMOTE_STAGE_PATH}",
                    )
                    if self._failed(staged):
                        raise RuntimeError(
                            f"Could not stage the binary: {self._detail(staged)}"
                        )
                    # Rename within the same filesystem: atomic, and safe while
                    # the old binary is executing (the running process keeps its
                    # inode).
                    moved = self._ssh_run(
                        conn, device, f"mv -f {REMOTE_STAGE_PATH} {REMOTE_BINARY_PATH}"
                    )
                    if self._failed(moved):
                        raise RuntimeError(
                            f"Could not install the binary: {self._detail(moved)}"
                        )
                    # Same reasoning as the unit file: an unflushed executable
                    # that survives a reset as NULs would fail to exec, and the
                    # checksum verified above was of the upload, before the
                    # rename.
                    self._ssh_run(conn, device, "sync")
                    self._ssh_run(conn, device, f"rm -f {REMOTE_UPLOAD_PATH}")

                # Piped into `tee` rather than `cat > path`: a shell redirect
                # is performed by the *calling* shell, so `sudo -n cat > path`
                # would try to create the unit file as the unprivileged user.
                #
                # `printf` of a single-quoted one-liner rather than a heredoc:
                # the command crosses SSH and is re-parsed by whatever login
                # shell the board uses, and a heredoc body is sensitive to how
                # that shell handles the embedded newlines. A CR reaching the
                # file makes every value invalid ("Type=simple\r"), which
                # systemd reports only as "bad unit file setting".
                tee = self._privileged(device, f"tee {SERVICE_UNIT_STAGE_PATH}")
                written = self._ssh_run(
                    conn,
                    device,
                    f"printf '%s' '{_shell_single_quote(unit)}' | {tee} > /dev/null",
                    privileged=False,
                )
                if self._failed(written):
                    raise RuntimeError(
                        f"Could not write the systemd unit: {self._detail(written)}"
                    )
                # Flush before renaming, and again after. Without this the file
                # can exist at the right size with its contents still in page
                # cache; a board that loses power or resets before writeback
                # comes back with a NUL-filled file, which systemd reports only
                # as "bad unit file setting". Observed on real hardware.
                self._ssh_run(conn, device, "sync")
                moved_unit = self._ssh_run(
                    conn, device, f"mv -f {SERVICE_UNIT_STAGE_PATH} {SERVICE_UNIT_PATH}"
                )
                if self._failed(moved_unit):
                    raise RuntimeError(
                        f"Could not install the systemd unit: {self._detail(moved_unit)}"
                    )
                self._ssh_run(conn, device, "sync")

                mismatch = self._verify_remote_file(conn, device, SERVICE_UNIT_PATH, unit)
                if mismatch is not None:
                    raise RuntimeError(mismatch)

                # systemd parses the unit at daemon-reload; ask it directly
                # whether the file it now holds is usable. Without this the only
                # symptom is `systemctl start` failing with "bad unit file
                # setting", which names neither the setting nor the reason.
                self._ssh_run(conn, device, "systemctl daemon-reload")
                load = self._ssh_run(
                    conn,
                    device,
                    f"systemctl show -p LoadState -p LoadError --value {SERVICE_NAME}",
                )
                load_output = (getattr(load, "stdout", "") or "").strip()
                if "not-found" in load_output or "bad-setting" in load_output or (
                    load_output and not load_output.startswith("loaded")
                ):
                    dumped = self._ssh_run(
                        conn, device, f"cat -A {SERVICE_UNIT_PATH}", privileged=False
                    )
                    raise RuntimeError(
                        "systemd rejected the unit file "
                        f"({load_output or 'no LoadState'}). "
                        f"File as written: {self._detail(dumped)[:300]}"
                    )

                for command, failure in (
                    (f"systemctl enable {SERVICE_NAME}", "Could not enable the service"),
                    (f"systemctl restart {SERVICE_NAME}", "Could not start the service"),
                ):
                    result = self._ssh_run(conn, device, command)
                    if self._failed(result):
                        journal = self._service_journal(device, conn)
                        raise RuntimeError(
                            self._with_journal(
                                f"{failure}: {self._detail(result)}", journal
                            )
                        )

                active = self._ssh_run(
                    conn, device, f"systemctl is-active {SERVICE_NAME}"
                )
                active_state = (getattr(active, "stdout", "") or "").strip()
                if active_state != "active":
                    journal = self._service_journal(device, conn)
                    raise RuntimeError(
                        self._with_journal(
                            f"Service did not become active (systemctl reports "
                            f"{active_state or 'nothing'}).",
                            journal,
                        )
                    )
        except RuntimeError as exc:
            self._emit_log(
                logging.ERROR,
                "rp_telemetry_install_failed",
                "Red Pitaya telemetry install failed.",
                key,
                {"error": str(exc)},
            )
            raise
        except Exception as exc:  # noqa: BLE001 - surface SSH errors uniformly
            self._emit_log(
                logging.ERROR,
                "rp_telemetry_install_failed",
                "Red Pitaya telemetry install failed.",
                key,
                {"error": str(exc)},
            )
            raise RuntimeError(f"Telemetry install failed: {exc}") from exc

        # Recorded before the protocol check: at this point the binary is in
        # place and the unit is enabled and active, so the board *is* installed.
        # Raising without recording it would leave a board that starts the
        # daemon on every boot while the UI reports "not installed" and offers
        # Install rather than Restart.
        self._persist_install_record(
            device,
            {
                "installed": True,
                "version": BUNDLED_VERSION,
                "installed_at": time.time(),
                "port": self._port,
                "sha256": digest,
            },
        )
        with self._lock:
            entry = self._entry(key)
            entry.installed = True
            entry.version = BUNDLED_VERSION
            entry.version_probed = False
            entry.logged_version_mismatch = None
            entry.consecutive_failures = 0
            entry.loss_reported = False
            # Bumped here, not only after the verification below: that can take
            # a couple of seconds, and a poll whose VERSION probe is in flight
            # would otherwise land with a matching seq and write back the
            # pre-install version and `installed=False` -- leaving the board
            # permanently reported as needing the update it just received.
            entry.mutation_seq += 1

        reading = self._verify_installed_daemon(getattr(device, "host", "") or "")
        if reading.state != STATE_RUNNING:
            with self._lock:
                entry = self._entry(key)
                entry.state = reading.state
                entry.error = reading.error
                entry.mutation_seq += 1
            self._publish(key)
            journal = self._service_journal(device)
            self._emit_log(
                logging.ERROR,
                "rp_telemetry_install_failed",
                "Red Pitaya telemetry installed but did not answer.",
                key,
                {"state": reading.state, "error": reading.error, "journal": journal},
            )
            raise RuntimeError(
                self._with_journal(
                    "The service started but the telemetry port did not return "
                    f"a temperature ({reading.state}: {reading.error}).",
                    journal,
                )
            )

        with self._lock:
            entry = self._entry(key)
            entry.state = STATE_RUNNING
            entry.temperature_c = reading.temperature_c
            entry.sampled_at = time.time()
            entry.last_success_at = entry.sampled_at
            entry.error = None
            entry.mutation_seq += 1
        self._emit_log(
            logging.INFO,
            "rp_telemetry_installed",
            "Red Pitaya telemetry installed and verified.",
            key,
            {"version": BUNDLED_VERSION, "temperature_c": reading.temperature_c},
        )
        self._publish(key)
        return {
            "ok": True,
            "version": BUNDLED_VERSION,
            "temperature_c": reading.temperature_c,
        }

    def _remote_binary_matches(self, conn: Any, device: Any, digest: str) -> bool:
        """True when the board already carries exactly this binary.

        Cheap (one sha256sum) compared with what it saves (an SFTP transfer, a
        copy, and an fsync). Returns False whenever it cannot tell, so an
        unverifiable board still gets a full install.
        """
        checked = self._ssh_run(
            conn, device, f"sha256sum {REMOTE_BINARY_PATH}", privileged=False
        )
        if self._failed(checked):
            return False
        remote = (getattr(checked, "stdout", "") or "").split()
        return bool(remote) and remote[0] == digest

    def _verify_remote_file(
        self, conn: Any, device: Any, path: str, expected: str
    ) -> str | None:
        """Read `path` back off the board and compare. None when it matches.

        Catches anything that corrupts the file between writing and reading --
        a short write, a NUL-filled page-cache casualty, a shell that mangled
        the content -- at the point of install, rather than leaving systemd to
        report it later as an unexplained "bad unit file setting".
        """
        digest = hashlib.sha256(expected.encode("utf-8")).hexdigest()
        checked = self._ssh_run(conn, device, f"sha256sum {path}", privileged=False)
        if not self._failed(checked):
            remote = (getattr(checked, "stdout", "") or "").split()
            if remote and remote[0] == digest:
                return None
            dumped = self._ssh_run(conn, device, f"cat -A {path}", privileged=False)
            return (
                f"{path} does not match what was written "
                f"(contents as stored: {self._detail(dumped)[:300]})"
            )
        # Minimal images may lack sha256sum; fall back to a size check, which
        # still catches truncation though not corruption.
        sized = self._ssh_run(conn, device, f"wc -c < {path}", privileged=False)
        if self._failed(sized):
            return f"Could not read back {path} to verify it."
        try:
            remote_size = int((getattr(sized, "stdout", "") or "").strip())
        except ValueError:
            return f"Could not read back {path} to verify it."
        expected_size = len(expected.encode("utf-8"))
        if remote_size != expected_size:
            return (
                f"{path} is truncated ({remote_size} of {expected_size} bytes)."
            )
        return None

    def _service_journal(self, device: Any, conn: Any = None) -> str:
        """Last few journal lines for the unit. Never raises; "" when unknown.

        Used only on failure paths, to turn "did not become active" into
        something self-diagnosing.
        """
        command = (
            f"journalctl -u {SERVICE_NAME} -n {SERVICE_JOURNAL_LINES} "
            "--no-pager --output=cat"
        )
        try:
            if conn is not None:
                result = self._ssh_run(conn, device, command)
            else:
                with self._open_ssh(device) as fresh:
                    result = self._ssh_run(fresh, device, command)
        except Exception:  # noqa: BLE001 - diagnostics must not mask the error
            logger.debug("rp-telemetry journal read failed", exc_info=True)
            return ""
        if self._failed(result):
            return ""
        return (getattr(result, "stdout", "") or "").strip()

    @staticmethod
    def _with_journal(message: str, journal: str) -> str:
        if not journal:
            return message
        tail = journal[-SERVICE_JOURNAL_MESSAGE_CHARS:].strip()
        return f"{message} Board log: {tail}"

    def _verify_installed_daemon(self, host: str) -> TelemetryReading:
        """Poll the freshly started daemon until it answers, briefly.

        `systemctl is-active` reports a `Type=simple` unit active as soon as the
        process is forked, which can be before it has bound its socket. A single
        immediate check would therefore report a false failure for a perfectly
        good install on a loaded board.
        """
        reading = TelemetryReading(STATE_OFFLINE, error="not checked")
        for attempt in range(INSTALL_VERIFY_ATTEMPTS):
            reading = read_telemetry_sync(host, self._port)
            if reading.state == STATE_RUNNING:
                return reading
            if attempt < INSTALL_VERIFY_ATTEMPTS - 1:
                time.sleep(INSTALL_VERIFY_INTERVAL_S)
        return reading

    def _verify_upload(
        self, conn: Any, device: Any, digest: str, size: int
    ) -> str | None:
        """Return an error string if the uploaded file doesn't match."""
        checked = self._ssh_run(
            conn, device, f"sha256sum {REMOTE_UPLOAD_PATH}", privileged=False
        )
        if not self._failed(checked):
            remote_digest = (getattr(checked, "stdout", "") or "").split()
            if remote_digest and remote_digest[0] == digest:
                return None
            return "Uploaded binary checksum does not match the bundled binary."
        # Minimal images may not ship sha256sum; fall back to a size check.
        sized = self._ssh_run(
            conn, device, f"wc -c < {REMOTE_UPLOAD_PATH}", privileged=False
        )
        if self._failed(sized):
            return "Could not verify the uploaded binary on the device."
        try:
            remote_size = int((getattr(sized, "stdout", "") or "").strip())
        except ValueError:
            return "Could not verify the uploaded binary on the device."
        if remote_size != size:
            return (
                f"Uploaded binary is truncated ({remote_size} of {size} bytes)."
            )
        return None

    def uninstall(self, device: Any) -> dict[str, Any]:
        key = getattr(device, "key", "")
        try:
            with self._open_ssh(device) as conn:
                self._ssh_run(conn, device, f"systemctl stop {SERVICE_NAME}")
                self._ssh_run(conn, device, f"systemctl disable {SERVICE_NAME}")
                self._ssh_run(conn, device, f"rm -f {SERVICE_UNIT_PATH}")
                self._ssh_run(conn, device, f"rm -f {SERVICE_UNIT_STAGE_PATH}")
                self._ssh_run(conn, device, "systemctl daemon-reload")
                # Leftovers from an install that died between the staging
                # copy and the atomic rename, or before its own cleanup: a
                # ~400 KB binary each, otherwise left on the board forever.
                self._ssh_run(conn, device, f"rm -f {REMOTE_STAGE_PATH}")
                self._ssh_run(conn, device, f"rm -f {REMOTE_UPLOAD_PATH}")
                removed = self._ssh_run(conn, device, f"rm -f {REMOTE_BINARY_PATH}")
                if self._failed(removed):
                    raise RuntimeError(
                        f"Could not remove the binary: {self._detail(removed)}"
                    )
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Telemetry uninstall failed: {exc}") from exc

        self._persist_install_record(device, None)
        with self._lock:
            entry = self._entry(key)
            entry.installed = False
            entry.state = STATE_NOT_INSTALLED
            entry.version = None
            entry.version_probed = False
            entry.logged_version_mismatch = None
            entry.mutation_seq += 1
            entry.temperature_c = None
            entry.sampled_at = None
            entry.error = None
            entry.consecutive_failures = 0
            entry.loss_reported = False
        self._emit_log(
            logging.INFO,
            "rp_telemetry_uninstalled",
            "Red Pitaya telemetry removed.",
            key,
            None,
        )
        self._publish(key)
        return {"ok": True}

    def _service_action(self, device: Any, action: str) -> dict[str, Any]:
        key = getattr(device, "key", "")
        try:
            with self._open_ssh(device) as conn:
                result = self._ssh_run(
                    conn, device, f"systemctl {action} {SERVICE_NAME}"
                )
                if self._failed(result):
                    raise RuntimeError(
                        self._with_journal(
                            f"systemctl {action} failed: {self._detail(result)}",
                            self._service_journal(device, conn),
                        )
                    )
                active = self._ssh_run(
                    conn, device, f"systemctl is-active {SERVICE_NAME}"
                )
                active_state = (getattr(active, "stdout", "") or "").strip()
        except RuntimeError as exc:
            if action in ("restart", "start"):
                self._emit_log(
                    logging.ERROR,
                    "rp_telemetry_service_action_failed",
                    f"Red Pitaya telemetry {action} failed.",
                    key,
                    {"error": str(exc)},
                )
            raise
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"systemctl {action} failed: {exc}") from exc

        active = active_state == "active"
        with self._lock:
            entry = self._entry(key)
            entry.mutation_seq += 1
            if action == "stop":
                entry.state = STATE_STOPPED
                entry.error = "service stopped by operator"
            else:
                entry.consecutive_failures = 0
                entry.loss_reported = False
                # The UI refetches the status straight after this call so the
                # card updates without waiting for the next 30 s poll. Leaving
                # the old stopped/offline state here would make a successful
                # Start still render as "stopped" with a Start button.
                if active:
                    entry.state = STATE_RUNNING
                    entry.error = None
                    # Drop the last reading: it predates the restart, and if it
                    # is older than the staleness window `_effective_state`
                    # would immediately downgrade this to `stale` and show a
                    # Restart button right after a restart that worked. With no
                    # reading the UI says "waiting for the first reading".
                    entry.temperature_c = None
                    entry.sampled_at = None
                    # A restart may be running a different build.
                    entry.version_probed = False
        self._publish(key)
        return {"ok": True, "active": active, "state": active_state}

    def start_service(self, device: Any) -> dict[str, Any]:
        return self._service_action(device, "start")

    def stop_service(self, device: Any) -> dict[str, Any]:
        return self._service_action(device, "stop")

    def restart_service(self, device: Any) -> dict[str, Any]:
        return self._service_action(device, "restart")

    def service_status(self, device: Any) -> dict[str, Any]:
        """Explicit SSH health check. Not used by the 30 s poll loop."""
        try:
            with self._open_ssh(device) as conn:
                active = self._ssh_run(
                    conn, device, f"systemctl is-active {SERVICE_NAME}"
                )
                enabled = self._ssh_run(
                    conn, device, f"systemctl is-enabled {SERVICE_NAME}"
                )
                version = self._ssh_run(
                    conn, device, f"{REMOTE_BINARY_PATH} --version", privileged=False
                )
                journal = self._service_journal(device, conn)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Could not read the telemetry service state: {exc}")
        installed_version = (getattr(version, "stdout", "") or "").strip() or None
        if self._failed(version):
            installed_version = None
        active_state = (getattr(active, "stdout", "") or "").strip()
        key = getattr(device, "key", "")
        # `installed` in this response describes the binary on disk, as found
        # over SSH. The cached flag stays sourced from the install record --
        # correcting it here would just be undone by the next poll, which reads
        # the record again. Uninstall is the path that clears the record.
        if installed_version is not None:
            with self._lock:
                entry = self._entry(key)
                entry.version = installed_version
                # NOT version_probed: `--version` reports the binary on DISK,
                # which is not necessarily what is running (a manual scp
                # without a restart, or an install that died between the mv and
                # the restart). Marking it probed would suppress the TCP
                # VERSION request against the live process for the rest of the
                # gateway's life, and the board could sit on the old build with
                # update_available=false. Display it, keep probing.
                #
                # The bump still applies: this read is newer than any probe
                # already in flight (see TelemetryEntry.mutation_seq).
                entry.mutation_seq += 1
        return {
            "installed": installed_version is not None,
            "active": active_state == "active",
            "active_state": active_state,
            "enabled_state": (getattr(enabled, "stdout", "") or "").strip(),
            "version": installed_version,
            "bundled_version": BUNDLED_VERSION,
            # The daemon's own output lives on the board; surface it here so a
            # misbehaving service can be diagnosed without an SSH session.
            "journal": journal,
        }

    async def read_temperature(self, device: Any) -> dict[str, Any]:
        """One-shot on-demand read that also refreshes the cache."""
        host = getattr(device, "host", "") or ""
        key = getattr(device, "key", "")
        # Same staleness discipline as the poll loop: an operator action landing
        # while this read is in flight must win over it.
        with self._lock:
            expect_seq = self._entry(key).mutation_seq
        reading = await self._read_fn(
            host,
            self._port,
            connect_timeout=CONNECT_TIMEOUT_S,
            read_timeout=READ_TIMEOUT_S,
        )
        installed = self._installed_from_record(device)
        if reading.state == STATE_STOPPED and not installed:
            reading = TelemetryReading(
                STATE_NOT_INSTALLED, error="no telemetry service on this board"
            )
        self._apply_reading(
            device,
            reading,
            None,
            expect_seq=expect_seq,
            track_health=False,
            installed=installed,
        )
        return self.status_fields(key)
