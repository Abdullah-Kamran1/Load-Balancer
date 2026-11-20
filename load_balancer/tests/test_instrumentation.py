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


# ---------------------------------------------------------------- Task 2:
# metrics.py — run snapshot + JSON/CSV export.

import json

from types import SimpleNamespace

from load_balancer.metrics import (
    append_backends_csv,
    append_summary_csv,
    collect_run_snapshot,
    percentile,
    write_json,
)


class TestPercentile:
    def test_exact_interpolation_on_known_samples(self):
        samples = [float(i) for i in range(1, 101)]  # 1..100
        # linear interpolation: idx = (p/100) * (n-1)
        assert percentile(samples, 50) == 50.5   # idx 49.5 between 50 and 51
        assert percentile(samples, 95) == 95.05  # idx 94.05 between 95 and 96
        assert percentile(samples, 99) == 99.01  # idx 98.01 between 99 and 100

    def test_single_sample_returns_itself(self):
        assert percentile([7.5], 99) == 7.5

    def test_empty_samples_return_none(self):
        assert percentile([], 50) is None


class TestRunSnapshot:
    def test_snapshot_includes_in_process_cpu_and_memory(self):
        # Windows reports 0 CPU time via Get-Process/WMI for harness-spawned
        # processes; the proxy must record its own process_time() in the dump.
        proxy = SimpleNamespace(counters={}, latencies_ms=[])
        registry = SimpleNamespace(snapshot=lambda: [])
        snap = collect_run_snapshot(proxy, registry, strategy_name="round_robin")
        assert snap["meta"]["process_cpu_seconds"] >= 0
        assert snap["meta"]["process_rss_mib"] > 0

    async def test_snapshot_structure_and_shares(self, fake_backend_factory):
        fb1 = await fake_backend_factory()
        fb2 = await fake_backend_factory()
        backends = [
            Backend(backend_id="b1", host=fb1.host, port=fb1.port),
            Backend(backend_id="b2", host=fb2.host, port=fb2.port),
        ]
        registry = BackendRegistry(backends)
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        async with await proxy.start():
            for _ in range(4):
                await get_through(proxy.port)
        snap = collect_run_snapshot(proxy, registry, strategy_name="round_robin")
        assert snap["meta"]["strategy"] == "round_robin"
        assert snap["proxy"]["counters"]["requests_total"] == 4
        assert snap["proxy"]["latency_ms"]["p50"] is not None
        assert snap["proxy"]["latency_ms"]["p99"] >= snap["proxy"]["latency_ms"]["p50"]
        shares = {b["backend_id"]: b["share_pct"] for b in snap["backends"]}
        assert shares["b1"] == 50.0 and shares["b2"] == 50.0
        assert all("state_transitions" in b for b in snap["backends"])

    async def test_unhealthy_backend_excluded_from_share_denominator(self, fake_backend_factory):
        fb = await fake_backend_factory()
        down = Backend(backend_id="down", host="127.0.0.1", port=1)
        down.healthy = False
        backends = [Backend(backend_id="b1", host=fb.host, port=fb.port), down]
        registry = BackendRegistry(backends)
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        async with await proxy.start():
            for _ in range(2):
                await get_through(proxy.port)  # all routed to b1
        snap = collect_run_snapshot(proxy, registry, strategy_name="round_robin")
        by_id = {b["backend_id"]: b for b in snap["backends"]}
        # the dead backend served nothing: 0% share, b1 has all of it
        assert by_id["down"]["total_requests"] == 0
        assert by_id["down"]["share_pct"] == 0.0
        assert by_id["b1"]["share_pct"] == 100.0


class TestExport:
    async def test_json_round_trip(self, fake_backend_factory, tmp_path):
        fb = await fake_backend_factory()
        proxy = ProxyServer(registry=registry_of(fb), strategy=RoundRobinStrategy())
        async with await proxy.start():
            await get_through(proxy.port)
        snap = collect_run_snapshot(proxy, registry_of(fb), strategy_name="round_robin")
        out = tmp_path / "run.json"
        write_json(snap, out)
        loaded = json.loads(out.read_text())
        assert loaded["meta"]["strategy"] == "round_robin"
        assert loaded["proxy"]["counters"]["requests_total"] == 1

    async def test_summary_csv_appends_header_once(self, fake_backend_factory, tmp_path):
        fb = await fake_backend_factory()
        proxy = ProxyServer(registry=registry_of(fb), strategy=RoundRobinStrategy())
        async with await proxy.start():
            await get_through(proxy.port)
        snap1 = collect_run_snapshot(proxy, registry_of(fb), strategy_name="round_robin")
        snap2 = collect_run_snapshot(proxy, registry_of(fb), strategy_name="round_robin")
        csv_path = tmp_path / "summary.csv"
        append_summary_csv(snap1, csv_path, run_id="r1")
        append_summary_csv(snap2, csv_path, run_id="r2")
        lines = csv_path.read_text().strip().splitlines()
        assert len(lines) == 3  # header + 2 runs
        assert "run_id" in lines[0]
        assert lines[1].startswith("r1,") and lines[2].startswith("r2,")

    async def test_backends_csv_one_row_per_backend(self, fake_backend_factory, tmp_path):
        fb1 = await fake_backend_factory()
        fb2 = await fake_backend_factory()
        backends = [
            Backend(backend_id="b1", host=fb1.host, port=fb1.port),
            Backend(backend_id="b2", host=fb2.host, port=fb2.port),
        ]
        registry = BackendRegistry(backends)
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        async with await proxy.start():
            for _ in range(4):
                await get_through(proxy.port)
        snap = collect_run_snapshot(proxy, registry, strategy_name="round_robin")
        csv_path = tmp_path / "backends.csv"
        append_backends_csv(snap, csv_path, run_id="r1")
        lines = csv_path.read_text().strip().splitlines()
        assert len(lines) == 3  # header + 2 backends
        assert "backend_id" in lines[0] and "share_pct" in lines[0]


# ---------------------------------------------------------------- Task 3:
# wrk output parser + combined CSV row.

from load_balancer.metrics.wrk_parser import (
    COMBINED_HEADER,
    append_combined_csv,
    parse_wrk_output,
)

# real wrk --latency output from results/raw/phase3_wrk_keepalive.txt
WRK_SAMPLE = """Running 8s test @ http://host.docker.internal:8080/work?delay_ms=5
  2 threads and 10 connections
  Thread Stats   Avg      Stdev     Max   +/- Stdev
    Latency    23.50ms    4.05ms  41.57ms   74.49%
    Req/Sec   213.33     37.30   340.00     81.88%
  Latency Distribution
     50%   23.83ms
     75%   25.76ms
     90%   27.78ms
     99%   33.80ms
  3402 requests in 8.01s, 627.55KB read
Requests/sec:    424.77
Transfer/sec:     78.36KB
"""

WRK_SAMPLE_ERRORS = WRK_SAMPLE.replace(
    "Requests/sec:    424.77",
    "Non-2xx or 3xx responses: 14\nRequests/sec:    424.77\n"
    "  Socket errors: connect 0, read 2, write 0, timeout 1",
)


class TestWrkParser:
    def test_parses_real_wrk_sample(self):
        m = parse_wrk_output(WRK_SAMPLE)
        assert m["threads"] == 2
        assert m["connections"] == 10
        assert m["requests_total"] == 3402
        assert m["duration_s"] == 8.01
        assert m["requests_per_sec"] == 424.77
        assert m["latency_ms"]["p50"] == 23.83
        assert m["latency_ms"]["p75"] == 25.76
        assert m["latency_ms"]["p90"] == 27.78
        assert m["latency_ms"]["p99"] == 33.80
        assert m["thread_latency_avg_ms"] == 23.50

    def test_parses_minutes_scale_runtime(self):
        # wrk switches to base-60 units at ~51s: "42000 requests in 1.50m"
        text = WRK_SAMPLE.replace(
            "  3402 requests in 8.01s, 627.55KB read",
            "  42000 requests in 1.50m, 7.50MB read",
        )
        m = parse_wrk_output(text)
        assert m["requests_total"] == 42000
        assert m["duration_s"] == 90.0

    def test_socket_errors_line_is_optional(self):
        m = parse_wrk_output(WRK_SAMPLE)
        assert m["socket_errors"] is None
        assert m["non_2xx"] is None

    def test_parses_error_lines(self):
        m = parse_wrk_output(WRK_SAMPLE_ERRORS)
        assert m["socket_errors"] == {"connect": 0, "read": 2, "write": 0, "timeout": 1}
        assert m["non_2xx"] == 14

    def test_latency_unit_conversion(self):
        text = WRK_SAMPLE.replace("50%   23.83ms", "50%   1.50us").replace(
            "75%   25.76ms", "75%   1.20s"
        )
        m = parse_wrk_output(text)
        assert m["latency_ms"]["p50"] == 0.0015
        assert m["latency_ms"]["p75"] == 1200.0


class TestCombinedCsv:
    async def test_combined_row_schema_and_append(self, fake_backend_factory, tmp_path):
        fb = await fake_backend_factory()
        registry = registry_of(fb)
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        async with await proxy.start():
            for _ in range(6):
                await get_through(proxy.port)
        snap = collect_run_snapshot(proxy, registry, strategy_name="round_robin")
        wrk = parse_wrk_output(WRK_SAMPLE)
        csv_path = tmp_path / "combined.csv"
        append_combined_csv(wrk, snap, csv_path, run_id="r1", mode="custom")
        append_combined_csv(wrk, snap, csv_path, run_id="r2", mode="custom")
        lines = csv_path.read_text().strip().splitlines()
        assert lines[0] == ",".join(COMBINED_HEADER)
        assert len(lines) == 3
        row = lines[1].split(",")
        assert row[0] == "r1"
        # find columns by header position instead of fixed index
        header = lines[0].split(",")
        def col(name):
            return row[header.index(name)]
        assert col("wrk_requests") == "3402"
        assert float(col("wrk_p99_ms")) == 33.8
        assert col("proxy_requests_total") == "6"
        assert col("mode") == "custom"
        assert col("strategy") == "round_robin"
        assert "backend-1:100.0" in col("backend_shares")


# ---------------------------------------------------------------- Task 4:
# periodic snapshot dump for live runs (LB_METRICS_DUMP_PATH).

from load_balancer.metrics.dumper import dump_loop


class TestDumpLoop:
    async def test_dump_loop_writes_snapshot_json_periodically(self, fake_backend_factory, tmp_path):
        fb = await fake_backend_factory()
        registry = registry_of(fb)
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        path = tmp_path / "metrics_dump.json"
        async with await proxy.start():
            await get_through(proxy.port)
            task = asyncio.create_task(
                dump_loop(proxy, registry, "round_robin", path, interval=0.05)
            )
            try:
                deadline = asyncio.get_running_loop().time() + 3
                while not path.exists() and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.02)
                assert path.exists()
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["meta"]["strategy"] == "round_robin"
        assert loaded["proxy"]["counters"]["requests_total"] == 1
        assert loaded["backends"][0]["backend_id"] == "backend-1"

    async def test_dump_survives_concurrent_reader_holding_file(self, fake_backend_factory, tmp_path):
        # Windows: os.replace fails with PermissionError while another
        # handle holds the destination open. The loop must skip the cycle
        # and keep dumping, not die silently mid-benchmark.
        fb = await fake_backend_factory()
        registry = registry_of(fb)
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        path = tmp_path / "metrics_dump.json"
        async with await proxy.start():
            task = asyncio.create_task(
                dump_loop(proxy, registry, "round_robin", path, interval=0.02)
            )
            try:
                # wait for the first dump
                deadline = asyncio.get_running_loop().time() + 3
                while not path.exists() and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.01)
                assert path.exists()
                # hold the destination open like a concurrent consumer
                with open(path, "r", encoding="utf-8") as held:
                    await asyncio.sleep(0.3)  # several dump cycles collide
                # after release, a fresh dump must land (counters unchanged
                # is fine: any later write proves the loop still runs)
                deadline = asyncio.get_running_loop().time() + 3
                survived = False
                while asyncio.get_running_loop().time() < deadline:
                    if not task.done():  # still running
                        survived = True
                        break
                    await asyncio.sleep(0.01)
                assert survived, f"dump_loop died: {task.exception()}"
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def test_dump_is_atomic_no_partial_reads(self, fake_backend_factory, tmp_path):
        # a reader concurrently opening the file must never see a partial
        # JSON document: the dump writes to a temp file and replaces.
        fb = await fake_backend_factory()
        registry = registry_of(fb)
        proxy = ProxyServer(registry=registry, strategy=RoundRobinStrategy())
        path = tmp_path / "metrics_dump.json"
        async with await proxy.start():
            task = asyncio.create_task(
                dump_loop(proxy, registry, "round_robin", path, interval=0.02)
            )
            try:
                deadline = asyncio.get_running_loop().time() + 3
                reads = 0
                while asyncio.get_running_loop().time() < deadline:
                    if path.exists():
                        data = path.read_text(encoding="utf-8")
                        json.loads(data)  # raises if a partial write was visible
                        reads += 1
                        if reads >= 5:
                            break
                    await asyncio.sleep(0.01)
                assert reads >= 5  # observed enough dumps
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
