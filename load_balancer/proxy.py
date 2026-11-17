"""The asyncio + h11 L7 reverse proxy.

Phase 3 semantics (deliberate, from the plan):
- persistent client connections: each accepted connection is served by a
  per-connection request loop; one HttpStreamReader owns the connection's
  h11 state machine for parsing requests and serializing responses.
- the connection is closed when the client asks (Connection: close /
  HTTP/1.0), on idle/stalled reads, and after 413/400 (mid-request h11
  state is unusable); 502/503 answers keep the connection alive.
- the upstream connection is opened per request and closed after it
  (upstream pooling is a later plan extension).
- response bodies are read fully (bounded by max_body_bytes) and then
  written back; streaming relay remains a later refinement.

Error contract:
- no healthy backend                  -> 503
- upstream connect/timeout/protocol   -> 502
- oversized client body               -> 413
- malformed request / clean close     -> connection closed, proxy lives on

Every failure path releases sockets and backend counters (finally blocks).
"""

import asyncio
import json
import logging
import time

import h11

from .algorithms.base import LoadBalancingStrategy
from .health.registry import BackendRegistry
from .http_connection import (
    BodyTooLargeError,
    HttpStreamReader,
    HttpRequest,
    read_response,
    serialize_request,
    serialize_response,
)

logger = logging.getLogger("lb.proxy")

# Hop-by-hop headers (never forwarded end-to-end).
HOP_BY_HOP = {
    b"connection",
    b"keep-alive",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"te",
    b"trailer",
    b"transfer-encoding",
    b"upgrade",
}


def filter_headers(
    headers: list[tuple[bytes, bytes]],
    connection_named: set[bytes],
    drop_host: bool = False,
) -> list[tuple[bytes, bytes]]:
    """Drop hop-by-hop headers, headers named by the message's Connection
    header, and optionally Host."""
    out = []
    for name, value in headers:
        lowered = name.lower()
        if lowered in HOP_BY_HOP or lowered in connection_named:
            continue
        if drop_host and lowered == b"host":
            continue
        out.append((name, value))
    return out


def connection_named_by(header_value: bytes | None) -> set[bytes]:
    """Header names listed in a Connection header (lowercased)."""
    named = set()
    if header_value:
        for token in header_value.split(b","):
            name = token.strip().lower()
            if name:
                named.add(name)
    return named


def upstream_headers(request: HttpRequest, backend_netloc: str) -> list[tuple[bytes, bytes]]:
    """Build the headers to send upstream: strip hop-by-hop headers and any
    header named by the client's Connection header; rewrite Host."""
    out = filter_headers(request.raw_headers, connection_named_by(request.header("connection")), drop_host=True)
    out.append((b"host", backend_netloc.encode("ascii")))
    return out


def downstream_headers(response_headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    """Strip hop-by-hop headers (and Connection-named ones) from upstream
    response headers before sending them to the client."""
    connection_header = next(
        (v for k, v in response_headers if k.lower() == b"connection"), None
    )
    return filter_headers(response_headers, connection_named_by(connection_header))


class ProxyServer:
    def __init__(
        self,
        registry: BackendRegistry,
        strategy: LoadBalancingStrategy,
        host: str = "127.0.0.1",
        port: int = 0,
        connect_timeout: float = 5.0,
        request_timeout: float = 30.0,
        write_timeout: float = 30.0,
        max_body_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        self._registry = registry
        self._strategy = strategy
        self._host = host
        self._port = port
        self._connect_timeout = connect_timeout
        self._request_timeout = request_timeout
        self._write_timeout = write_timeout
        self._max_body_bytes = max_body_bytes
        self._server: asyncio.AbstractServer | None = None
        self._request_seq = 0
        self.counters = {
            "requests_total": 0,
            "connections_total": 0,
            "responses_5xx": 0,
            "errors": 0,
            # Phase 6: exact per-status counts (200/400/413/502/503/...)
            "status_counts": {},
        }
        # Phase 6: one full-request-latency sample (ms, monotonic
        # time.perf_counter) per answered request, for percentile export.
        self.latencies_ms: list[float] = []

    @property
    def port(self) -> int:
        assert self._server is not None, "proxy not started"
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> "ProxyServer":
        self._server = await asyncio.start_server(self._handle_client, self._host, self._port)
        return self

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *exc):
        await self.close()

    def _record(self, status: int, duration_ms: float) -> None:
        """Book one answered request: exact status count + latency sample."""
        self.counters["status_counts"][status] = (
            self.counters["status_counts"].get(status, 0) + 1
        )
        self.latencies_ms.append(duration_ms)

    # ---- connection handling -------------------------------------------------

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        client_ip = peer[0] if peer else None
        self.counters["connections_total"] += 1
        # One HttpStreamReader owns this connection's h11 state machine for
        # its whole lifetime: request parsing, response serialization, and
        # keep-alive cycling all flow through it.
        http = HttpStreamReader(reader, self._max_body_bytes)

        try:
            while True:
                self._request_seq += 1
                request_id = self._request_seq
                start = time.perf_counter()
                status = None
                backend = None

                try:
                    request = await asyncio.wait_for(
                        http.read_next_request(), self._request_timeout
                    )
                except asyncio.TimeoutError:
                    # Idle or stalled read: close without a response (the
                    # h11 state may be mid-request and unusable).
                    break
                except BodyTooLargeError:
                    status = 413
                    await self._write_error(writer, 413, "request body too large")
                    self._record(status, round((time.perf_counter() - start) * 1000, 2))
                    break  # mid-request state: cannot continue this connection
                except (h11.RemoteProtocolError, ValueError):
                    status = 400
                    await self._write_error(writer, 400, "malformed request")
                    self._record(status, round((time.perf_counter() - start) * 1000, 2))
                    break  # mid-request state: cannot continue this connection

                if request is None:
                    break  # clean EOF: client closed a keep-alive connection

                self.counters["requests_total"] += 1

                backend = self._strategy.select_backend(
                    client_ip, self._registry.healthy_backends()
                )
                if backend is None:
                    status = 503
                    self.counters["responses_5xx"] += 1
                    writer.write(
                        http.serialize_response(
                            503,
                            [(b"content-type", b"application/json")],
                            self._error_body("no healthy backend available"),
                        )
                    )
                    await self._drain_client(writer)
                else:
                    status, headers, body = await self._forward(request, backend)
                    if status == 0:
                        # The client went away mid-flight; _forward already
                        # released everything and there is no client socket
                        # left to answer. h11 cannot serialize status 0.
                        break
                    writer.write(
                        http.serialize_response(status, downstream_headers(headers), body)
                    )
                    await self._drain_client(writer)

                duration_ms = round((time.perf_counter() - start) * 1000, 2)
                self._record(status, duration_ms)
                logger.info(
                    "request=%s client=%s backend=%s status=%s duration_ms=%s",
                    request_id,
                    client_ip,
                    backend.backend_id if backend else None,
                    status,
                    duration_ms,
                    extra={
                        "request": request_id,
                        "client": client_ip,
                        "backend": backend.backend_id if backend else None,
                        "status": status,
                        "duration_ms": duration_ms,
                    },
                )
                if http.should_close:
                    break
        except (ConnectionError, OSError):
            # Client went away; release everything via finally.
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _forward(
        self, request: HttpRequest, backend
    ) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
        """Forward one request to one backend; return its response.

        Does not touch the client socket: responses are serialized by the
        caller through the connection's h11 state machine. Failures return
        a synthetic 502 response instead of raising. Status 0 means the
        client went away mid-flight (nothing to answer); the caller closes
        the connection silently.
        """
        backend.active_requests += 1
        backend.total_requests += 1
        upstream_writer = None
        try:
            try:
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(*backend.address), self._connect_timeout
                )
            except (asyncio.TimeoutError, OSError):
                self.counters["responses_5xx"] += 1
                return 502, [], self._error_body(
                    f"backend {backend.backend_id} unreachable"
                )

            out = serialize_request(
                request.method,
                request.target,
                upstream_headers(request, backend.netloc),
                request.body,
            )
            upstream_writer.write(out)
            await upstream_writer.drain()

            try:
                status, headers, body = await asyncio.wait_for(
                    read_response(upstream_reader, self._max_body_bytes),
                    self._request_timeout,
                )
            except asyncio.TimeoutError:
                self.counters["responses_5xx"] += 1
                return 502, [], self._error_body(f"backend {backend.backend_id} timeout")
            except BodyTooLargeError:
                self.counters["responses_5xx"] += 1
                return 502, [], self._error_body(
                    f"backend {backend.backend_id} response too large"
                )
            except (h11.RemoteProtocolError, h11.LocalProtocolError, ConnectionError):
                self.counters["responses_5xx"] += 1
                return 502, [], self._error_body(
                    f"backend {backend.backend_id} bad response"
                )

            return status, headers, body
        except (ConnectionError, OSError):
            # Client went away mid-flight; nothing to report to it.
            self.counters["errors"] += 1
            return 0, [], b""
        finally:
            backend.active_requests -= 1
            if upstream_writer is not None:
                upstream_writer.close()
                try:
                    await upstream_writer.wait_closed()
                except (ConnectionError, OSError):
                    pass

    def _error_body(self, message: str) -> bytes:
        return json.dumps({"error": message}).encode()

    async def _write_error(self, writer: asyncio.StreamWriter, status: int, message: str) -> None:
        """Write an error response outside the keep-alive cycle (the h11
        request state is unusable), then the caller closes the connection."""
        try:
            writer.write(
                serialize_response(
                    status,
                    [(b"content-type", b"application/json")],
                    self._error_body(message),
                )
            )
            await self._drain_client(writer)
        except (ConnectionError, OSError):
            pass

    async def _drain_client(self, writer: asyncio.StreamWriter) -> None:
        """Drain the client write buffer, bounded by write_timeout.

        A client that stops reading (stalled socket buffer) must not hold
        the backend slot, the upstream socket, and this handler forever.
        The TimeoutError propagates to the caller's error handling, which
        releases everything in its finally blocks.
        """
        await asyncio.wait_for(writer.drain(), self._write_timeout)

