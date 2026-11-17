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
