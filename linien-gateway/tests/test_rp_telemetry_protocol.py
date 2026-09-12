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

