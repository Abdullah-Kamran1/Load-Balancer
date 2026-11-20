"""Phase 8: unit tests for the benchmark runner's pure logic.

Live orchestration is proven by the matrix/failure runs themselves; these
tests pin the derived values those runs depend on (case expansion, wrk
command, run ids, medians, resource-sample parsing, CSV append contract).
"""

import csv
import json
import time
from datetime import datetime

from scripts import benchmark_matrix as bm


def test_build_cases_expands_every_mode_algorithm_and_load():
    cases = bm.build_cases()
    assert len(cases) == 18
    assert {(c.mode, c.strategy) for c in cases} == {
        ("custom", "round_robin"),
        ("custom", "least_active_requests"),
        ("custom", "ip_hash"),
        ("haproxy", "roundrobin"),
        ("haproxy", "leastconn"),
        ("haproxy", "source"),
    }
    assert {c.clients for c in cases} == {50, 200, 500}


def test_run_case_safe_logs_and_continues(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise RuntimeError("wrk exploded")

    monkeypatch.setattr(bm, "run_case", boom)
    log = tmp_path / "errors.csv"
    case = bm.build_cases()[0]
    assert bm.run_case_safe(case, 2, log) is None
    rows = list(csv.reader(log.read_text(encoding="utf-8").splitlines()))
    assert rows[1][:3] == ["custom-round_robin-50", "2", "RuntimeError"]
    assert "wrk exploded" in rows[1][3]


def test_haproxy_case_carries_both_stats_proxy_names():
    case = next(
        c
        for c in bm.build_cases()
        if c.mode == "haproxy" and c.strategy == "leastconn" and c.clients == 200
    )
    assert case.port == 8092
    assert case.pxname == "be_leastconn"
    assert case.frontend == "fe_leastconn"


def test_custom_cases_all_use_the_single_custom_port():
    assert {c.port for c in bm.build_cases() if c.mode == "custom"} == {8080}
    assert {c.pxname for c in bm.build_cases() if c.mode == "custom"} == {""}


def test_wrk_threads_scale_only_for_the_largest_load():
    assert bm.wrk_threads(50) == 4
    assert bm.wrk_threads(200) == 4
    assert bm.wrk_threads(500) == 8


def test_run_id_is_unique_per_case_and_repetition():
    case = bm.build_cases()[0]
    now = datetime(2026, 11, 20, 12, 0, 0)
    assert bm.make_run_id(case, 2, now) == "20261120-120000-custom-round_robin-50-r2"


def test_median_reports_the_middle_and_even_average():
    assert bm.median_of([9, 1, 4]) == 4
    assert bm.median_of([1, 2, 3, 4]) == 2.5


def test_select_cases_applies_start_before_limit():
    cases = bm.build_cases()
    assert bm.select_cases(cases, 4, 1) == [cases[3]]
    assert bm.select_cases(cases, 1, 2) == cases[:2]
    assert bm.select_cases(cases, 18, 1) == [cases[-1]]


def test_docker_stats_parses_cpu_and_memory_units():
    assert bm.parse_docker_stats("3.25%,12.50MiB / 1.953GiB") == (3.25, 12.5)
    assert bm.parse_docker_stats("0.10%,1.20GiB / 1.953GiB") == (0.1, 1228.8)
    assert bm.parse_docker_stats("0.00%,512KiB / 1.953GiB") == (0.0, 0.5)


def test_cpu_percent_delta_is_percent_of_one_core():
    # 2 CPU-seconds consumed over 4 wall-seconds = 50% of one core.
    assert bm.cpu_percent_delta(12.5, 14.5, 100.0, 104.0) == 50.0


def test_wrk_command_uses_the_fixed_path_and_case_specific_frontend():
    custom = next(c for c in bm.build_cases() if c.mode == "custom" and c.clients == 50)
    assert bm.wrk_command(custom, "run-1", 60) == [
        "docker", "run", "--rm", "--name", "lb-wrk-run-1", "lb/wrk",
        "-t4", "-c50", "-d60s", "--latency",
        "http://host.docker.internal:8080/work?delay_ms=25",
    ]
    source = next(
        c for c in bm.build_cases()
        if c.mode == "haproxy" and c.strategy == "source" and c.clients == 500
    )
    assert bm.wrk_command(source, "run-2", 60) == [
        "docker", "run", "--rm", "--name", "lb-wrk-run-2", "lb/wrk",
        "-t8", "-c500", "-d60s", "--latency",
        "http://host.docker.internal:8093/work?delay_ms=25",
    ]


def test_snapshot_kwargs_identify_the_haproxy_stats_rows():
    custom = next(c for c in bm.build_cases() if c.mode == "custom")
    haproxy = next(
        c for c in bm.build_cases() if c.mode == "haproxy" and c.strategy == "source"
    )
    assert bm.snapshot_kwargs(custom) == {}
    assert bm.snapshot_kwargs(haproxy) == {
        "strategy_name": "source",
        "pxname": "be_source",
        "frontend_pxname": "fe_source",
    }


def test_append_row_writes_header_once(tmp_path):
    path = tmp_path / "resources.csv"
    header = ["run_id", "cpu_pct"]
    bm.append_row(path, header, ["r1", 12.5])
    bm.append_row(path, header, ["r2", 3.0])
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows == [header, ["r1", "12.5"], ["r2", "3.0"]]


def test_zero_warmup_skips_the_warmup_wrk_run(monkeypatch, tmp_path):
    # A --smoke run with --warmup 0 previously invoked wrk with -d0s and
    # crashed; the measured run must be the only wrk invocation.
    dump = tmp_path / "dump.json"
    dump.write_text("{}", encoding="utf-8")
    calls = []
    fake_proc = type("FakeProc", (), {"pid": 123})()

    class FakeSampler:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def finish(self):
            pass

    monkeypatch.setattr(bm, "ensure_farm", lambda: None)
    monkeypatch.setattr(bm, "start_custom_proxy", lambda case, rid: (fake_proc, None, dump))
    monkeypatch.setattr(bm, "stop_custom_proxy", lambda proc, log: None)
    monkeypatch.setattr(bm, "restart_haproxy", lambda: None)
    monkeypatch.setattr(bm, "ResourceSampler", FakeSampler)
    monkeypatch.setattr(bm, "run", lambda *a, **k: None)
    monkeypatch.setattr(bm, "_write_run_result", lambda *a, **k: None)
    monkeypatch.setattr(bm.time, "sleep", lambda seconds: None)

    def fake_wrk(case, label, duration, warmup=False):
        calls.append((label, duration, warmup))
        return {
            "metrics": {"requests_total": 100, "requests_per_sec": 12.5,
                        "duration_s": duration},
            "path": tmp_path / "wrk.txt",
        }

    monkeypatch.setattr(bm, "run_wrk", fake_wrk)
    case = next(c for c in bm.build_cases() if c.mode == "custom" and c.clients == 50)
    run_id = bm.run_case(case, 1, warmup_s=0, duration_s=8)
    assert calls == [(run_id, 8, False)]


def test_port_available_detects_a_bound_listener():
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        assert bm.port_available(port) is False
    assert bm.port_available(port) is True


def test_host_sampler_reads_process_usage_from_dump(tmp_path):
    dump = tmp_path / "dump.json"
    dump.write_text(
        json.dumps({"meta": {"process_cpu_seconds": 1.0, "process_rss_mib": 50.0}}),
        encoding="utf-8",
    )
    case = bm.build_cases()[0]
    sampler = bm.ResourceSampler("r", case, str(dump), container=False)
    sample, prev = sampler._sample_one(str(dump), False, None)
    assert sample == (0.0, 50.0)
    assert prev[0] == 1.0


def test_host_sampler_computes_cpu_delta_between_dumps(tmp_path):
    dump = tmp_path / "dump.json"
    dump.write_text(
        json.dumps({"meta": {"process_cpu_seconds": 2.5, "process_rss_mib": 51.0}}),
        encoding="utf-8",
    )
    case = bm.build_cases()[0]
    sampler = bm.ResourceSampler("r", case, str(dump), container=False)
    sample, _ = sampler._sample_one(
        str(dump), False, (1.0, time.monotonic() - 2.0)
    )
    assert 70 <= sample[0] <= 80  # 1.5 CPU-seconds over 2 wall-seconds
    assert sample[1] == 51.0


def test_host_sampler_ignores_malformed_dump(tmp_path):
    dump = tmp_path / "dump.json"
    dump.write_text('{"meta": {}}', encoding="utf-8")
    case = bm.build_cases()[0]
    sampler = bm.ResourceSampler("r", case, str(dump), container=False)
    sample, prev = sampler._sample_one(str(dump), False, (1.0, 100.0))
    assert sample is None
    assert prev == (1.0, 100.0)
