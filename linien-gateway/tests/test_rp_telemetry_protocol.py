from __future__ import annotations

import re
from pathlib import Path

from app import rp_telemetry as rpt


def _c_source() -> str:
    source = (
        Path(__file__).resolve().parents[2]
        / "rp-telemetry"
        / "src"
        / "rp_telemetry.c"
    )
    return source.read_text(encoding="utf-8")


def test_bundled_version_matches_the_c_daemon():
    match = re.search(r'#define RPT_VERSION "([^"]+)"', _c_source())
    assert match is not None
    assert match.group(1) == rpt.BUNDLED_VERSION


def test_protocol_id_matches_the_c_daemon():
    match = re.search(r'#define RPT_PROTOCOL "([^"]+)"', _c_source())
    assert match is not None
    assert match.group(1) == rpt.PROTOCOL_ID


def test_daemon_requests_64_bit_file_offsets():
    """Regression guard for a bug found by running the armv7 build.

    On 32-bit ARM, glibc's non-LFS readdir() returns EOVERFLOW for directory
    entries whose inode number exceeds 32 bits, so XADC discovery silently
    found nothing and every STATUS answered ERR XADC. The define must also come
    before any #include to take effect.
    """
    source = _c_source()
    assert "#define _FILE_OFFSET_BITS 64" in source
    assert source.index("#define _FILE_OFFSET_BITS 64") < source.index("#include")


def test_parses_a_valid_temperature():
    reading = rpt.parse_status_line("RPT1 57.34\n")
    assert reading.state == rpt.STATE_RUNNING
    assert reading.temperature_c == 57.34
    assert reading.error is None


def test_parses_a_negative_temperature():
    assert rpt.parse_status_line("RPT1 -5.00").temperature_c == -5.0


def test_xadc_error_is_reported_as_an_error_state():
    reading = rpt.parse_status_line("RPT1 ERR XADC\n")
    assert reading.state == rpt.STATE_ERROR
    assert "XADC" in (reading.error or "")


def test_command_error_is_reported_as_an_error_state():
    assert rpt.parse_status_line("RPT1 ERR COMMAND").state == rpt.STATE_ERROR


def test_unknown_protocol_id_is_a_version_mismatch():
    reading = rpt.parse_status_line("HTTP/1.1 200 OK")
    assert reading.state == rpt.STATE_VERSION_MISMATCH


def test_empty_response_is_an_error():
    assert rpt.parse_status_line("").state == rpt.STATE_ERROR
    assert rpt.parse_status_line("   \n").state == rpt.STATE_ERROR


def test_truncated_response_is_an_error():
    assert rpt.parse_status_line("RPT1").state == rpt.STATE_ERROR


def test_unparsable_temperature_is_an_error():
    reading = rpt.parse_status_line("RPT1 not-a-number")
    assert reading.state == rpt.STATE_ERROR
    assert "unparsable" in (reading.error or "")


def test_implausible_temperature_is_rejected():
    assert rpt.parse_status_line("RPT1 9999").state == rpt.STATE_ERROR
    assert rpt.parse_status_line("RPT1 -273").state == rpt.STATE_ERROR


def test_nan_is_rejected():
    assert rpt.parse_status_line("RPT1 nan").state == rpt.STATE_ERROR


def test_version_parsing():
    assert rpt.parse_version_line("RPT1 VERSION 1.2.3\n") == "1.2.3"
    assert rpt.parse_version_line("RPT1 57.34") is None
    assert rpt.parse_version_line("") is None


def test_service_unit_uses_the_installed_path_and_port():
    unit = rpt.render_service_unit(12345)
    assert f"ExecStart={rpt.REMOTE_BINARY_PATH} --port 12345" in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=multi-user.target" in unit


def test_xadc_temperature_formula_matches_the_c_daemon():
    # (raw + offset) * scale / 1000, per the IIO ABI and the daemon's
    # xadc_read_temperature(). Values from a real STEMlab 125-14.
    raw, offset, scale = 2504, -2219, 123.040771484
    expected = (raw + offset) * scale / 1000.0
    assert 30.0 < expected < 40.0
    # The daemon prints %.2f, which is what the gateway then parses back.
    reading = rpt.parse_status_line(f"RPT1 {expected:.2f}")
    assert reading.state == rpt.STATE_RUNNING
    assert abs((reading.temperature_c or 0) - expected) < 0.005


def test_the_bundled_binary_is_the_one_the_gateway_claims_to_ship():
    """Guard against editing the daemon and forgetting build-arm.sh.

    Nothing else ties the committed armv7 asset to BUNDLED_VERSION: the
    compile-and-run tests build the C source fresh, so they stay green while
    the bundled binary goes stale. The board would then keep whatever it had,
    and the UI would show an update that installing never clears.
    """
    binary = rpt.BUNDLED_BINARY_PATH.read_bytes()

    assert rpt.BUNDLED_VERSION.encode() in binary

    # ...and it is still an ARM binary, so the board does not answer a fresh
    # install with "Exec format error".
    assert binary[:4] == b"\x7fELF"
    assert int.from_bytes(binary[18:20], "little") == 40  # EM_ARM



# --- host metrics tail ----------------------------------------------------
#
# The daemon appends `key=value` pairs after the temperature. It is an
# extension, not a new command: the temperature stays in the same place, every
# key is optional, and an unrecognised one is ignored rather than fatal.


def test_the_metric_tail_is_parsed():
    reading = rpt.parse_status_line(
        "RPT1 57.34 cpu=3.2 load1=0.41 memtotal=509216 memavail=311044 "
        "uptime=690.2 rootfree=1204880\n"
    )

    assert reading.state == rpt.STATE_RUNNING
    assert reading.temperature_c == 57.34
    metrics = reading.metrics
    assert metrics is not None
    assert metrics.cpu_percent == 3.2
    assert metrics.load1 == 0.41
    assert metrics.mem_total_kb == 509216
    assert metrics.mem_available_kb == 311044
    assert metrics.uptime_s == 690.2
    assert metrics.root_free_kb == 1204880


def test_a_daemon_without_a_tail_still_parses():
    """Every board in the field runs 1.1.0 until it is reinstalled."""
    reading = rpt.parse_status_line("RPT1 57.34\n")

    assert reading.temperature_c == 57.34
    assert reading.metrics is not None
    assert reading.metrics.is_empty()


def test_an_unknown_key_is_ignored_not_fatal():
    """The tail is the extension point: a newer daemon must not break a
    gateway that has never heard of the metric it added."""
    reading = rpt.parse_status_line("RPT1 57.34 cpu=3.2 fanrpm=1800\n")

    assert reading.state == rpt.STATE_RUNNING
    assert reading.metrics.cpu_percent == 3.2


def test_a_garbled_metric_does_not_cost_the_temperature():
    reading = rpt.parse_status_line("RPT1 57.34 cpu=hot load1= memtotal=509216\n")

    assert reading.temperature_c == 57.34
    assert reading.metrics.cpu_percent is None
    assert reading.metrics.load1 is None
    assert reading.metrics.mem_total_kb == 509216


def test_implausible_metrics_are_dropped_individually():
    """Same discipline the temperature gets -- a nonsense number displayed as a
    measurement is worse than a blank -- but one bad key never invalidates the
    rest of the reading."""
    reading = rpt.parse_status_line(
        "RPT1 57.34 cpu=410 load1=-1 memtotal=509216 uptime=-5\n"
    )

    assert reading.metrics.cpu_percent is None
    assert reading.metrics.load1 is None
    assert reading.metrics.uptime_s is None
    assert reading.metrics.mem_total_kb == 509216


def test_free_disk_beyond_the_memory_bound_is_kept():
    """Regression: rootfree was checked against the 64 GiB memory bound, so a
    host with a terabyte free (the daemon tests in a container) lost it."""
    reading = rpt.parse_status_line("RPT1 57.34 rootfree=993232668\n")

    assert reading.metrics.root_free_kb == 993232668


def test_available_memory_above_the_total_is_refused():
    reading = rpt.parse_status_line("RPT1 57.34 memtotal=1000 memavail=9999\n")

    assert reading.metrics.mem_total_kb == 1000
    assert reading.metrics.mem_available_kb is None
    assert reading.metrics.mem_used_percent is None


def test_memory_use_is_derived_from_the_raw_kilobytes():
    metrics = rpt.HostMetrics(mem_total_kb=1000, mem_available_kb=250)

    assert metrics.mem_used_percent == 75.0


def test_the_gateway_read_limit_admits_a_full_metric_line():
    """MAX_RESPONSE_BYTES caps one line; past it the peer is dropped. It must
    stay at or above the daemon's own MAX_RESPONSE or a complete response
    would be treated as hostile."""
    match = re.search(r"#define MAX_RESPONSE (\d+)", _c_source())
    assert match is not None
    assert rpt.MAX_RESPONSE_BYTES >= int(match.group(1))
