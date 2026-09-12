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


def test_an_existing_journal_directory_alone_does_not_mean_persistent():
    """The directory can exist while journald still logs to RAM -- another
    drop-in overriding Storage=, or a restart that has not happened. Keying on
    the directory reported success for a board that would still lose its logs.
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


def test_persistence_is_unknown_when_journald_could_not_be_asked():
    conn = FakeConnection(
        rules={"STORAGE=PERSISTENT": FakeResult(stdout="STORAGE=UNKNOWN")}
    )

    bundle = bd.collect_diagnostics(Device(), connection_factory=factory_for(conn))

    assert bundle["persistent_journal"] is None


# --- enabling persistence ------------------------------------------------


def _enable_conn(**overrides):
    import hashlib

    digest = hashlib.sha256(bd.JOURNALD_DROPIN.encode("utf-8")).hexdigest()
    rules = {
        "sha256sum": FakeResult(stdout=f"{digest}  {bd.JOURNALD_DROPIN_PATH}"),
        "STORAGE=PERSISTENT": FakeResult(stdout="STORAGE=PERSISTENT"),
    }
    rules.update(overrides)
    return FakeConnection(rules=rules)


def test_enabling_persistence_writes_verifies_and_restarts():
    conn = _enable_conn()

    result = bd.enable_persistent_journal(Device(), connection_factory=factory_for(conn))

    assert result == {"ok": True, "persistent_journal": True}
    joined = "\n".join(conn.commands)
    assert f"mkdir -p {bd.JOURNALD_DROPIN_DIR}" in joined
    # `tee`, not a redirect: the redirect would be performed by the calling,
    # unprivileged shell.
    assert f"tee {bd.JOURNALD_DROPIN_PATH}" in joined
    assert "sha256sum" in joined
    assert "systemctl restart systemd-journald" in joined
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
    conn = _enable_conn(sha256sum=FakeResult(stdout="0000  path"))

    with pytest.raises(RuntimeError, match="does not match"):
        bd.enable_persistent_journal(Device(), connection_factory=factory_for(conn))


def test_a_board_without_sha256sum_falls_back_to_a_size_check():
    size = len(bd.JOURNALD_DROPIN.encode("utf-8"))
    conn = _enable_conn(
        sha256sum=FakeResult(exited=127, stderr="not found"),
        **{"wc -c": FakeResult(stdout=str(size))},
    )

    result = bd.enable_persistent_journal(Device(), connection_factory=factory_for(conn))

    assert result["ok"] is True


def test_a_truncated_config_is_caught_by_the_size_check():
    conn = _enable_conn(
        sha256sum=FakeResult(exited=127, stderr="not found"),
        **{"wc -c": FakeResult(stdout="3")},
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


def test_a_journald_that_cannot_be_asked_is_not_reported_as_success():
    conn = _enable_conn(**{"STORAGE=PERSISTENT": FakeResult(stdout="STORAGE=UNKNOWN")})

    with pytest.raises(RuntimeError, match="could not be determined"):
        bd.enable_persistent_journal(Device(), connection_factory=factory_for(conn))


def test_the_drop_in_is_ordered_to_win():
    """A `00-` prefix loses to the conventional `99-*.conf` a vendor image may
    already ship, silently reverting Storage=."""
    assert bd.JOURNALD_DROPIN_PATH.rsplit("/", 1)[-1].startswith("99-")
