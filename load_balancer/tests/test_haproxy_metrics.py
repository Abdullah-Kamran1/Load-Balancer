"""Phase 7: HAProxy stats CSV parsing + proxy-shaped snapshot synthesis.

The combined CSV expects a snapshot dict shaped like collect_run_snapshot().
HAProxy's exported stats CSV is the machine-readable source for its
counters, so these tests pin the parse and the synthesis contract.
"""

import json

from load_balancer.metrics.haproxy_stats import (
    parse_haproxy_stats_csv,
    synthesize_snapshot,
)
from load_balancer.metrics.wrk_parser import append_combined_csv, parse_wrk_output

# Real-shaped export (HAProxy 3.4.7). The live export has ~250 columns;
# only the consumed columns are shown, because the parser reads by name.
SAMPLE_CSV = """\
# pxname,svname,qcur,qmax,scur,smax,slim,stot,status,lastchg,check_status,check_code,check_health,chkfail,chkdown,hrsp_1xx,hrsp_2xx,hrsp_3xx,hrsp_4xx,hrsp_5xx,hrsp_other,addr
fe_roundrobin,FRONTEND,,,1,2,4096,3,OPEN,0,,,0,0,0,0,22,0,0,2,0,
be_roundrobin,backend-1,0,1,0,2,,14,UP,120,L7OK,200,4,0,0,0,12,0,0,2,0,172.19.0.2:8000
be_roundrobin,backend-2,0,0,0,0,,10,UP,30,L7OK,200,4,0,0,0,10,0,0,0,0,172.19.0.3:8000
be_roundrobin,backend-3,0,0,0,0,,0,DOWN,4,L4CON,0,0,3,1,0,0,0,0,0,0,172.19.0.4:8000
be_roundrobin,backend-4,0,0,0,0,,0,UP,300,L7OK,200,4,0,0,0,0,0,0,0,0,172.19.0.5:8000
be_roundrobin,BACKEND,0,1,0,2,,24,UP,300,,,0,3,1,0,22,0,0,2,0,
fe_leastconn,FRONTEND,,,0,0,4096,0,OPEN,0,,,0,0,0,0,0,0,0,0,0,,
be_leastconn,backend-1,0,0,0,0,,0,UP,300,L7OK,200,4,0,0,0,0,0,0,0,0,172.19.0.2:8000
be_leastconn,BACKEND,0,0,0,0,,0,UP,300,,,0,0,0,0,0,0,0,0,0,
"""

WRK_SAMPLE = """\
Running 8s test @ http://host.docker.internal:8091/work?delay_ms=5
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


class TestParseHaproxyStats:
    def test_header_hash_prefix_is_stripped(self):
        stats = parse_haproxy_stats_csv(SAMPLE_CSV)
        assert "fe_roundrobin" in stats["frontends"]
        assert stats["frontends"]["fe_roundrobin"]["status"] == "OPEN"

    def test_backend_and_server_rows_are_separated(self):
        stats = parse_haproxy_stats_csv(SAMPLE_CSV)
        assert set(stats["servers"]["be_roundrobin"]) == {
            "backend-1", "backend-2", "backend-3", "backend-4"
        }
        assert stats["backends"]["be_roundrobin"]["stot"] == 24

    def test_server_fields_and_health(self):
        stats = parse_haproxy_stats_csv(SAMPLE_CSV)
        down = stats["servers"]["be_roundrobin"]["backend-3"]
        assert down["status"] == "DOWN"
        assert down["check_status"] == "L4CON"
        assert down["check_health"] == 0
        assert down["chkfail"] == 3

    def test_status_class_counts_parsed(self):
        stats = parse_haproxy_stats_csv(SAMPLE_CSV)
        assert stats["backends"]["be_roundrobin"]["status_counts"] == {
            "1xx": 0, "2xx": 22, "3xx": 0, "4xx": 0, "5xx": 2
        }
        assert stats["servers"]["be_roundrobin"]["backend-1"]["status_counts"]["2xx"] == 12

    def test_empty_values_parse_as_zero(self):
        stats = parse_haproxy_stats_csv(SAMPLE_CSV)
        assert stats["frontends"]["fe_leastconn"]["stot"] == 0


class TestSynthesizeSnapshot:
    def _snap(self, pxname="be_roundrobin"):
        return synthesize_snapshot(
            parse_haproxy_stats_csv(SAMPLE_CSV),
            strategy_name="roundrobin",
            pxname=pxname,
            captured_at="2026-11-18T10:00:00",
        )

    def test_snapshot_has_combined_csv_shape(self):
        snap = self._snap()
        assert snap["meta"]["strategy"] == "roundrobin"
        assert snap["meta"]["run_started_at"] == "2026-11-18T10:00:00"
        assert snap["proxy"]["counters"]["requests_total"] == 24
        assert snap["proxy"]["counters"]["responses_5xx"] == 2
        assert snap["proxy"]["counters"]["status_counts"]["2xx"] == 22
        assert snap["proxy"]["latency_ms"]["p99"] is None
        assert len(snap["backends"]) == 4

    def test_backend_health_and_shares(self):
        snap = self._snap()
        by_id = {b["backend_id"]: b for b in snap["backends"]}
        assert by_id["backend-1"]["healthy"] is True
        assert by_id["backend-3"]["healthy"] is False
        assert by_id["backend-1"]["total_requests"] == 14
        assert by_id["backend-1"]["share_pct"] == round(100 * 14 / 24, 2)
        assert by_id["backend-3"]["share_pct"] == 0.0
        assert by_id["backend-1"]["host"] == "172.19.0.2"
        assert by_id["backend-1"]["port"] == 8000

    def test_empty_backend_has_zero_shares(self):
        snap = self._snap("be_leastconn")
        assert snap["proxy"]["counters"]["requests_total"] == 0
        assert all(b["share_pct"] == 0.0 for b in snap["backends"])

    def test_connections_total_comes_from_frontend_row(self):
        # FRONTEND stot counts client connections (3), while server stot
        # counts requests/sessions (24); the metric contract calls this
        # connections_total.
        snap = synthesize_snapshot(
            parse_haproxy_stats_csv(SAMPLE_CSV),
            strategy_name="roundrobin",
            pxname="be_roundrobin",
            frontend_pxname="fe_roundrobin",
        )
        assert snap["proxy"]["counters"]["connections_total"] == 3

    def test_connections_total_zero_without_frontend(self):
        snap = self._snap()
        assert snap["proxy"]["counters"]["connections_total"] == 0

    def test_combined_csv_accepts_haproxy_snapshot(self, tmp_path):
        snap = self._snap()
        csv_path = tmp_path / "combined.csv"
        append_combined_csv(
            parse_wrk_output(WRK_SAMPLE), snap, csv_path,
            run_id="h1", mode="haproxy",
        )
        lines = csv_path.read_text().strip().splitlines()
        header = lines[0].split(",")
        row = lines[1].split(",")
        col = lambda name: row[header.index(name)]  # noqa: E731
        assert col("mode") == "haproxy"
        assert col("strategy") == "roundrobin"
        assert col("proxy_requests_total") == "24"
        assert col("proxy_status_counts") == "1xx:0;2xx:22;3xx:0;4xx:0;5xx:2"
        assert col("proxy_p99_ms") == ""
        assert "backend-1:" in col("backend_shares")
        assert "backend-3:0.0" in col("backend_shares")
