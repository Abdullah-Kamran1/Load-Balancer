"""Phase 3 Task 1: persistent HTTP/1.1 server-side reading.

HttpStreamReader owns one client TCP connection's h11 state machine and
can parse sequential requests on it (keep-alive), signal when the client
wants the connection closed, and stop cleanly on EOF.
"""

import asyncio

import pytest

from load_balancer.http_connection import HttpStreamReader


def make_reader(*chunks: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    reader.feed_eof()
    return reader


def request_bytes(target="/", headers=(b"host: t",), body: bytes = b"") -> bytes:
    head = f"GET {target} HTTP/1.1".encode() if not body else f"POST {target} HTTP/1.1".encode()
    lines = [head, *headers]
    if body:
        lines.append(f"content-length: {len(body)}".encode())
    return b"\r\n".join(lines) + b"\r\n\r\n" + body


class TestSequentialRequests:
    async def test_two_requests_on_one_connection(self):
        reader = HttpStreamReader(
            make_reader(request_bytes("/one") + request_bytes("/two")),
            max_body_bytes=1024,
        )
        first = await reader.read_next_request()
        assert first.target == "/one"
        # a real server responds before parsing the next request; h11's
        # state machine requires it (PAUSED until the response is sent)
        reader.serialize_response(200, [], b"r1")
        second = await reader.read_next_request()
        assert second.target == "/two"

    async def test_body_is_fully_consumed_before_next_request(self):
        body = b"payload-data"
        reader = HttpStreamReader(
            make_reader(
                request_bytes("/a", headers=(b"host: t",), body=body)
                + request_bytes("/b")
            ),
            max_body_bytes=1024,
        )
        first = await reader.read_next_request()
        assert first.body == body
        reader.serialize_response(200, [], b"r1")
        second = await reader.read_next_request()
        assert second.target == "/b"  # parser did not eat the next request line


class TestCloseSemantics:
    async def test_connection_close_sets_should_close(self):
        reader = HttpStreamReader(
            make_reader(request_bytes("/", headers=(b"host: t", b"connection: close"))),
            max_body_bytes=1024,
        )
        req = await reader.read_next_request()
        assert req is not None
        assert reader.should_close is True

    async def test_keep_alive_defaults_to_false_should_close(self):
        reader = HttpStreamReader(
            make_reader(request_bytes("/", headers=(b"host: t",))),
            max_body_bytes=1024,
        )
        req = await reader.read_next_request()
        assert req is not None
        assert reader.should_close is False

    async def test_returns_none_on_clean_eof_between_requests(self):
        reader = HttpStreamReader(make_reader(request_bytes("/x")), max_body_bytes=1024)
        first = await reader.read_next_request()
        assert first is not None
        reader.serialize_response(200, [], b"r1")
        second = await reader.read_next_request()
        assert second is None  # client closed the connection cleanly

    async def test_first_request_none_on_immediate_eof(self):
        reader = HttpStreamReader(make_reader(b""), max_body_bytes=1024)
        assert await reader.read_next_request() is None

    async def test_http_10_defaults_to_close(self):
        """HTTP/1.0 semantics: connection closes unless keep-alive requested."""
        raw = b"GET / HTTP/1.0\r\nHost: t\r\n\r\n"
        reader = HttpStreamReader(make_reader(raw), max_body_bytes=1024)
        req = await reader.read_next_request()
        assert req is not None
        assert reader.should_close is True


class TestReaderOwnedSerialization:
    """The response must be serialized through the same h11 connection,
    otherwise the server-side state machine never advances to the next
    request (it sits in SEND_RESPONSE and reports PAUSED forever)."""

    def _response_bytes(self, reader: HttpStreamReader) -> bytes:
        return reader.serialize_response(
            200, [(b"content-type", b"text/plain")], b"ok-body"
        )

    async def test_response_advances_state_machine(self):
        reader = HttpStreamReader(
            make_reader(request_bytes("/one") + request_bytes("/two")),
            max_body_bytes=1024,
        )
        first = await reader.read_next_request()
        assert first.target == "/one"
        out = self._response_bytes(reader)
        assert out.startswith(b"HTTP/1.1 200")
        second = await reader.read_next_request()
        assert second.target == "/two"

    async def test_close_request_response_carries_connection_close(self):
        raw = request_bytes("/", headers=(b"host: t", b"connection: close"))
        reader = HttpStreamReader(make_reader(raw), max_body_bytes=1024)
        await reader.read_next_request()
        assert reader.should_close is True
        out = self._response_bytes(reader)
        assert b"connection: close" in out.lower()
