from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.device_recovery as recovery


def _device(username: str = "root"):
    return SimpleNamespace(host="rp.local", username=username, password="secret")


def test_reboot_confirms_changed_boot_id(monkeypatch):
    boot_ids = iter(["before", OSError("offline"), "after"])
    commands: list[str] = []
    phases: list[str] = []

    def read_boot_id(_device):
        value = next(boot_ids)
        if isinstance(value, Exception):
            raise value
        return value

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def open(self):
            return None

        def run(self, command, **_kwargs):
            commands.append(command)
            return SimpleNamespace(exited=0)

    monkeypatch.setattr(recovery, "_read_boot_id", read_boot_id)
    monkeypatch.setattr(recovery, "open_ssh_connection", lambda _device: Connection())
    monkeypatch.setattr(recovery.time, "sleep", lambda _seconds: None)

    result = recovery.reboot_device(
        _device(), phases.append, lambda: False, timeout_s=1, poll_interval_s=0
    )

    assert result == "after"
    assert commands == ["systemctl reboot"]
    assert phases == ["dispatching", "waiting_for_boot", "host_online"]


def test_reboot_uses_noninteractive_sudo_for_non_root(monkeypatch):
    monkeypatch.setattr(recovery, "_read_boot_id", lambda _device: "before")
    commands: list[str] = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def open(self):
            return None

        def run(self, command, **_kwargs):
            commands.append(command)
            return SimpleNamespace(exited=1)

    monkeypatch.setattr(recovery, "open_ssh_connection", lambda _device: Connection())

    with pytest.raises(RuntimeError, match="rejected"):
        recovery.reboot_device(_device("linien"), lambda _phase: None, lambda: False)

    assert commands == ["sudo -n systemctl reboot"]


def test_reboot_cancellation_stops_boot_wait(monkeypatch):
    monkeypatch.setattr(recovery, "_read_boot_id", lambda _device: "same")

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def open(self):
            return None

        def run(self, _command, **_kwargs):
            return SimpleNamespace(exited=0)

    monkeypatch.setattr(recovery, "open_ssh_connection", lambda _device: Connection())

    with pytest.raises(recovery.RecoveryCancelled):
        recovery.reboot_device(_device(), lambda _phase: None, lambda: True)


def test_reboot_cancelled_before_dispatch_does_not_open_ssh(monkeypatch):
    opened: list[bool] = []
    monkeypatch.setattr(
        recovery,
        "open_ssh_connection",
        lambda _device: opened.append(True),
    )

    with pytest.raises(recovery.RecoveryCancelled):
        recovery.reboot_device(_device(), lambda _phase: None, lambda: True)

    assert opened == []
