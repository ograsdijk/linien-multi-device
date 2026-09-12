"""End-to-end tests of the C daemon itself.

The daemon is compiled for the host (not ARM) and run against a fake IIO sysfs
tree, so the protocol, the XADC maths, and the socket behaviour are exercised
for real. Skipped when no POSIX C compiler is available (e.g. on a Windows dev
box); CI on Linux runs them.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import rp_telemetry as rpt

SOURCE = (
    Path(__file__).resolve().parents[2] / "rp-telemetry" / "src" / "rp_telemetry.c"
)


def _compiler() -> str | None:
    if sys.platform == "win32":
        # The daemon is POSIX-only (sys/socket.h, dirent.h).
        return None
    for candidate in ("cc", "gcc", "clang"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


pytestmark = pytest.mark.skipif(
    _compiler() is None, reason="no POSIX C compiler available for the rp-telemetry daemon"
)


@pytest.fixture(scope="module")
def daemon_binary(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("rp-telemetry-build") / "rp-telemetry"
    subprocess.run(
        [
            _compiler(),
            "-O2",
            "-Wall",
            "-Wextra",
            "-std=c99",
            "-D_DEFAULT_SOURCE",
            "-o",
            str(out),
            str(SOURCE),
        ],
        check=True,
    )
    return out


def make_iio_root(tmp_path, *, raw="2504", offset="-2219", scale="123.040771484"):
    """Build a fake /sys/bus/iio/devices tree with a non-zero device index."""
    root = tmp_path / "iio"
    # Deliberately not iio:device0 -- the daemon must discover, not assume.
    device = root / "iio_device3"
    device.mkdir(parents=True)
    (root / "iio_device1").mkdir()  # a decoy without temperature channels
    if raw is not None:
        (device / "in_temp0_raw").write_text(raw)
    if offset is not None:
        (device / "in_temp0_offset").write_text(offset)
    if scale is not None:
        (device / "in_temp0_scale").write_text(scale)
    return root, device


def make_board_like_iio_root(tmp_path, *, ps=True, pl=True):
    """Reproduce a Red Pitaya's IIO tree: two devices, both named "xadc".

        iio:device0 -> /sys/devices/soc0/axi/f8007100.adc/...      (PS, safe)
        iio:device1 -> /sys/devices/soc0/axi/83c00000.xadc_wiz/... (in the FPGA)

    Reading the second one's in_temp0_raw while the Linien bitstream is loaded
    issues an AXI access nothing answers, which hangs the bus and reboots the
    board -- so the daemon must identify the device by its resolved path, not
    by its name.
    """
    soc = tmp_path / "sys" / "devices" / "soc0" / "axi"
    root = tmp_path / "iio"
    root.mkdir(parents=True)

    def add(link_name, device_dir, raw):
        device_dir.mkdir(parents=True)
        (device_dir / "name").write_text("xadc\n")
        (device_dir / "in_temp0_raw").write_text(raw)
        (device_dir / "in_temp0_offset").write_text("-2219")
        (device_dir / "in_temp0_scale").write_text("123.040771484")
        (root / link_name).symlink_to(device_dir, target_is_directory=True)

    if ps:
        add("iio_device0", soc / "f8007100.adc" / "iio_device0", "2504")
    if pl:
        # A distinct value, so a test can tell which device was read.
        add("iio_device1", soc / "83c00000.xadc_wiz" / "iio_device1", "3000")
    return root


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Daemon:
    def __init__(self, process, port):
        self.process = process
        self.port = port

    def request(self, payload: bytes, *, timeout=2.0, close_early=False) -> bytes:
        with socket.create_connection(("127.0.0.1", self.port), timeout=timeout) as s:
            s.settimeout(timeout)
            if payload:
                s.sendall(payload)
            if close_early:
                return b""
            chunks = []
            while True:
                try:
                    chunk = s.recv(256)
                except socket.timeout:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
            return b"".join(chunks)


@pytest.fixture
def daemon(daemon_binary, tmp_path):
    def start(iio_root: Path) -> Daemon:
        port = _free_port()
        process = subprocess.Popen(
            [str(daemon_binary), "--port", str(port), "--iio-root", str(iio_root)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                if process.poll() is not None:
                    raise AssertionError("daemon exited during startup")
                time.sleep(0.05)
        else:
            raise AssertionError("daemon never started listening")
        return Daemon(process, port)

    started: list[Daemon] = []

    def factory(iio_root):
        instance = start(iio_root)
        started.append(instance)
        return instance

    yield factory

    for instance in started:
        instance.process.terminate()
        try:
            instance.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            instance.process.kill()


def test_status_returns_the_computed_temperature(daemon, tmp_path):
    root, _device = make_iio_root(tmp_path)
    server = daemon(root)

    response = server.request(b"STATUS\n")

    expected = (2504 + -2219) * 123.040771484 / 1000.0
    assert response == f"RPT1 {expected:.2f}\n".encode()
    # ...and the gateway parses exactly what the daemon emits.
    reading = rpt.parse_status_line(response.decode())
    assert reading.state == rpt.STATE_RUNNING
    assert abs((reading.temperature_c or 0) - expected) < 0.01


def test_the_fpga_backed_xadc_is_never_read(daemon, tmp_path):
    """Both devices are named "xadc"; only the PS one is safe to touch.

    Picking whichever one readdir() returned first was a coin flip that, with
    the Linien bitstream loaded, reset the board on the first STATUS request.
    """
    root = make_board_like_iio_root(tmp_path)
    server = daemon(root)

    response = server.request(b"STATUS\n")

    ps_temperature = (2504 + -2219) * 123.040771484 / 1000.0
    assert response == f"RPT1 {ps_temperature:.2f}\n".encode()

    # ...and it says so, so the choice is visible in the journal.
    server.process.terminate()
    _stdout, stderr = server.process.communicate(timeout=5)
    assert "iio_device0/in_temp0_raw" in stderr.decode()


def test_a_board_with_only_the_fpga_xadc_reports_an_error(daemon, tmp_path):
    """Refusing to answer is correct here; reading it would reboot the board."""
    root = make_board_like_iio_root(tmp_path, ps=False)
    server = daemon(root)

    assert server.request(b"STATUS\n") == b"RPT1 ERR XADC\n"

    server.process.terminate()
    _stdout, stderr = server.process.communicate(timeout=5)
    text = stderr.decode()
    assert "iio_device1" in text and "FPGA-backed" in text


def test_an_unclassifiable_device_is_still_usable(daemon, tmp_path):
    """Other hardware (and the plain test tree) has no f8007100 in its path."""
    root, _device = make_iio_root(tmp_path)
    server = daemon(root)

    assert server.request(b"STATUS\n").startswith(b"RPT1 ")


def test_version_request(daemon, tmp_path):
    root, _ = make_iio_root(tmp_path)
    server = daemon(root)
    assert server.request(b"VERSION\n") == f"RPT1 VERSION {rpt.BUNDLED_VERSION}\n".encode()


def test_unknown_request_is_rejected(daemon, tmp_path):
    root, _ = make_iio_root(tmp_path)
    server = daemon(root)
    assert server.request(b"REBOOT\n") == b"RPT1 ERR COMMAND\n"
    assert server.request(b"\n") == b"RPT1 ERR COMMAND\n"
    assert server.request(b"status\n") == b"RPT1 ERR COMMAND\n"


def test_crlf_line_ending_is_tolerated(daemon, tmp_path):
    root, _ = make_iio_root(tmp_path)
    server = daemon(root)
    assert server.request(b"STATUS\r\n").startswith(b"RPT1 ")


def test_oversized_request_is_bounded(daemon, tmp_path):
    root, _ = make_iio_root(tmp_path)
    server = daemon(root)
    # No newline, far beyond MAX_REQUEST: the daemon must answer and close
    # rather than buffer.
    assert server.request(b"A" * 4096) == b"RPT1 ERR COMMAND\n"


def test_missing_xadc_reports_a_protocol_error(daemon, tmp_path):
    root, _ = make_iio_root(tmp_path, raw=None, offset=None, scale=None)
    server = daemon(root)
    assert server.request(b"STATUS\n") == b"RPT1 ERR XADC\n"
    assert rpt.parse_status_line("RPT1 ERR XADC").state == rpt.STATE_ERROR


def test_malformed_sysfs_value_reports_a_protocol_error(daemon, tmp_path):
    root, _ = make_iio_root(tmp_path, raw="not-a-number")
    server = daemon(root)
    assert server.request(b"STATUS\n") == b"RPT1 ERR XADC\n"


def test_xadc_disappearing_at_runtime_does_not_crash(daemon, tmp_path):
    root, device = make_iio_root(tmp_path)
    server = daemon(root)
    assert server.request(b"STATUS\n").startswith(b"RPT1 ")

    (device / "in_temp0_raw").unlink()
    assert server.request(b"STATUS\n") == b"RPT1 ERR XADC\n"

    # ...and it recovers when the file comes back.
    (device / "in_temp0_raw").write_text("2504")
    assert server.request(b"STATUS\n").startswith(b"RPT1 ")
    assert server.process.poll() is None


def test_silent_client_is_dropped_and_the_daemon_keeps_serving(daemon, tmp_path):
    root, _ = make_iio_root(tmp_path)
    server = daemon(root)

    # Connect and say nothing; the daemon's receive timeout closes it.
    started = time.monotonic()
    with socket.create_connection(("127.0.0.1", server.port), timeout=10) as s:
        s.settimeout(10)
        assert s.recv(64) == b""
    elapsed = time.monotonic() - started
    assert elapsed < 5, "the silent client must be dropped, not held forever"

    assert server.request(b"STATUS\n").startswith(b"RPT1 ")


def test_client_disconnecting_early_does_not_kill_the_daemon(daemon, tmp_path):
    root, _ = make_iio_root(tmp_path)
    server = daemon(root)

    for _ in range(5):
        server.request(b"STATUS", close_early=True)

    assert server.process.poll() is None
    assert server.request(b"STATUS\n").startswith(b"RPT1 ")


def test_returns_to_accept_after_every_request(daemon, tmp_path):
    root, _ = make_iio_root(tmp_path)
    server = daemon(root)
    for _ in range(25):
        assert server.request(b"STATUS\n").startswith(b"RPT1 ")
    assert server.process.poll() is None


@pytest.mark.skipif(
    not hasattr(os, "times") or sys.platform == "darwin",
    reason="per-process CPU accounting via /proc is Linux-specific",
)
def test_idle_daemon_does_not_busy_loop(daemon, tmp_path):
    root, _ = make_iio_root(tmp_path)
    server = daemon(root)
    stat_path = Path(f"/proc/{server.process.pid}/stat")
    if not stat_path.exists():
        pytest.skip("/proc not available")

    def cpu_ticks() -> int:
        fields = stat_path.read_text().rsplit(") ", 1)[1].split()
        # utime + stime, fields 14 and 15 of proc(5) (1-based, after comm).
        return int(fields[11]) + int(fields[12])

    before = cpu_ticks()
    time.sleep(1.0)
    after = cpu_ticks()

    # Blocked in accept() the whole time: a handful of ticks at most, versus
    # ~100/s for a spin loop.
    assert after - before <= 3


def test_bad_port_argument_is_rejected(daemon_binary):
    result = subprocess.run(
        [str(daemon_binary), "--port", "not-a-port"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "invalid port" in result.stderr


def test_version_flag(daemon_binary):
    result = subprocess.run(
        [str(daemon_binary), "--version"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert result.stdout.strip() == rpt.BUNDLED_VERSION


def test_daemon_is_quiet_during_normal_requests(daemon, tmp_path):
    """systemd journal traffic must not scale with request count."""
    root, _ = make_iio_root(tmp_path)
    server = daemon(root)
    for _ in range(10):
        server.request(b"STATUS\n")
    server.process.terminate()
    _stdout, stderr = server.process.communicate(timeout=5)
    # Exactly the two startup lines (the device it settled on, and the
    # listening port) -- nothing per request.
    assert len(stderr.decode().strip().splitlines()) == 2
