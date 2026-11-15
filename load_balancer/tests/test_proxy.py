"""Task 3 (Phase 2): the asyncio+h11 reverse proxy forwarding engine."""

import asyncio

import h11
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
        return status, resp_body, {k.decode(): v.decode() for k, v in headers}
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


class TestPersistentClientConnections:
    """Phase 3: multiple sequential requests over one client TCP connection."""

    async def test_two_sequential_requests_on_one_connection_reach_two_backends(
        self, fake_backend_factory
    ):
        b1 = await fake_backend_factory(response_body=b"one")
        b2 = await fake_backend_factory(response_body=b"two")
        registry = registry_of(b1, b2)
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        async with await proxy.start():
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            try:
                writer.write(serialize_request("GET", "/", [(b"host", b"t")], b""))
                await writer.drain()
                s1, _, body1 = await asyncio.wait_for(read_response(reader, 1 << 20), 5)
                assert s1 == 200 and body1 == b"one"
                # second request on the SAME socket
                writer.write(serialize_request("GET", "/", [(b"host", b"t")], b""))
                await writer.drain()
                s2, _, body2 = await asyncio.wait_for(read_response(reader, 1 << 20), 5)
                assert s2 == 200 and body2 == b"two"
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass
            assert proxy.counters["requests_total"] == 2
            assert proxy.counters["connections_total"] == 1

    async def test_connection_close_closes_after_response(self, fake_backend_factory):
        backend = await fake_backend_factory()
        registry = registry_of(backend)
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        async with await proxy.start():
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            try:
                writer.write(
                    serialize_request(
                        "GET", "/", [(b"host", b"t"), (b"connection", b"close")], b""
                    )
                )
                await writer.drain()
                status, _, _ = await asyncio.wait_for(read_response(reader, 1 << 20), 5)
                assert status == 200
                # server must close: next read is EOF
                tail = await asyncio.wait_for(reader.read(), 5)
                assert tail == b""
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass

    async def test_backend_counters_release_between_keep_alive_requests(
        self, fake_backend_factory
    ):
        backend = await fake_backend_factory()
        registry = registry_of(backend)
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        async with await proxy.start():
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            try:
                for _ in range(3):
                    writer.write(serialize_request("GET", "/", [(b"host", b"t")], b""))
                    await writer.drain()
                    await asyncio.wait_for(read_response(reader, 1 << 20), 5)
                    assert registry.backends()[0].active_requests == 0
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass


class TestReviewFixes:
    """Phase 3 final review fix pass (review 8ec257d..318e036)."""

    async def test_forward_status_zero_does_not_crash_handler(
        self, fake_backend_factory, monkeypatch
    ):
        """_forward returns status 0 when the client went away mid-flight;
        the connection handler must close silently, not raise."""
        backend = await fake_backend_factory()
        registry = registry_of(backend)
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        async with await proxy.start():
            async def gone(request, backend):
                return 0, [], b""

            monkeypatch.setattr(proxy, "_forward", gone)
            recorded = []
            loop = asyncio.get_running_loop()

            def record(loop, context):
                recorded.append(context.get("exception", context.get("message")))

            loop.set_exception_handler(record)
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
                try:
                    writer.write(serialize_request("GET", "/", [(b"host", b"t")], b""))
                    await writer.drain()
                    tail = await asyncio.wait_for(reader.read(), 5)
                    assert tail == b""  # silent close: no response bytes
                finally:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except (ConnectionError, OSError):
                        pass
                await asyncio.sleep(0.2)
                protocol_errors = [
                    e for e in recorded if isinstance(e, h11.LocalProtocolError)
                ]
                assert protocol_errors == []
            finally:
                loop.set_exception_handler(None)

    def test_write_error_is_bounded_not_shadowed(self):
        """The bounded _write_error (drains via _drain_client with
        write_timeout) must not be shadowed by an older unbounded one."""
        import inspect

        from load_balancer import proxy as proxy_mod

        source = inspect.getsource(proxy_mod.ProxyServer._write_error)
        assert "_drain_client" in source
        assert "await writer.drain()" not in source

    async def test_malformed_request_after_good_request_400_then_close(
        self, fake_backend_factory
    ):
        backend = await fake_backend_factory()
        registry = registry_of(backend)
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        async with await proxy.start():
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            try:
                writer.write(serialize_request("GET", "/", [(b"host", b"t")], b""))
                await writer.drain()
                status, _, _ = await asyncio.wait_for(read_response(reader, 1 << 20), 5)
                assert status == 200
                writer.write(b"GARBAGE\r\n\r\n")
                await writer.drain()
                status, _, _ = await asyncio.wait_for(read_response(reader, 1 << 20), 5)
                assert status == 400
                tail = await asyncio.wait_for(reader.read(), 5)
                assert tail == b""  # then the connection closes
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass

    async def test_oversized_client_body_413_then_close(self, fake_backend_factory):
        backend = await fake_backend_factory()
        registry = registry_of(backend)
        proxy = ProxyServer(
            registry=registry, strategy=RoundRobinStrategy(), max_body_bytes=64
        )
        async with await proxy.start():
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            try:
                body = b"x" * 128
                writer.write(
                    serialize_request(
                        "POST", "/", [(b"host", b"t"), (b"content-length", b"128")], body
                    )
                )
                await writer.drain()
                status, _, resp_body = await asyncio.wait_for(
                    read_response(reader, 1 << 20), 5
                )
                assert status == 413
                assert resp_body  # error body present, valid framing
                tail = await asyncio.wait_for(reader.read(), 5)
                assert tail == b""  # connection closed after 413
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass

    async def test_connection_continues_serving_after_502(self, fake_backend_factory):
        backend = await fake_backend_factory()
        registry = registry_of(backend)
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        async with await proxy.start():
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            try:
                writer.write(serialize_request("GET", "/", [(b"host", b"t")], b""))
                await writer.drain()
                status, _, _ = await asyncio.wait_for(read_response(reader, 1 << 20), 5)
                assert status == 200
                await backend.close()  # upstream dies between requests
                for _ in range(2):
                    writer.write(serialize_request("GET", "/", [(b"host", b"t")], b""))
                    await writer.drain()
                    status, resp_body, _ = await asyncio.wait_for(
                        read_response(reader, 1 << 20), 5
                    )
                    assert status == 502  # same client connection still serving
                    assert resp_body
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass

    async def test_truncated_request_body_400_then_close(self, fake_backend_factory):
        """EOF while a request body is still pending: protocol error, 400."""
        backend = await fake_backend_factory()
        registry = registry_of(backend)
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        async with await proxy.start():
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            try:
                writer.write(b"POST / HTTP/1.1\r\nhost: t\r\ncontent-length: 32\r\n\r\nonly")
                await writer.drain()
                writer.write_eof()  # client half-closes mid-body
                status, _, _ = await asyncio.wait_for(read_response(reader, 1 << 20), 5)
                assert status == 400
                tail = await asyncio.wait_for(reader.read(), 5)
                assert tail == b""
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass


class TestStrategyIntegration:
    """Phase 4 Task 4: strategies driven through the running proxy."""

    async def test_least_active_routes_around_in_flight_request(
        self, fake_backend_factory
    ):
        from load_balancer.algorithms.least_active import LeastActiveRequestsStrategy

        slow = await fake_backend_factory(response_body=b"slow", delay_ms=400)
        idle = await fake_backend_factory(response_body=b"idle")
        registry = registry_of(slow, idle)
        proxy = ProxyServer(
            registry=registry, strategy=LeastActiveRequestsStrategy()
        )
        async with await proxy.start():
            # Request 1: ties broken toward the first backend (slow), and it
            # holds slow.active_requests at 1 while in flight.
            r1, w1 = await asyncio.open_connection("127.0.0.1", proxy.port)
            w1.write(serialize_request("GET", "/", [(b"host", b"t")], b""))
            await w1.drain()
            await asyncio.sleep(0.1)  # let it be assigned to `slow`
            backend_state = {b.backend_id: b for b in registry.backends()}
            assert backend_state["backend-1"].active_requests == 1
            # Request 2 on a fresh connection must avoid the busy backend.
            status2, body2, _ = await get_through(proxy.port)
            assert body2 == b"idle"
            # Request 1 completes from the slow backend.
            status1, _, body1 = await asyncio.wait_for(
                read_response(r1, 1 << 20), 5
            )
            assert body1 == b"slow"
            w1.close()
            try:
                await w1.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def test_least_active_returns_to_released_backend(
        self, fake_backend_factory
    ):
        from load_balancer.algorithms.least_active import LeastActiveRequestsStrategy

        b1 = await fake_backend_factory(response_body=b"one")
        b2 = await fake_backend_factory(response_body=b"two")
        registry = registry_of(b1, b2)
        proxy = ProxyServer(
            registry=registry, strategy=LeastActiveRequestsStrategy()
        )
        async with await proxy.start():
            # With everything idle, requests rotate over the tied pair.
            first = await get_through(proxy.port)
            second = await get_through(proxy.port)
            assert {first[1], second[1]} == {b"one", b"two"}

    async def test_ip_hash_is_deterministic_per_client_ip(
        self, fake_backend_factory
    ):
        from load_balancer.algorithms.ip_hash import IpHashStrategy

        b1 = await fake_backend_factory(response_body=b"one")
        b2 = await fake_backend_factory(response_body=b"two")
        registry = registry_of(b1, b2)
        proxy = ProxyServer(registry=registry, strategy=IpHashStrategy())
        async with await proxy.start():
            bodies = []
            for _ in range(5):  # 5 fresh connections, same peer IP 127.0.0.1
                status, body, _ = await get_through(proxy.port)
                bodies.append(body)
            assert len(set(bodies)) == 1  # every connection hit the same backend

    async def test_round_robin_alternates_on_one_keep_alive_connection(
        self, fake_backend_factory
    ):
        b1 = await fake_backend_factory(response_body=b"one")
        b2 = await fake_backend_factory(response_body=b"two")
        registry = registry_of(b1, b2)
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        async with await proxy.start():
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            try:
                bodies = []
                for _ in range(4):
                    writer.write(serialize_request("GET", "/", [(b"host", b"t")], b""))
                    await writer.drain()
                    _, _, body = await asyncio.wait_for(
                        read_response(reader, 1 << 20), 5
                    )
                    bodies.append(body)
                assert bodies == [b"one", b"two", b"one", b"two"]
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass


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

    async def test_response_hop_by_hop_headers_are_stripped(self, fake_backend_factory):
        backend = await fake_backend_factory(
            response_headers=[
                (b"content-type", b"text/plain"),
                (b"connection", b"close, keep-alive-secret"),
                (b"keep-alive", b"timeout=5"),
                (b"keep-alive-secret", b"must-not-forward"),
                (b"x-visible", b"yes"),
            ]
        )
        registry = registry_of(backend)
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        async with await proxy.start():
            status, body, headers = await get_through(proxy.port)
            assert status == 200
            lowered = {k.lower(): v for k, v in headers.items()}
            assert lowered.get("connection") is None
            assert lowered.get("keep-alive") is None
            assert lowered.get("keep-alive-secret") is None
            assert lowered.get("x-visible") == "yes"

    async def test_oversized_upstream_response_returns_502(self, fake_backend_factory):
        big = await fake_backend_factory(response_body=b"x" * 4096)
        registry = registry_of(big)
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy(), max_body_bytes=1024)
        async with await proxy.start():
            status, body, _ = await get_through(proxy.port)
            assert status == 502
            assert b"too large" in body.lower()

    async def test_active_requests_released_on_upstream_failure(self, fake_backend_factory):
        backend = await fake_backend_factory()
        registry = registry_of(backend)
        registry.backends()[0].port = 1
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        async with await proxy.start():
            await get_through(proxy.port)
            assert registry.backends()[0].active_requests == 0
