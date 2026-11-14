"""Shared test fixtures for load balancer tests (Phase 2).

FakeBackend is a real TCP server speaking HTTP/1.1 (via h11 through the
production serialize_response helper), used as a hermetic upstream for
proxy tests. It records every request it receives.
"""

import asyncio
import sys

import pytest

from load_balancer.http_connection import serialize_response

# Python 3.15 on Windows: the default ProactorEventLoop raises
# AttributeError('_stop_serving') when test servers close at teardown.
# The selector loop is the standard workaround (uvicorn does the same on
# Windows); the proxy code is loop-agnostic.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class FakeBackend:
    def __init__(self, response_body: bytes = b'{"backend_id": "stub"}', status: int = 200):
        self.response_body = response_body
        self.status = status
        self.requests: list[dict] = []
        self._server: asyncio.AbstractServer | None = None
        self.host = "127.0.0.1"
        self.port = 0

    async def start(self) -> "FakeBackend":
        self._server = await asyncio.start_server(self._handle, self.host, 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def close(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = await reader.read(65536)
                if not chunk:
                    return
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            method, target, _version = lines[0].split(b" ", 2)
            headers = {}
            for line in lines[1:]:
                name, _, value = line.partition(b":")
                headers[name.strip().lower().decode()] = value.strip().decode()
            body = b""
            length = int(headers.get("content-length", "0"))
            while len(rest) < length:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                rest += chunk
            body = rest[:length]
            self.requests.append(
                {
                    "method": method.decode(),
                    "target": target.decode(),
                    "headers": headers,
                    "body": body,
                }
            )
            raw = serialize_response(
                self.status,
                [(b"content-type", b"application/json"), (b"x-backend", b"stub")],
                self.response_body,
            )
            writer.write(raw)
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


@pytest.fixture
async def fake_backend_factory():
    started = []

    async def make(**kwargs) -> FakeBackend:
        backend = await FakeBackend(**kwargs).start()
        started.append(backend)
        return backend

    yield make
    for backend in started:
        await backend.close()
