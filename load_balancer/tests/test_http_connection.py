"""Task 2 (Phase 2): h11-based HTTP/1.1 message IO over asyncio streams."""

import asyncio

import h11
import pytest

from load_balancer.http_connection import (
    BodyTooLargeError,
    read_request,
    read_response,
    serialize_request,
    serialize_response,
)


def make_reader(*chunks: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    reader.feed_eof()
    return reader


class TestReadRequest:
    async def test_parses_get_with_no_body(self):
        reader = make_reader(b"GET /path?q=1 HTTP/1.1\r\nHost: backend-1\r\n\r\n")
        req = await read_request(reader, max_body_bytes=1024)
        assert req is not None
        assert req.method == "GET"
        assert req.target == "/path?q=1"
        assert req.headers["host"] == "backend-1"
        assert req.body == b""

    async def test_parses_post_with_content_length_body(self):
        reader = make_reader(
            b"POST /submit HTTP/1.1\r\nHost: h\r\nContent-Length: 5\r\n\r\nhello"
        )
        req = await read_request(reader, max_body_bytes=1024)
        assert req.method == "POST"
        assert req.target == "/submit"
        assert req.body == b"hello"

    async def test_returns_none_on_clean_close_before_request(self):
        reader = make_reader(b"")
        assert await read_request(reader, max_body_bytes=1024) is None

    async def test_body_over_cap_raises(self):
        reader = make_reader(
            b"POST /big HTTP/1.1\r\nHost: h\r\nContent-Length: 10\r\n\r\n0123456789"
        )
        with pytest.raises(BodyTooLargeError):
            await read_request(reader, max_body_bytes=4)


class TestReadResponse:
    async def test_parses_status_headers_body(self):
        raw = serialize_response(
            200, [(b"content-type", b"application/json")], b'{"ok": true}'
        )
        reader = make_reader(raw)
        status, headers, body = await read_response(reader, max_body_bytes=1024)
        assert status == 200
        assert headers[b"content-type"] == b"application/json"
        assert body == b'{"ok": true}'

    async def test_body_over_cap_raises(self):
        conn = h11.Connection(our_role=h11.SERVER)
        raw = conn.send(
            h11.Response(status_code=200, headers=[(b"content-length", b"20")])
        )
        raw += conn.send(h11.Data(data=b"x" * 20))
        raw += conn.send(h11.EndOfMessage())
        reader = make_reader(raw)
        with pytest.raises(BodyTooLargeError):
            await read_response(reader, max_body_bytes=10)


class TestSerializeRequest:
    def test_client_request_includes_start_line_and_body(self):
        raw = serialize_request(
            "POST", "/work", [(b"host", b"backend-1:8000")], b"payload"
        )
        text = raw.split(b"\r\n\r\n")[0].decode()
        assert text.startswith("POST /work HTTP/1.1")
        assert "content-length: 7" in text.lower()
        assert raw.endswith(b"\r\n\r\npayload")


class TestSerializeResponse:
    def test_response_framing_is_complete(self):
        raw = serialize_response(503, [(b"content-type", b"text/plain")], b"no backend")
        text = raw.split(b"\r\n\r\n")[0].decode()
        assert text.startswith("HTTP/1.1 503")
        assert "content-length: 10" in text.lower()
        assert raw.endswith(b"\r\n\r\nno backend")
