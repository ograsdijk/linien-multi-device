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
_STORAGE_PROBE = (
    'MID=$(cat /etc/machine-id 2>/dev/null || true); '
    'if [ -z "$MID" ]; then echo STORAGE=UNKNOWN; '
    'elif [ -d "' + RUNTIME_JOURNAL_DIR + '/$MID" ]; then echo STORAGE=VOLATILE; '
    'elif [ -d "' + JOURNAL_DIR + '/$MID" ]; then echo STORAGE=PERSISTENT; '
    "else echo STORAGE=UNKNOWN; fi"
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
        "systemctl show " + LINIEN_UNIT + " -p ActiveState -p SubState -p Result "
        "-p ExecMainStatus -p ExecMainCode -p ExecMainStartTimestamp "
        "-p ExecMainExitTimestamp -p NRestarts",
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
    }


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
        if "STORAGE=VOLATILE" in output:
            return False
        # UNKNOWN, or the section did not run at all. Stay honest: reporting
        # False here would nag about a board we could not read, and True would
        # promise logs that may not survive.
        return None
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
            exited, out, _err = run(_bounded(_STORAGE_PROBE))
            if "STORAGE=PERSISTENT" not in out:
                state = (
                    "could not be determined"
                    if "STORAGE=UNKNOWN" in out or exited != 0
                    else "is still volatile"
                )
                raise RuntimeError(
                    "journald restarted but its storage "
                    f"{state} -- logs would still not survive a reboot. "
                    f"Check for another drop-in in {JOURNALD_DROPIN_DIR} "
                    "overriding Storage=."
                )
    except AuthenticationException as exc:
        raise RuntimeError(f"SSH authentication failed: {exc}") from exc
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator as a message
        raise RuntimeError(f"Could not enable persistent logging: {exc}") from exc

    return {"ok": True, "persistent_journal": True}


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
