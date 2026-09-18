"""Out-of-band diagnosis of why a linien-server connection was lost.

When the RPyC connection to a Red Pitaya drops, the gateway cannot tell *why*
from the RPyC side alone — a server crash, a board reboot, and a network blip
all surface as the same dead socket. This module probes the board out-of-band
(plain TCP + SSH) and classifies the situation so the UI can show something
useful, in particular whether the FPGA lock is likely still held.

Key domain facts that drive the logic (see linien-server):
- The PID lock runs in FPGA gateware, so a linien-server *crash* does NOT drop
  the lock — the FPGA keeps running. A reboot (or a server restart, which
  reflashes the FPGA) resets the registers and loses the lock.
- The user has NOT enabled linien-server auto-start on boot, so after a reboot
  the server stays down. That makes ``/proc/uptime`` a clean discriminator:
  low uptime => rebooted (lock lost); high uptime + server down => crash
  (lock likely still held).
- The lock-status register is only meaningful when the linien gateware is still
  loaded, i.e. no reboot happened since we were connected. We therefore only
  read it when uptime rules out a reboot.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from fabric import Connection
from linien_common.config import SERVER_PORT
from paramiko.ssh_exception import (
    AuthenticationException,
    NoValidConnectionsError,
    SSHException,
)

from .ssh import open_ssh_connection

if TYPE_CHECKING:
    from .session_registry import SessionRegistry

logger = logging.getLogger(__name__)

# FPGA lock-status register address, derived from linien-server:
#   PythonCSR.offset = 0x40300000                                    (csr.py)
#   csr['logic_autolock_lock_running'] = (map=8, addr=0x10f, 1, False)  (csrmap.py)
#   address = offset + (map << 11) + (addr << 2)
#           = 0x40300000 + (8 << 11) + (0x10f << 2) = 0x4030443C
# Bit 0 of the 32-bit word at this address is 1 while the FPGA lock loop runs.
LOCK_RUNNING_REGISTER_ADDR = 0x4030443C
FPGA_STATE_PATH = "/sys/class/fpga_manager/fpga0/state"
# The kernel regenerates this on every boot, so comparing it against the
# value we saw last turns "did this board reboot?" from a threshold into a
# fact. Free to collect: it rides along in the compound read below.
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"

# Ordered methods for reading the lock-status register over SSH. All are pure
# 32-bit register reads (mmap of /dev/mem): reading a status register has no
# side effects and CANNOT disturb the loaded gateware — that is the
# fpga_manager/bitstream path, which we never touch. Where `timeout` exists it
# bounds a possible AXI bus hang if the PL region is unmapped (such a hang exits
# 124, which is what tells the two cases apart in the recorded detail string).
#   1. busybox/standalone `devmem` — present on many images, fast.
#   2. `busybox devmem` — same applet on images where it is not symlinked.
#   3. Red Pitaya's own `monitor` tool (/opt/redpitaya/bin) — present on the
#      stock Red Pitaya OS even when `devmem` is not.
#   4./5. python3 (then python) /dev/mem mmap — for images without any of the
#      above, which is the common case on stock Red Pitaya OS.
#      python3 is available whenever linien-server is (it is a Python service).
#      The one-liner reads exactly 4 bytes at the (page-aligned) register
#      address; only bit 0 is used, so word endianness is irrelevant.
#
# A non-interactive SSH session gets a minimal PATH on some images, which is by
# itself enough to make every method "missing" (exit 127), so PATH is extended
# with the sbin/Red Pitaya directories first.
#
# `timeout` is likewise not guaranteed: on a minimal busybox it is absent, and
# hardcoding it would make EVERY method exit 127 before it even runs. So it is
# probed once and used only if present — bounding a possible AXI bus hang is a
# nice-to-have, being able to read the register at all is not.
_LOCK_CMD_PREFIX = (
    "export PATH=$PATH:/usr/local/sbin:/usr/sbin:/sbin:/opt/redpitaya/bin; "
    "if command -v timeout >/dev/null 2>&1; then TO='timeout 2'; else TO=''; fi; "
)
_LOCK_BIT_PY_SCRIPT = (
    "import mmap,os,struct;"
    f"A={hex(LOCK_RUNNING_REGISTER_ADDR)};"
    "P=mmap.PAGESIZE;b=A&~(P-1);"
    "f=os.open('/dev/mem',os.O_RDONLY);"
    "m=mmap.mmap(f,P,mmap.MAP_SHARED,mmap.PROT_READ,offset=b);"
    "print('0x%08x'%struct.unpack('<I',m[A-b:A-b+4])[0])"
)
_ADDR = hex(LOCK_RUNNING_REGISTER_ADDR)
# (method name, shell command). The name is only used in the recorded detail
# string that explains *why* a read failed.
_LOCK_BIT_CMDS: tuple[tuple[str, str], ...] = tuple(
    (name, _LOCK_CMD_PREFIX + body)
    for name, body in (
        ("devmem", f"$TO devmem {_ADDR}"),
        ("busybox-devmem", f"$TO busybox devmem {_ADDR}"),
        ("rp-monitor", f"$TO monitor {_ADDR}"),
        ("python3", f'$TO python3 -c "{_LOCK_BIT_PY_SCRIPT}"'),
        # Some Red Pitaya images ship only /opt/redpitaya/bin/python.
        ("python", f'$TO python -c "{_LOCK_BIT_PY_SCRIPT}"'),
    )
)

# Exit codes worth spelling out in the failure detail: these are the ones that
# actually happen in the field, and they call for different fixes.
_EXIT_CODE_HINTS = {
    127: "command not found",
    124: "timed out (AXI read hung — PL region likely unmapped)",
    126: "not executable",
}
# Longest per-method failure text kept in the detail string, so one chatty
# command cannot bloat the diagnosis payload sent to the UI.
_LOCK_DETAIL_MAX_CHARS = 120

TCP_PROBE_TIMEOUT_S = 2.0
# Deliberately much shorter than `ssh.SSH_COMMAND_TIMEOUT_S` (20 s), which
# bounds the diagnostics *bundle* -- a dozen commands an operator waited for.
# This bounds an automatic probe that runs on every disconnect, for every
# device, so it must give up quickly rather than be generous. Do not
# consolidate the two: a probe at 20 s stalls reconnection for the whole fleet.
PROBE_COMMAND_TIMEOUT_S = 5.0
# A board up longer than this is assumed not to have rebooted since we lost the
# connection. Used as the reboot/crash discriminator.
DEFAULT_UPTIME_THRESHOLD_S = 600.0
# Minimum delay between repeated probes of the same disconnected device.
MIN_REPROBE_INTERVAL_S = 20.0
# Probe a few disconnected devices concurrently so a mass outage doesn't serialise
# behind one slow SSH timeout, while staying small enough to avoid an SSH storm.
DIAGNOSIS_PROBE_WORKERS = 4

# Diagnosis categories.
CATEGORY_RECOVERING = "recovering"
CATEGORY_HOST_UNREACHABLE = "host_unreachable"
CATEGORY_SERVER_DOWN_UNKNOWN = "server_down_unknown"
CATEGORY_REBOOTED = "rebooted"
CATEGORY_SERVER_CRASHED = "server_crashed"


@dataclass
class ProbeResult:
    """Raw signals gathered by :func:`probe_device`. Never carries exceptions."""

    server_listening: bool
    host_reachable: bool
    uptime_s: float | None
    fpga_operating: bool | None
    lock_bit: int | None
    error: str | None = None
    # The board's current kernel boot id, when it could be read.
    boot_id: str | None = None
    # True when conditions warranted reading the lock register. Combined with
    # lock_bit is None, this distinguishes "read attempted but unreadable"
    # (e.g. devmem missing / wrong fpga path) from "deliberately not read".
    lock_read_attempted: bool = False
    # Why the lock read produced nothing, e.g.
    # "devmem: exit 127 (command not found); python3: exit 1: Permission denied".
    # Set only when a read was attempted and every method failed.
    lock_read_detail: str | None = None


def _tcp_open(host: str, port: int, timeout: float) -> bool:
    """Return True if a plain TCP connection to ``host:port`` is accepted.

    This is NOT an SSH/RPyC handshake — it just opens and immediately closes a
    socket to learn whether *something* is listening (i.e. the linien-server
    process is back up).
    """
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _parse_uptime_fpga(text: str) -> tuple[float | None, bool | None, str | None]:
    uptime_s: float | None = None
    fpga_operating: bool | None = None
    boot_id: str | None = None
    parts = text.split("---")
    head = parts[0].strip().split() if parts else []
    if head:
        try:
            uptime_s = float(head[0])
        except ValueError:
            uptime_s = None
    if len(parts) > 1:
        state = parts[1].strip()
        if state:
            fpga_operating = state == "operating"
    if len(parts) > 2:
        candidate = parts[2].strip()
        if candidate:
            boot_id = candidate
    return uptime_s, fpga_operating, boot_id


def _read_uptime_and_fpga(
    conn: Connection,
) -> tuple[float | None, bool | None, str | None]:
    # One compound command to avoid extra SSH round-trips. The boot id rides
    # along for free -- it is the difference between inferring a reboot from an
    # uptime threshold and knowing one happened.
    cmd = (
        f"cat /proc/uptime; echo '---'; cat {FPGA_STATE_PATH} 2>/dev/null; "
        f"echo '---'; cat {BOOT_ID_PATH} 2>/dev/null"
    )
    result = conn.run(cmd, hide=True, warn=True, timeout=PROBE_COMMAND_TIMEOUT_S)
    return _parse_uptime_fpga(result.stdout or "")


def _run_lock_bit_cmd(conn: Connection, cmd: str) -> tuple[int | None, str | None]:
    """Run one register-read command; return (bit 0 of its value, failure detail).

    A ``None`` bit means "this method yielded nothing" — command missing (exit
    127), non-zero exit, empty/unparseable output, or a transport error — so the
    caller can fall through to the next method. The accompanying detail says
    which of those it was: without it, an image where *every* method fails is
    indistinguishable from an image where the register genuinely reads as
    unavailable, and the user is left with nothing to act on.
    """
    try:
        result = conn.run(cmd, hide=True, warn=True, timeout=PROBE_COMMAND_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - any transport/command error -> next method
        logger.debug("lock-bit command errored cmd=%r", cmd, exc_info=True)
        return None, f"{type(exc).__name__}: {exc}"

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip().splitlines()
    stderr_first = stderr[0] if stderr else ""
    if result.exited != 0:
        hint = _EXIT_CODE_HINTS.get(result.exited)
        detail = f"exit {result.exited}"
        if hint:
            detail = f"{detail} ({hint})"
        if stderr_first:
            detail = f"{detail}: {stderr_first}"
        return None, detail
    tokens = stdout.split()
    if not tokens:
        return None, "exit 0 but no output"
    try:
        value = int(tokens[0], 0)
    except ValueError:
        return None, f"unparseable output: {stdout[:40]!r}"
    return value & 1, None


def _read_lock_bit(conn: Connection) -> tuple[int | None, str | None]:
    """Read bit 0 of the FPGA lock-status register over SSH.

    Tries each method in :data:`_LOCK_BIT_CMDS` in order, returning on the first
    that yields a value. All are pure register reads and cannot disturb the
    loaded gateware. Returns ``(None, detail)`` only if every method failed, with
    ``detail`` naming each method and why it failed.
    """
    failures: list[str] = []
    for name, cmd in _LOCK_BIT_CMDS:
        bit, detail = _run_lock_bit_cmd(conn, cmd)
        if bit is not None:
            return bit, None
        failures.append(f"{name}: {(detail or 'no value')[:_LOCK_DETAIL_MAX_CHARS]}")
    return None, "; ".join(failures)


def _looks_rebooted(
    uptime_s: float,
    seconds_since_last_connected: float | None,
    uptime_threshold_s: float,
    *,
    known_boot_id: str | None = None,
    boot_id: str | None = None,
) -> bool:
    """Has the board restarted since we were last talking to it?

    Shared by the probe (which uses it to decide whether the lock register is
    worth reading) and the classifier (which uses it to decide whether the lock
    is lost). These MUST agree: when they drifted apart, the probe skipped the
    read for a device with no known last-connected time while the classifier
    called the same board "not rebooted", and the result was a confident
    "lock register unreadable" for a register nobody had tried to read.

    The test is `uptime < seconds_since_last_connected`: a board whose uptime is
    shorter than our absence must have restarted during it, and one whose uptime
    is longer cannot have. That is exact whenever both numbers are known, and it
    needs no threshold.

    `uptime_threshold_s` is only the fallback for a device we have never been
    connected to -- which is every device after a gateway restart -- where there
    is no absence to compare against. It must NOT be applied when we do know: a
    linien-server that died on a board which had just finished booting has low
    uptime and has not rebooted, and calling that a reboot would wrongly report
    the FPGA lock as lost.

    The kernel boot id beats both: it is a different string for every boot, so
    comparing the board's current one against the one it had while we were last
    connected answers the question outright -- no clock, no threshold, no
    absence to measure. It is used whenever both ids are known, which requires
    `known_boot_id` to have been recorded while the server was actually up
    (see `DeviceSession._record_healthy_boot_id`); a boot id read by a probe of
    an already-dead board says nothing, because the reboot would have happened
    before the probe.
    """
    if known_boot_id and boot_id:
        return known_boot_id != boot_id
    if seconds_since_last_connected is not None:
        return uptime_s < seconds_since_last_connected
    return uptime_s < uptime_threshold_s


def read_boot_id(device: Any, connection_factory: Any = None) -> str | None:
    """Read the board's current kernel boot id over SSH. None on any failure.

    Called once per successful connect, off the connect path, so the gateway
    holds an id from a moment the board was demonstrably healthy. That is the
    half of the comparison a probe cannot supply.
    """
    try:
        with open_ssh_connection(device, connection_factory or Connection) as conn:
            result = conn.run(
                f"cat {BOOT_ID_PATH}",
                hide=True,
                warn=True,
                timeout=PROBE_COMMAND_TIMEOUT_S,
            )
            boot_id = (getattr(result, "stdout", "") or "").strip()
            return boot_id or None
    except Exception:  # noqa: BLE001 - best effort; absence just means no id
        logger.debug(
            "boot id read failed host=%s",
            getattr(device, "host", ""),
            exc_info=True,
        )
        return None


def probe_device(
    device: Any,
    *,
    seconds_since_last_connected: float | None,
    uptime_threshold_s: float = DEFAULT_UPTIME_THRESHOLD_S,
    read_lock_register: bool = True,
    known_boot_id: str | None = None,
) -> ProbeResult:
    """Probe a (presumed disconnected) device out-of-band. Never raises."""
    host = getattr(device, "host", "") or ""

    # 1. Is the RPyC server listening again? Plain TCP, no SSH.
    server_port = getattr(device, "port", SERVER_PORT) or SERVER_PORT
    if _tcp_open(host, server_port, TCP_PROBE_TIMEOUT_S):
        return ProbeResult(
            server_listening=True,
            host_reachable=True,
            uptime_s=None,
            fpga_operating=None,
            lock_bit=None,
        )

    # 2. SSH probe for uptime / FPGA state / (gated) lock register.
    try:
        with open_ssh_connection(device, Connection) as conn:
            uptime_s, fpga_operating, boot_id = _read_uptime_and_fpga(conn)
            lock_bit: int | None = None
            # Only trust the lock register when a reboot is ruled out: the
            # gateware must be loaded (fpga_operating), uptime must be high, and
            # the board must not have rebooted since we were last connected.
            should_read = (
                read_lock_register
                and uptime_s is not None
                and bool(fpga_operating)
                and not _looks_rebooted(
                    uptime_s,
                    seconds_since_last_connected,
                    uptime_threshold_s,
                    known_boot_id=known_boot_id,
                    boot_id=boot_id,
                )
            )
            lock_detail: str | None = None
            if should_read:
                try:
                    lock_bit, lock_detail = _read_lock_bit(conn)
                except Exception as exc:  # noqa: BLE001 - register read is best-effort
                    logger.debug("lock register read raised host=%s", host, exc_info=True)
                    lock_bit = None
                    lock_detail = f"{type(exc).__name__}: {exc}"
                if lock_bit is None:
                    # The read was warranted but every method produced nothing.
                    # Log what each one actually said so the perpetual "lock
                    # likely held" fallback is debuggable instead of silent.
                    logger.warning(
                        "Lock register read attempted but unreadable for host=%s "
                        "(%s). Falling back to inferred lock state.",
                        host,
                        lock_detail or "no detail",
                    )
            return ProbeResult(
                server_listening=False,
                host_reachable=True,
                uptime_s=uptime_s,
                fpga_operating=fpga_operating,
                lock_bit=lock_bit,
                boot_id=boot_id,
                lock_read_attempted=should_read,
                lock_read_detail=lock_detail if lock_bit is None else None,
            )
    except AuthenticationException as exc:
        # Reachable, but we can't read board state.
        return ProbeResult(False, True, None, None, None, error=f"ssh auth failed: {exc}")
    except (NoValidConnectionsError, OSError, SSHException) as exc:
        # socket.timeout / socket.error are aliases of OSError.
        return ProbeResult(False, False, None, None, None, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - the probe must never raise
        logger.debug("probe_device unexpected error host=%s", host, exc_info=True)
        return ProbeResult(False, False, None, None, None, error=str(exc))


def classify_diagnosis(
    result: ProbeResult,
    *,
    host: str,
    seconds_since_last_connected: float | None,
    probed_at: float,
    uptime_threshold_s: float = DEFAULT_UPTIME_THRESHOLD_S,
    known_boot_id: str | None = None,
) -> dict[str, Any]:
    """Turn raw probe signals into a category, lock state, and a message.

    The reboot test is `uptime < seconds_since_last_connected`: a board whose
    uptime is shorter than our absence must have restarted during it, and one
    whose uptime is longer cannot have. That is exact whenever both numbers are
    known, and it needs no threshold.

    `uptime_threshold_s` is only the fallback for a device we have never been
    connected to, where there is no absence to compare against. It must not be
    applied when we do know: a linien-server that died on a board which had
    just finished booting has low uptime and has not rebooted, and calling that
    a reboot would wrongly report the FPGA lock as lost.
    """
    uptime_s = result.uptime_s

    if result.server_listening:
        category = CATEGORY_RECOVERING
        lock_state = "unknown"
        message = "Server is back online — reconnecting."
    elif not result.host_reachable:
        category = CATEGORY_HOST_UNREACHABLE
        lock_state = "unknown"
        message = f"Cannot reach {host or 'the device'}. The lock is lost if the board is powered off."
    elif uptime_s is None:
        category = CATEGORY_SERVER_DOWN_UNKNOWN
        lock_state = "unknown"
        message = (
            "Board is reachable but linien-server is down; board state could not be read."
        )
    elif _looks_rebooted(
        uptime_s,
        seconds_since_last_connected,
        uptime_threshold_s,
        known_boot_id=known_boot_id,
        boot_id=result.boot_id,
    ):
        category = CATEGORY_REBOOTED
        lock_state = "lost"
        message = (
            "Red Pitaya rebooted — the lock was lost. linien-server is not running "
            "(auto-start is disabled)."
        )
    else:
        category = CATEGORY_SERVER_CRASHED
        if result.lock_bit == 1:
            lock_state = "locked"
            message = (
                "linien-server is down but the FPGA is still locked (confirmed). "
                "Do NOT restart the server if you want to keep the lock."
            )
        elif result.lock_bit == 0:
            lock_state = "unlocked"
            message = "linien-server is down; the FPGA is running but not locked."
        elif result.fpga_operating is False:
            # The lock register could not be read because the gateware is not
            # loaded (fpga_manager state != "operating"). With no gateware the
            # FPGA cannot be holding the lock, so do NOT claim "likely held".
            lock_state = "lost"
            message = (
                "linien-server is down and the FPGA gateware is not loaded "
                "(fpga_manager state is not 'operating'), so the lock is lost."
            )
        elif result.fpga_operating is None:
            # FPGA state could not be determined at all — stay honest.
            lock_state = "unknown"
            message = (
                "linien-server is down; the FPGA state could not be read, so the "
                "lock state is unknown."
            )
        elif not result.lock_read_attempted:
            # The gate above declined to read. Saying "unreadable" here would
            # blame the board for a decision this code made.
            lock_state = "likely_held"
            message = (
                "linien-server is down; the FPGA gateware is still loaded, so the "
                "lock is likely still held (the lock register was not read)."
            )
        else:
            # FPGA gateware is loaded but every read method failed.
            lock_state = "likely_held"
            reason = result.lock_read_detail or "no method returned a value"
            message = (
                "linien-server is down; the FPGA gateware is still loaded, so the "
                "lock is likely still held (lock register unreadable — "
                f"{reason})."
            )

    return {
        "category": category,
        "lock_state": lock_state,
        "message": message,
        "probed_at": probed_at,
        "uptime_s": uptime_s,
        "host_reachable": result.host_reachable,
        "server_running": result.server_listening,
        "fpga_operating": result.fpga_operating,
        "lock_read_detail": result.lock_read_detail,
        "seconds_since_last_connected": seconds_since_last_connected,
        "boot_id": result.boot_id,
        "known_boot_id": known_boot_id,
        # Which test answered "did it reboot", so a surprising verdict can be
        # read back rather than guessed at.
        "reboot_evidence": (
            "boot_id"
            if known_boot_id and result.boot_id
            else "absence"
            if seconds_since_last_connected is not None
            else "uptime_threshold"
        ),
    }


class DiagnosisProbe:
    """Shared scheduler that probes disconnected devices over SSH.

    A single dispatcher thread tracks *when* each disconnected device is due and
    hands the actual (blocking, ~6-11 s) SSH probe to a small bounded thread pool,
    so a mass power-cut doesn't serialise every device behind one slow timeout
    while still capping concurrency to avoid an SSH storm. Each still-disconnected
    device is re-probed every ``reprobe_interval_s`` so the UI badge tracks a board
    rebooting/recovering. The poll loop and status fan-out never block on it — they
    only enqueue keys via :meth:`request`.
    """

    def __init__(
        self,
        registry: "SessionRegistry",
        *,
        probe_fn: Callable[..., ProbeResult] = probe_device,
        reprobe_interval_s: float = MIN_REPROBE_INTERVAL_S,
        uptime_threshold_s: float = DEFAULT_UPTIME_THRESHOLD_S,
        max_workers: int = DIAGNOSIS_PROBE_WORKERS,
        event_store: Any = None,
    ) -> None:
        self._registry = registry
        self._probe_fn = probe_fn
        self._reprobe_interval_s = reprobe_interval_s
        self._uptime_threshold_s = uptime_threshold_s
        # Optional: without it the probe simply leaves no trace in the
        # board timeline.
        self._event_store = event_store
        self._max_workers = max_workers
        self._heap: list[tuple[float, int, str]] = []
        self._pending: set[str] = set()  # scheduled in the heap, not yet running
        self._inflight: set[str] = set()  # handed to the pool, probe in progress
        self._heap_lock = threading.Lock()
        self._seq = itertools.count()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._clock = time.monotonic

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers, thread_name_prefix="diagnosis-probe"
        )
        self._thread = threading.Thread(
            target=self._run, name="diagnosis-dispatch", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None
        executor = self._executor
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        self._executor = None

    def request(self, key: str, delay: float = 0.0) -> None:
        """Schedule a probe of ``key`` after ``delay`` seconds (deduplicated).

        Deduplicates against both scheduled (``_pending``) and currently-running
        (``_inflight``) probes so a device is never probed twice concurrently.
        """
        due = self._clock() + max(0.0, delay)
        with self._heap_lock:
            if key in self._pending or key in self._inflight:
                return
            self._pending.add(key)
            heapq.heappush(self._heap, (due, next(self._seq), key))
        self._wake.set()

    def _pop_ready(self) -> tuple[str | None, float | None]:
        now = self._clock()
        with self._heap_lock:
            if not self._heap:
                return None, None
            due = self._heap[0][0]
            if due <= now:
                _, _, key = heapq.heappop(self._heap)
                self._pending.discard(key)
                self._inflight.add(key)
                return key, None
            return None, max(0.0, due - now)

    def _run(self) -> None:
        while not self._stop.is_set():
            key, wait = self._pop_ready()
            if key is None:
                self._wake.wait(timeout=wait if wait is not None else 1.0)
                self._wake.clear()
                continue
            executor = self._executor
            if executor is None:
                with self._heap_lock:
                    self._inflight.discard(key)
                break
            try:
                executor.submit(self._probe_and_reschedule, key)
            except RuntimeError:
                # Executor was shut down between the check and submit.
                with self._heap_lock:
                    self._inflight.discard(key)
                break

    def _probe_and_reschedule(self, key: str) -> None:
        try:
            self._probe_once(key)
        except Exception:  # noqa: BLE001 - one bad probe must not kill the pool
            logger.debug("diagnosis worker iteration failed key=%s", key, exc_info=True)
        finally:
            with self._heap_lock:
                self._inflight.discard(key)
            # Keep monitoring while the device is still down and still wants it.
            # Done after clearing _inflight so request() isn't deduped away.
            session = self._registry.get(key)
            if (
                session is not None
                and session.wants_diagnosis()
                and not (
                    getattr(session, "connected", False)
                    or getattr(session, "connecting", False)
                )
            ):
                self.request(key, delay=self._reprobe_interval_s)

    def _probe_once(self, key: str) -> None:
        session = self._registry.get(key)
        if session is None:
            return  # device removed
        if getattr(session, "connected", False) or getattr(session, "connecting", False):
            return  # reconnected / reconnecting; will be re-requested on next drop
        if not session.wants_diagnosis():
            return  # intentionally disconnected
        device = session.device
        since = session.seconds_since_last_connected()
        known_boot_id = None
        try:
            known_boot_id = session.last_healthy_boot_id()
        except Exception:  # noqa: BLE001 - absence of the hook is not fatal
            logger.debug("last_healthy_boot_id failed key=%s", key, exc_info=True)
        probed_at = time.time()
        try:
            result = self._probe_fn(
                device,
                seconds_since_last_connected=since,
                uptime_threshold_s=self._uptime_threshold_s,
                known_boot_id=known_boot_id,
            )
        except Exception:  # noqa: BLE001 - defense in depth; probe_fn shouldn't raise
            logger.debug("diagnosis probe raised key=%s", key, exc_info=True)
            result = ProbeResult(False, False, None, None, None, error="probe error")
        diagnosis = classify_diagnosis(
            result,
            host=getattr(device, "host", "") or "",
            seconds_since_last_connected=since,
            probed_at=probed_at,
            uptime_threshold_s=self._uptime_threshold_s,
            known_boot_id=known_boot_id,
        )
        # Timeline only, and distinct from `known_boot_id` above. The event
        # store holds the id from the last *probe*, not from when we were last
        # connected, so it cannot answer "did it reboot during this outage" --
        # and re-probing a still-down board every 20 s would compare the id
        # against itself and retract a reboot it had just correctly reported.
        # The session's id is the one that can answer it, because it was
        # recorded while the server was up.
        #
        # Skipped once a recovery is under way, the same way apply_diagnosis
        # drops its result: an SSH probe takes ~6-11 s, so one already running
        # when the operator clicks Reboot would finish afterwards and re-latch
        # the pre-reboot id that `forget_boot_id` had just cleared -- and the
        # operator's own reboot would end up in the instability count.
        recovery_active = False
        try:
            recovery_active = bool(session.recovery_active())
        except Exception:  # noqa: BLE001 - absence of the hook is not fatal
            logger.debug("recovery_active check failed key=%s", key, exc_info=True)
        if self._event_store is not None and result.boot_id and not recovery_active:
            try:
                self._event_store.note_boot_id(key, result.boot_id)
            except Exception:  # noqa: BLE001 - history must not break the probe
                logger.debug("note_boot_id failed key=%s", key, exc_info=True)
        session.apply_diagnosis(diagnosis)
