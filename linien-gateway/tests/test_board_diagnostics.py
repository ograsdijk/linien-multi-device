import builtins
import subprocess
import struct
from types import SimpleNamespace

import pytest

from app import board_diagnostics as bd


class FakeResult:
    def __init__(self, exited=0, stdout="", stderr=""):
        self.exited = exited
        self.stdout = stdout
        self.stderr = stderr


class FakeConnection:
    """Matches commands by substring, in the order the rules are given."""

    def __init__(self, rules=None, default=None, raises=None):
        self.rules = rules or {}
        self.default = default if default is not None else FakeResult(stdout="ok")
        self.raises = raises or {}
        self.commands = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def run(self, command, **_kwargs):
        self.commands.append(command)
        for needle, exc in self.raises.items():
            if needle in command:
                raise exc
        for needle, result in self.rules.items():
            if needle in command:
                # A list answers successive matches in turn, holding the last
                # value once exhausted -- for probes whose answer is supposed to
                # change because of something that happened in between.
                if isinstance(result, list):
                    return result.pop(0) if len(result) > 1 else result[0]
                return result
        return self.default


class Device:
    key = "dev-1"
    host = "10.0.0.2"
    username = "root"
    password = "secret"


def factory_for(conn):
    return lambda *_args, **_kwargs: conn


# --- collection ----------------------------------------------------------


def test_every_section_is_collected_over_one_connection():
    conn = FakeConnection()

    bundle = bd.collect_diagnostics(Device(), connection_factory=factory_for(conn))

    assert bundle["ok"] is True
    assert len(bundle["sections"]) == len(bd._SECTIONS)
    assert len(conn.commands) == len(bd._SECTIONS)
    assert {section["name"] for section in bundle["sections"]} == {
        name for name, _title, _command, _root in bd._SECTIONS
    }


def test_every_command_is_time_bounded():
    """A board that has stopped answering must not hold the worker twelve times.

    Same defence `diagnosis.py` applies to a possible AXI stall.
    """
    conn = FakeConnection()

    bd.collect_diagnostics(Device(), connection_factory=factory_for(conn))

    assert all(command.startswith("timeout ") for command in conn.commands)


def test_one_missing_tool_does_not_lose_the_other_sections():
    """These images vary; a section that cannot run is a finding, not a failure."""
    conn = FakeConnection(
        rules={"pstore": FakeResult(exited=1, stderr="cat: not found")},
        default=FakeResult(stdout="fine"),
    )

    bundle = bd.collect_diagnostics(Device(), connection_factory=factory_for(conn))

    sections = {section["name"]: section for section in bundle["sections"]}
    assert bundle["ok"] is True
    assert sections["pstore"]["error"] == "cat: not found"
    assert sections["kernel"]["output"] == "fine"


def test_a_section_that_raises_is_confined_to_that_section():
    conn = FakeConnection(raises={"dmesg | tail": RuntimeError("channel closed")})

    bundle = bd.collect_diagnostics(Device(), connection_factory=factory_for(conn))

    sections = {section["name"]: section for section in bundle["sections"]}
    assert sections["kernel"]["error"] == "channel closed"
    assert sections["identity"]["error"] is None


def test_output_is_kept_alongside_a_non_zero_exit():
    """Partial output from a failing command is usually the interesting part."""
    conn = FakeConnection(
        rules={"list-boots": FakeResult(exited=1, stdout="partial", stderr="boom")}
    )

    bundle = bd.collect_diagnostics(Device(), connection_factory=factory_for(conn))

    section = next(s for s in bundle["sections"] if s["name"] == "boots")
    assert section["output"] == "partial"
    assert section["error"] == "boom"


def test_a_server_started_outside_systemd_is_still_visible():
    """The gateway's own autostart runs `linien-server start` over SSH.

    systemd then knows nothing about the process, so every journal and unit
    section comes back empty and a board where the server died looks exactly
    like one where it was never running. The process list and the log file are
    what distinguish them.
    """
    names = {name for name, _title, _command, _root in bd._SECTIONS}
    assert {"linien_process", "linien_logfile"} <= names

    commands = {name: command for name, _t, command, _r in bd._SECTIONS}
    # `[l]inien` so the grep does not report itself.
    assert "[l]inien" in commands["linien_process"]
    # The log lives on the root filesystem, not in the journal -- which is why
    # it survives on an image that cannot keep a journal at all.
    assert bd.LINIEN_LOG_SUBDIR in commands["linien_logfile"]
    assert "linien.log" in commands["linien_logfile"]


def test_the_log_file_section_shows_its_timestamp():
    """The file's mtime is the time of death when nothing else recorded one."""
    command = next(c for n, _t, c, _r in bd._SECTIONS if n == "linien_logfile")
    assert "ls -la" in command
    assert "tail -n" in command


def test_the_rotated_log_is_collected_too():
    """linien logs through a RotatingFileHandler.

    The run that died is routinely one rotation back, with the live file
    holding nothing but the restart that followed it -- which is exactly what
    a board looked like when this was written.
    """
    command = next(c for n, _t, c, _r in bd._SECTIONS if n == "linien_logfile")
    assert "linien.log.1" in command


def test_a_dead_connection_is_reported_not_raised():
    def factory(*_args, **_kwargs):
        raise OSError("no route to host")

    bundle = bd.collect_diagnostics(Device(), connection_factory=factory)

    assert bundle["ok"] is False
    assert "no route to host" in bundle["error"]
    assert bundle["sections"] == []


def test_a_huge_section_is_truncated_from_the_front():
    """A wedged board emits megabytes of repeats; the tail is the useful end."""
    conn = FakeConnection(
        rules={"dmesg | tail": FakeResult(stdout="x" * (bd.SECTION_MAX_CHARS + 5_000))}
    )

    bundle = bd.collect_diagnostics(Device(), connection_factory=factory_for(conn))

    section = next(s for s in bundle["sections"] if s["name"] == "kernel")
    assert section["output"].startswith("[...truncated...]")
    assert len(section["output"]) <= bd.SECTION_MAX_CHARS + 32


def test_only_the_sections_that_need_root_get_sudo():
    """A board whose SSH user has no passwordless sudo must still yield the
    half of the bundle that reads world-readable files. Putting `sudo -n` in
    front of everything turned that board into an empty bundle -- and with no
    `journald` section, the Enable persistent logs button never appeared
    either, so the operator got no explanation at all.
    """
    class Pi(Device):
        username = "pi"

    conn = FakeConnection()

    bd.collect_diagnostics(Pi(), connection_factory=factory_for(conn))

    sudoed = [c for c in conn.commands if c.startswith("sudo -n ")]
    plain = [c for c in conn.commands if not c.startswith("sudo -n ")]
    assert any("dmesg" in c for c in sudoed)
    assert any("journalctl -u linien-server" in c for c in sudoed)
    assert any("/proc/uptime" in c for c in plain)
    assert any("free -m" in c for c in plain)


def test_root_needs_no_sudo_at_all():
    conn = FakeConnection()

    bd.collect_diagnostics(Device(), connection_factory=factory_for(conn))

    assert all(command.startswith("timeout ") for command in conn.commands)


# --- persistence detection ----------------------------------------------


def test_a_board_whose_journald_writes_to_disk_is_persistent():
    conn = FakeConnection(
        rules={"STORAGE=PERSISTENT": FakeResult(stdout="STORAGE=PERSISTENT")}
    )

    bundle = bd.collect_diagnostics(Device(), connection_factory=factory_for(conn))

    assert bundle["persistent_journal"] is True


def test_a_board_whose_journald_is_still_in_tmpfs_is_not():
    """The state every stock Red Pitaya image is in, and the reason the
    enable action exists."""
    conn = FakeConnection(
        rules={"STORAGE=PERSISTENT": FakeResult(stdout="STORAGE=VOLATILE")}
    )

    bundle = bd.collect_diagnostics(Device(), connection_factory=factory_for(conn))

    assert bundle["persistent_journal"] is False


def test_leftover_journal_files_do_not_pass_for_persistent():
    """A board that was persistent once and is volatile now still has files
    under /var/log/journal. Grepping `journalctl --header` matched those
    leftovers and reported the board as safe, so the Enable button was never
    offered and the next crash again left nothing behind.

    The probe decides on journald's *active* per-machine directory, and checks
    the runtime one first precisely because both can exist at once.
    """
    conn = FakeConnection(
        rules={
            "STORAGE=PERSISTENT": FakeResult(
                stdout=(
                    "STORAGE=VOLATILE\n"
                    "File path: /run/log/journal/x/system.journal\n"
                    "/var/log/journal\n"
                )
            )
        }
    )

    bundle = bd.collect_diagnostics(Device(), connection_factory=factory_for(conn))

    assert bundle["persistent_journal"] is False


def test_the_probe_checks_the_runtime_directory_before_the_persistent_one():
    probe = bd._STORAGE_PROBE
    assert probe.index(bd.RUNTIME_JOURNAL_DIR) < probe.index(bd.JOURNAL_DIR + '/$MID')
    # And it is decided by journald's own per-machine directory, not by a grep
    # of every journal header it can read.
    assert "machine-id" in probe
    assert "journalctl" not in probe


def test_a_journal_directory_on_a_ram_disk_is_not_persistence():
    """Some images mount /var/log on a tmpfs.

    journald then obeys Storage=persistent, creates its per-machine directory
    there, and still loses every line at the next reset -- so a directory check
    alone would paint the green badge over a board that keeps nothing.
    """
    conn = FakeConnection(
        rules={"STORAGE=PERSISTENT": FakeResult(stdout="STORAGE=TMPFS")}
    )

    bundle = bd.collect_diagnostics(Device(), connection_factory=factory_for(conn))

    assert bundle["persistent_journal"] is False


def test_persistence_is_unknown_when_journald_could_not_be_asked():
    conn = FakeConnection(
        rules={"STORAGE=PERSISTENT": FakeResult(stdout="STORAGE=UNKNOWN")}
    )

    bundle = bd.collect_diagnostics(Device(), connection_factory=factory_for(conn))

    assert bundle["persistent_journal"] is None


# --- reset cause ---------------------------------------------------------
#
# SLCR REBOOT_STATUS is the one reading that separates "the power dropped"
# from "the board reset itself", which the kernel log cannot answer: a board
# that lost power wrote nothing before it went.


def _reboot_bundle(stdout):
    conn = FakeConnection(rules={bd.REBOOT_STATUS_ADDR: FakeResult(stdout=stdout)})
    return bd.collect_diagnostics(Device(), connection_factory=factory_for(conn))


def test_an_empty_reset_register_is_not_read_as_lost_power():
    """It used to be. A field board reading 0x00410000 -- a watchdog timeout
    and a power-on standing together -- shows the bits accumulate, so a power
    loss leaves bit 22 SET and an empty register means only that something
    cleared it."""
    bundle = _reboot_bundle("REBOOT_STATUS=0x00000000")

    status = bundle["reboot_status"]
    assert status["value"] == 0
    assert status["causes"] == []
    assert status["power_on"] is False
    assert "lost power" not in status["description"]
    assert "nothing has been recorded" in status["description"]


def test_a_power_on_is_the_bit_being_set_not_the_register_being_empty():
    bundle = _reboot_bundle("REBOOT_STATUS=0x00400000")

    status = bundle["reboot_status"]
    assert status["power_on"] is True
    assert status["watchdog"] is False


def test_a_watchdog_and_a_power_on_can_stand_together():
    """The field reading that settled how the register behaves."""
    bundle = _reboot_bundle("REBOOT_STATUS=0x00410000")

    status = bundle["reboot_status"]
    assert status["causes"] == ["SWDT_RST", "POR"]
    assert status["watchdog"] is True
    assert status["power_on"] is True
    assert status["software_reboot"] is False
    assert "accumulate" in status["description"]


def test_a_software_reboot_is_reported_as_one():
    bundle = _reboot_bundle("REBOOT_STATUS=0x00080000")

    status = bundle["reboot_status"]
    assert status["software_reboot"] is True
    assert status["watchdog"] is False
    assert "SLC_RST" in status["description"]


def test_a_watchdog_reset_names_the_watchdog():
    assert "system watchdog" in bd.describe_reboot_status(1 << 16)
    assert "CPU0 watchdog" in bd.describe_reboot_status(1 << 17)
    assert "CPU1 watchdog" in bd.describe_reboot_status(1 << 18)


def test_the_unverified_bits_say_so():
    """16-19 are documented and 22 was confirmed in the field; 20 and 21 still
    follow only the TRM's ordering, so a decode resting on them must not read
    as fact."""
    assert "unverified" in bd.describe_reboot_status(1 << 20)
    assert "unverified" in bd.describe_reboot_status(1 << 21)
    assert "unverified" not in bd.describe_reboot_status(1 << 19)
    assert "unverified" not in bd.describe_reboot_status(1 << 22)


def test_the_bootloader_scratch_byte_is_not_a_reset_cause():
    """Bits 31:24 are scratch space for the BootROM and u-boot."""
    described = bd.describe_reboot_status(0xF0000000)

    assert "boot state 0xf0" in described
    assert "no reset-cause bit set" in described


def test_a_board_with_no_way_to_read_the_register_reports_nothing():
    bundle = _reboot_bundle("no way to read 0xF8000258")

    assert bundle["reboot_status"] is None
    # ...and the section is still in the bundle, so the operator sees why.
    section = next(s for s in bundle["sections"] if s["name"] == "reboot_status")
    assert "no way to read" in section["output"]


def test_a_garbled_register_value_is_not_guessed_at():
    assert bd.parse_reboot_status("REBOOT_STATUS=junk") is None
    assert bd.parse_reboot_status("REBOOT_STATUS=") is None
    assert bd.parse_reboot_status("") is None
    assert bd.describe_reboot_status(None) is None


def test_a_decimal_register_value_is_accepted_too():
    """`monitor` prints hex, busybox devmem can print either."""
    assert bd.parse_reboot_status("REBOOT_STATUS=524288") == 1 << 19


def test_every_reader_is_tried_rather_than_only_the_first_one_present():
    """The chain was an if/elif, so a `devmem` that exists and fails silently
    ended it with an empty answer and nothing else was attempted."""
    command = next(s for s in bd._SECTIONS if s[0] == "reboot_status")[2]

    # Every reader after the first is guarded on the value still being empty,
    # rather than a single if/elif whose first present tool decides the
    # outcome. Three `if`s plus the python loop's own `[ -z "$V" ] || break`.
    assert "elif" not in command
    assert command.count('[ -z "$V" ]') == 4


def test_the_register_is_read_with_python_on_a_board_with_no_tools():
    """A field board had no devmem, no devmem2, no monitor and no busybox at
    all. python3 is the one reader a board running Linien must have: the
    server is itself a Python process."""
    command = next(s for s in bd._SECTIONS if s[0] == "reboot_status")[2]

    assert "python3" in command
    assert "mmap.mmap" in command
    assert "for PY in python3 python" in command


def test_the_register_is_mapped_rather_than_read():
    """`read()` on /dev/mem copies from `__va(phys)`, valid only for RAM. SLCR
    is IO space, so a read of it fails with EFAULT ("Bad address") on any
    kernel -- a field board returned exactly that. Only mmap reaches it, which
    is why every devmem tool mmaps."""
    command = next(s for s in bd._SECTIONS if s[0] == "reboot_status")[2]

    assert "dd if=/dev/mem" not in command
    assert "mmap.MAP_SHARED" in command


def test_the_mapped_page_and_offset_land_on_the_register():
    """mmap takes a page-aligned offset, so the address is split in two. The
    halves are derived from it, and this is the check that they still add up."""
    assert bd.REBOOT_STATUS_PAGE_ADDR % bd.REBOOT_STATUS_PAGE_SIZE == 0
    assert (
        bd.REBOOT_STATUS_PAGE_ADDR + bd.REBOOT_STATUS_PAGE_OFFSET
        == int(bd.REBOOT_STATUS_ADDR, 16)
    )
    assert 0 <= bd.REBOOT_STATUS_PAGE_OFFSET < bd.REBOOT_STATUS_PAGE_SIZE


def test_an_unreadable_register_says_what_was_tried():
    """"no way to read 0xF8000258" alone sent a whole round of diagnosis
    looking for a gateway bug rather than a missing tool on the board."""
    bundle = _reboot_bundle(
        "no way to read 0xF8000258 "
        "(tried devmem, devmem2, monitor, busybox devmem, python mmap)"
    )

    assert bundle["reboot_status"] is None
    section = next(s for s in bundle["sections"] if s["name"] == "reboot_status")
    for tool in ("devmem", "devmem2", "monitor", "python mmap"):
        assert tool in section["output"]


def test_reading_the_register_never_touches_the_fpga():
    """The PL-backed XADC hangs the AXI bus when the Linien bitstream is
    loaded; the same caution applies to anything else read over /dev/mem."""
    section = next(s for s in bd._SECTIONS if s[0] == "reboot_status")
    command = section[2]

    import re

    # Every address the command dereferences must lie in the SLCR page. The
    # mmap reader adds the page base to the register address, so an exact
    # match against the one literal no longer holds -- but "inside the SLCR
    # page" is the invariant that actually matters, and it also covers the
    # decimal forms mmap needs.
    slcr = bd.REBOOT_STATUS_PAGE_ADDR
    hex_addresses = {
        int(match, 16) for match in re.findall(r"0x[0-9A-Fa-f]{6,}", command)
    }
    decimal_addresses = {
        int(match)
        for match in re.findall(r"(?<![\w.])\d{7,}(?![\w.])", command)
    }
    for address in hex_addresses | decimal_addresses:
        assert slcr <= address < slcr + bd.REBOOT_STATUS_PAGE_SIZE, hex(address)
    # ...and the SLCR page is nowhere near the FPGA's window on this SoC.
    assert not (0x40000000 <= slcr < 0xC0000000)


# --- clearing the reset register -----------------------------------------
#
# The bits accumulate and carry no timestamp, so without a clear a board that
# has run for months reads as every cause it has ever seen. A field board came
# back 0x00410000 -- a watchdog timeout and a power-on standing together --
# which is what established the accumulation in the first place.


class FakeSlcrPage:
    """A stand-in for the mapped SLCR page.

    Models the write protection, and can behave as either write-one-to-clear
    or plain read/write, because the manual does not settle which these bits
    are and the script has to work on both.
    """

    def __init__(self, initial, write_one_to_clear):
        self.buf = bytearray(bd.REBOOT_STATUS_PAGE_SIZE)
        self.write_one_to_clear = write_one_to_clear
        self.unlocked = False
        self.wrote_while_locked = False
        reg = bd.REBOOT_STATUS_PAGE_OFFSET
        self.buf[reg : reg + 4] = struct.pack("<I", initial)

    def __setitem__(self, where, data):
        offset = where.start
        value = struct.unpack("<I", data)[0]
        if offset == bd.SLCR_UNLOCK_ADDR - bd.REBOOT_STATUS_PAGE_ADDR:
            self.unlocked = value == bd.SLCR_UNLOCK_KEY
            return
        if offset == bd.SLCR_LOCK_ADDR - bd.REBOOT_STATUS_PAGE_ADDR:
            self.unlocked = False
            return
        if not self.unlocked:
            self.wrote_while_locked = True
        if self.write_one_to_clear:
            current = struct.unpack("<I", bytes(self.buf[offset : offset + 4]))[0]
            value = current & ~value & 0xFFFFFFFF
        self.buf[offset : offset + 4] = struct.pack("<I", value)

    def __getitem__(self, where):
        return bytes(self.buf[where.start : where.stop])


def _run_clear_script(page):
    """Execute the generated script against `page`, returning what it printed.

    The fakes are supplied through `__import__` rather than as globals: the
    script imports mmap and os itself, which rebinds anything put in the
    globals dict under those names.
    """
    printed = []
    fake_mmap = SimpleNamespace(
        mmap=lambda *a, **k: page, MAP_SHARED=1, PROT_READ=1, PROT_WRITE=2
    )
    fake_os = SimpleNamespace(open=lambda *a, **k: 3, O_RDWR=2, O_SYNC=0)
    fakes = {"mmap": fake_mmap, "os": fake_os}

    def fake_import(name, *args, **kwargs):
        if name in fakes:
            return fakes[name]
        return builtins.__import__(name, *args, **kwargs)

    exec(  # noqa: S102 - running the very text we ship is the point
        compile(bd._clear_reboot_status_script(), "<clear>", "exec"),
        {
            "__builtins__": {**vars(builtins), "__import__": fake_import},
            "print": lambda *a: printed.append(" ".join(str(x) for x in a)),
        },
    )
    return printed


@pytest.mark.parametrize("write_one_to_clear", [True, False])
def test_the_clear_works_whichever_way_the_bits_behave(write_one_to_clear):
    """UG585 does not say whether 22:16 are write-one-to-clear or plain
    read/write, so the script tries the first and falls back to the second."""
    page = FakeSlcrPage(0x00410000, write_one_to_clear)

    printed = _run_clear_script(page)

    assert "BEFORE=0x00410000" in printed
    assert "AFTER=0x00000000" in printed
    assert page.wrote_while_locked is False


def test_the_clear_says_which_way_worked():
    """Reported rather than assumed: the operator is never told "cleared" on
    the strength of a guess about the silicon."""
    w1c = _run_clear_script(FakeSlcrPage(0x00410000, True))
    plain = _run_clear_script(FakeSlcrPage(0x00410000, False))

    assert "METHOD=write-one-to-clear" in w1c
    assert "METHOD=write-zero" in plain


def test_the_clear_leaves_the_bootloader_scratch_byte_alone():
    """Bits 31:24 are the BootROM's and u-boot's, not a reset cause."""
    page = FakeSlcrPage(0xA5410000, False)

    printed = _run_clear_script(page)

    assert "AFTER=0xa5000000" in printed


def test_the_clear_relocks_slcr_even_when_the_write_fails():
    """Leaving SLCR unlocked would outlive the diagnostic and is the one
    lasting harm this action could do."""
    page = FakeSlcrPage(0x00410000, False)
    original = FakeSlcrPage.__setitem__

    def explode(self, where, data):
        if where.start == bd.REBOOT_STATUS_PAGE_OFFSET:
            raise OSError("bus error")
        original(self, where, data)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(FakeSlcrPage, "__setitem__", explode)
        with pytest.raises(OSError):
            _run_clear_script(page)

    assert page.unlocked is False


def test_the_script_reaches_the_board_intact(tmp_path):
    """The script is multi-line, so the shell only sees one command if the
    escaped text is quoted. Unquoted, the board read the script's own newlines
    as command separators: nothing was written, nothing was cleared, and the
    causes came back unchanged on the next collect."""
    conn = FakeConnection(
        rules={"/dev/mem": FakeResult(stdout="BEFORE=0x1\nAFTER=0x0\nMETHOD=x\n")}
    )

    bd.clear_reboot_status(Device(), connection_factory=factory_for(conn))

    # Run the write half of the real command through a real shell, against a
    # scratch path, and compare what lands with what was meant to.
    landed = tmp_path / "clear.py"
    command = conn.commands[0].replace("/tmp/linien-clear-reset-cause.py", str(landed))
    subprocess.run(["sh", "-c", command.split(" && ", 1)[0]], check=True)

    assert landed.read_text() == bd._clear_reboot_status_script()


def test_the_interpreter_is_the_part_that_runs_as_root():
    """It is the process opening /dev/mem for writing. `sudo -n` in front of
    the compound would privilege the `printf` and nothing else."""

    class Sudoer(Device):
        username = "pi"

    conn = FakeConnection(
        rules={"/dev/mem": FakeResult(stdout="BEFORE=0x1\nAFTER=0x0\nMETHOD=x\n")}
    )

    bd.clear_reboot_status(Sudoer(), connection_factory=factory_for(conn))

    command = conn.commands[0]
    assert "sudo -n python3 /tmp/linien-clear-reset-cause.py" in command
    assert not command.startswith("sudo")


def test_a_board_that_prints_no_reading_is_not_reported_as_cleared():
    conn = FakeConnection(rules={"/dev/mem": FakeResult(stdout="", stderr="no python")})

    result = bd.clear_reboot_status(Device(), connection_factory=factory_for(conn))

    assert result["ok"] is False
    assert "no python" in result["error"]


def test_a_successful_clear_reports_what_was_there_before():
    """The reading is destroyed by the action, so the action has to carry it."""
    conn = FakeConnection(
        rules={
            "/dev/mem": FakeResult(
                stdout="BEFORE=0x00410000\nAFTER=0x00000000\nMETHOD=write-zero\n"
            )
        }
    )

    result = bd.clear_reboot_status(Device(), connection_factory=factory_for(conn))

    assert result["ok"] is True
    assert result["before"] == 0x00410000
    assert result["after"] == 0
    assert result["method"] == "write-zero"
    assert "SWDT_RST" in result["before_description"]


def test_bits_that_refuse_to_clear_are_not_reported_as_success():
    conn = FakeConnection(
        rules={
            "/dev/mem": FakeResult(
                stdout="BEFORE=0x00410000\nAFTER=0x00410000\nMETHOD=none\n"
            )
        }
    )

    result = bd.clear_reboot_status(Device(), connection_factory=factory_for(conn))

    assert result["ok"] is False
    assert "did not clear" in result["error"]


# --- enabling persistence ------------------------------------------------


def _digest_rule(path, text):
    import hashlib

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256sum {path}", FakeResult(stdout=f"{digest}  {path}")


def _enable_conn(**overrides):
    # Keyed per file: the drop-in and the mount unit are both written and both
    # read back, and one digest cannot stand for both.
    dropin_key, dropin_result = _digest_rule(
        bd.JOURNALD_DROPIN_PATH, bd.JOURNALD_DROPIN
    )
    mount_key, mount_result = _digest_rule(
        bd.JOURNAL_MOUNT_UNIT_PATH, bd.JOURNAL_MOUNT_UNIT_TEXT
    )
    # Order matters, because rules are matched by substring in insertion order.
    #
    # The storage probe goes first: it embeds the same `df -Pk /var/log/journal`
    # that the filesystem report uses, so a test that stubs the filesystem would
    # otherwise answer the storage probe too. Only the storage probe contains
    # the literal STORAGE=PERSISTENT, so keying on that separates them.
    #
    # Then the remaining overrides, so a broad one like "sha256sum" is seen
    # before the per-file keys it stands in for. Then the defaults.
    storage_key = "STORAGE=PERSISTENT"
    rules = {
        storage_key: overrides.pop(storage_key, FakeResult(stdout=storage_key))
    }
    rules.update(overrides)
    for key, result in ((dropin_key, dropin_result), (mount_key, mount_result)):
        rules.setdefault(key, result)
    return FakeConnection(rules=rules)


def test_enabling_persistence_writes_verifies_and_restarts():
    conn = _enable_conn()

    result = bd.enable_persistent_journal(Device(), connection_factory=factory_for(conn))

    assert result == {
        "ok": True,
        "persistent_journal": True,
        "backing_mount": False,
    }
    joined = "\n".join(conn.commands)
    assert f"mkdir -p {bd.JOURNALD_DROPIN_DIR}" in joined
    # `tee`, not a redirect: the redirect would be performed by the calling,
    # unprivileged shell.
    assert f"tee {bd.JOURNALD_DROPIN_PATH}" in joined
    assert "sha256sum" in joined
    assert "systemctl restart systemd-journald" in joined
    # Flushed, so the logs already in RAM -- possibly the ones being chased --
    # move to disk instead of being lost at the next reset, and journald's
    # runtime directory goes away so the verification reads a settled state.
    assert "journalctl --flush" in joined
    assert "sync" in joined


def test_the_written_config_is_the_capped_one():
    """Uncapped journald on an SD card is a wear and free-space problem."""
    conn = _enable_conn()

    bd.enable_persistent_journal(Device(), connection_factory=factory_for(conn))

    written = next(c for c in conn.commands if "tee" in c)
    assert "Storage=persistent" in written
    assert "SystemMaxUse=32M" in written
    # No stray carriage returns: a CR makes every journald value invalid.
    assert "\\r" not in written


def test_a_config_that_did_not_land_intact_is_an_error():
    """The failure the rp-telemetry install learned to catch on real hardware."""
    conn = _enable_conn(**{"sha256sum": FakeResult(stdout="0000  path")})

    with pytest.raises(RuntimeError, match="does not match"):
        bd.enable_persistent_journal(Device(), connection_factory=factory_for(conn))


def test_a_board_without_sha256sum_falls_back_to_a_size_check():
    size = len(bd.JOURNALD_DROPIN.encode("utf-8"))
    conn = _enable_conn(
        **{
            "sha256sum": FakeResult(exited=127, stderr="not found"),
            "wc -c": FakeResult(stdout=str(size)),
        }
    )

    result = bd.enable_persistent_journal(Device(), connection_factory=factory_for(conn))

    assert result["ok"] is True


def test_a_truncated_config_is_caught_by_the_size_check():
    conn = _enable_conn(
        **{
            "sha256sum": FakeResult(exited=127, stderr="not found"),
            "wc -c": FakeResult(stdout="3"),
        }
    )

    with pytest.raises(RuntimeError, match="truncated"):
        bd.enable_persistent_journal(Device(), connection_factory=factory_for(conn))


def test_a_journald_that_would_not_restart_is_reported():
    conn = _enable_conn(
        **{"systemctl restart": FakeResult(exited=1, stderr="job failed")}
    )

    with pytest.raises(RuntimeError, match="journald"):
        bd.enable_persistent_journal(Device(), connection_factory=factory_for(conn))


def test_journald_still_volatile_afterwards_is_not_reported_as_success():
    """The verification must not just confirm our own mkdir.

    Claiming success here would leave the next crash unexplained again, and
    would paint the green "logs survive a reboot" badge over a board that will
    lose them.
    """
    conn = _enable_conn(**{"STORAGE=PERSISTENT": FakeResult(stdout="STORAGE=VOLATILE")})

    with pytest.raises(RuntimeError, match="still volatile"):
        bd.enable_persistent_journal(Device(), connection_factory=factory_for(conn))


def test_the_verification_waits_for_the_flush_to_finish():
    """`journalctl --flush` returns before the flush is done on older systemd.

    Deciding on the first look reported boards that had just been configured
    correctly as failures: the runtime directory was simply still there a
    moment later. The retry happens on the board, in one command, rather than
    as a second SSH round trip.
    """
    conn = _enable_conn()

    bd.enable_persistent_journal(Device(), connection_factory=factory_for(conn))

    probe = next(c for c in conn.commands if "STORAGE=PERSISTENT" in c)
    assert "sleep 1" in probe
    assert "while" in probe
    # And it stops at the first non-volatile answer rather than always sleeping
    # the full budget.
    assert "break" in probe


def test_a_journal_directory_on_a_ram_disk_is_not_reported_as_success():
    conn = _enable_conn(**{"STORAGE=PERSISTENT": FakeResult(stdout="STORAGE=TMPFS")})

    with pytest.raises(RuntimeError, match="RAM disk"):
        bd.enable_persistent_journal(Device(), connection_factory=factory_for(conn))


def test_a_failed_verification_reports_what_the_board_said():
    """"Still volatile" on its own leaves the operator nothing to act on."""
    conn = _enable_conn(
        **{
            "STORAGE=PERSISTENT": FakeResult(stdout="STORAGE=VOLATILE"),
            "ActiveState": FakeResult(stdout="Storage=volatile\nActiveState=active"),
        }
    )

    with pytest.raises(RuntimeError) as excinfo:
        bd.enable_persistent_journal(Device(), connection_factory=factory_for(conn))

    assert "Storage=volatile" in str(excinfo.value)


def test_a_detail_command_that_fails_does_not_replace_the_real_error():
    conn = _enable_conn(**{"STORAGE=PERSISTENT": FakeResult(stdout="STORAGE=VOLATILE")})
    conn.raises["ActiveState"] = RuntimeError("channel closed")

    with pytest.raises(RuntimeError, match="still volatile"):
        bd.enable_persistent_journal(Device(), connection_factory=factory_for(conn))


def _tmpfs(mount="/var/log", kb=5120):
    return FakeResult(stdout=f"FSTYPE=tmpfs MOUNT={mount} AVAILKB={kb}")


def _real_fs(mount="/", kb=2_000_000):
    return FakeResult(stdout=f"FSTYPE=ext4 MOUNT={mount} AVAILKB={kb}")


def _journal_probe():
    return "df -Pk " + bd.JOURNAL_DIR


def _backing_probe():
    return "df -Pk " + bd.JOURNAL_BACKING_DIR


def test_a_ram_disk_on_var_log_is_repaired_with_a_bind_mount():
    """The stock Red Pitaya image mounts /var/log as a 5 MB tmpfs.

    journald's path is hardcoded, but what is mounted at that path is ours to
    choose -- so rather than refusing, put real storage under it.
    """
    conn = _enable_conn(
        **{
            _backing_probe(): _real_fs(),
            # tmpfs before the mount, real storage after it.
            _journal_probe(): [_tmpfs(), _real_fs(mount=bd.JOURNAL_DIR)],
        }
    )

    result = bd.enable_persistent_journal(Device(), connection_factory=factory_for(conn))

    assert result["backing_mount"] is True
    joined = "\n".join(conn.commands)
    assert f"mkdir -p {bd.JOURNAL_BACKING_DIR}" in joined
    assert f"tee {bd.JOURNAL_MOUNT_UNIT_PATH}" in joined
    assert "systemctl daemon-reload" in joined
    assert f"systemctl start {bd.JOURNAL_MOUNT_UNIT}" in joined
    # And the mount is established before journald is restarted, so the flush
    # has real storage to land on.
    assert joined.index("systemctl start " + bd.JOURNAL_MOUNT_UNIT) < joined.index(
        "systemctl restart systemd-journald"
    )


def test_a_board_with_nowhere_to_put_a_journal_is_refused():
    """Both the path and its backing store on RAM disks.

    Nothing this action does can repair that, so it must not pretend to try --
    and it must not restart journald to find out.
    """
    conn = _enable_conn(
        **{
            _journal_probe(): _tmpfs(),
            _backing_probe(): _tmpfs(mount="/var"),
        }
    )

    with pytest.raises(RuntimeError, match="nowhere"):
        bd.enable_persistent_journal(Device(), connection_factory=factory_for(conn))

    assert not any("systemctl restart systemd-journald" in c for c in conn.commands)


def test_a_bind_mount_that_did_not_take_is_not_reported_as_success():
    """`mount` can exit zero and leave the old filesystem visible."""
    conn = _enable_conn(
        **{
            _backing_probe(): _real_fs(),
            _journal_probe(): _tmpfs(),  # still a RAM disk afterwards
        }
    )

    with pytest.raises(RuntimeError, match="still a RAM disk"):
        bd.enable_persistent_journal(Device(), connection_factory=factory_for(conn))


def test_the_mount_unit_is_named_for_its_mount_point():
    """systemd derives the name from the path; any other name is never used."""
    assert bd.JOURNAL_MOUNT_UNIT == "var-log-journal.mount"
    assert bd.JOURNAL_DIR == "/var/log/journal"


def test_the_mount_unit_is_not_wired_into_local_fs_target():
    """A failed mount must not drop a headless board into emergency mode.

    systemd-journal-flush.service carries RequiresMountsFor=/var/log/journal,
    which pulls the unit in and orders it ahead of the flush on its own. Adding
    WantedBy=local-fs.target would look tidier and would make a broken mount a
    boot failure on a board reachable only over the network.
    """
    assert "[Install]" not in bd.JOURNAL_MOUNT_UNIT_TEXT
    assert "WantedBy" not in bd.JOURNAL_MOUNT_UNIT_TEXT
    assert "Options=bind" in bd.JOURNAL_MOUNT_UNIT_TEXT
    assert f"What={bd.JOURNAL_BACKING_DIR}" in bd.JOURNAL_MOUNT_UNIT_TEXT
    assert f"Where={bd.JOURNAL_DIR}" in bd.JOURNAL_MOUNT_UNIT_TEXT


def test_the_backing_store_is_outside_the_directory_it_backs():
    """Inside /var/log it would be swallowed by the same tmpfs at every boot."""
    assert not bd.JOURNAL_BACKING_DIR.startswith("/var/log/")


def test_a_filesystem_too_small_for_a_journal_file_is_refused():
    conn = _enable_conn(
        **{"FSTYPE=": FakeResult(stdout="FSTYPE=ext4 MOUNT=/var/log AVAILKB=4096")}
    )

    with pytest.raises(RuntimeError, match="MB free"):
        bd.enable_persistent_journal(Device(), connection_factory=factory_for(conn))


def test_real_storage_with_room_passes_the_preflight():
    conn = _enable_conn(
        **{"FSTYPE=": FakeResult(stdout="FSTYPE=ext4 MOUNT=/ AVAILKB=2000000")}
    )

    result = bd.enable_persistent_journal(Device(), connection_factory=factory_for(conn))

    assert result["ok"] is True


def test_a_filesystem_that_could_not_be_read_does_not_block_the_attempt():
    """Undetermined is not a refusal -- the verification afterwards is the backstop."""
    conn = _enable_conn(**{"FSTYPE=": FakeResult(exited=1, stderr="df: not found")})

    result = bd.enable_persistent_journal(Device(), connection_factory=factory_for(conn))

    assert result["ok"] is True


def test_the_filesystem_type_comes_from_proc_mounts_not_the_device_name():
    """A tmpfs is routinely mounted with the source `none`.

    Matching df's first column missed exactly the board this check exists for.
    """
    assert "/proc/mounts" in bd._FS_PROBE
    assert "FSTYPE=" in bd._JOURNAL_FS_REPORT


def test_the_verification_survives_a_non_root_board():
    """`sudo -n if ...; then ...; fi` is a shell syntax error.

    The probe is a compound command, so it has to reach the board inside
    `sh -c`. Without that, a non-root board wrote the drop-in, created the
    directory, restarted journald -- and then reported the whole thing as a
    failure it had in fact completed, leaving the Enable button on screen
    forever.
    """
    class Pi(Device):
        username = "pi"

    conn = _enable_conn()

    result = bd.enable_persistent_journal(Pi(), connection_factory=factory_for(conn))

    assert result["ok"] is True
    probe = next(c for c in conn.commands if "STORAGE=PERSISTENT" in c)
    assert probe.startswith("sudo -n timeout ")
    assert " sh -c " in probe


def test_a_journald_that_cannot_be_asked_is_not_reported_as_success():
    conn = _enable_conn(**{"STORAGE=PERSISTENT": FakeResult(stdout="STORAGE=UNKNOWN")})

    with pytest.raises(RuntimeError, match="could not be determined"):
        bd.enable_persistent_journal(Device(), connection_factory=factory_for(conn))


def test_the_drop_in_is_ordered_to_win():
    """A `00-` prefix loses to the conventional `99-*.conf` a vendor image may
    already ship, silently reverting Storage=."""
    assert bd.JOURNALD_DROPIN_PATH.rsplit("/", 1)[-1].startswith("99-")
