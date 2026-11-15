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


class TestLeastActiveRequests:
    """Phase 4 Task 1: pick the healthy backend with the fewest in-flight
    proxied requests; deterministic round-robin tie-break among ties."""

    def test_picks_backend_with_smallest_active_count(self):
        from load_balancer.algorithms.least_active import LeastActiveRequestsStrategy

        backends = make_backends("a", "b", "c")
        backends[0].active_requests = 3
        backends[1].active_requests = 1
        backends[2].active_requests = 2
        strategy = LeastActiveRequestsStrategy()
        assert strategy.select_backend("10.0.0.1", backends).backend_id == "b"

    def test_tie_breaks_round_robin_among_tied_backends(self):
        from load_balancer.algorithms.least_active import LeastActiveRequestsStrategy

        backends = make_backends("a", "b", "c")
        for b in backends:
            b.active_requests = 0  # all tied at zero
        strategy = LeastActiveRequestsStrategy()
        picks = [strategy.select_backend("10.0.0.1", backends) for _ in range(6)]
        assert [b.backend_id for b in picks] == ["a", "b", "c", "a", "b", "c"]

    def test_tie_break_restarts_cycle_only_over_tied_set(self):
        from load_balancer.algorithms.least_active import LeastActiveRequestsStrategy

        backends = make_backends("a", "b")
        backends[0].active_requests = 5
        strategy = LeastActiveRequestsStrategy()
        assert strategy.select_backend("10.0.0.1", backends).backend_id == "b"
        # a and b now tie at 5; the rotation index persists across
        # selections, so the cycle continues from where it was —
        # still deterministic and even over the tied set.
        backends[1].active_requests = 5
        picks = [strategy.select_backend("10.0.0.1", backends) for _ in range(4)]
        assert [b.backend_id for b in picks] == ["b", "a", "b", "a"]

    def test_skips_unhealthy_backends_even_when_least_active(self):
        from load_balancer.algorithms.least_active import LeastActiveRequestsStrategy

        backends = make_backends("a", "b")
        backends[0].active_requests = 10
        backends[0].healthy = False  # least active but down
        backends[1].active_requests = 4
        strategy = LeastActiveRequestsStrategy()
        assert strategy.select_backend("10.0.0.1", backends).backend_id == "b"

    def test_returns_none_when_no_healthy_backends(self):
        from load_balancer.algorithms.least_active import LeastActiveRequestsStrategy

        backends = make_backends("a", "b")
        for b in backends:
            b.healthy = False
        strategy = LeastActiveRequestsStrategy()
        assert strategy.select_backend("10.0.0.1", backends) is None

    def test_selection_is_read_only_on_counters(self):
        """The strategy must not mutate active_requests; the proxy owns
        increment (assign) and decrement (finally)."""
        from load_balancer.algorithms.least_active import LeastActiveRequestsStrategy

        backends = make_backends("a", "b")
        backends[1].active_requests = 1
        strategy = LeastActiveRequestsStrategy()
        strategy.select_backend("10.0.0.1", backends)
        strategy.select_backend("10.0.0.1", backends)
        assert backends[0].active_requests == 0
        assert backends[1].active_requests == 1

class TestIpHash:
    """Phase 4 Task 2: deterministic hash of the client's TCP peer IP onto
    the healthy backend set. No X-Forwarded-For involvement by construction
    (the proxy passes the socket peer address only)."""

    def test_same_ip_maps_to_same_backend(self):
        from load_balancer.algorithms.ip_hash import IpHashStrategy

        backends = make_backends("a", "b", "c")
        strategy = IpHashStrategy()
        first = strategy.select_backend("203.0.113.7", backends)
        for _ in range(10):
            assert strategy.select_backend("203.0.113.7", backends) is first

    def test_mapping_is_a_stable_function_of_ip_and_set(self):
        from load_balancer.algorithms.ip_hash import IpHashStrategy

        backends = make_backends("a", "b", "c")
        a = IpHashStrategy().select_backend("198.51.100.23", backends)
        b = IpHashStrategy().select_backend("198.51.100.23", backends)
        assert a.backend_id == b.backend_id  # two instances agree

    def test_different_ips_can_land_on_different_backends(self):
        from load_balancer.algorithms.ip_hash import IpHashStrategy

        backends = make_backends("a", "b", "c", "d")
        strategy = IpHashStrategy()
        picked = {
            strategy.select_backend(f"10.0.0.{n}", backends).backend_id
            for n in range(1, 41)
        }
        assert len(picked) > 1  # sanity: not everything hashes to one slot

    def test_unhealthy_backend_not_selected_and_mapping_shifts(self):
        """Removing a backend remaps some clients (documented property of
        hash-on-set); the mapping must never return an unhealthy backend."""
        from load_balancer.algorithms.ip_hash import IpHashStrategy

        backends = make_backends("a", "b", "c")
        strategy = IpHashStrategy()
        assert strategy.select_backend("203.0.113.7", backends).backend_id == "c"
        backends[2].healthy = False  # c goes down
        pick = strategy.select_backend("203.0.113.7", backends)
        assert pick.backend_id in ("a", "b")

    def test_all_requests_go_to_healthy_backends(self):
        from load_balancer.algorithms.ip_hash import IpHashStrategy

        backends = make_backends("a", "b", "c")
        backends[0].healthy = False
        strategy = IpHashStrategy()
        for n in range(50):
            assert strategy.select_backend(f"10.0.{n}.1", backends).backend_id in ("b", "c")

    def test_none_client_ip_falls_back_to_first_healthy(self):
        from load_balancer.algorithms.ip_hash import IpHashStrategy

        backends = make_backends("a", "b")
        strategy = IpHashStrategy()
        assert strategy.select_backend(None, backends).backend_id == "a"

    def test_returns_none_when_no_healthy_backends(self):
        from load_balancer.algorithms.ip_hash import IpHashStrategy

        backends = make_backends("a")
        backends[0].healthy = False
        strategy = IpHashStrategy()
        assert strategy.select_backend("10.0.0.1", backends) is None

    def test_ignores_x_forwarded_for(self):
        """Documented: forwarded headers are never consulted; the strategy
        hashes only the TCP peer address it is handed."""
        from load_balancer.algorithms.ip_hash import IpHashStrategy

        backends = make_backends("a", "b")
        strategy = IpHashStrategy()
        # the proxy's contract: client_ip is the socket peer. There is no
        # header parameter at all in the interface to abuse.
        import inspect

        assert "forwarded" not in inspect.getsource(IpHashStrategy).lower()

