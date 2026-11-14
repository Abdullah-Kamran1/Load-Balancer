"""The asyncio + h11 L7 reverse proxy (Phase 2).

Phase 2 semantics (deliberate, from the plan):
- one HTTP request per client connection; the connection is closed after
  the response. Persistent connections are Phase 3.
- the upstream connection is opened per request and closed after it.
- response bodies are read fully (bounded by max_body_bytes) and then
  written back; streaming relay is a Phase 3 refinement.

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
    HttpRequest,
    read_request,
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


def upstream_headers(request: HttpRequest, backend_netloc: str) -> list[tuple[bytes, bytes]]:
    """Build the headers to send upstream: strip hop-by-hop headers and any
    header named by the client's Connection header; rewrite Host."""
    connection_named = set()
    conn_header = request.header("connection")
    if conn_header:
        for token in conn_header.split(b","):
            name = token.strip().lower()
            if name:
                connection_named.add(name)

    out = []
    for name, value in request.raw_headers:
        lowered = name.lower()
        if lowered in HOP_BY_HOP or lowered in connection_named:
            continue
        if lowered == b"host":
            continue
        out.append((name, value))
    out.append((b"host", backend_netloc.encode("ascii")))
    return out


class ProxyServer:
    def __init__(
        self,
        registry: BackendRegistry,
        strategy: LoadBalancingStrategy,
        host: str = "127.0.0.1",
        port: int = 0,
        connect_timeout: float = 5.0,
        request_timeout: float = 30.0,
        max_body_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        self._registry = registry
        self._strategy = strategy
        self._host = host
        self._port = port
        self._connect_timeout = connect_timeout
        self._request_timeout = request_timeout
        self._max_body_bytes = max_body_bytes
        self._server: asyncio.AbstractServer | None = None
        self._request_seq = 0
        self.counters = {"requests_total": 0, "responses_5xx": 0, "errors": 0}

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

    # ---- connection handling -------------------------------------------------

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._request_seq += 1
        request_id = self._request_seq
        self.counters["requests_total"] += 1
        start = time.perf_counter()
        peer = writer.get_extra_info("peername")
        client_ip = peer[0] if peer else None
        backend = None
        status = None

        try:
            try:
                request = await asyncio.wait_for(
                    read_request(reader, self._max_body_bytes), self._request_timeout
                )
            except asyncio.TimeoutError:
                request = None
                await self._write_error(writer, 408, "request timed out")
                status = 408
            except BodyTooLargeError:
                request = None
                await self._write_error(writer, 413, "request body too large")
                status = 413
            except (h11.RemoteProtocolError, ValueError):
                request = None
                await self._write_error(writer, 400, "malformed request")
                status = 400

            if request is not None:
                backend = self._strategy.select_backend(
                    client_ip, self._registry.healthy_backends()
                )
                if backend is None:
                    status = 503
                    self.counters["responses_5xx"] += 1
                    await self._write_error(writer, 503, "no healthy backend available")
                else:
                    status = await self._forward(request, backend, writer)
        except Exception:
            # Never let an unexpected error kill the connection loop.
            self.counters["errors"] += 1
            logger.exception("request %s: unhandled error", request_id)
            status = "crash-guarded"
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
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
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _forward(self, request: HttpRequest, backend, writer: asyncio.StreamWriter) -> int:
        """Forward one request to one backend; return the response status."""
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
                await self._write_error(writer, 502, f"backend {backend.backend_id} unreachable")
                return 502

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
                    read_response(upstream_reader, self._max_body_bytes), self._request_timeout
                )
            except asyncio.TimeoutError:
                self.counters["responses_5xx"] += 1
                await self._write_error(writer, 502, f"backend {backend.backend_id} timeout")
                return 502
            except (h11.RemoteProtocolError, ConnectionError):
                self.counters["responses_5xx"] += 1
                await self._write_error(writer, 502, f"backend {backend.backend_id} bad response")
                return 502

            writer.write(serialize_response(status, list(headers.items()), body))
            await writer.drain()
            return status
        except (ConnectionError, OSError):
            # Client went away mid-flight; nothing to report to it.
            self.counters["errors"] += 1
            return 0
        finally:
            backend.active_requests -= 1
            if upstream_writer is not None:
                upstream_writer.close()
                try:
                    await upstream_writer.wait_closed()
                except (ConnectionError, OSError):
                    pass

    async def _write_error(self, writer: asyncio.StreamWriter, status: int, message: str) -> None:
        try:
            body = json.dumps({"error": message}).encode()
            writer.write(
                serialize_response(
                    status,
                    [(b"content-type", b"application/json")],
                    body,
                )
            )
            await writer.drain()
        except (ConnectionError, OSError):
            pass
