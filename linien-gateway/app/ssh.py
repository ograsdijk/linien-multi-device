from __future__ import annotations

from typing import Any, Callable

from fabric import Connection

SSH_PORT = 22
SSH_CONNECT_TIMEOUT_S = 6.0


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
        connect_timeout=SSH_CONNECT_TIMEOUT_S,
        connect_kwargs={"password": password},
    )
