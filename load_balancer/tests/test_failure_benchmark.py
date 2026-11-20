"""Phase 8: pure logic for the timed backend-failure benchmark."""

from scripts import benchmark_matrix as bm
from scripts import failure_benchmark as fb


def test_failure_cases_cover_all_six_mode_algorithm_pairs():
    cases = fb.failure_cases()
    assert len(cases) == 6
    assert {(c.mode, c.strategy) for c in cases} == {
        ("custom", "round_robin"), ("custom", "least_active_requests"),
        ("custom", "ip_hash"), ("haproxy", "roundrobin"),
        ("haproxy", "leastconn"), ("haproxy", "source"),
    }
    assert {c.clients for c in cases} == {50}
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


def test_sticky_algorithms_judge_recovery_by_observed_health():
    # ip_hash/source pin one client IP to one backend; traffic need not reach
    # the recovered victim, but its health state must actually be UP.
    assert fb.recovery_ok({"backend-1": 12}, "backend-2", sticky=True,
                          healthy=True) is True
    assert fb.recovery_ok({"backend-1": 12}, "backend-2", sticky=True,
                          healthy=False) is False


def test_detection_failure_delta_counts_only_the_window():
    assert fb.detection_failure_delta(100, 137) == 37


def test_wrk_output_valid_rejects_connection_refused():
    assert fb.wrk_output_valid("100 requests in 8.01s, 1MB read\nRequests/sec: 12\n")
    assert not fb.wrk_output_valid("unable to connect to host.docker.internal:8091 "
                                   "Connection refused\n")


def test_ensure_victim_restarts_a_stopped_container(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stdout = "exited"
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Result()

    monkeypatch.setattr(fb.bm, "run", fake_run)
    monkeypatch.setattr(fb.bm, "wait_for", lambda *a, **k: True)
    monkeypatch.setattr(fb, "http_get", lambda url: (200, ""))
    fb.ensure_victim_running()
    assert any(c[:2] == ["docker", "start"] for c in calls)


def test_error_count_accepts_json_string_status_keys():
    # JSON object keys are strings, so counts.get(502) silently returned 0.
    snap = {"proxy": {"counters": {"status_counts": {"200": 10, "502": 3, "503": 2}}}}
    assert fb._error_count(bm.build_cases()[0], snap) == 5


def test_watcher_ignores_transitions_before_the_kill():
    case = bm.build_cases()[0]
    watcher = fb.HealthWatcher(case, None)
    watcher.record("DOWN")  # stale state from before the run
    assert watcher.down_at is None
    watcher.arm("UP")
    watcher.record("UP 2/3")
    assert watcher.down_at is None
    watcher.record("DOWN")
    assert watcher.down_at is not None
