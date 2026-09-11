from __future__ import annotations

import http.client

import pytest

from app.influx_writer import (
    InfluxDestination,
    InfluxLineWriter,
    InfluxWriteError,
    escape_measurement,
    format_point,
)


class FakeResponse:
    def __init__(self, status=204, body=b""):
        self.status = status
        self._body = body

    def read(self):
        return self._body


class FakeConnection:
    def __init__(self, responses=None, request_error=None):
        self.requests: list[tuple[str, str, bytes, dict]] = []
        self.responses = list(responses or [FakeResponse()])
        self.request_error = request_error
        self.closed = False

    def request(self, method, path, body=None, headers=None):
        if self.request_error is not None:
            error, self.request_error = self.request_error, None
            raise error
        self.requests.append((method, path, body, headers or {}))

    def getresponse(self):
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def close(self):
        self.closed = True


def destination(url="http://influx.example:8086"):
    return InfluxDestination(url=url, org="lab", token="tok", bucket="linien")


def test_format_point():
    line = format_point("linien", {"rp_temperature_c": 57.25}, 1_700_000_000_000_000_000)
    assert line == "linien rp_temperature_c=57.25 1700000000000000000"


def test_format_point_with_a_tag():
    line = format_point(
        "linien", {"rp_temperature_c": 57.25}, 5, tags={"device": "laser-a"}
    )
    assert line == "linien,device=laser-a rp_temperature_c=57.25 5"


def test_tag_values_are_escaped():
    line = format_point(
        "m", {"a": 1.0}, 5, tags={"device": "a b,c=d"}
    )
    # Spaces, commas and equals signs all terminate a tag in line protocol.
    assert line.startswith("m,device=a\\ b\\,c\\=d ")


def test_empty_tag_values_are_omitted():
    # InfluxDB rejects an empty tag value rather than ignoring it.
    line = format_point("m", {"a": 1.0}, 5, tags={"device": ""})
    assert line == "m a=1.0 5"


def test_format_point_requires_a_field():
    with pytest.raises(ValueError):
        format_point("linien", {}, 1)


def test_measurement_escaping():
    assert escape_measurement("my meas,ure") == "my\\ meas\\,ure"
    line = format_point("my meas", {"a": 1.0}, 5)
    assert line.startswith("my\\ meas ")


def test_incomplete_destination_is_refused():
    writer = InfluxLineWriter()
    incomplete = InfluxDestination(url="http://x", org="", token="t", bucket="b")
    with pytest.raises(InfluxWriteError, match="incomplete"):
        writer.write(incomplete, ["m f=1 1"])


def test_unsupported_scheme_is_refused():
    writer = InfluxLineWriter()
    with pytest.raises(InfluxWriteError, match="scheme"):
        writer.write(destination("ftp://influx"), ["m f=1 1"])


def test_empty_batch_is_a_no_op():
    writer = InfluxLineWriter()
    writer.write(destination(), [])  # must not raise or connect


def test_write_posts_line_protocol_with_auth(monkeypatch):
    connection = FakeConnection()
    writer = InfluxLineWriter()
    monkeypatch.setattr(writer, "_connection", lambda parsed: connection)

    writer.write(destination(), ["linien rp_temperature_c=57.25 5"])

    method, path, body, headers = connection.requests[0]
    assert method == "POST"
    assert path.startswith("/api/v2/write?")
    assert "org=lab" in path and "bucket=linien" in path and "precision=ns" in path
    assert body == b"linien rp_temperature_c=57.25 5"
    assert headers["Authorization"] == "Token tok"


def test_write_honours_a_url_path_prefix(monkeypatch):
    connection = FakeConnection()
    writer = InfluxLineWriter()
    monkeypatch.setattr(writer, "_connection", lambda parsed: connection)
    writer.write(destination("http://host:8086/influx/"), ["m f=1 1"])
    assert connection.requests[0][1].startswith("/influx/api/v2/write?")


def test_http_error_is_reported():
    connection = FakeConnection([FakeResponse(status=401, body=b"unauthorized")])
    writer = InfluxLineWriter()
    writer._connection = lambda parsed: connection  # type: ignore[assignment]
    with pytest.raises(InfluxWriteError, match="HTTP 401"):
        writer.write(destination(), ["m f=1 1"])


def test_a_stale_pooled_connection_is_retried_once(monkeypatch):
    """A keep-alive socket the server dropped must not surface as a write error."""
    connections = [
        FakeConnection(request_error=http.client.RemoteDisconnected("closed")),
        FakeConnection(),
    ]
    handed_out: list[FakeConnection] = []
    writer = InfluxLineWriter()

    def fake_connection(_parsed):
        connection = connections[len(handed_out)] if len(handed_out) < len(connections) else connections[-1]
        handed_out.append(connection)
        return connection

    monkeypatch.setattr(writer, "_connection", fake_connection)
    monkeypatch.setattr(writer, "_drop", lambda parsed: None)

    writer.write(destination(), ["m f=1 1"])

    assert len(handed_out) == 2
    assert connections[1].requests


def test_a_persistent_socket_error_raises(monkeypatch):
    writer = InfluxLineWriter()
    monkeypatch.setattr(
        writer,
        "_connection",
        lambda parsed: FakeConnection(request_error=OSError("connection reset")),
    )
    monkeypatch.setattr(writer, "_drop", lambda parsed: None)
    with pytest.raises(InfluxWriteError, match="connection reset"):
        writer.write(destination(), ["m f=1 1"])


def test_connections_are_reused_per_host():
    writer = InfluxLineWriter()
    import urllib.parse

    parsed = urllib.parse.urlparse("http://influx.example:8086")
    first = writer._connection(parsed)
    second = writer._connection(parsed)
    assert first is second
    writer.close()


def test_https_uses_a_tls_connection():
    writer = InfluxLineWriter()
    import urllib.parse

    connection = writer._connection(urllib.parse.urlparse("https://influx.example"))
    assert isinstance(connection, http.client.HTTPSConnection)
    writer.close()


def test_close_clears_the_pool():
    writer = InfluxLineWriter()
    import urllib.parse

    writer._connection(urllib.parse.urlparse("http://influx.example:8086"))
    writer.close()
    assert writer._connections == {}


def test_destination_from_credentials():
    class Credentials:
        url = "http://influx:8086"
        org = "lab"
        token = "tok"
        bucket = "b"
        measurement = "m"

    dest = InfluxDestination.from_credentials(Credentials())
    assert dest.is_complete()
    assert dest.bucket == "b"


def test_destination_is_hashable_for_batching():
    # Batching groups points by destination, which requires hashability.
    assert len({destination(), destination()}) == 1
