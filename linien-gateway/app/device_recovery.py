from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from paramiko.ssh_exception import AuthenticationException

from .ssh import open_ssh_connection

BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
REBOOT_TIMEOUT_S = 180.0
REBOOT_POLL_INTERVAL_S = 2.0
SSH_COMMAND_TIMEOUT_S = 5.0


class RecoveryCancelled(Exception):
    pass


def _read_boot_id(device: Any) -> str:
    with open_ssh_connection(device) as conn:
        result = conn.run(
            f"cat {BOOT_ID_PATH}",
            hide=True,
            warn=True,
            timeout=SSH_COMMAND_TIMEOUT_S,
        )
    boot_id = (result.stdout or "").strip()
    if result.exited != 0 or not boot_id:
        raise RuntimeError("Could not read the Red Pitaya boot ID over SSH")
    return boot_id


def reboot_device(
    device: Any,
    update_phase: Callable[[str], None],
    is_cancelled: Callable[[], bool],
    *,
    timeout_s: float = REBOOT_TIMEOUT_S,
    poll_interval_s: float = REBOOT_POLL_INTERVAL_S,
) -> str:
    if is_cancelled():
        raise RecoveryCancelled()
    try:
        previous_boot_id = _read_boot_id(device)
    except AuthenticationException as exc:
        raise RuntimeError("SSH authentication failed") from exc
    if is_cancelled():
        raise RecoveryCancelled()
    update_phase("dispatching")
    if is_cancelled():
        raise RecoveryCancelled()
    username = getattr(device, "username", "root") or "root"
    command = "systemctl reboot" if username == "root" else "sudo -n systemctl reboot"
    try:
        with open_ssh_connection(device) as conn:
            conn.open()
            try:
                result = conn.run(
                    command,
                    hide=True,
                    warn=True,
                    timeout=SSH_COMMAND_TIMEOUT_S,
                )
            except Exception:
                result = None
        if result is not None and result.exited != 0:
            raise RuntimeError("The remote reboot command was rejected")
    except AuthenticationException as exc:
        raise RuntimeError("SSH authentication failed") from exc
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("Could not dispatch the remote reboot command") from exc

    update_phase("waiting_for_boot")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if is_cancelled():
            raise RecoveryCancelled()
        try:
            boot_id = _read_boot_id(device)
        except AuthenticationException as exc:
            raise RuntimeError("SSH authentication failed after reboot") from exc
        except Exception:
            time.sleep(poll_interval_s)
            continue
        if boot_id != previous_boot_id:
            update_phase("host_online")
            return boot_id
        time.sleep(poll_interval_s)
    raise RuntimeError("Timed out waiting for the Red Pitaya to reboot")
