"""Phase 5 Task 1: backend state transitions recorded with timestamps.

The registry is the single place that flips health state; every actual
state change (up<->down) appends a {state, timestamp} entry to
Backend.state_transitions so detection/recovery times can be measured
later (acceptance script, Phase 5).
"""

import time

from load_balancer.models import Backend
from load_balancer.health.registry import BackendRegistry


def make_backends(*ids):
    return [Backend(backend_id=i, host="127.0.0.1", port=8000 + n) for n, i in enumerate(ids, start=1)]


class TestTransitionRecording:
    def test_backend_starts_with_empty_transition_history(self):
        b = Backend(backend_id="backend-1", host="127.0.0.1", port=8001)
        assert b.state_transitions == []

    def test_mark_down_records_down_transition_with_timestamp(self):
        backends = make_backends("a")
        registry = BackendRegistry(backends)
        before = time.time()
        registry.mark_down(backends[0])
        after = time.time()

        assert backends[0].healthy is False
        assert len(backends[0].state_transitions) == 1
        entry = backends[0].state_transitions[0]
        assert entry["state"] == "down"
        assert before <= entry["timestamp"] <= after

    def test_mark_up_records_up_transition_with_timestamp(self):
        backends = make_backends("a")
        backends[0].healthy = False
        registry = BackendRegistry(backends)
        before = time.time()
        registry.mark_up(backends[0])
        after = time.time()

        assert backends[0].healthy is True
        assert backends[0].state_transitions == [{"state": "up", "timestamp": backends[0].state_transitions[0]["timestamp"]}]
        assert before <= backends[0].state_transitions[0]["timestamp"] <= after

    def test_full_down_up_cycle_records_both_transitions_in_order(self):
        backends = make_backends("a")
        registry = BackendRegistry(backends)
        registry.mark_down(backends[0])
        registry.mark_up(backends[0])

        states = [e["state"] for e in backends[0].state_transitions]
        assert states == ["down", "up"]
        # timestamps are non-decreasing
        ts = [e["timestamp"] for e in backends[0].state_transitions]
        assert ts == sorted(ts)

    def test_redundant_mark_down_records_no_duplicate_transition(self):
        # only actual state changes are transitions: marking an already
        # down backend down again must not append a second entry
        backends = make_backends("a")
        registry = BackendRegistry(backends)
        registry.mark_down(backends[0])
        registry.mark_down(backends[0])
        assert len(backends[0].state_transitions) == 1

        registry.mark_up(backends[0])
        registry.mark_up(backends[0])
        assert len(backends[0].state_transitions) == 2

    def test_snapshot_includes_state_transitions(self):
        backends = make_backends("a", "b")
        registry = BackendRegistry(backends)
        registry.mark_down(backends[1])

        snap = registry.snapshot()
        by_id = {s["backend_id"]: s for s in snap}
        assert by_id["a"]["state_transitions"] == []
        assert [e["state"] for e in by_id["b"]["state_transitions"]] == ["down"]


# ---------------------------------------------------------------- Task 2:
# HealthMonitor — active GET /health probes driving registry state.

import asyncio

from load_balancer.health.monitor import HealthMonitor
from .conftest import FakeBackend


async def wait_until(pred, timeout: float = 3.0, step: float = 0.01):
    """Poll pred() until true or deadline; assert-style helper."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(step)
    return False


class TestHealthMonitor:
    async def test_probes_health_endpoint_with_get(self, fake_backend_factory):
        fb = await fake_backend_factory()
        registry = BackendRegistry(
            [Backend(backend_id="b1", host=fb.host, port=fb.port)]
        )
        monitor = HealthMonitor(registry, interval=10, timeout=0.5,
                                failure_threshold=3, recovery_threshold=2)
        await monitor.start()
        try:
            assert await wait_until(lambda: len(fb.requests) >= 1)
            probe = fb.requests[0]
            assert probe["method"] == "GET"
            assert probe["target"] == "/health"
            # healthy backend stays healthy
            assert registry.healthy_backends() != []
        finally:
            await monitor.stop()

    async def test_marks_down_after_consecutive_failures(self):
        # port 1: nothing listening -> connection refused on every probe
        b = Backend(backend_id="dead", host="127.0.0.1", port=1)
        registry = BackendRegistry([b])
        monitor = HealthMonitor(registry, interval=0.05, timeout=0.5,
                                failure_threshold=2, recovery_threshold=2)
        await monitor.start()
        try:
            assert await wait_until(lambda: b.healthy is False)
            assert [e["state"] for e in b.state_transitions] == ["down"]
            assert registry.healthy_backends() == []
        finally:
            await monitor.stop()

    async def test_counts_non_200_response_as_failure(self, fake_backend_factory):
        fb = await fake_backend_factory(status=500)
        b = Backend(backend_id="b1", host=fb.host, port=fb.port)
        registry = BackendRegistry([b])
        monitor = HealthMonitor(registry, interval=0.05, timeout=0.5,
                                failure_threshold=2, recovery_threshold=2)
        await monitor.start()
        try:
            assert await wait_until(lambda: b.healthy is False)
        finally:
            await monitor.stop()

    async def test_probe_timeout_counts_as_failure(self, fake_backend_factory):
        fb = await fake_backend_factory(delay_ms=800)  # answers, but too late
        b = Backend(backend_id="b1", host=fb.host, port=fb.port)
        registry = BackendRegistry([b])
        monitor = HealthMonitor(registry, interval=0.05, timeout=0.1,
                                failure_threshold=1, recovery_threshold=2)
        await monitor.start()
        try:
            assert await wait_until(lambda: b.healthy is False)
        finally:
            await monitor.stop()

    async def test_marks_up_after_recovery_threshold(self, fake_backend_factory):
        fb = await fake_backend_factory(status=500)
        b = Backend(backend_id="b1", host=fb.host, port=fb.port)
        registry = BackendRegistry([b])
        monitor = HealthMonitor(registry, interval=0.05, timeout=0.5,
                                failure_threshold=2, recovery_threshold=2)
        await monitor.start()
        try:
            assert await wait_until(lambda: b.healthy is False)
            # backend recovers
            fb.status = 200
            assert await wait_until(lambda: b.healthy is True)
            assert [e["state"] for e in b.state_transitions] == ["down", "up"]
        finally:
            await monitor.stop()

    async def test_single_success_does_not_flip_unhealthy_backend(self, fake_backend_factory):
        # anti-flapping: recovery needs `recovery_threshold` consecutive
        # successes; drive check_backend directly for determinism.
        fb = await fake_backend_factory(status=200)
        b = Backend(backend_id="b1", host=fb.host, port=fb.port)
        b.healthy = False
        registry = BackendRegistry([b])
        monitor = HealthMonitor(registry, interval=10, timeout=0.5,
                                failure_threshold=3, recovery_threshold=2)
        success = await monitor.check_backend(b)
        assert success is True
        assert b.healthy is False  # one success is not enough
        success = await monitor.check_backend(b)
        assert success is True
        assert b.healthy is True  # second consecutive success -> UP
        assert [e["state"] for e in b.state_transitions] == ["up"]

    async def test_stop_halts_probing_and_cancels_tasks(self, fake_backend_factory):
        fb = await fake_backend_factory()
        b = Backend(backend_id="b1", host=fb.host, port=fb.port)
        registry = BackendRegistry([b])
        monitor = HealthMonitor(registry, interval=0.02, timeout=0.5,
                                failure_threshold=100, recovery_threshold=2)
        await monitor.start()
        assert await wait_until(lambda: len(fb.requests) >= 2)
        await monitor.stop()
        count = len(fb.requests)
        await asyncio.sleep(0.15)
        assert len(fb.requests) == count  # no probes after stop
