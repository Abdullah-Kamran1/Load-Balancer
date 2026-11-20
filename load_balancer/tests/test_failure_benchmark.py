"""Phase 8: pure logic for the timed backend-failure benchmark."""

from scripts import failure_benchmark as fb


def test_failure_cases_cover_all_six_mode_algorithm_pairs():
    cases = fb.failure_cases()
    assert len(cases) == 6
    assert {(c.mode, c.strategy) for c in cases} == {
        ("custom", "round_robin"), ("custom", "least_active_requests"),
        ("custom", "ip_hash"), ("haproxy", "roundrobin"),
        ("haproxy", "leastconn"), ("haproxy", "source"),
    }
    assert {c.clients for c in cases} == {200}
    assert fb.VICTIM == "backend-2"


def test_kill_time_is_half_the_load_window():
    assert fb.kill_at(60) == 30
    assert fb.kill_at(40) == 20


def test_custom_transitions_extract_only_the_victim():
    snap = {
        "backends": [
            {"backend_id": "backend-1", "state_transitions": [
                {"state": "down", "timestamp": 1.0}]},
            {"backend_id": "backend-2", "state_transitions": [
                {"state": "down", "timestamp": 100.0},
                {"state": "up", "timestamp": 110.0}]},
        ]
    }
    assert fb.custom_transitions(snap, "backend-2") == [
        ("down", 100.0), ("up", 110.0)
    ]


def test_haproxy_transitional_status_is_not_a_completed_state():
    assert fb.is_down("UP 2/3") is False
    assert fb.is_down("DOWN 1/2") is True
    assert fb.is_down("DOWN") is True
    assert fb.is_fully_up("UP 2/3") is False
    assert fb.is_fully_up("UP") is True


def test_status_counter_delta_counts_only_new_errors():
    before = {200: 100, 502: 2, 503: 1}
    after = {200: 140, 502: 5, 503: 3}
    assert fb.status_counter_delta(before, after, (502, 503)) == 5


def test_recovery_requires_the_victim_to_receive_traffic():
    assert fb.recovery_ok({"backend-1": 7, "backend-2": 6, "backend-3": 7},
                          "backend-2") is True
    assert fb.recovery_ok({"backend-1": 12, "backend-2": 1, "backend-3": 11},
                          "backend-2") is False


def test_sticky_algorithms_judge_recovery_by_health_not_traffic():
    # ip_hash/source pin one client IP to one backend; after re-admission a
    # healthy victim may legitimately receive zero requests from that client.
    assert fb.recovery_ok({"backend-1": 12}, "backend-2", sticky=True) is True
