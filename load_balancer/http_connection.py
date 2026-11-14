"""h11-based HTTP/1.1 message IO over asyncio streams (Phase 2).

This module owns HTTP framing only. Connection lifecycle (who opens what,
when connections close) belongs to proxy.py.

Design decisions:
- Request/response bodies are read fully into memory, bounded by
  max_body_bytes; anything larger raises BodyTooLargeError. This keeps
  memory bounded without full streaming plumbing (extended in Phase 3).
- Header names are normalised to lowercase str for lookups; h11 keeps the
  original bytes on the wire.

h11 does the framing arithmetic (Content-Length, chunked decoding, etc.);
we never write HTTP framing by hand.
"""

from dataclasses import dataclass

import h11

DEFAULT_CHUNK = 65536


class BodyTooLargeError(Exception):
    """The message body exceeded the configured cap."""


@dataclass
class HttpRequest:
    method: str
    target: str
    http_version: str
    raw_headers: list[tuple[bytes, bytes]]
    body: bytes

    @property
    def headers(self) -> dict[str, str]:
        """Lowercase header name -> decoded value (first occurrence wins)."""
        result: dict[str, str] = {}
        for key, value in self.raw_headers:
            name = key.decode("ascii").lower()
            if name not in result:
                result[name] = value.decode("latin-1")
        return result

    def header(self, name: str) -> bytes | None:
        """First header matching name (case-insensitive), or None."""
        lowered = name.lower().encode()
        for key, value in self.raw_headers:
            if key.lower() == lowered:
                return value
        return None


async def read_request(
    reader: asyncio.StreamReader, max_body_bytes: int
) -> HttpRequest | None:
    """Read one HTTP/1.1 request.

    Returns None if the peer closed cleanly without sending a request.
    """
    conn = h11.Connection(our_role=h11.SERVER)
    request: h11.Request | None = None
    body = bytearray()

    while True:
        event = conn.next_event()
        if event is h11.NEED_DATA:
            data = await reader.read(DEFAULT_CHUNK)
            if not data:
                if request is None:
                    return None
                raise h11.RemoteProtocolError("connection closed mid-request")
            conn.receive_data(data)
            continue
        if isinstance(event, h11.Request):
            request = event
        elif isinstance(event, h11.Data):
            body += event.data
            if len(body) > max_body_bytes:
                raise BodyTooLargeError
        elif isinstance(event, h11.EndOfMessage):
            if request is None:
                raise h11.RemoteProtocolError("end of message without request")
            return HttpRequest(
                method=request.method.decode("ascii"),
                target=request.target.decode("ascii"),
                http_version=request.http_version.decode("ascii"),
                raw_headers=list(request.headers),
                body=bytes(body),
            )


async def read_response(
    reader: asyncio.StreamReader, max_body_bytes: int
) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    """Read one HTTP/1.1 response from an upstream connection.

    Returns (status_code, headers-as-lowercased-tuples, body). Header
    duplicates are preserved (e.g. multiple Set-Cookie).
    """
    conn = h11.Connection(our_role=h11.CLIENT)
    response: h11.Response | None = None
    headers: list[tuple[bytes, bytes]] = []
    body = bytearray()

    while True:
        event = conn.next_event()
        if event is h11.NEED_DATA:
            data = await reader.read(DEFAULT_CHUNK)
            if not data:
                # EOF: let h11 finish read-until-close framing before
                # declaring the connection broken.
                conn.receive_data(b"")
                event = conn.next_event()
                if isinstance(event, h11.EndOfMessage) and response is not None:
                    return response.status_code, headers, bytes(body)
                raise h11.RemoteProtocolError("connection closed mid-response")
            conn.receive_data(data)
            continue
        if isinstance(event, h11.Response):
            response = event
            headers = [(k.lower(), v) for k, v in event.headers]
        elif isinstance(event, h11.Data):
            body += event.data
            if len(body) > max_body_bytes:
                raise BodyTooLargeError
        elif isinstance(event, h11.EndOfMessage):
            if response is None:
                raise h11.RemoteProtocolError("end of message without response")
            return response.status_code, headers, bytes(body)


def _ensure_framing(
    headers: list[tuple[bytes, bytes]], body: bytes
) -> list[tuple[bytes, bytes]]:
    """Append Content-Length when a body is sent but no framing is declared."""
    if not body:
        return headers
    lowered = {k.lower() for k, _ in headers}
    if b"content-length" in lowered or b"transfer-encoding" in lowered:
        return headers
    return list(headers) + [(b"content-length", str(len(body)).encode("ascii"))]


def serialize_request(
    method: str, target: str, headers: list[tuple[bytes, bytes]], body: bytes
) -> bytes:
    """Serialize one complete HTTP/1.1 client request (headers + body)."""
    conn = h11.Connection(our_role=h11.CLIENT)
    out = conn.send(
        h11.Request(
            method=method,
            target=target.encode("ascii"),
            headers=_ensure_framing(headers, body),
        )
    )
    if body:
        out += conn.send(h11.Data(data=body))
    out += conn.send(h11.EndOfMessage())
    return out


def serialize_response(
    status_code: int,
    headers: list[tuple[bytes, bytes]],
    body: bytes,
    reason: bytes | str = b"",
) -> bytes:
    """Serialize one complete HTTP/1.1 server response (headers + body)."""
    conn = h11.Connection(our_role=h11.SERVER)
    out = conn.send(
        h11.Response(
            status_code=status_code,
            headers=_ensure_framing(headers, body),
            reason=reason,
        )
    )
    if body:
        out += conn.send(h11.Data(data=body))
    out += conn.send(h11.EndOfMessage())
    return out
