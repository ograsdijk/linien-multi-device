"""TCP behaviour of the gateway-side telemetry client.

These run against a real asyncio server on localhost rather than a mock, so
connect/read timeouts, refused connections, and half-open peers are exercised
through the actual socket paths.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket

from app import rp_telemetry as rpt


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.asynccontextmanager
async def serving(handler, *, host: str = "127.0.0.1"):
    """Run `handler` as a TCP server on an ephemeral port.

    Teardown deliberately calls `close()` without `wait_closed()`: since Python
    3.12.1 `wait_closed()` blocks until every connection handler has finished,
    and several tests here need handlers that are stuck on purpose (a peer that
    never answers, a peer that floods a client which stops reading). Those tasks
    are cancelled by `asyncio.run` when the loop shuts down.
    """
    server = await asyncio.start_server(handler, host, 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()


def test_status_round_trip():
    async def run():
        async def handler(reader, writer):
            request = await reader.readline()
            assert request == b"STATUS\n"
            writer.write(b"RPT1 57.34\n")
            await writer.drain()
            writer.close()

        async with serving(handler) as port:
            reading = await rpt.read_telemetry("127.0.0.1", port)
        assert reading.state == rpt.STATE_RUNNING
        assert reading.temperature_c == 57.34

    asyncio.run(run())


def test_version_round_trip():
    async def run():
        async def handler(reader, writer):
            await reader.readline()
            writer.write(b"RPT1 VERSION 1.0.0\n")
            await writer.drain()
            writer.close()

        async with serving(handler) as port:
            version = await rpt.read_version("127.0.0.1", port)
        assert version == "1.0.0"

    asyncio.run(run())


def test_repeated_sequential_connections_are_independent():
    async def run():
        temperatures = iter([b"RPT1 50.00\n", b"RPT1 51.00\n", b"RPT1 52.00\n"])

        async def handler(reader, writer):
            await reader.readline()
            writer.write(next(temperatures))
            await writer.drain()
            writer.close()

        async with serving(handler) as port:
            values = [
                (await rpt.read_telemetry("127.0.0.1", port)).temperature_c
                for _ in range(3)
            ]
        assert values == [50.0, 51.0, 52.0]

    asyncio.run(run())


def test_refused_connection_is_classified_as_stopped():
    # Classification is asserted directly because the wall-clock behaviour of a
    # refused loopback connection is platform dependent: Linux refuses
    # immediately, while the Windows proactor loop can take ~2 s, in which case
    # the connect timeout fires first and it is reported as offline instead.
    assert (
        rpt._classify_exception(ConnectionRefusedError()).state == rpt.STATE_STOPPED
    )


def test_nothing_listening_never_yields_a_temperature():
    async def run():
        reading = await rpt.read_telemetry("127.0.0.1", _free_port())
        assert reading.state in (rpt.STATE_STOPPED, rpt.STATE_OFFLINE)
        assert reading.temperature_c is None
        assert reading.error

    asyncio.run(run())


def test_read_timeout_maps_to_offline():
    async def run():
        async def handler(reader, writer):
            await reader.readline()
            # Accept, then never answer.
            await asyncio.sleep(30)

        async with serving(handler) as port:
            reading = await rpt.read_telemetry(
                "127.0.0.1", port, connect_timeout=1.0, read_timeout=0.2
            )
        assert reading.state == rpt.STATE_OFFLINE
        assert reading.error == "timed out"

    asyncio.run(run())


def test_connect_timeout_maps_to_offline():
    async def run():
        async def never_connects(*_args, **_kwargs):
            await asyncio.sleep(5)

        # A host that black-holes SYNs behaves like this: open_connection
        # simply never completes.
        original = asyncio.open_connection
        asyncio.open_connection = never_connects  # type: ignore[assignment]
        try:
            reading = await rpt.read_telemetry(
                "192.0.2.1", 18864, connect_timeout=0.1, read_timeout=0.1
            )
        finally:
            asyncio.open_connection = original  # type: ignore[assignment]
        assert reading.state == rpt.STATE_OFFLINE

    asyncio.run(run())


def test_peer_closing_without_answering_is_an_error():
    async def run():
        async def handler(reader, writer):
            await reader.readline()
            writer.close()

        async with serving(handler) as port:
            reading = await rpt.read_telemetry("127.0.0.1", port)
        # readline() returns b"" on a clean close -> empty response.
        assert reading.state == rpt.STATE_ERROR

    asyncio.run(run())


def test_malformed_response_is_an_error():
    async def run():
        async def handler(reader, writer):
            await reader.readline()
            writer.write(b"garbage without a protocol id\n")
            await writer.drain()
            writer.close()

        async with serving(handler) as port:
            reading = await rpt.read_telemetry("127.0.0.1", port)
        assert reading.state == rpt.STATE_VERSION_MISMATCH

    asyncio.run(run())


def test_oversized_response_is_bounded_and_reported():
    async def run():
        async def handler(reader, writer):
            await reader.readline()
            # A peer that never sends a newline must not make the gateway
            # buffer without bound. No drain(): if the client stops reading,
            # draining would block this handler indefinitely.
            writer.write(b"RPT1 " + b"9" * 10_000)
            await asyncio.sleep(30)

        async with serving(handler) as port:
            reading = await rpt.read_telemetry(
                "127.0.0.1", port, connect_timeout=1.0, read_timeout=0.5
            )
        assert reading.state in (rpt.STATE_ERROR, rpt.STATE_OFFLINE)
        assert reading.temperature_c is None

    asyncio.run(run())


def test_empty_host_does_not_attempt_a_connection():
    async def run():
        reading = await rpt.read_telemetry("")
        assert reading.state == rpt.STATE_OFFLINE
        assert "no host" in (reading.error or "")

    asyncio.run(run())


def test_sync_read_used_by_install_verification():
    async def run():
        async def handler(reader, writer):
            await reader.readline()
            writer.write(b"RPT1 42.50\n")
            await writer.drain()
            writer.close()

        async with serving(handler) as port:
            reading = await asyncio.to_thread(
                rpt.read_telemetry_sync, "127.0.0.1", port
            )
        assert reading.state == rpt.STATE_RUNNING
        assert reading.temperature_c == 42.5

    asyncio.run(run())


def test_sync_read_reports_nothing_listening():
    reading = rpt.read_telemetry_sync("127.0.0.1", _free_port())
    assert reading.state in (rpt.STATE_STOPPED, rpt.STATE_OFFLINE)
    assert reading.temperature_c is None
