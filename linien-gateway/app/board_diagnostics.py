"""On-demand, read-only post-mortem collection from a Red Pitaya.

`diagnosis.py` answers *what* happened to a lost connection -- rebooted, server
crashed, host unreachable -- from a handful of signals. This module answers
*why*, by fetching the evidence: the linien-server journal, the kernel ring
buffer, the unit's exit status, and whatever the board recorded about its own
reset.

Two things shape the design.

**It is operator-triggered, never periodic.** Every command here crosses SSH,
and the whole point of `rp-telemetry` is that nothing polls a board over SSH.
Collection happens when somebody asks, from a bounded worker pool.

**Sections fail independently.** These images vary: some have no `pstore`, some
were never given a persistent journal, some run linien-server by hand rather
than through systemd. A missing section is itself a finding, so one failure
must not lose the other ten. Every command runs with `warn=True` and each
section carries its own error.

The one non-read-only action lives here too: `enable_persistent_journal`. It
exists because of a hard limitation -- these boards ship with a volatile
journal, so after a reset the pre-crash logs are simply gone and no amount of
collection recovers them. Persistence has to be switched on *before* the crash
you want to read about.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Callable

from paramiko.ssh_exception import AuthenticationException

from .ssh import open_ssh_connection, privileged, run_remote, shell_single_quote

logger = logging.getLogger(__name__)

LINIEN_UNIT = "linien-server.service"
# Where linien puts its log, per `linien_common.config.LOG_FILE_PATH`:
# `AppDirs("linien").user_data_dir / "linien.log"`.
LINIEN_LOG_SUBDIR = ".local/share/linien"
TELEMETRY_UNIT = "rp-telemetry"

# Per-command ceiling. Bounds a hung command the way `diagnosis.py` bounds a
# possible AXI stall: a board that has stopped answering must not hold the
# worker for the full SSH timeout, twelve times over.
SECTION_TIMEOUT_S = 10.0
# Generous enough to hold a Python traceback plus the lines that led to it.
JOURNAL_LINES = 200
DMESG_LINES = 200
# Truncate before the payload reaches the browser. A wedged board can emit
# megabytes of repeated kernel messages, and the tail is the interesting end.
SECTION_MAX_CHARS = 20_000

# SLCR REBOOT_STATUS, and the marker the section prints its value behind so
# the decoder can find it without re-running anything.
REBOOT_STATUS_ADDR = "0xF8000258"
REBOOT_STATUS_PREFIX = "REBOOT_STATUS="
# The page the register lives in, and its offset inside that page, for the
# mmap fallback below. Derived rather than written out so they cannot disagree
# with the address, and mmap will only accept a page-aligned offset.
REBOOT_STATUS_PAGE_SIZE = 4096
REBOOT_STATUS_PAGE_ADDR = int(REBOOT_STATUS_ADDR, 16) & ~(REBOOT_STATUS_PAGE_SIZE - 1)
REBOOT_STATUS_PAGE_OFFSET = int(REBOOT_STATUS_ADDR, 16) - REBOOT_STATUS_PAGE_ADDR
# Bit -> a reset cause the register has recorded. Bits 16-19 are as documented
# for the Zynq-7000 (UG585 section 6.3.12); 20 and 21 follow the same order the
# TRM lists the reset sources in and are not confirmed here -- hence
# `unverified`, and hence the raw value stays in the section output.
#
# Bit 22 was confirmed in the field: a board came back 0x00410000, i.e. 22 and
# 16 set together. Which also settles how the register behaves, and it is not
# how this code originally assumed. The bits ACCUMULATE. Were each reset to
# clear the register and set one bit, a power-on and a later watchdog timeout
# could never both be standing. So a reading is every cause since the register
# was last cleared, not the last reset -- and an empty register means only
# that something cleared it (some bootloaders do), never "the board lost
# power". Bit 22 being set is what a power-on leaves behind.
REBOOT_STATUS_BITS: tuple[tuple[int, str, str, bool], ...] = (
    (16, "SWDT_RST", "system watchdog timeout", False),
    (17, "AWDT0_RST", "CPU0 watchdog timeout", False),
    (18, "AWDT1_RST", "CPU1 watchdog timeout", False),
    (19, "SLC_RST", "software reboot (SLCR), e.g. `reboot`", False),
    (20, "DBG_RST", "debug reset over JTAG", True),
    (21, "SRST_B", "external reset pin asserted", True),
    (22, "POR", "power-on reset", False),
)

# The causes worth a dedicated flag on the summary, because they are what an
# operator is actually asking about: did it hang, or did someone restart it.
REBOOT_STATUS_WATCHDOG_BITS = (16, 17, 18)
REBOOT_STATUS_SOFTWARE_BIT = 19
REBOOT_STATUS_POWER_ON_BIT = 22

JOURNALD_DROPIN_DIR = "/etc/systemd/journald.conf.d"
# `99-` so it wins: systemd applies drop-ins in lexical order, and a `00-`
# prefix loses to the conventional `99-*.conf` a vendor image may already
# ship -- silently, with Storage= reverting and nothing to show for it.
JOURNALD_DROPIN_PATH = JOURNALD_DROPIN_DIR + "/99-linien-persistent.conf"
JOURNAL_DIR = "/var/log/journal"

# SD cards wear out and fill up, so the journal is capped rather than left to
# journald's default of 10% of the filesystem. 32 MB is days of a quiet board.
JOURNALD_DROPIN = "[Journal]\nStorage=persistent\nSystemMaxUse=32M\nSystemMaxFileSize=8M\n"

RUNTIME_JOURNAL_DIR = "/run/log/journal"

# Where the journal actually lives on an image whose /var/log is a RAM disk.
# On the root filesystem, beside /var/log rather than inside it, so it is not
# swallowed by the tmpfs that covers /var/log at every boot.
JOURNAL_BACKING_DIR = "/var/log-persistent/journal"

# The unit name is not a choice: systemd derives it from the mount point, and
# `/var/log/journal` escapes to exactly this. A different name would never be
# matched to the path and would never be pulled in.
JOURNAL_MOUNT_UNIT = "var-log-journal.mount"
JOURNAL_MOUNT_UNIT_PATH = "/etc/systemd/system/" + JOURNAL_MOUNT_UNIT

# No [Install] section, deliberately. systemd-journal-flush.service carries
# `RequiresMountsFor=/var/log/journal`, which pulls this unit in *and* orders it
# before the flush at every boot -- so the mount is established before journald
# moves anything to disk, with no enablement to remember.
#
# `WantedBy=local-fs.target` would look tidier and is the trap: it would make a
# failed mount a local-fs.target failure, and a headless board that drops to
# emergency mode needs a lab visit with an SD reader. Pulled in only by the
# flush, the worst case of a broken mount is the journal staying in RAM --
# which is the status quo this action is trying to improve on.
#
# systemd creates the mount point itself (src/core/mount.c), which matters:
# /var/log is emptied by its tmpfs at every boot, so /var/log/journal cannot be
# created once at install time and expected to still be there.
JOURNAL_MOUNT_UNIT_TEXT = (
    "[Unit]\n"
    "Description=Persistent backing store for the systemd journal\n"
    "DefaultDependencies=no\n"
    "\n"
    "[Mount]\n"
    "What=" + JOURNAL_BACKING_DIR + "\n"
    "Where=" + JOURNAL_DIR + "\n"
    "Type=none\n"
    "Options=bind\n"
)

# What filesystem is under the journal directory, and how much room it has.
# Sets FS and AVAILKB; echoes nothing, so it can be pasted in front of a test.
#
# The type comes from /proc/mounts, not from df's first column: a tmpfs is
# routinely mounted with the source `none`, which is how a Red Pitaya image
# with a 5 MB RAM disk on /var/log got past an earlier check that matched on
# the device name. `stat -f` would be shorter and busybox does not have it.
def _fs_probe(path: str) -> str:
    return (
        "L=$(df -Pk " + path + " 2>/dev/null | tail -n 1 | tr -s \" \"); "
        'MP=$(echo "$L" | cut -d" " -f6); '
        'AVAILKB=$(echo "$L" | cut -d" " -f4); '
        'FS=$(grep " $MP " /proc/mounts 2>/dev/null | cut -d" " -f3 | tail -n 1); '
    )


_FS_PROBE = _fs_probe(JOURNAL_DIR)

# A journal file is created at SystemMaxFileSize and journald keeps a margin
# free, so a filesystem that cannot hold one is refused outright rather than
# filled. journald's own failure mode here is silent: it falls back to runtime
# storage and the board looks exactly like one that was never configured.
JOURNAL_MIN_FREE_KB = 16 * 1024

# Which storage journald is *currently* using, decided by where its per-machine
# directory lives. journald creates `<dir>/<machine-id>` under whichever of the
# two it is writing to, and a flush removes the runtime copy once the logs have
# moved to disk.
#
# Not the existence of /var/log/journal: `enable_persistent_journal` creates
# that directory itself, so checking for it afterwards would confirm nothing
# but our own mkdir. And not a grep of `journalctl --header` either -- that
# lists every journal file it can read, archived ones included, so a board that
# was persistent once and is volatile now would match on its own leftovers and
# be reported as safe when the next crash would again leave nothing.
#
# The runtime directory is checked first for exactly that reason: when both
# exist, the one journald is writing to now is the runtime one.
#
# TMPFS is the case a directory check alone gets wrong: some images mount
# /var/log (or all of /var) on a tmpfs, so journald obeys Storage=persistent,
# creates its per-machine directory there, and still loses every line at the
# next reset.
_STORAGE_PROBE = (
    'MID=$(cat /etc/machine-id 2>/dev/null || true); '
    'if [ -z "$MID" ]; then echo STORAGE=UNKNOWN; '
    'elif [ -d "' + RUNTIME_JOURNAL_DIR + '/$MID" ]; then echo STORAGE=VOLATILE; '
    'elif [ -d "' + JOURNAL_DIR + '/$MID" ]; then '
    + _FS_PROBE
    + 'if [ "$FS" = tmpfs ] || [ "$FS" = ramfs ]; then echo STORAGE=TMPFS; '
    "else echo STORAGE=PERSISTENT; fi; "
    "else echo STORAGE=UNKNOWN; fi"
)

# Run before journald is restarted, so an image that cannot hold a journal is
# told so instead of being reconfigured, restarted and then found wanting.
_FS_REPORT_TAIL = 'echo "FSTYPE=$FS MOUNT=$MP AVAILKB=$AVAILKB"'
_JOURNAL_FS_REPORT = _FS_PROBE + _FS_REPORT_TAIL
_BACKING_FS_REPORT = _fs_probe(JOURNAL_BACKING_DIR) + _FS_REPORT_TAIL

# journald flushes the runtime journal to disk asynchronously, and on older
# systemd `journalctl --flush` only signals the daemon and returns. Deciding on
# the first look therefore reported boards that had just been configured
# correctly as failures -- the runtime directory was simply still there a
# moment later. Poll instead, and stop at the first non-volatile answer.
_STORAGE_SETTLE_ATTEMPTS = 5

_STORAGE_PROBE_SETTLED = (
    "STATE=; i=0; while [ $i -lt " + str(_STORAGE_SETTLE_ATTEMPTS) + " ]; do "
    "STATE=$(" + _STORAGE_PROBE + "); "
    'case "$STATE" in *VOLATILE*) ;; *) break;; esac; '
    "i=$((i+1)); sleep 1; done; "
    'echo "$STATE"'
)

# Shown to the operator when the verification fails, because "it is still
# volatile" on its own leaves them with nothing to act on. These four answers
# cover every cause seen so far: a runtime directory that never went away, a
# /var/log on tmpfs, another drop-in overriding Storage=, and a journald that
# did not come back up.
_STORAGE_DETAIL = (
    "ls -d " + RUNTIME_JOURNAL_DIR + " " + JOURNAL_DIR + " 2>/dev/null; "
    "df -Pk " + JOURNAL_DIR + " 2>/dev/null | tail -n 1; "
    + _JOURNAL_FS_REPORT
    + "; "
    'grep -sHE "^[[:space:]]*Storage=" /etc/systemd/journald.conf '
    "/etc/systemd/journald.conf.d/*.conf || true; "
    "systemctl show systemd-journald -p ActiveState -p SubState || true; "
    # The bind mount, when this board has one: a journal that stopped
    # persisting because its mount unit failed looks identical from journald's
    # side to one that was never configured.
    "systemctl show " + JOURNAL_MOUNT_UNIT + " -p LoadState -p ActiveState "
    "-p Result || true"
)

# (name, title, command, needs_root). Ordered as an operator reads them: what
# board is this, does it even keep logs, what did the server do, what did the
# kernel say. `|| true` where a non-match is a normal outcome rather than a
# fault.
#
# `needs_root` is per section on purpose. Running everything through `sudo -n`
# means that on a board whose SSH user has no passwordless sudo, all twelve
# sections come back "a password is required" -- an empty bundle, and no signal
# at all about persistence, so the Enable button never appears either. The
# sections that read world-readable files work unprivileged and should stay
# that way, so such a board still yields the half of the bundle it can.
_SECTIONS: tuple[tuple[str, str, str, bool], ...] = (
    (
        "identity",
        "Board identity and uptime",
        "cat /proc/uptime; cat /proc/sys/kernel/random/boot_id; uname -a; date -Is",
        False,
    ),
    (
        "journald",
        "Journal persistence",
        _STORAGE_PROBE + "; "
        # What is under /var/log, so an image that cannot hold a journal at all
        # is visible in the bundle rather than only on a failed Enable.
        + _JOURNAL_FS_REPORT + "; "
        'journalctl --header 2>/dev/null | grep -i "file path" | head -n 3 || true; '
        "ls -d " + JOURNAL_DIR + " " + RUNTIME_JOURNAL_DIR + " 2>/dev/null; "
        "grep -hE \"^[[:space:]]*Storage=\" /etc/systemd/journald.conf "
        "/etc/systemd/journald.conf.d/*.conf 2>/dev/null || true",
        True,
    ),
    ("boots", "Recorded boots", "journalctl --list-boots --no-pager || true", True),
    (
        "linien_unit",
        "linien-server unit state",
        # LoadState and UnitFileState first: `systemctl show` prints a full set
        # of defaults for a unit that does not exist at all, so without them
        # "never installed" and "installed but never started" render as the
        # same all-zeroes, all-n/a output.
        "systemctl show " + LINIEN_UNIT + " -p LoadState -p UnitFileState "
        "-p ActiveState -p SubState -p Result "
        "-p ExecMainStatus -p ExecMainCode -p ExecMainStartTimestamp "
        "-p ExecMainExitTimestamp -p NRestarts",
        False,
    ),
    (
        "linien_process",
        "linien-server process",
        # The unit sections go blank on a board where the server was started by
        # `linien-server start` over SSH rather than by systemd -- which is
        # what the gateway's own autostart does. Without this, such a board
        # looks identical to one where nothing was ever running.
        'ps -eo pid,etime,rss,args 2>/dev/null | grep -i "[l]inien" '
        '|| echo "no linien process running"',
        False,
    ),
    (
        "linien_logfile",
        "linien-server log file",
        # linien logs to a file under the user data directory
        # (`linien_common.config.LOG_FILE_PATH`), on the root filesystem rather
        # than in the journal. On these images that is the only account of a
        # server death that survives anything at all.
        #
        # `linien.log.1` as well as `linien.log`: the handler is a
        # RotatingFileHandler, so the run that died is often one rotation back
        # and the live file holds nothing but the restart that followed it.
        # The directory listing comes first because the mtimes are the timeline.
        'D="$HOME/' + LINIEN_LOG_SUBDIR + '"; '
        '[ -d "$D" ] || D="/root/' + LINIEN_LOG_SUBDIR + '"; '
        'ls -la "$D" 2>/dev/null; '
        'for f in "$D/linien.log.1" "$D/linien.log"; do '
        'if [ -f "$f" ]; then echo; echo "== $f"; tail -n '
        + str(JOURNAL_LINES)
        + ' "$f"; fi; done',
        False,
    ),
    (
        "linien_journal",
        "linien-server log (this boot)",
        "journalctl -u " + LINIEN_UNIT + " -b 0 -n " + str(JOURNAL_LINES)
        + " --no-pager --output=short-iso",
        True,
    ),
    (
        "linien_journal_prev",
        "linien-server log (previous boot)",
        "journalctl -u " + LINIEN_UNIT + " -b -1 -n " + str(JOURNAL_LINES)
        + " --no-pager --output=short-iso",
        True,
    ),
    ("kernel", "Kernel ring buffer", "dmesg | tail -n " + str(DMESG_LINES), True),
    (
        "reset_cause",
        "Reset, OOM and panic lines",
        "dmesg | grep -iE "
        "\"watchdog|reset|reboot|panic|oom-kill|out of memory|bus error|hung task\" "
        "|| true",
        True,
    ),
    (
        "reboot_status",
        "Zynq reset-cause register",
        # SLCR REBOOT_STATUS (0xF8000258). The one reading that separates "the
        # power actually dropped" from "something reset the board": it is
        # sticky through every reset EXCEPT power-on, which clears it. The
        # kernel log cannot answer that, because a board that lost power wrote
        # nothing before it went.
        #
        # Five ways to read one PS register, because these images differ and a
        # board that cannot be read is a board whose reset cause is gone for
        # good. In order of directness: the busybox `devmem` applet, `devmem2`,
        # Red Pitaya's own `monitor`, busybox invoked by name for an image that
        # never symlinked the applet, and -- needing nothing installed at all
        # -- `dd` on /dev/mem formatted by `od`. A field board had none of the
        # first three and reported "no way to read", which is what the last two
        # are for.
        #
        # Each step runs when the previous produced nothing, not merely when
        # its tool was absent: the chain used to be an if/elif, so a `devmem`
        # that existed and failed silently ended it with an empty answer and
        # the remaining tools were never tried.
        #
        # The last one mmaps, and that is not a stylistic choice: `read()` on
        # /dev/mem copies from `__va(phys)`, which is only valid for RAM. SLCR
        # is IO space, so the kernel has to ioremap it and that path exists
        # only for mmap -- a `dd` of this address fails with EFAULT ("Bad
        # address") on any kernel, which is what the field board returned. It
        # is why every devmem tool mmaps, and why there is no shell-only
        # reader here.
        #
        # python3 is a fair assumption on a board running Linien: the server
        # itself is a Python process. `python` is tried after it for an image
        # that never made the version-suffixed name.
        #
        # Reading SLCR touches nothing in the FPGA fabric.
        'V=""; '
        "if command -v devmem >/dev/null 2>&1; then "
        'V=$(devmem ' + REBOOT_STATUS_ADDR + ' 32 2>/dev/null); fi; '
        'if [ -z "$V" ] && command -v devmem2 >/dev/null 2>&1; then '
        'V=$(devmem2 ' + REBOOT_STATUS_ADDR + ' w 2>/dev/null '
        '| sed -n "s/.*: 0x/0x/p" | tail -n 1); fi; '
        'if [ -z "$V" ] && command -v monitor >/dev/null 2>&1; then '
        'V=$(monitor ' + REBOOT_STATUS_ADDR + ' 2>/dev/null); fi; '
        'if [ -z "$V" ] && command -v busybox >/dev/null 2>&1; then '
        'V=$(busybox devmem ' + REBOOT_STATUS_ADDR + ' 32 2>/dev/null); fi; '
        'for PY in python3 python; do '
        '[ -z "$V" ] || break; '
        'command -v $PY >/dev/null 2>&1 || continue; '
        'V=$($PY -c "'
        "import mmap, os, struct; "
        "f = os.open('/dev/mem', os.O_RDONLY | getattr(os, 'O_SYNC', 0)); "
        "m = mmap.mmap(f, " + str(REBOOT_STATUS_PAGE_SIZE) + ", mmap.MAP_SHARED, "
        "mmap.PROT_READ, offset=" + str(REBOOT_STATUS_PAGE_ADDR) + "); "
        "print('0x%08x' % struct.unpack('<I', m["
        + str(REBOOT_STATUS_PAGE_OFFSET) + ":" + str(REBOOT_STATUS_PAGE_OFFSET + 4)
        + "])[0])"
        '" 2>/dev/null); '
        'done; '
        'if [ -n "$V" ]; then echo "' + REBOOT_STATUS_PREFIX + '$V"; '
        'else echo "no way to read ' + REBOOT_STATUS_ADDR
        + ' (tried devmem, devmem2, monitor, busybox devmem, python mmap)"; fi',
        True,
    ),
    (
        "pstore",
        "Crash dump remnants",
        "cat /sys/fs/pstore/* 2>/dev/null || echo \"no pstore records\"",
        True,
    ),
    (
        "resources",
        "Memory, disk and load",
        "free -m; df -h /; cat /proc/loadavg",
        False,
    ),
    ("fpga", "FPGA manager state", "cat /sys/class/fpga_manager/fpga0/state", False),
    (
        "telemetry",
        "rp-telemetry log",
        "journalctl -u " + TELEMETRY_UNIT + " -n 40 --no-pager --output=cat",
        True,
    ),
)


def _bounded(command: str) -> str:
    """Wrap a command in `timeout`, via `sh -c` so pipes survive the wrapping."""
    return "timeout {} sh -c '{}'".format(
        int(SECTION_TIMEOUT_S), shell_single_quote(command)
    )


def _truncate(text: str) -> str:
    text = text.strip()
    if len(text) <= SECTION_MAX_CHARS:
        return text
    return "[...truncated...]\n" + text[-SECTION_MAX_CHARS:]


def _open(device: Any, connection_factory: Callable[..., Any] | None):
    if connection_factory is not None:
        return open_ssh_connection(device, connection_factory)
    return open_ssh_connection(device)


def collect_diagnostics(
    device: Any, *, connection_factory: Callable[..., Any] | None = None
) -> dict[str, Any]:
    """Gather the diagnostic bundle over one SSH connection. Never raises.

    A transport failure is returned as `ok: False` with whatever sections were
    already collected, because "the board stopped answering halfway through" is
    itself worth showing.
    """
    collected_at = time.time()
    sections: list[dict[str, Any]] = []
    error: str | None = None

    try:
        with _open(device, connection_factory) as conn:
            for name, title, command, needs_root in _SECTIONS:
                sections.append(
                    _run_section(conn, device, name, title, command, needs_root)
                )
    except AuthenticationException as exc:
        error = f"SSH authentication failed: {exc}"
    except Exception as exc:  # noqa: BLE001 - collection must never raise
        logger.debug("diagnostics collection failed", exc_info=True)
        error = str(exc)

    return {
        "ok": error is None,
        "error": error,
        "collected_at": collected_at,
        "sections": sections,
        "persistent_journal": _persistent_journal(sections),
        "reboot_status": _reboot_status_summary(sections),
    }


def _reboot_status_summary(sections: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Decoded reset causes for the bundle header, or None if unavailable.

    `power_on` is the POR bit, not the absence of every other bit. The earlier
    reading -- no bit set means the board lost power -- was exactly backwards,
    and the field value that showed it stood at 0x00410000.
    """
    for section in sections:
        if section.get("name") != "reboot_status":
            continue
        value = parse_reboot_status(section.get("output", ""))
        description = describe_reboot_status(value)
        if description is None:
            return None
        return {
            "value": value,
            "causes": [
                name
                for bit, name, _meaning, _unverified in REBOOT_STATUS_BITS
                if value & (1 << bit)
            ],
            "watchdog": any(
                value & (1 << bit) for bit in REBOOT_STATUS_WATCHDOG_BITS
            ),
            "software_reboot": bool(value & (1 << REBOOT_STATUS_SOFTWARE_BIT)),
            "power_on": bool(value & (1 << REBOOT_STATUS_POWER_ON_BIT)),
            "description": description,
        }
    return None


def _run_section(
    conn: Any, device: Any, name: str, title: str, command: str, needs_root: bool
) -> dict[str, Any]:
    try:
        exited, stdout, stderr = run_remote(
            conn,
            device,
            _bounded(command),
            timeout=SECTION_TIMEOUT_S + 5.0,
            privileged_command=needs_root,
        )
    except Exception as exc:  # noqa: BLE001 - one section, not the bundle
        logger.debug("diagnostics section failed name=%s", name, exc_info=True)
        return {
            "name": name,
            "title": title,
            "command": command,
            "output": "",
            "error": str(exc),
        }
    # A non-zero exit is usually "this image does not have that", which is
    # worth showing beside whatever the command did print rather than
    # replacing it.
    section_error = None
    if exited != 0:
        detail = (stderr or stdout or "").strip()[:300]
        section_error = detail or f"exited {exited}"
    return {
        "name": name,
        "title": title,
        "command": command,
        "output": _truncate(stdout),
        "error": section_error,
    }


def parse_reboot_status(output: str) -> int | None:
    """The register value the `reboot_status` section printed, or None."""
    for line in (output or "").splitlines():
        line = line.strip()
        if not line.startswith(REBOOT_STATUS_PREFIX):
            continue
        rest = line[len(REBOOT_STATUS_PREFIX) :].split()
        if not rest:
            return None
        token = rest[0]
        try:
            return int(token, 16) if token.lower().startswith("0x") else int(token, 10)
        except ValueError:
            return None
    return None


def describe_reboot_status(value: int | None) -> str | None:
    """Plain-language reading of REBOOT_STATUS, or None if there is nothing.

    Every cause SINCE THE REGISTER WAS LAST CLEARED, not the last reset: the
    bits accumulate (see REBOOT_STATUS_BITS). So this deliberately says
    "recorded" rather than "caused", and an empty register is reported as
    nothing recorded rather than as a power loss.

    Bits 20 and 21 remain unverified against the manual, so a decode resting
    on them still says so.
    """
    if value is None:
        return None
    causes = [
        (name, meaning, unverified)
        for bit, name, meaning, unverified in REBOOT_STATUS_BITS
        if value & (1 << bit)
    ]
    # Bits 31:24 are scratch space for the BootROM and u-boot, not a cause.
    state = (value >> 24) & 0xFF
    tail = f" (boot state 0x{state:02x})" if state else ""
    if not causes:
        return (
            f"0x{value:08x}: no reset-cause bit set, so nothing has been "
            "recorded since the register was last cleared. Some bootloaders "
            "clear it at every boot; this is not evidence of a power loss."
            + tail
        )
    listed = "; ".join(
        f"{name} ({meaning}{', bit position unverified' if unverified else ''})"
        for name, meaning, unverified in causes
    )
    return (
        f"0x{value:08x}: {listed}. These accumulate, so they are every cause "
        "recorded since the register was last cleared, in no particular order "
        "and with no timestamp -- the board timeline dates the restart."
        + tail
    )


def _persistent_journal(sections: list[dict[str, Any]]) -> bool | None:
    """Whether the board keeps logs across a reboot. None when undetermined.

    Keyed on `/var/log/journal` rather than on the number of recorded boots:
    journald writes there exactly when storage is persistent, whereas a board
    that has only booted once looks identical to a volatile one in
    `--list-boots`.
    """
    for section in sections:
        if section.get("name") != "journald":
            continue
        output = section.get("output") or ""
        if "STORAGE=PERSISTENT" in output:
            return True
        # TMPFS is a no as firmly as VOLATILE is: journald is writing to
        # /var/log/journal and that directory is a RAM disk.
        if "STORAGE=VOLATILE" in output or "STORAGE=TMPFS" in output:
            return False
        # UNKNOWN, or the section did not run at all. Stay honest: reporting
        # False here would nag about a board we could not read, and True would
        # promise logs that may not survive.
        return None
    return None


# Clearing the register needs SLCR's write protection lifted and put back.
# These two magic words are fixed by the silicon (UG585 section 4.1.2); they
# are not configuration.
SLCR_UNLOCK_ADDR = 0xF8000008
SLCR_UNLOCK_KEY = 0xDF0D
SLCR_LOCK_ADDR = 0xF8000004
SLCR_LOCK_KEY = 0x767B

# Markers the clear script prints its readings behind, so the result is parsed
# rather than pattern-matched out of prose.
REBOOT_CLEAR_BEFORE_PREFIX = "BEFORE="
REBOOT_CLEAR_AFTER_PREFIX = "AFTER="
REBOOT_CLEAR_METHOD_PREFIX = "METHOD="


def _clear_reboot_status_script() -> str:
    """Python that clears SLCR REBOOT_STATUS and proves whether it worked.

    Two ways to clear it, because the manual does not settle which these bits
    are. Write the value back first, which clears them if they are write-one-
    to-clear and is a no-op if they are plain read/write; re-read; and only if
    they are still standing write zeros instead. Whichever worked is reported,
    along with the readings before and after, so the operator is never told
    "cleared" on the strength of an assumption.

    The bootloader scratch byte in 31:24 is preserved either way -- it is not
    a reset cause and something else owns it.
    """
    page = REBOOT_STATUS_PAGE_ADDR
    size = REBOOT_STATUS_PAGE_SIZE
    reg = REBOOT_STATUS_PAGE_OFFSET
    unlock = SLCR_UNLOCK_ADDR - page
    lock = SLCR_LOCK_ADDR - page
    causes = sum(1 << bit for bit, _n, _m, _u in REBOOT_STATUS_BITS)
    return (
        "import mmap, os, struct\n"
        "f = os.open('/dev/mem', os.O_RDWR | getattr(os, 'O_SYNC', 0))\n"
        f"m = mmap.mmap(f, {size}, mmap.MAP_SHARED, "
        "mmap.PROT_READ | mmap.PROT_WRITE, "
        f"offset={page})\n"
        "def rd(off):\n"
        "    return struct.unpack('<I', m[off:off + 4])[0]\n"
        "def wr(off, value):\n"
        "    m[off:off + 4] = struct.pack('<I', value)\n"
        f"before = rd({reg})\n"
        f"print('{REBOOT_CLEAR_BEFORE_PREFIX}0x%08x' % before)\n"
        f"wr({unlock}, {SLCR_UNLOCK_KEY})\n"
        "try:\n"
        f"    wr({reg}, before & {causes})\n"
        f"    after = rd({reg})\n"
        "    method = 'write-one-to-clear'\n"
        f"    if after & {causes}:\n"
        # `& 0xFFFFFFFF` because Python's ~ is signed and struct would refuse
        # the negative that falls out of it.
        f"        wr({reg}, before & ~{causes} & 0xFFFFFFFF)\n"
        f"        after = rd({reg})\n"
        "        method = 'write-zero'\n"
        f"    if after & {causes}:\n"
        "        method = 'none (the bits did not clear)'\n"
        "finally:\n"
        f"    wr({lock}, {SLCR_LOCK_KEY})\n"
        f"print('{REBOOT_CLEAR_AFTER_PREFIX}0x%08x' % after)\n"
        f"print('{REBOOT_CLEAR_METHOD_PREFIX}%s' % method)\n"
    )


def clear_reboot_status(
    device: Any, *, connection_factory: Callable[..., Any] | None = None
) -> dict[str, Any]:
    """Zero the recorded reset causes so the next incident stands alone.

    The bits accumulate and carry no timestamp, so a board that has been
    running for months reads as every cause it has ever seen. Clearing after
    reading is what makes the next reading mean "since I last looked".

    This is the only hardware write in the diagnostics feature, and it is
    deliberately an explicit operator action rather than something `collect`
    does: an automatic clear on every read would let a second collect erase a
    cause nobody had looked at yet.
    """
    script = _clear_reboot_status_script()
    # Piped to the interpreter on stdin rather than staged in a file. The
    # interpreter is what needs root -- it opens /dev/mem for writing -- and a
    # script root executes must never sit, however briefly, at a fixed path in
    # a world-writable directory: any other account on the board could swap it
    # between the write and the run, or simply pre-create it and make our own
    # write fail. Nothing touches the filesystem this way.
    #
    # The quotes around the escaped text are load-bearing -- without them the
    # remote shell reads the script's own newlines as command separators and
    # the interpreter is fed nothing, which reads back as "the board printed
    # no reading" rather than as the quoting mistake it is.
    #
    # The interpreter is resolved before the pipe so the script crosses once:
    # a `python3 || python` fallback around the pipeline would have to repeat
    # the whole text, the first run having consumed stdin.
    command = (
        "PY=$(command -v python3 || command -v python); "
        "printf %s '" + shell_single_quote(script) + "' | "
        + privileged(device, "$PY -")
    )
    try:
        with _open(device, connection_factory) as conn:
            exited, stdout, stderr = run_remote(
                conn,
                device,
                command,
                timeout=SECTION_TIMEOUT_S + 5.0,
                privileged_command=False,
            )
    except Exception as exc:  # noqa: BLE001 - reported, never raised at the API
        logger.debug("clearing the reset register failed", exc_info=True)
        return {"ok": False, "error": str(exc)}

    before = _prefixed_value(stdout, REBOOT_CLEAR_BEFORE_PREFIX)
    after = _prefixed_value(stdout, REBOOT_CLEAR_AFTER_PREFIX)
    method = _prefixed_text(stdout, REBOOT_CLEAR_METHOD_PREFIX)
    causes = sum(1 << bit for bit, _n, _m, _u in REBOOT_STATUS_BITS)
    cleared = after is not None and not (after & causes)
    if before is None or after is None:
        detail = (stderr or stdout or "").strip()[:300]
        return {
            "ok": False,
            "error": detail or f"the board printed no reading (exited {exited})",
        }
    return {
        "ok": cleared,
        "error": None if cleared else "the bits did not clear",
        "before": before,
        "after": after,
        "method": method,
        "before_description": describe_reboot_status(before),
    }


def _prefixed_value(output: str, prefix: str) -> int | None:
    text = _prefixed_text(output, prefix)
    if text is None:
        return None
    try:
        return int(text, 16) if text.lower().startswith("0x") else int(text, 10)
    except ValueError:
        return None


def _prefixed_text(output: str, prefix: str) -> str | None:
    for line in (output or "").splitlines():
        line = line.strip()
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return None


def enable_persistent_journal(
    device: Any, *, connection_factory: Callable[..., Any] | None = None
) -> dict[str, Any]:
    """Make the board's journal survive a reboot.

    Without this, a board that resets takes its own explanation with it: the
    default journal lives in a tmpfs, so `journalctl -b -1` has nothing to
    return and the crash that caused the reset is unrecoverable.

    Written with the same care as the rp-telemetry unit -- `tee` rather than a
    redirect (the redirect would be performed by the calling, unprivileged
    shell), `printf` of a single-quoted one-liner rather than a heredoc, a
    checksum read-back, and `sync` -- because a half-written journald config
    leaves journald refusing to start, which would cost the board its logging
    entirely.
    """
    try:
        with _open(device, connection_factory) as conn:

            def run(command: str, *, privileged_command: bool = True):
                return run_remote(
                    conn,
                    device,
                    command,
                    timeout=SECTION_TIMEOUT_S + 5.0,
                    privileged_command=privileged_command,
                )

            exited, _out, err = run("mkdir -p " + JOURNALD_DROPIN_DIR)
            if exited != 0:
                raise RuntimeError(
                    f"Could not create {JOURNALD_DROPIN_DIR}: {err.strip()[:300]}"
                )

            tee = privileged(device, "tee " + JOURNALD_DROPIN_PATH)
            exited, _out, err = run(
                "printf '%s' '"
                + shell_single_quote(JOURNALD_DROPIN)
                + "' | "
                + tee
                + " > /dev/null",
                privileged_command=False,
            )
            if exited != 0:
                raise RuntimeError(
                    f"Could not write {JOURNALD_DROPIN_PATH}: {err.strip()[:300]}"
                )
            run("sync")

            mismatch = _verify_remote_file(run, JOURNALD_DROPIN_PATH, JOURNALD_DROPIN)
            if mismatch is not None:
                raise RuntimeError(mismatch)

            exited, _out, err = run("mkdir -p " + JOURNAL_DIR)
            if exited != 0:
                raise RuntimeError(
                    f"Could not create {JOURNAL_DIR}: {err.strip()[:300]}"
                )

            # Before the restart, not after. journald fails this silently --
            # it falls back to runtime storage -- so a board that cannot hold a
            # journal would otherwise be reconfigured, restarted, and then
            # reported as mysteriously "still volatile". On an image whose
            # /var/log is a RAM disk this also puts real storage under the path
            # first, so the flush below has somewhere to land.
            bind_mounted = _require_usable_journal_filesystem(run, device)

            # systemd-journald re-reads its configuration only on restart.
            # Restarting it is safe: it is socket-activated, so messages
            # produced meanwhile are queued rather than lost.
            exited, _out, err = run("systemctl restart systemd-journald")
            if exited != 0:
                raise RuntimeError(
                    f"Could not restart systemd-journald: {err.strip()[:300]}"
                )
            # Move what is already in RAM onto the disk and drop the runtime
            # copy. Two reasons: the logs from this boot -- possibly including
            # whatever is being investigated right now -- are preserved instead
            # of discarded at the next reset, and journald's runtime directory
            # goes away, which is what the verification below reads.
            run("journalctl --flush")
            run("sync")

            # Ask journald which file it is now writing to. Checking that
            # JOURNAL_DIR exists would only confirm the mkdir above, and would
            # report success for a board whose Storage= is still being
            # overridden by another drop-in.
            #
            # Through `_bounded`, which wraps it in `sh -c`. The probe is a
            # compound `if ...; then ...; fi`, and `sudo -n if ...` is a syntax
            # error -- so on a non-root board the verification failed every
            # time and reported a board it had just configured correctly as a
            # failure. The section path was always safe because it is bounded;
            # this call was the one that was not.
            exited, out, _err = run(_bounded(_STORAGE_PROBE_SETTLED))
            if "STORAGE=PERSISTENT" not in out:
                if "STORAGE=TMPFS" in out:
                    state = "is a RAM disk"
                    hint = (
                        f"{JOURNAL_DIR} is on a tmpfs on this image, so journald "
                        "obeys the setting and still loses everything at reset. "
                        "The image has to give /var/log real storage first."
                    )
                elif "STORAGE=UNKNOWN" in out or exited != 0:
                    state = "could not be determined"
                    hint = (
                        "Check that /etc/machine-id is readable and that "
                        "systemd-journald came back up."
                    )
                else:
                    state = "is still volatile"
                    hint = (
                        "Check for another drop-in in {} overriding Storage=."
                    ).format(JOURNALD_DROPIN_DIR)
                raise RuntimeError(
                    "journald restarted but its storage "
                    f"{state} -- logs would still not survive a reboot. "
                    f"{hint}{_storage_detail(run)}"
                )
    except AuthenticationException as exc:
        raise RuntimeError(f"SSH authentication failed: {exc}") from exc
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator as a message
        raise RuntimeError(f"Could not enable persistent logging: {exc}") from exc

    return {
        "ok": True,
        "persistent_journal": True,
        # True when /var/log was a RAM disk and the journal is now bind-mounted
        # from real storage. Worth surfacing: that is a change to how the board
        # boots, not just a journald setting.
        "backing_mount": bind_mounted,
    }


def _filesystem_facts(run: Callable[..., Any], report: str) -> tuple[str, str, int | None] | None:
    """(fstype, mount point, free KB) for a path, or None when unreadable."""
    exited, out, _err = run(_bounded(report))
    if exited != 0 or "FSTYPE=" not in out:
        return None
    fields = dict(
        token.split("=", 1) for token in out.split() if token.count("=") >= 1
    )
    try:
        avail_kb: int | None = int(fields.get("AVAILKB", ""))
    except ValueError:
        avail_kb = None
    return fields.get("FSTYPE", ""), fields.get("MOUNT", ""), avail_kb


def _require_usable_journal_filesystem(
    run: Callable[..., Any], device: Any
) -> bool:
    """Make /var/log/journal able to hold a journal, or explain why it cannot.

    Returns True when a bind mount had to be set up to get there.

    Two ways the path is unusable, both on stock Red Pitaya images: /var/log is
    a RAM disk, so "persistent" logs die with the board anyway; or it is real
    but smaller than one journal file, in which case journald quietly falls
    back to runtime storage and the board looks exactly like one that was never
    configured.

    The first is repairable without touching the image: journald's path is
    hardcoded, but what is *mounted* at that path is ours to choose. The second
    is not -- nothing here can conjure free space.
    """
    facts = _filesystem_facts(run, _JOURNAL_FS_REPORT)
    if facts is None:
        # Undetermined is not a reason to refuse: the restart below is still
        # worth attempting, and the verification afterwards is the backstop.
        return False
    fstype, mount, avail_kb = facts

    if fstype in ("tmpfs", "ramfs"):
        _mount_journal_backing_store(run, device)
        return True
    if avail_kb is not None and avail_kb < JOURNAL_MIN_FREE_KB:
        raise RuntimeError(
            f"{mount or JOURNAL_DIR} has only {avail_kb // 1024} MB free, less "
            f"than the {JOURNAL_MIN_FREE_KB // 1024} MB a journal file needs. "
            "journald would silently keep logging to RAM. Free space there, or "
            "give /var/log a larger filesystem, then try again."
        )
    return False


def _mount_journal_backing_store(run: Callable[..., Any], device: Any) -> None:
    """Bind a directory on real storage over /var/log/journal.

    journald's path is hardcoded and /var/log is a RAM disk on these images, so
    the only way to keep a journal is to change what is mounted at that path.
    A systemd mount unit rather than an fstab line or a boot-time symlink: the
    flush that moves this boot's logs to disk runs *before*
    systemd-tmpfiles-setup, so a symlink recreated by tmpfiles would not exist
    yet and the whole boot would stay in RAM -- while a mount unit is pulled in
    and ordered ahead of the flush by its RequiresMountsFor.
    """
    exited, _out, err = run("mkdir -p " + JOURNAL_BACKING_DIR)
    if exited != 0:
        raise RuntimeError(
            f"Could not create {JOURNAL_BACKING_DIR}: {err.strip()[:300]}"
        )

    # The backing store has to be real storage itself, or this buys nothing.
    facts = _filesystem_facts(run, _BACKING_FS_REPORT)
    if facts is not None:
        fstype, mount, avail_kb = facts
        if fstype in ("tmpfs", "ramfs"):
            raise RuntimeError(
                f"{JOURNAL_DIR} is a RAM disk on this image, and so is "
                f"{mount or JOURNAL_BACKING_DIR} ({fstype}) -- there is nowhere "
                "on this board to put a journal that survives a reboot. The "
                "image has to give /var real storage first."
            )
        if avail_kb is not None and avail_kb < JOURNAL_MIN_FREE_KB:
            raise RuntimeError(
                f"{mount or JOURNAL_BACKING_DIR} has only {avail_kb // 1024} MB "
                f"free, less than the {JOURNAL_MIN_FREE_KB // 1024} MB a journal "
                "file needs."
            )

    tee = privileged(device, "tee " + JOURNAL_MOUNT_UNIT_PATH)
    exited, _out, err = run(
        "printf '%s' '"
        + shell_single_quote(JOURNAL_MOUNT_UNIT_TEXT)
        + "' | "
        + tee
        + " > /dev/null",
        privileged_command=False,
    )
    if exited != 0:
        raise RuntimeError(
            f"Could not write {JOURNAL_MOUNT_UNIT_PATH}: {err.strip()[:300]}"
        )
    run("sync")
    mismatch = _verify_remote_file(run, JOURNAL_MOUNT_UNIT_PATH, JOURNAL_MOUNT_UNIT_TEXT)
    if mismatch is not None:
        raise RuntimeError(mismatch)

    exited, _out, err = run("systemctl daemon-reload")
    if exited != 0:
        raise RuntimeError(f"Could not reload systemd: {err.strip()[:300]}")

    exited, _out, err = run("systemctl start " + JOURNAL_MOUNT_UNIT)
    if exited != 0:
        raise RuntimeError(
            f"Could not mount {JOURNAL_DIR} from {JOURNAL_BACKING_DIR}: "
            f"{err.strip()[:300]}"
        )

    # Confirm the mount took, rather than trusting a zero exit: a bind mount
    # onto a path journald is using can succeed and still leave the old
    # filesystem visible to anything holding it open.
    facts = _filesystem_facts(run, _JOURNAL_FS_REPORT)
    if facts is not None and facts[0] in ("tmpfs", "ramfs"):
        raise RuntimeError(
            f"{JOURNAL_MOUNT_UNIT} started but {JOURNAL_DIR} is still a RAM "
            f"disk ({facts[0]}). The journal would not survive a reboot."
        )


def _storage_detail(run: Callable[..., Any]) -> str:
    """What journald and the filesystem say, appended to a failure message.

    Best effort by construction: this runs only on a path that is already
    failing, so a detail command that itself fails must not replace the real
    error with its own.
    """
    try:
        _exited, out, _err = run(_bounded(_STORAGE_DETAIL))
    except Exception:  # noqa: BLE001 - diagnostics for an error we are reporting
        return ""
    out = (out or "").strip()
    if not out:
        return ""
    return "\n\nWhat the board reports:\n" + out[:800]


def _verify_remote_file(run: Callable[..., Any], path: str, expected: str) -> str | None:
    """None when the remote file holds exactly `expected`, else a reason.

    Catches the failure the rp-telemetry install already learned to catch on
    real hardware: a file that exists at the right size with its contents still
    in page cache, which comes back NUL-filled after a reset.
    """
    digest = hashlib.sha256(expected.encode("utf-8")).hexdigest()
    exited, out, _err = run(f"sha256sum {path}", privileged_command=False)
    if exited == 0:
        remote = out.split()
        if remote and remote[0] == digest:
            return None
        return f"{path} does not match what was written"
    # Minimal images may lack sha256sum; a size check still catches truncation.
    exited, out, _err = run(f"wc -c < {path}", privileged_command=False)
    if exited != 0:
        return f"Could not read back {path} to verify it."
    try:
        remote_size = int(out.strip())
    except ValueError:
        return f"Could not read back {path} to verify it."
    expected_size = len(expected.encode("utf-8"))
    if remote_size != expected_size:
        return f"{path} is truncated ({remote_size} of {expected_size} bytes)."
    return None
