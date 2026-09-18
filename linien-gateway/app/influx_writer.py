"""Minimal InfluxDB v2 line-protocol writer with connection reuse.

The gateway writes exactly one small point per device per telemetry cycle
(~30 s), so pulling in `influxdb-client` — which the gateway does not
otherwise depend on — would be a lot of machinery for a single HTTP POST.
This module speaks the v2 `/api/v2/write` endpoint directly over
`http.client`, keeping one connection alive per destination host so a sample
does not pay for a fresh TCP (and TLS) handshake every time.

Only used for the Red Pitaya die-temperature field. The Linien parameter
logging still runs on the Red Pitaya itself and is untouched by this.
"""

from __future__ import annotations

import http.client
import threading
import urllib.parse
from dataclasses import dataclass
from typing import Any

WRITE_TIMEOUT_S = 3.0


class InfluxWriteError(RuntimeError):
    """A write did not reach InfluxDB. Always non-fatal for the caller."""


def escape_measurement(value: str) -> str:
    return value.replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ")


def escape_tag(value: str) -> str:
    """Escape a tag key or value (line protocol also reserves `=` there)."""
    return (
        value.replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace(" ", "\\ ")
        .replace("=", "\\=")
    )


def format_point(
    measurement: str,
    fields: dict[str, float],
    timestamp_ns: int,
    tags: dict[str, str] | None = None,
) -> str:
    """Render one line-protocol point with float fields only."""
    if not fields:
        raise ValueError("a line-protocol point needs at least one field")
    rendered = ",".join(f"{name}={float(value)}" for name, value in fields.items())
    key = escape_measurement(measurement)
    for tag_name, tag_value in (tags or {}).items():
        # Tags with an empty value are omitted: InfluxDB rejects them rather
        # than treating them as absent.
        if not tag_value:
            continue
        key += f",{escape_tag(tag_name)}={escape_tag(tag_value)}"
    return f"{key} {rendered} {int(timestamp_ns)}"


@dataclass(frozen=True)
class InfluxDestination:
    """The subset of InfluxDB credentials a write needs."""

    url: str
    org: str
    token: str
    bucket: str

    @classmethod
    def from_credentials(cls, credentials: Any) -> "InfluxDestination":
        return cls(
            url=str(getattr(credentials, "url", "") or ""),
            org=str(getattr(credentials, "org", "") or ""),
            token=str(getattr(credentials, "token", "") or ""),
            bucket=str(getattr(credentials, "bucket", "") or ""),
        )

    def is_complete(self) -> bool:
        return bool(self.url and self.org and self.token and self.bucket)


class InfluxLineWriter:
    """Writes line-protocol batches, reusing one connection per host.

    Not an async client: callers run `write()` on a worker thread. A single
    lock serialises access because one `http.client` connection object cannot
    be shared across concurrent requests.
    """

    def __init__(self, timeout_s: float = WRITE_TIMEOUT_S) -> None:
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self._connections: dict[
            tuple[str, str, int], http.client.HTTPConnection
        ] = {}

    def _connection(self, parsed: urllib.parse.ParseResult) -> http.client.HTTPConnection:
        scheme = parsed.scheme or "http"
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if scheme == "https" else 80)
        key = (scheme, host, port)
        existing = self._connections.get(key)
        if existing is not None:
            return existing
        if scheme == "https":
            created: http.client.HTTPConnection = http.client.HTTPSConnection(
                host, port, timeout=self._timeout_s
            )
        else:
            created = http.client.HTTPConnection(host, port, timeout=self._timeout_s)
        self._connections[key] = created
        return created

    def _drop(self, parsed: urllib.parse.ParseResult) -> None:
        scheme = parsed.scheme or "http"
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if scheme == "https" else 80)
        connection = self._connections.pop((scheme, host, port), None)
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - closing a dead socket is best effort
                pass

    def write(self, destination: InfluxDestination, lines: list[str]) -> None:
        """POST `lines` to InfluxDB. Raises InfluxWriteError on any failure."""
        if not lines:
            return
        if not destination.is_complete():
            raise InfluxWriteError("incomplete InfluxDB credentials")
        parsed = urllib.parse.urlparse(destination.url)
        if parsed.scheme not in ("http", "https"):
            raise InfluxWriteError(f"unsupported InfluxDB URL scheme: {destination.url}")
        base_path = parsed.path.rstrip("/")
        query = urllib.parse.urlencode(
            {"org": destination.org, "bucket": destination.bucket, "precision": "ns"}
        )
        path = f"{base_path}/api/v2/write?{query}"
        body = ("\n".join(lines)).encode("utf-8")
        headers = {
            "Authorization": f"Token {destination.token}",
            "Content-Type": "text/plain; charset=utf-8",
            "Content-Length": str(len(body)),
        }

        with self._lock:
            # One retry: a pooled connection that the server closed while idle
            # fails on the first use and must not be reported as a write error.
            for attempt in (0, 1):
                connection = self._connection(parsed)
                try:
                    connection.request("POST", path, body=body, headers=headers)
                    response = connection.getresponse()
                    status = response.status
                    payload = response.read()
                except (OSError, http.client.HTTPException) as exc:
                    self._drop(parsed)
                    if attempt == 0:
                        continue
                    raise InfluxWriteError(str(exc)) from exc
                if status >= 400:
                    # Keep the connection: an HTTP error is an application-level
                    # response, not a broken socket.
                    detail = payload.decode("utf-8", "replace").strip()[:200]
                    raise InfluxWriteError(f"HTTP {status}: {detail}")
                return

    def close(self) -> None:
        with self._lock:
            for connection in self._connections.values():
                try:
                    connection.close()
                except Exception:  # noqa: BLE001 - best effort
                    pass
            self._connections.clear()
