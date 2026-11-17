"""Phase 6 Task 1: aggregate counters (per-status) + per-request latency samples.

The proxy already logs duration_ms per request with a monotonic clock
(time.perf_counter); this task makes the data machine-readable:
- counters["status_counts"]: how many responses of each status
- proxy.latencies_ms: one float (ms) per answered request, for exact
  percentile math at export time.
"""

import asyncio

from load_balancer.algorithms.round_robin import RoundRobinStrategy
from load_balancer.health.registry import BackendRegistry
from load_balancer.models import Backend
from load_balancer.proxy import ProxyServer

from .conftest import FakeBackend
from .test_proxy import get_through, registry_of


class TestStatusCounters:
    async def test_200s_counted_per_status(self, fake_backend_factory):
        fb = await fake_backend_factory()
        proxy = ProxyServer(registry=registry_of(fb), strategy=RoundRobinStrategy())
        async with await proxy.start():
            for _ in range(3):
                status, _, _ = await get_through(proxy.port)
                assert status == 200
        assert proxy.counters["status_counts"][200] == 3
        assert proxy.counters["requests_total"] == 3

    async def test_503_no_backend_counted(self):
        dead = Backend(backend_id="dead", host="127.0.0.1", port=1)
        registry = BackendRegistry([dead])
        registry.mark_down(dead)  # no healthy backends at all
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        async with await proxy.start():
            status, _, _ = await get_through(proxy.port)
            assert status == 503
        assert proxy.counters["status_counts"][503] == 1

    async def test_502_dead_upstream_counted(self):
        dead = Backend(backend_id="dead", host="127.0.0.1", port=1)
        proxy = ProxyServer(registry=BackendRegistry([dead]), strategy=RoundRobinStrategy())
        async with await proxy.start():
            status, _, _ = await get_through(proxy.port)
            assert status == 502
        assert proxy.counters["status_counts"][502] == 1

    async def test_status_counts_accumulate_across_statuses(self, fake_backend_factory):
        fb = await fake_backend_factory()
        dead = Backend(backend_id="dead", host="127.0.0.1", port=1)
        backends = [
            Backend(backend_id="alive", host=fb.host, port=fb.port),
            dead,
        ]
        proxy = ProxyServer(registry=BackendRegistry(backends), strategy=RoundRobinStrategy())
        async with await proxy.start():
            statuses = []
            for _ in range(4):
                status, _, _ = await get_through(proxy.port)
                statuses.append(status)
        # round robin over both: some 200s, some 502s
        assert statuses.count(200) >= 1
        assert statuses.count(502) >= 1
        assert proxy.counters["status_counts"][200] == statuses.count(200)
        assert proxy.counters["status_counts"][502] == statuses.count(502)


class TestLatencySamples:
    async def test_one_sample_per_answered_request(self, fake_backend_factory):
        fb = await fake_backend_factory()
        proxy = ProxyServer(registry=registry_of(fb), strategy=RoundRobinStrategy())
        async with await proxy.start():
            for _ in range(5):
                await get_through(proxy.port)
        assert len(proxy.latencies_ms) == proxy.counters["requests_total"] == 5
        assert all(isinstance(v, float) and v >= 0 for v in proxy.latencies_ms)

    async def test_errors_produce_samples_too(self):
        dead = Backend(backend_id="dead", host="127.0.0.1", port=1)
        proxy = ProxyServer(registry=BackendRegistry([dead]), strategy=RoundRobinStrategy())
        async with await proxy.start():
            await get_through(proxy.port)  # 502
        assert len(proxy.latencies_ms) == 1

    async def test_sample_reflects_backend_delay(self, fake_backend_factory):
        fb = await fake_backend_factory(delay_ms=120)
        proxy = ProxyServer(registry=registry_of(fb), strategy=RoundRobinStrategy())
        async with await proxy.start():
            await get_through(proxy.port)
        assert proxy.latencies_ms[0] >= 110  # must include the upstream delay
