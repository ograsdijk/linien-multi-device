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
    def start(iio_root: Path, proc_root: Path | None = None) -> Daemon:
        port = _free_port()
        command = [
            str(daemon_binary),
            "--port",
            str(port),
            "--iio-root",
            str(iio_root),
        ]
        if proc_root is not None:
            command += ["--proc-root", str(proc_root)]
        process = subprocess.Popen(
            command,
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

    def factory(iio_root, proc_root=None):
        instance = start(iio_root, proc_root)
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
    # The temperature is the first field and always in the same place; what
    # follows it is the optional host-metric tail, which this test does not
    # constrain (the host's own /proc decides how much of it appears).
    assert response.startswith(f"RPT1 {expected:.2f}".encode())
    assert response.endswith(b"\n")
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
    assert response.startswith(f"RPT1 {ps_temperature:.2f}".encode())

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


def test_a_refused_device_is_reported_once_not_once_per_request(daemon, tmp_path):
    """Discovery re-runs per request while it fails; the warning must not.

    A line per request is the steady background work this daemon exists to
    avoid -- and it would land on exactly the boards already in trouble.
    """
    root = make_board_like_iio_root(tmp_path, ps=False)
    server = daemon(root)

    for _ in range(5):
        assert server.request(b"STATUS\n") == b"RPT1 ERR XADC\n"

    server.process.terminate()
    _stdout, stderr = server.process.communicate(timeout=5)
    warnings = [
        line for line in stderr.decode().splitlines() if "FPGA-backed" in line
    ]
    assert len(warnings) == 1


def test_the_default_root_accepts_nothing_but_the_ps_xadc(daemon_binary):
    """Matching on "adc_wiz" only catches the wizard by the name it happens to
    have. On a real board anything unrecognised is refused as well, because the
    cost of being wrong is a reset -- while a caller that overrode --iio-root
    still gets the permissive behaviour the fixtures rely on.
    """
    process = subprocess.Popen(
        [str(daemon_binary), "--port", str(_free_port())],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        time.sleep(0.5)
    finally:
        process.terminate()
    _stdout, stderr = process.communicate(timeout=5)
    assert "restricting discovery to the PS XADC" in stderr.decode()


def test_an_overridden_root_is_not_restricted(daemon, tmp_path):
    root, _device = make_iio_root(tmp_path)
    server = daemon(root)

    assert server.request(b"STATUS\n").startswith(b"RPT1 ")

    server.process.terminate()
    _stdout, stderr = server.process.communicate(timeout=5)
    assert "restricting discovery" not in stderr.decode()


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


# --- host metrics --------------------------------------------------------
#
# The daemon reads CPU, memory, load, uptime and free disk from /proc and
# statvfs() on the same request that reads the temperature. `--proc-root` lets
# these run against a fixture tree, so they exercise the real parsing on a
# machine that has no /proc at all (macOS) as well as on CI.

PROC_STAT = "cpu  1000 10 200 8000 50 0 5 0 0 0\ncpu0 500 5 100 4000 25 0 2 0 0 0\n"
# Same counters advanced by 900 jiffies total, 400 of them idle: 500/900 busy.
PROC_STAT_LATER = "cpu  1500 10 200 8400 50 0 5 0 0 0\n"
PROC_MEMINFO = (
    "MemTotal:         509216 kB\n"
    "MemFree:          100000 kB\n"
    "MemAvailable:     311044 kB\n"
    "Buffers:            1000 kB\n"
)


def make_proc_root(tmp_path, *, stat=PROC_STAT, meminfo=PROC_MEMINFO,
                   loadavg="0.41 0.55 0.60 1/93 1234\n", uptime="690.23 1300.11\n"):
    root = tmp_path / "proc"
    root.mkdir(parents=True, exist_ok=True)
    for name, content in (
        ("stat", stat),
        ("meminfo", meminfo),
        ("loadavg", loadavg),
        ("uptime", uptime),
    ):
        if content is not None:
            (root / name).write_text(content)
    return root


def _metrics(response: bytes) -> rpt.HostMetrics:
    """Parse a STATUS response the way the gateway does."""
    reading = rpt.parse_status_line(response.decode())
    assert reading.state == rpt.STATE_RUNNING, reading
    assert reading.metrics is not None
    return reading.metrics


def test_status_reports_host_metrics_alongside_the_temperature(daemon, tmp_path):
    root, _device = make_iio_root(tmp_path)
    server = daemon(root, make_proc_root(tmp_path))

    metrics = _metrics(server.request(b"STATUS\n"))

    assert metrics.load1 == 0.41
    assert metrics.mem_total_kb == 509216
    assert metrics.mem_available_kb == 311044
    assert metrics.uptime_s == 690.2
    # statvfs("/") is the real root filesystem in every environment this runs
    # in, so assert it was reported rather than pinning a number.
    assert metrics.root_free_kb is not None and metrics.root_free_kb > 0


def test_cpu_usage_is_measured_between_two_requests(daemon, tmp_path):
    """There is no sampling timer: the previous request is the baseline.

    That makes `cpu` the busy fraction over the caller's own polling interval,
    which is what a 30 s poll wants -- and it costs one cached counter pair
    rather than a thread.
    """
    root, _device = make_iio_root(tmp_path)
    proc = make_proc_root(tmp_path)
    server = daemon(root, proc)

    # Nothing to measure against yet, so no figure is invented.
    assert _metrics(server.request(b"STATUS\n")).cpu_percent is None

    (proc / "stat").write_text(PROC_STAT_LATER)

    assert _metrics(server.request(b"STATUS\n")).cpu_percent == pytest.approx(55.6, abs=0.1)


def test_a_request_too_soon_after_the_last_reuses_the_previous_figure(daemon, tmp_path):
    """A sliver of a window is noise; two clients must not produce it."""
    root, _device = make_iio_root(tmp_path)
    proc = make_proc_root(tmp_path)
    server = daemon(root, proc)

    server.request(b"STATUS\n")
    (proc / "stat").write_text(PROC_STAT_LATER)
    first = _metrics(server.request(b"STATUS\n")).cpu_percent

    # Counters have not moved since, so there is no new window to measure.
    second = _metrics(server.request(b"STATUS\n")).cpu_percent

    assert first == second == pytest.approx(55.6, abs=0.1)


def test_restarted_counters_do_not_produce_a_bogus_figure(daemon, tmp_path):
    """After a reboot the jiffies start over; a delta against the old baseline
    would be fiction, so the figure is withdrawn until a fresh window exists."""
    root, _device = make_iio_root(tmp_path)
    proc = make_proc_root(tmp_path)
    server = daemon(root, proc)

    server.request(b"STATUS\n")
    (proc / "stat").write_text(PROC_STAT_LATER)
    assert _metrics(server.request(b"STATUS\n")).cpu_percent is not None

    (proc / "stat").write_text("cpu  1 0 0 5 0 0 0 0 0 0\n")

    assert _metrics(server.request(b"STATUS\n")).cpu_percent is None


def test_an_absent_proc_still_reports_the_temperature(daemon, tmp_path):
    """Every metric is optional. Losing the tail must not lose the reading."""
    root, _device = make_iio_root(tmp_path)
    server = daemon(root, tmp_path / "no-such-proc")

    response = server.request(b"STATUS\n")

    expected = (2504 + -2219) * 123.040771484 / 1000.0
    assert response.startswith(f"RPT1 {expected:.2f}".encode())
    metrics = _metrics(response)
    assert metrics.cpu_percent is None
    assert metrics.load1 is None
    assert metrics.mem_total_kb is None
    assert metrics.uptime_s is None


def test_memavailable_falls_back_to_memfree(daemon, tmp_path):
    """Kernels before 3.14 have no MemAvailable, and Red Pitaya images in the
    field are old. Reporting nothing there would be a silent gap."""
    root, _device = make_iio_root(tmp_path)
    proc = make_proc_root(
        tmp_path,
        meminfo="MemTotal:         509216 kB\nMemFree:          100000 kB\n",
    )
    server = daemon(root, proc)

    metrics = _metrics(server.request(b"STATUS\n"))

    assert metrics.mem_total_kb == 509216
    assert metrics.mem_available_kb == 100000


def test_a_garbled_proc_file_drops_only_its_own_metric(daemon, tmp_path):
    root, _device = make_iio_root(tmp_path)
    proc = make_proc_root(tmp_path, stat="not a stat file at all\n", loadavg="nonsense\n")
    server = daemon(root, proc)

    metrics = _metrics(server.request(b"STATUS\n"))

    assert metrics.cpu_percent is None
    assert metrics.load1 is None
    # ...while the files that were fine still reported.
    assert metrics.mem_total_kb == 509216
    assert metrics.uptime_s == 690.2


def test_the_response_fits_the_gateways_read_limit(daemon, tmp_path):
    """The gateway caps one line at MAX_RESPONSE_BYTES and drops the peer past
    it, so a full metric tail must fit with room to spare."""
    root, _device = make_iio_root(tmp_path)
    proc = make_proc_root(tmp_path)
    server = daemon(root, proc)

    server.request(b"STATUS\n")  # prime the CPU baseline so `cpu` is present too
    (proc / "stat").write_text(PROC_STAT_LATER)
    response = server.request(b"STATUS\n")

    assert b"cpu=" in response
    assert len(response) <= rpt.MAX_RESPONSE_BYTES
