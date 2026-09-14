from __future__ import annotations

from typing import Any, Callable

from fabric import Connection

SSH_PORT = 22
SSH_CONNECT_TIMEOUT_S = 6.0
SSH_COMMAND_TIMEOUT_S = 20.0


def open_ssh_connection(
    device: Any, connection_factory: Callable[..., Connection] = Connection
) -> Connection:
    host = getattr(device, "host", "") or ""
    username = getattr(device, "username", "root") or "root"
    password = getattr(device, "password", "") or ""
    return connection_factory(
        host,
        user=username,
        port=SSH_PORT,
        connect_kwargs={"password": password},
        connect_timeout=SSH_CONNECT_TIMEOUT_S,
    )


def privileged(device: Any, command: str) -> str:
    """Prefix `command` with `sudo -n` unless the device logs in as root.

    `-n` rather than a password prompt: a command that blocks waiting for input
    would hold the SSH session open until the timeout.
    """
    username = getattr(device, "username", "root") or "root"
    return command if username == "root" else f"sudo -n {command}"


def run_remote(
    conn: Any,
    device: Any,
    command: str,
    *,
    timeout: float = SSH_COMMAND_TIMEOUT_S,
    privileged_command: bool = True,
) -> tuple[int, str, str]:
    """Run one command on an open connection. Returns (exit code, stdout, stderr).

    `warn=True` so a non-zero exit is data rather than an exception -- most of
    the diagnostic commands are expected to fail on some images (no `devmem`,
    no pstore, a journal that was never made persistent) and a missing section
    must not lose the rest of the bundle.

    Raises only if the transport itself fails, which the caller handles once
    for the whole connection.
    """
    full = privileged(device, command) if privileged_command else command
    result = conn.run(full, hide=True, warn=True, timeout=timeout)
    return (
        getattr(result, "exited", 1),
        (getattr(result, "stdout", "") or ""),
        (getattr(result, "stderr", "") or ""),
    )


def shell_single_quote(value: str) -> str:
    """Make `value` safe inside single quotes in a POSIX shell command.

    The text is embedded in a command that crosses SSH, so anything the remote
    shell would reinterpret has to be neutralised.
    """
    return value.replace("'", "'\"'\"'")
