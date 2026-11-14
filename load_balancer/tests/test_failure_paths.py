"""Task 4 (Phase 2): upstream timeout handling and structured log content."""

import asyncio
import logging

from load_balancer.http_connection import read_response, serialize_request
from load_balancer.algorithms.round_robin import RoundRobinStrategy
from load_balancer.health.registry import BackendRegistry
from load_balancer.models import Backend
from load_balancer.proxy import ProxyServer

from .test_proxy import get_through, registry_of
from .conftest import FakeBackend


class SlowBackend(FakeBackend):
    """A backend that accepts the request but answers after a delay."""

    def __init__(self, delay: float):
        super().__init__()
        self.delay = delay

    async def _handle(self, reader, writer):
        await asyncio.sleep(self.delay)
        await super()._handle(reader, writer)


class TestUpstreamTimeout:
    async def test_slow_backend_returns_502_and_proxy_survives(self, fake_backend_factory):
        slow = await SlowBackend(delay=1.0).start()
        fast = await fake_backend_factory(response_body=b"fast")
        # only the slow backend is healthy
        registry = registry_of(slow, fast)
        registry.backends()[1].healthy = False
        proxy = ProxyServer(
            registry=registry, strategy=RoundRobinStrategy(), request_timeout=0.2
        )
        async with await proxy.start():
            status, body, _ = await get_through(proxy.port)
            assert status == 502
            assert b"timeout" in body.lower()
            # proxy is still alive afterwards
            registry.backends()[1].healthy = True
            status, body, _ = await get_through(proxy.port)
            assert status == 200
            assert body == b"fast"
            assert registry.backends()[0].active_requests == 0
        await slow.close()


class TestErrorContractE2E:
    """End-to-end checks of the documented error contract through a live proxy."""

    async def test_oversized_client_body_returns_413(self, fake_backend_factory):
        backend = await fake_backend_factory()
        registry = registry_of(backend)
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy(), max_body_bytes=64)
        async with await proxy.start():
            status, body, _ = await get_through(
                proxy.port, "/submit", method="POST", body=b"z" * 128
            )
            assert status == 413
            assert b"too large" in body.lower()
            assert backend.requests == []  # nothing reached the backend

    async def test_malformed_request_line_returns_400(self, fake_backend_factory):
        backend = await fake_backend_factory()
        registry = registry_of(backend)
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        async with await proxy.start():
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(b"NOT-A-VALID-REQUEST-LINE\r\n\r\n")
            await writer.drain()
            status, _, _ = await asyncio.wait_for(read_response(reader, 4096), 10)
            assert status == 400
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


class TestWriteTimeout:
    async def test_stalled_client_releases_resources(self, fake_backend_factory):
        """A client that never reads its 1 MB response must not hold the
        backend slot and sockets forever."""
        big = await fake_backend_factory(response_body=b"x" * (1024 * 1024))
        registry = registry_of(big)
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy(), write_timeout=0.3)
        async with await proxy.start():
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(serialize_request("GET", "/", [(b"host", b"t")], b""))
            await writer.drain()
            await asyncio.sleep(1.0)  # > write_timeout; client reads nothing
            assert registry.backends()[0].active_requests == 0
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


class TestStructuredLogs:
    async def test_log_line_contains_request_id_client_backend_status_duration(
        self, fake_backend_factory, caplog
    ):
        backend = await fake_backend_factory()
        registry = registry_of(backend)
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        async with await proxy.start():
            with caplog.at_level(logging.INFO, logger="lb.proxy"):
                await get_through(proxy.port)

        assert len(caplog.records) == 1
        record = caplog.records[0]
        # structured fields arrive as logger kwargs -> record attributes
        assert hasattr(record, "request") and record.request >= 1
        assert record.client == "127.0.0.1"
        assert record.backend == "backend-1"
        assert record.status == 200
        assert isinstance(record.duration_ms, (int, float))
