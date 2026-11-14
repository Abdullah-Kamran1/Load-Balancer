"""Task 3 (Phase 2): the asyncio+h11 reverse proxy forwarding engine."""

import asyncio

import pytest

from load_balancer.algorithms.round_robin import RoundRobinStrategy
from load_balancer.health.registry import BackendRegistry
from load_balancer.models import Backend
from load_balancer.proxy import ProxyServer

from load_balancer.http_connection import read_response, serialize_request


def registry_of(*fake_backends, ids=None):
    backends = [
        Backend(backend_id=ids[i] if ids else f"backend-{i + 1}", host=b.host, port=b.port)
        for i, b in enumerate(fake_backends)
    ]
    return BackendRegistry(backends)


async def get_through(proxy_port: int, target: str = "/", method: str = "GET", body: bytes | None = None):
    """One fresh client connection per request (Phase 2 semantics).

    Asyncio-native so it never blocks the loop the proxy runs on.
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    writer.write(
        serialize_request(method, target, [(b"host", b"testclient")], body or b"")
    )
    await writer.drain()
    try:
        status, headers, resp_body = await asyncio.wait_for(
            read_response(reader, 10 * 1024 * 1024), 10
        )
        return status, resp_body, {k.decode(): v.decode() for k, v in headers.items()}
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


class TestForwarding:
    async def test_forwards_request_and_streams_response_back(self, fake_backend_factory):
        backend = await fake_backend_factory(response_body=b'{"backend_id": "stub"}')
        registry = registry_of(backend)
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        async with await proxy.start():
            status, body, headers = await get_through(proxy.port, "/work?delay_ms=5")
            assert status == 200
            assert body == b'{"backend_id": "stub"}'
            assert headers.get("x-backend") == "stub"
            # the stub saw the original request line
            assert backend.requests[0]["method"] == "GET"
            assert backend.requests[0]["target"] == "/work?delay_ms=5"

    async def test_forwards_post_body(self, fake_backend_factory):
        backend = await fake_backend_factory()
        registry = registry_of(backend)
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        async with await proxy.start():
            status, body, _ = await get_through(proxy.port, "/submit", method="POST", body=b"payload")
            assert status == 200
            assert backend.requests[0]["method"] == "POST"
            assert backend.requests[0]["body"] == b"payload"

    async def test_round_robin_distributes_across_backends(self, fake_backend_factory):
        b1 = await fake_backend_factory(response_body=b"one")
        b2 = await fake_backend_factory(response_body=b"two")
        registry = registry_of(b1, b2)
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        async with await proxy.start():
            bodies = []
            for _ in range(4):
                _, body, _ = await get_through(proxy.port)
                bodies.append(body)
            assert bodies == [b"one", b"two", b"one", b"two"]


class TestCounterAccounting:
    async def test_backend_counters_increment_and_release(self, fake_backend_factory):
        backend = await fake_backend_factory()
        registry = registry_of(backend)
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        async with await proxy.start():
            for _ in range(3):
                await get_through(proxy.port)
            assert registry.backends()[0].total_requests == 3
            assert registry.backends()[0].active_requests == 0
            assert proxy.counters["requests_total"] == 3


class TestFailurePaths:
    async def test_503_when_no_healthy_backend(self, fake_backend_factory):
        backend = await fake_backend_factory()
        registry = registry_of(backend)
        registry.mark_down(registry.backends()[0])
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        async with await proxy.start():
            status, body, _ = await get_through(proxy.port)
            assert status == 503
            assert b"no healthy backend" in body.lower()

    async def test_502_when_upstream_unreachable_and_proxy_survives(self, fake_backend_factory):
        backend = await fake_backend_factory()
        registry = registry_of(backend)
        # point at a port with no listener
        registry.backends()[0].port = 1  # port 1: connection refused
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        async with await proxy.start():
            status, _, _ = await get_through(proxy.port)
            assert status == 502
            # proxy still serves subsequent requests once backend is fixed
            registry.backends()[0].port = backend.port
            registry.backends()[0].healthy = True
            status, body, _ = await get_through(proxy.port)
            assert status == 200
            assert body == backend.response_body

    async def test_active_requests_released_on_upstream_failure(self, fake_backend_factory):
        backend = await fake_backend_factory()
        registry = registry_of(backend)
        registry.backends()[0].port = 1
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        async with await proxy.start():
            await get_through(proxy.port)
            assert registry.backends()[0].active_requests == 0
