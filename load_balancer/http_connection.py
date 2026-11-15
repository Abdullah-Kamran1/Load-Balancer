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
    """Read one HTTP/1.1 request (single-request semantics).

    Returns None if the peer closed cleanly without sending a request.
    """
    http = HttpStreamReader(reader, max_body_bytes)
    return await http.read_next_request()


class HttpStreamReader:
    """Persistent server-side HTTP/1.1 reader (Phase 3 keep-alive).

    Owns the h11 state machine for one client TCP connection, so multiple
    sequential requests can be parsed from the same stream. h11 owns all
    framing arithmetic: bodies (Content-Length or chunked) are fully
    consumed before the next request is returned.

    After each successful read, ``should_close`` says whether the client
    wants the connection closed (Connection: close, or HTTP/1.0 without
    keep-alive). A clean EOF between requests yields None.
    """

    def __init__(self, reader: asyncio.StreamReader, max_body_bytes: int) -> None:
        self._reader = reader
        self._max_body_bytes = max_body_bytes
        self._conn = h11.Connection(our_role=h11.SERVER)
        self._eof = False
        self.should_close = False

    async def read_next_request(self) -> HttpRequest | None:
        request: h11.Request | None = None
        body = bytearray()

        while True:
            event = self._conn.next_event()
            if event is h11.NEED_DATA:
                if self._eof:
                    # EOF already signalled into h11 and still incomplete.
                    if request is None:
                        return None
                    raise h11.RemoteProtocolError("connection closed mid-request")
                data = await self._reader.read(DEFAULT_CHUNK)
                if not data:
                    # Peer closed. While idle (request is None) this is a
                    # clean end of a keep-alive connection; mid-request it is
                    # a protocol error. We do not call next_event() after EOF
                    # here: at an idle state h11 answers with PAUSED forever,
                    # which would spin this loop without ever awaiting.
                    self._eof = True
                    if request is None:
                        return None
                    raise h11.RemoteProtocolError("connection closed mid-request")
                self._conn.receive_data(data)
                continue
            if isinstance(event, h11.Request):
                request = event
            elif isinstance(event, h11.Data):
                body += event.data
                if len(body) > self._max_body_bytes:
                    raise BodyTooLargeError
            elif isinstance(event, h11.EndOfMessage):
                if request is None:
                    # EOF while idle: the client closed a keep-alive connection.
                    return None
                self.should_close = self._conn.their_state is h11.MUST_CLOSE
                return HttpRequest(
                    method=request.method.decode("ascii"),
                    target=request.target.decode("ascii"),
                    http_version=request.http_version.decode("ascii"),
                    raw_headers=list(request.headers),
                    body=bytes(body),
                )
            elif event is h11.PAUSED:
                # Defensive guard against the PAUSED busy-spin bug class:
                # at this point PAUSED is never a legitimate answer (a read
                # cycle always ends by returning or serializing first).
                raise h11.LocalProtocolError("unexpected PAUSED while reading request")

    def serialize_response(
        self,
        status_code: int,
        headers: list[tuple[bytes, bytes]],
        body: bytes,
    ) -> bytes:
        """Serialize the response through this connection's h11 state machine.

        The same h11.Connection that parsed the request must also send the
        response: only then does the state machine return from SEND_RESPONSE
        to DONE so the next request can be parsed (keep-alive). When the
        client asked to close, Connection: close is injected and the state
        machine transitions to MUST_CLOSE/CLOSED.
        """
        out_headers = list(headers)
        lowered = {k.lower() for k, _ in out_headers}
        if self.should_close and b"connection" not in lowered:
            out_headers.append((b"connection", b"close"))
        out = self._conn.send(
            h11.Response(
                status_code=status_code,
                headers=_ensure_framing(out_headers, body),
            )
        )
        if body:
            out += self._conn.send(h11.Data(data=body))
        out += self._conn.send(h11.EndOfMessage())
        if not self.should_close and self._conn.our_state is h11.DONE:
            # h11 keeps returning PAUSED until the next cycle is armed;
            # only a keep-alive connection continues.
            self._conn.start_next_cycle()
        return out


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
