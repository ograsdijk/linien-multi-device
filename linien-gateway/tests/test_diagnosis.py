from __future__ import annotations

from types import SimpleNamespace

import pytest
from paramiko.ssh_exception import AuthenticationException, NoValidConnectionsError

from app import diagnosis
from app.diagnosis import (
    CATEGORY_HOST_UNREACHABLE,
    CATEGORY_REBOOTED,
    CATEGORY_RECOVERING,
    CATEGORY_SERVER_CRASHED,
    CATEGORY_SERVER_DOWN_UNKNOWN,
    ProbeResult,
    classify_diagnosis,
    probe_device,
)

THRESHOLD = 600.0


def _classify(result: ProbeResult, since=None):
    return classify_diagnosis(
        result,
        host="rp-test.local",
        seconds_since_last_connected=since,
        probed_at=123.0,
        uptime_threshold_s=THRESHOLD,
    )


def test_classify_recovering_when_server_listening():
    d = _classify(ProbeResult(True, True, None, None, None))
    assert d["category"] == CATEGORY_RECOVERING
    assert d["lock_state"] == "unknown"


def test_classify_host_unreachable():
    d = _classify(ProbeResult(False, False, None, None, None))
    assert d["category"] == CATEGORY_HOST_UNREACHABLE
    assert d["lock_state"] == "unknown"


def test_classify_server_down_unknown_when_uptime_missing():
    d = _classify(ProbeResult(False, True, None, None, None))
    assert d["category"] == CATEGORY_SERVER_DOWN_UNKNOWN
    assert d["lock_state"] == "unknown"


def test_classify_rebooted_on_low_uptime():
    d = _classify(ProbeResult(False, True, 30.0, True, None))
    assert d["category"] == CATEGORY_REBOOTED
    assert d["lock_state"] == "lost"
    assert "rebooted" in d["message"].lower()


def test_classify_rebooted_when_uptime_below_since_connected():
    # High absolute uptime, but the board rebooted after our last connection.
    d = _classify(ProbeResult(False, True, 3600.0, True, None), since=7200.0)
    assert d["category"] == CATEGORY_REBOOTED
    assert d["lock_state"] == "lost"


def test_classify_crash_lock_confirmed_held():
    d = _classify(ProbeResult(False, True, 3600.0, True, 1), since=60.0)
    assert d["category"] == CATEGORY_SERVER_CRASHED
    assert d["lock_state"] == "locked"
    assert "do not restart" in d["message"].lower()


def test_classify_crash_lock_confirmed_unlocked():
    d = _classify(ProbeResult(False, True, 3600.0, True, 0), since=60.0)
    assert d["category"] == CATEGORY_SERVER_CRASHED
    assert d["lock_state"] == "unlocked"


def test_classify_crash_lock_likely_when_fpga_operating_but_register_unreadable():
    # Gateware loaded but the lock register couldn't be read (e.g. devmem
    # missing) -> lock is likely still held.
    result = ProbeResult(False, True, 3600.0, True, None, lock_read_attempted=True)
    d = _classify(result, since=60.0)
    assert d["category"] == CATEGORY_SERVER_CRASHED
    assert d["lock_state"] == "likely_held"
    assert "unreadable" in d["message"].lower()
    assert "devmem" in d["message"].lower()


def test_classify_crash_lock_lost_when_fpga_not_operating():
    # Gateware is NOT loaded -> the FPGA cannot be holding the lock, so the
    # classifier must not claim "likely held".
    d = _classify(ProbeResult(False, True, 3600.0, False, None), since=60.0)
    assert d["category"] == CATEGORY_SERVER_CRASHED
    assert d["lock_state"] == "lost"
    assert "not loaded" in d["message"].lower()


def test_classify_crash_lock_unknown_when_fpga_state_unknown():
    # FPGA state couldn't be determined at all -> stay honest.
    d = _classify(ProbeResult(False, True, 3600.0, None, None), since=60.0)
    assert d["category"] == CATEGORY_SERVER_CRASHED
    assert d["lock_state"] == "unknown"


# --- probe_device --------------------------------------------------------


class _FakeResult:
    def __init__(self, stdout: str, exited: int = 0):
        self.stdout = stdout
        self.exited = exited


class _FakeConnection:
    def __init__(self, *args, **kwargs):
        self.commands: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def run(self, cmd, **_kwargs):
        self.commands.append(cmd)
        if "uptime" in cmd:
            return _FakeResult("3601.5 1234.5\n---\noperating\n")
        if "devmem" in cmd:
            return _FakeResult("0x00000001\n")
        return _FakeResult("", exited=1)


def _device():
    return SimpleNamespace(
        host="rp-test.local", port=18862, username="root", password="root"
    )


def test_probe_server_listening_skips_ssh(monkeypatch):
    monkeypatch.setattr(diagnosis, "_tcp_open", lambda *a, **k: True)

    def _boom(*_a, **_k):
        raise AssertionError("SSH must not be attempted when server is listening")

    monkeypatch.setattr(diagnosis, "Connection", _boom)
    result = probe_device(_device(), seconds_since_last_connected=10.0)
    assert result.server_listening is True
    assert result.host_reachable is True


def test_probe_reads_lock_register_when_no_reboot(monkeypatch):
    monkeypatch.setattr(diagnosis, "_tcp_open", lambda *a, **k: False)
    monkeypatch.setattr(diagnosis, "Connection", _FakeConnection)
    result = probe_device(_device(), seconds_since_last_connected=60.0)
    assert result.server_listening is False
    assert result.host_reachable is True
    assert result.uptime_s == pytest.approx(3601.5)
    assert result.fpga_operating is True
    assert result.lock_bit == 1
    assert result.lock_read_attempted is True


def test_probe_skips_register_read_on_low_uptime(monkeypatch):
    monkeypatch.setattr(diagnosis, "_tcp_open", lambda *a, **k: False)

    class _LowUptimeConn(_FakeConnection):
        def run(self, cmd, **_kwargs):
            self.commands.append(cmd)
            if "uptime" in cmd:
                return _FakeResult("42.0 10.0\n---\noperating\n")
            raise AssertionError("devmem must not run on low uptime")

    monkeypatch.setattr(diagnosis, "Connection", _LowUptimeConn)
    result = probe_device(_device(), seconds_since_last_connected=10.0)
    assert result.uptime_s == pytest.approx(42.0)
    assert result.lock_bit is None
    assert result.lock_read_attempted is False


def test_probe_falls_back_to_python_when_devmem_missing(monkeypatch):
    # devmem absent (shell exit 127) -> python3 /dev/mem read supplies the bit.
    monkeypatch.setattr(diagnosis, "_tcp_open", lambda *a, **k: False)

    class _NoDevmemConn(_FakeConnection):
        def run(self, cmd, **_kwargs):
            self.commands.append(cmd)
            if "uptime" in cmd:
                return _FakeResult("3601.5 1234.5\n---\noperating\n")
            if "devmem" in cmd:
                return _FakeResult("", exited=127)
            if "python3" in cmd:
                return _FakeResult("0x00000001\n")
            return _FakeResult("", exited=1)

    monkeypatch.setattr(diagnosis, "Connection", _NoDevmemConn)
    result = probe_device(_device(), seconds_since_last_connected=60.0)
    assert result.lock_bit == 1
    assert result.lock_read_attempted is True


def test_probe_lock_bit_none_when_all_read_methods_fail(monkeypatch):
    # Neither devmem nor python3 yields a value -> lock_bit None but the read
    # was attempted (drives the "likely_held / unreadable" classification).
    monkeypatch.setattr(diagnosis, "_tcp_open", lambda *a, **k: False)

    class _NoReadConn(_FakeConnection):
        def run(self, cmd, **_kwargs):
            self.commands.append(cmd)
            if "uptime" in cmd:
                return _FakeResult("3601.5 1234.5\n---\noperating\n")
            return _FakeResult("", exited=127)

    monkeypatch.setattr(diagnosis, "Connection", _NoReadConn)
    result = probe_device(_device(), seconds_since_last_connected=60.0)
    assert result.fpga_operating is True
    assert result.lock_bit is None
    assert result.lock_read_attempted is True


def test_read_lock_bit_tries_devmem_before_python():
    # Ordering: devmem is consulted first; python3 only as a fallback.
    class _OrderConn(_FakeConnection):
        def run(self, cmd, **_kwargs):
            self.commands.append(cmd)
            return _FakeResult("0x00000000\n")  # both would parse to bit 0

    conn = _OrderConn()
    assert diagnosis._read_lock_bit(conn) == 0
    # Only the first (devmem) method runs because it already returned a value.
    assert len(conn.commands) == 1
    assert "devmem" in conn.commands[0]


def test_probe_auth_failure_marks_reachable_unknown(monkeypatch):
    monkeypatch.setattr(diagnosis, "_tcp_open", lambda *a, **k: False)

    def _auth(*_a, **_k):
        raise AuthenticationException("bad creds")

    monkeypatch.setattr(diagnosis, "Connection", _auth)
    result = probe_device(_device(), seconds_since_last_connected=10.0)
    assert result.host_reachable is True
    assert result.uptime_s is None
    assert result.error is not None


def test_probe_connection_error_marks_unreachable(monkeypatch):
    monkeypatch.setattr(diagnosis, "_tcp_open", lambda *a, **k: False)

    def _refused(*_a, **_k):
        raise NoValidConnectionsError({("rp-test.local", 22): OSError("refused")})

    monkeypatch.setattr(diagnosis, "Connection", _refused)
    result = probe_device(_device(), seconds_since_last_connected=10.0)
    assert result.host_reachable is False


def test_probe_never_raises_on_unexpected_error(monkeypatch):
    monkeypatch.setattr(diagnosis, "_tcp_open", lambda *a, **k: False)

    def _weird(*_a, **_k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(diagnosis, "Connection", _weird)
    result = probe_device(_device(), seconds_since_last_connected=10.0)
    assert result.host_reachable is False
    assert result.error is not None


# --- DiagnosisProbe worker (called directly, no threads for determinism) --


class _FakeSession:
    def __init__(self, key, *, connected=False, connecting=False, wants=True):
        self.device = SimpleNamespace(
            host=f"{key}.local", port=18862, username="root", password="root"
        )
        self.connected = connected
        self.connecting = connecting
        self._wants = wants
        self.applied: list[dict] = []

    def wants_diagnosis(self):
        return self._wants

    def seconds_since_last_connected(self):
        return 60.0

    def apply_diagnosis(self, d):
        self.applied.append(d)


class _FakeRegistry:
    def __init__(self, sessions):
        self._sessions = sessions

    def get(self, key):
        return self._sessions.get(key)


def _crash_probe(device, *, seconds_since_last_connected, uptime_threshold_s):
    return ProbeResult(False, True, 3600.0, True, 1, lock_read_attempted=True)


def _probe(sessions, **kwargs):
    return diagnosis.DiagnosisProbe(
        _FakeRegistry(sessions), probe_fn=_crash_probe, **kwargs
    )


def test_probe_once_applies_diagnosis():
    session = _FakeSession("dev-1")
    _probe({"dev-1": session})._probe_once("dev-1")
    assert len(session.applied) == 1
    assert session.applied[0]["category"] == diagnosis.CATEGORY_SERVER_CRASHED
    assert session.applied[0]["lock_state"] == "locked"


def test_probe_once_skips_connected_session():
    session = _FakeSession("dev-1", connected=True)
    _probe({"dev-1": session})._probe_once("dev-1")
    assert session.applied == []


def test_probe_once_skips_when_not_wanted():
    session = _FakeSession("dev-1", wants=False)
    _probe({"dev-1": session})._probe_once("dev-1")
    assert session.applied == []


def test_request_dedupes_pending_and_inflight():
    probe = _probe({})
    probe.request("dev-1")
    probe.request("dev-1")  # duplicate while scheduled
    assert len(probe._heap) == 1
    key, _ = probe._pop_ready()
    assert key == "dev-1"
    assert "dev-1" in probe._inflight
    probe.request("dev-1")  # duplicate while in-flight
    assert len(probe._heap) == 0


def test_probe_and_reschedule_requeues_while_disconnected():
    session = _FakeSession("dev-1")
    probe = _probe({"dev-1": session}, reprobe_interval_s=5.0)
    probe._inflight.add("dev-1")  # emulate dispatch
    probe._probe_and_reschedule("dev-1")
    assert session.applied  # was probed
    assert "dev-1" not in probe._inflight
    assert len(probe._heap) == 1  # re-enqueued for later


def test_probe_and_reschedule_stops_when_reconnected():
    session = _FakeSession("dev-1", connected=True)
    probe = _probe({"dev-1": session})
    probe._inflight.add("dev-1")
    probe._probe_and_reschedule("dev-1")
    assert "dev-1" not in probe._inflight
    assert len(probe._heap) == 0  # not re-enqueued


def test_start_stop_is_clean():
    probe = _probe({})
    probe.start()
    probe.stop()
    assert probe._thread is None
    assert probe._executor is None
