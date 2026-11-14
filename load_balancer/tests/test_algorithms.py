"""Task 1 (Phase 2): backend model, registry, and round-robin selection."""

import pytest

from load_balancer.models import Backend
from load_balancer.health.registry import BackendRegistry
from load_balancer.algorithms.round_robin import RoundRobinStrategy


def make_backends(*ids):
    return [Backend(backend_id=i, host="127.0.0.1", port=8000 + n) for n, i in enumerate(ids, start=1)]


class TestBackendModel:
    def test_backend_defaults_are_healthy_and_idle(self):
        b = Backend(backend_id="backend-1", host="127.0.0.1", port=8001)
        assert b.healthy is True
        assert b.active_requests == 0
        assert b.total_requests == 0


class TestRegistry:
    def test_healthy_backends_returns_only_healthy(self):
        backends = make_backends("a", "b", "c")
        backends[1].healthy = False
        registry = BackendRegistry(backends)
        assert [b.backend_id for b in registry.healthy_backends()] == ["a", "c"]

    def test_mark_down_and_mark_up_flip_state(self):
        backends = make_backends("a", "b")
        registry = BackendRegistry(backends)
        registry.mark_down(backends[0])
        assert [b.backend_id for b in registry.healthy_backends()] == ["b"]
        registry.mark_up(backends[0])
        assert [b.backend_id for b in registry.healthy_backends()] == ["a", "b"]


class TestRoundRobin:
    def test_cycles_evenly_through_healthy_backends(self):
        backends = make_backends("a", "b", "c")
        strategy = RoundRobinStrategy()
        picks = [strategy.select_backend("10.0.0.1", backends) for _ in range(6)]
        assert [b.backend_id for b in picks] == ["a", "b", "c", "a", "b", "c"]

    def test_skips_unhealthy_backends(self):
        backends = make_backends("a", "b", "c")
        backends[1].healthy = False  # b down
        strategy = RoundRobinStrategy()
        picks = [strategy.select_backend("10.0.0.1", backends) for _ in range(4)]
        assert [b.backend_id for b in picks] == ["a", "c", "a", "c"]

    def test_returns_none_when_no_healthy_backends(self):
        backends = make_backends("a", "b")
        backends[0].healthy = False
        backends[1].healthy = False
        strategy = RoundRobinStrategy()
        assert strategy.select_backend("10.0.0.1", backends) is None

    def test_accepts_client_ip_argument(self):
        """Selector interface takes (client_ip, healthy_backends) per the plan."""
        backends = make_backends("a")
        strategy = RoundRobinStrategy()
        assert strategy.select_backend("192.168.1.7", backends).backend_id == "a"
