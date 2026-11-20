# File reference

Every file created during Phases 0–9, grouped by area. Each entry gives the
file's purpose, its public functions/classes (signature + behavior), and its
callers/dependencies. Generated data files are catalogued at the end.
For *how* the pieces fit together, see [ARCHITECTURE_FLOW.md](ARCHITECTURE_FLOW.md);
for commands and configuration see [RUNBOOK.md](RUNBOOK.md).

---

## Backend farm

### `backend/app/config.py`
- **Purpose:** Reads backend container environment into validated capacity settings.
- **API:** `Settings` frozen dataclass (`backend_id`, `default_delay_ms`, `max_delay_ms`, `max_concurrency`); `load_settings() -> Settings` reads `BACKEND_ID` (`backend-1`), `DEFAULT_WORK_DELAY_MS` (50), `MAX_DELAY_MS` (60000), `MAX_CONCURRENCY` (8), falling back on invalid ints.
- **Called by:** `backend/app/main.py`.
- **Calls:** `os.getenv`.

### `backend/app/main.py`
- **Purpose:** FastAPI test backend with fixed URLs (`/`, `/health`, `/work`, `/stats`).
- **API:** `root()`, `health()` (200), `work(delay_ms=None)` (clamps delay to `[0, max_delay_ms]`, uses `asyncio.Semaphore(max_concurrency)`, returns `{"backend_id", "requested_delay_ms", "actual_ms"}` or 503 + `Retry-After: 1`), `stats()` (counters + configured capacity). Module globals `settings`, `metrics`, `_work_slots`, `app`.
- **Called by:** `uvicorn app.main:app` in `backend/Dockerfile`; probed by the health monitor, wrk and acceptance scripts.
- **Calls:** `.config.load_settings`, `.metrics.Metrics`, FastAPI, asyncio.

### `backend/app/metrics.py`
- **Purpose:** Backend-local in-process counters exposed through `GET /stats`.
- **API:** `Snapshot` dataclass (`total_requests`, `in_flight`, `successful_requests`, `error_responses`, `busy_rejections`, `uptime_seconds`); `Metrics()` with `request_started()`, `request_finished()`, `record_success()`, `record_error()`, `record_busy()`, `snapshot() -> Snapshot`.
- **Called by:** `backend/app/main.py`.
- **Calls:** `time.time`.

### `backend/app/__init__.py`
- **Purpose:** Package marker for the backend app.

### `backend/Dockerfile`
- **Purpose:** Builds the FastAPI backend image (Python, installs `backend/requirements.txt`, runs uvicorn on port 8000).
- **Called by:** `docker-compose.yml` (`build: ./backend`).

### `backend/requirements.txt`
- **Purpose:** Backend runtime dependencies (`fastapi`, `uvicorn[standard]`).

---

## Core proxy

### `load_balancer/config.py`
- **Purpose:** Parses environment variables (fail-fast) into a validated proxy configuration.
- **API:**
  - `HealthConfig` (dataclass): `interval=1.0`, `timeout=1.0`, `failure_threshold=3`, `recovery_threshold=2`; `__post_init__` validates finite positive values.
  - `ProxyConfig` (dataclass): `listen_host`, `listen_port`, `backends`, `strategy`, `health`; `__post_init__` defaults `strategy` to `RoundRobinStrategy`.
  - `parse_backends(raw: str) -> list[Backend]` — parses comma-separated `id:host:port`, rejects duplicates/bad ports/empty list.
  - `load_config(env: dict | None = None) -> ProxyConfig` — builds config from `os.environ` (or supplied mapping), selecting strategy by name.
- **Called by:** `load_balancer/main.py`; `tests/test_config.py`.
- **Calls:** `models.Backend`, the three strategy classes, `health` defaults.

### `load_balancer/models.py`
- **Purpose:** Shared mutable backend state used by proxy, health and metrics.
- **API:** `Backend` dataclass (`backend_id`, `host`, `port`, `healthy=True`, `active_requests=0`, `total_requests=0`, `state_transitions=[]`); properties `address -> (host, port)` and `netloc -> "host:port"`.
- **Called by:** config, registry, monitor, acceptance scripts, most tests.
- **Calls:** `dataclasses`.

### `load_balancer/http_connection.py`
- **Purpose:** h11 framing only — parse client requests, serialize responses, read upstream responses.
- **API:**
  - `BodyTooLargeError` — raised when a body exceeds the configured cap.
  - `HttpRequest` dataclass (`method`, `target`, `http_version`, `raw_headers`, `body`); `headers` property (lowercased dict), `header(name) -> bytes | None`.
  - `read_request(reader, max_body_bytes) -> HttpRequest | None` — single-request wrapper, `None` on clean EOF.
  - `HttpStreamReader(reader, max_body_bytes)` — persistent connection parser; `read_next_request() -> HttpRequest | None`, `serialize_response(status, headers, body) -> bytes`, `should_close` flag.
  - `read_response(reader, max_body_bytes) -> (status, headers, body)` — upstream response parser.
  - `serialize_request(method, target, headers, body) -> bytes` and `serialize_response(status, headers, body, reason=b"") -> bytes` — standalone h11 serializers.
- **Called by:** `proxy.py`, `health/monitor.py`, tests (`test_http_connection.py`, `test_persistent_connection.py`).
- **Calls:** `h11`, `dataclasses`.

### `load_balancer/proxy.py`
- **Purpose:** The L7 reverse proxy: persistent client connections, one upstream connection per request, strategy selection, error responses and counters.
- **API:**
  - `filter_headers(headers, connection_named, drop_host=False)`, `connection_named_by(header_value)`, `upstream_headers(request, backend_netloc)`, `downstream_headers(response_headers)` — hop-by-hop/`Host` header handling.
  - `ProxyServer(registry, strategy, host="127.0.0.1", port=0, connect_timeout=5.0, request_timeout=30.0, write_timeout=30.0, max_body_bytes=10*1024*1024)` — `start()`, `close()`, `port` property, async context manager; public `counters` (`requests_total`, `connections_total`, `responses_5xx`, `errors`, `status_counts`) and `latencies_ms`.
  - Private `_handle_client`, `_forward`, `_record`, `_write_error` implement the request state machine (400/413/502/503 paths).
- **Called by:** `main.py`, `scripts/phase4_acceptance.py`, `scripts/phase5_acceptance.py`, proxy/failure/instrumentation tests.
- **Calls:** `http_connection`, `health/registry.py`, `algorithms/base.py`, `asyncio`, `h11`, `json`.

### `load_balancer/main.py`
- **Purpose:** Entry point (`python -m load_balancer.main`) wiring config, proxy, health monitor and optional metrics dump.
- **API:** `async serve()` — builds all components, starts proxy + monitor, waits for SIGINT/SIGTERM, shuts down; `main()` — `asyncio.run(serve())`.
- **Called by:** shell/`run.ps1`/benchmark scripts via the module entry point.
- **Calls:** `config.load_config`, `BackendRegistry`, `HealthMonitor`, `ProxyServer`, `metrics.dumper.dump_loop`.

### `load_balancer/__init__.py`
- **Purpose:** Package marker.

---

## Algorithms

### `load_balancer/algorithms/base.py`
- **Purpose:** Strategy interface.
- **API:** `LoadBalancingStrategy.select_backend(self, client_ip, healthy_backends)` — abstract; returns a `Backend` or `None`.
- **Called by:** implemented by the three strategies; used as a type in `config.py`/`proxy.py`.

### `load_balancer/algorithms/round_robin.py`
- **Purpose:** Cycling selection over healthy backends.
- **API:** `RoundRobinStrategy()` with `select_backend(...)` using a rotation index.
- **Called by:** default in `config.py`, acceptance scripts, tests.

### `load_balancer/algorithms/least_active.py`
- **Purpose:** Picks the healthy backend with minimum `active_requests`, round-robin tie-break.
- **API:** `LeastActiveRequestsStrategy()` with `select_backend(...)`.
- **Called by:** `config.py` (`LB_STRATEGY=least_active_requests`), acceptance scripts, tests.

### `load_balancer/algorithms/ip_hash.py`
- **Purpose:** Sticky SHA-256 hash of client IP modulo the healthy set (remaps when that set changes).
- **API:** `IpHashStrategy()` with `select_backend(...)`; `None` IP falls back to the first healthy backend.
- **Called by:** `config.py` (`LB_STRATEGY=ip_hash`), acceptance scripts, tests.

### `load_balancer/algorithms/__init__.py`
- **Purpose:** Package marker; strategies are imported from their concrete modules.

---

## Health

### `load_balancer/health/registry.py`
- **Purpose:** Authoritative backend list and health state with transition history.
- **API:** `BackendRegistry(backends)`; `backends()`, `healthy_backends()`, `mark_down(backend)`, `mark_up(backend)`, `snapshot() -> list[dict]`.
- **Called by:** `main.py`, `proxy.py`, `monitor.py`, acceptance scripts, tests.
- **Calls:** `time`, `models.Backend`.

### `load_balancer/health/monitor.py`
- **Purpose:** Active checker — one probe task per backend; `GET /health` success iff HTTP 200; thresholds toggle registry state.
- **API:** `HealthMonitor(registry, *, interval=1.0, timeout=1.0, failure_threshold=3, recovery_threshold=2, max_body_bytes=65536)`; `start()`, `stop()`, `check_backend(backend) -> bool`.
- **Called by:** `main.py`, `scripts/phase5_acceptance.py`, `tests/test_health.py`.
- **Calls:** `http_connection.read_response`/`serialize_request`, `registry`.

### `load_balancer/health/__init__.py`
- **Purpose:** Package marker.

---

## Metrics

### `load_balancer/metrics/snapshot.py`
- **Purpose:** Metric definitions and export — process CPU/RSS, interpolated percentiles, full run snapshot, JSON/CSV writers.
- **API:** `process_usage() -> dict`, `percentile(samples, p) -> float | None`, `collect_run_snapshot(proxy, registry, strategy_name, started_at=None) -> dict`, `write_json(snapshot, path)`, `append_summary_csv(snapshot, path, run_id)`, `append_backends_csv(snapshot, path, run_id)`; headers `SUMMARY_HEADER`, `BACKENDS_HEADER`.
- **Called by:** `metrics/__init__.py`, `metrics/dumper.py`, `wrk_parser.py` (status flattening), `scripts/benchmark_matrix.py`, instrumentation tests.
- **Calls:** `ctypes` (psapi on Windows), `/proc` on Linux, `csv`, `json`, `time`.

### `load_balancer/metrics/dumper.py`
- **Purpose:** Periodic atomic JSON writer of the live snapshot.
- **API:** `async dump_loop(proxy, registry, strategy_name, path, interval=1.0)` — writes via temp file + `os.replace`, tolerates transient `OSError`.
- **Called by:** `main.py` when `LB_METRICS_DUMP_PATH` is set; instrumentation tests.
- **Calls:** `snapshot.collect_run_snapshot`, `snapshot.write_json`.

### `load_balancer/metrics/wrk_parser.py`
- **Purpose:** Parses wrk console output and appends combined wrk+proxy CSV rows.
- **API:** `parse_wrk_output(text) -> dict` (threads, connections, requests, duration, RPS, p50/p75/p90/p99, socket errors, non-2xx), `COMBINED_HEADER` (24 columns), `append_combined_csv(wrk, snapshot, path, run_id, mode)`.
- **Called by:** `scripts/parse_wrk.py`, `scripts/benchmark_matrix.py`, `scripts/build_report_data.py`, `scripts/build_charts.py`, tests.
- **Calls:** `snapshot._flatten_status_counts`, `csv`, `re`.

### `load_balancer/metrics/haproxy_stats.py`
- **Purpose:** Converts HAProxy `;csv` stats into the same snapshot shape as the custom proxy.
- **API:** `parse_haproxy_stats_csv(text) -> dict` (frontends/backends/servers, status classes, stot/req_tot); `synthesize_snapshot(stats, strategy_name, pxname="be_roundrobin", captured_at=None, frontend_pxname=None) -> dict` (latency percentiles intentionally empty, raises on unknown pxname).
- **Called by:** `scripts/haproxy_acceptance.py`, `scripts/benchmark_matrix.py`, `scripts/failure_benchmark.py`, `tests/test_haproxy_metrics.py`.
- **Calls:** `csv`, `io`, `time`.

### `load_balancer/metrics/__init__.py`
- **Purpose:** Public façade re-exporting `collect_run_snapshot`, `percentile`, `write_json`, `append_summary_csv`, `append_backends_csv`.
- **Called by:** `scripts/benchmark_matrix.py`, `tests/test_instrumentation.py`.

---

## Automation scripts

### `scripts/benchmark_matrix.py`
- **Purpose:** Phase 8 matrix runner: 18 cases (2 LBs × 3 strategies × 50/200/500 clients), 10 s warm-up and 3 × 60 s measured runs per case; writes raw wrk/snapshot evidence and appends structured CSVs.
- **API:** `Case` dataclass (`mode`, `strategy`, `clients`, `port`, `pxname`, `frontend`, `slug`); `build_cases()`, `wrk_threads(clients)`, `make_run_id(case, run_index, now)`, `median_of(values)`, `select_cases(cases, start, limit)`, `run_case_safe(case, index, log_path, **kwargs)`, `parse_docker_stats(text)`, `cpu_percent_delta(prev_cpu, cpu, prev_t, now_t)`, `wrk_command(case, run_id, duration_s)`, `snapshot_kwargs(case)`, `append_row(path, header, row)`, `run(cmd, timeout=180)`, `http_ok(url)`, `ensure_farm()`, `start_custom_proxy(case, run_id)`, `port_available(port)`, `stop_custom_proxy(proc, log)`, `restart_haproxy()`, `run_wrk(case, run_id, duration_s, warmup=False)`, `ResourceSampler` (`start()`, `finish()`), `run_case(case, run_index, phase, duration_s, warmup_s)`, `main()`.
- **CLI flags:** `--dry-run`, `--phase`, `--runs`, `--limit`, `--start`, `--duration`, `--warmup`, `--smoke`.
- **Called by:** CLI; imports in `failure_benchmark.py`, `build_charts.py`, and benchmark tests.
- **Calls:** `haproxy_acceptance` helpers, `metrics` exports, `haproxy_stats.synthesize_snapshot`, `wrk_parser`, Docker/wrk subprocesses.

### `scripts/build_charts.py`
- **Purpose:** Builds the Phase 8 median table, environment record and SVG charts from structured CSVs.
- **API:** `baseline_rows(manifest, combined)` (joins by `run_id`, maps percentile columns by name), `aggregate_medians(rows)`, `mean_resource(rows, run_ids, role)`, `failure_rows(path)`, `bar_chart_svg(title, categories, series)`, `line_chart_svg(title, categories, series)`, `share_chart_svg(summary, clients)`, `write_environment(path)`, `main()`.
- **Called by:** CLI; `build_report_plots.py` reuses `bar_chart_svg`; `test_benchmark_charts.py`.
- **Calls:** `wrk_parser.COMBINED_HEADER`, PowerShell for environment facts.

### `scripts/build_report_data.py`
- **Purpose:** Derives every figure quoted in the final report into `results/structured/report_data.csv` with an evidence column.
- **API:** `evenness_pct(shares)`, `max_share_pct(shares)`, `parse_shares(text)`, `read_wrk_text(path)` (BOM-aware UTF-16), `parse_timeline(path)`, `build_rows(medians, failures, keepalive, close, snapshot, timelines, haproxy_snapshot=None)`, `write_csv(path, rows)`, `main()` (`--out`).
- **Called by:** CLI; `build_report_plots.py` reuses `parse_timeline`; `test_report_data.py`.
- **Calls:** `wrk_parser.parse_wrk_output`.

### `scripts/build_report_plots.py`
- **Purpose:** Produces the two Phase 9 plots missing from Phase 8.
- **API:** `timeline_cases(failures, timelines)`, `event_timeline_svg(cases)` (kill → DOWN → load end → illustrative restart → UP markers), `cpu_comparison_svg(medians)` (3 normalized algorithm groups × 2 implementations, no fabricated zeros), `main()` (`--out`).
- **Called by:** CLI; `test_report_plots.py`.
- **Calls:** `build_charts.bar_chart_svg`, `build_report_data.parse_timeline`.

### `scripts/parse_wrk.py`
- **Purpose:** Combines one raw wrk output and one proxy snapshot into a row in `combined.csv`.
- **API:** `main()` with `--wrk`, `--snapshot`, `--run-id`, `--mode custom|haproxy`, `--csv`; private `_read_text` handles UTF-16 shell redirects.
- **Called by:** standalone CLI.
- **Calls:** `wrk_parser.parse_wrk_output`, `append_combined_csv`.

### `scripts/validate_report.py`
- **Purpose:** Structural validation of `docs/final_report.md`: links resolve, 10 questions present, 8 required plots exist, glance-table numbers traceable to `report_data.csv`.
- **API:** `linked_result_paths(text)`, `glance_figures(text)`, `validate(report, repo_root) -> list[str]`, `main()`, constant `REQUIRED_PLOTS`.
- **Called by:** CLI; `test_report_validation.py`.

### `scripts/haproxy_acceptance.py`
- **Purpose:** Phase 7 HAProxy acceptance: frontend routing contracts, kill/recovery detection through the stats CSV, and version/config evidence capture.
- **API:** `run(cmd)`, `http_get(url)`, `get_stats()`, `server_status(stats, pxname, server)`, `wait_for(pred, timeout, step, what)`, `ensure_stack(log=None)`, `hit(port)`, `frontend_checks(log)`, `env_path(run_id)`, `save_environment(run_id)`, `acceptance(log)`, `write_raw(run_id, log)`, `main()` (`--no-start`).
- **Called by:** CLI; `benchmark_matrix.py` and `failure_benchmark.py` reuse `get_stats`, `server_status`, `wait_for`, `save_environment`.
- **Calls:** `haproxy_stats`, Docker commands.

### `scripts/failure_benchmark.py`
- **Purpose:** Phase 8 timed failure test: 60 s wrk, kill `lb-backend-2` at t=30 s, watch health transitions, then separate recovery/re-admission check; writes `failures.csv` and raw timelines.
- **API:** `failure_cases()`, `kill_at(duration_s)`, `custom_transitions(snapshot, backend_id)`, `is_down(status)`, `is_fully_up(status)`, `status_counter_delta(before, after, statuses)`, `detection_failure_delta(before, at_down)`, `recovery_ok(counts, victim, min_hits=5, sticky=False, healthy=True)`, `wrk_output_valid(text)`, `http_get(url)`, `HealthWatcher` (`record`, `arm`, `start`, `finish`), `snapshot_now(case, dump)`, `ensure_victim_running()`, `all_backends_healthy(case, dump)`, `run_failure_case(case, run_id, duration_s)`, `main()`.
- **CLI flags:** `--runs`, `--duration`, `--start`, `--limit`, `--smoke`.
- **Called by:** CLI; `test_failure_benchmark.py`.
- **Calls:** `benchmark_matrix` helpers, `haproxy_acceptance.get_stats`/`server_status`, `haproxy_stats.synthesize_snapshot`.

### `scripts/phase2_acceptance.py`
- **Purpose:** Phase 2 40-request round-robin spread check against a live proxy on :8080.
- **API:** `fetch(url, timeout=10)`, `hit_work()`, `main()`.
- **Called by:** standalone CLI.

### `scripts/phase4_acceptance.py`
- **Purpose:** Phase 4 algorithm acceptance: healthy-only selection, RR spread, least-active avoids a busy backend, IP-hash determinism; `--live` checks a running proxy.
- **API:** `StubBackend` (`start`, `close`), `one_request(port)`, `backend_ids(bodies)`, `run_acceptance()`, `run_live()`, `main()` (`--live`, `--strategy …`).
- **Called by:** standalone CLI.
- **Calls:** strategies, `BackendRegistry`, `Backend`, `ProxyServer`.

### `scripts/phase5_acceptance.py`
- **Purpose:** Phase 5 health-check acceptance: kill → DOWN (3 failures) → no routing → restart → UP (2 successes), in-process or `--live`.
- **API:** `StubBackend` (`start`, `close`), `one_request(port)`, `wait_until(pred, timeout, step)`, `traffic_loop(port, log, stop)`, `run_acceptance()`, `http_get(url)`, `run_live()`, `main()` (`--live`).
- **Called by:** standalone CLI.
- **Calls:** `HealthMonitor`, `BackendRegistry`, `RoundRobinStrategy`, `ProxyServer`.

---

## Infrastructure and configuration

### `docker-compose.yml`
- **Purpose:** Defines the four unequal-capacity backends on `lb-net` and the profile-gated HAProxy service.
- **Settings:** backend-1 8001/25 ms/16 slots, backend-2 8002/100 ms/8, backend-3 8003/300 ms/4, backend-4 8004/50 ms/6; healthcheck `GET /health` every 5 s (3 retries); `haproxy` service uses profile `haproxy`, digest-pinned image, ports 8091/8092/8093/8404, config bind-mount, syntax healthcheck, `depends_on` all backends.

### `haproxy/haproxy.cfg`
- **Purpose:** HAProxy 3.4 reference configuration over the same farm.
- **Settings:** `mode http`, `option http-server-close`, `retries 0`, 5 s connect / 30 s client/server/request/keep-alive, `timeout check 1s`, `default-server check inter 1s fall 3 rise 2`, `httpchk GET /health HTTP/1.1` + expect 200; frontends `fe_roundrobin :8091`, `fe_leastconn :8092`, `fe_source :8093`; matching `be_*` backends; stats on `:8404` (`/stats`, CSV `/stats;csv`). Documented differences: 503 vs 502, leastconn counts connections, source hash ≠ SHA-256, multi-threaded vs single asyncio.

### `backend/Dockerfile`
- **Purpose:** `python:3.12-slim` image installing backend requirements and running `uvicorn app.main:app --host 0.0.0.0 --port 8000`.

### `docker/wrk/Dockerfile`
- **Purpose:** Ubuntu 24.04 image with `wrk` installed, `ENTRYPOINT ["wrk"]`; built locally as `lb/wrk` because WSL→Windows-host traffic is blocked.

### `run.ps1`
- **Purpose:** Interactive PowerShell launcher: `-Stop`, `-Status`, or a menu (strategy, port, backend string); functions `Test-DockerDaemon`, `Test-PortListening`, `Get-PortOwnerPid`, `Show-Menu`, `Wait-FarmHealthy`, `Start-Proxy`; writes `.lb-run.pid`.

### `pytest.ini`
- **Purpose:** pytest configuration: `asyncio_mode = auto`, session-scoped event loop, `testpaths = load_balancer/tests`, Windows warning filters.

### `.env.example`
- **Purpose:** Template for running a backend outside Compose: `BACKEND_ID`, `DEFAULT_WORK_DELAY_MS`, `MAX_DELAY_MS`, `MAX_CONCURRENCY`.

### `requirements-dev.txt`
- **Purpose:** Host dependencies for the proxy: `h11`, `pytest`, `pytest-asyncio` (plus the implicit standard library).

### `backend/requirements.txt`
- **Purpose:** Backend dependencies: `fastapi`, `uvicorn[standard]`.

---

## Tests (`load_balancer/tests/`)

### `load_balancer/tests/conftest.py`
- **Purpose:** Shared fixtures: `FakeBackend` real-TCP HTTP stub and `fake_backend_factory`; forces `WindowsSelectorEventLoopPolicy` on Windows.

### `load_balancer/tests/test_algorithms.py`
- **Purpose:** Backend model defaults, registry health filtering/transitions, and correctness of round robin, least-active and IP hash (including remapping when a backend is removed).
- **Classes:** `TestBackendModel`, `TestRegistry`, `TestRoundRobin`, `TestLeastActiveRequests`, `TestIpHash`.

### `load_balancer/tests/test_config.py`
- **Purpose:** Environment parsing, backend list validation, strategy wiring and health-config validation.
- **Classes:** `TestBackendListParsing`, `TestStrategyWiring`, `TestHealthConfigWiring`.

### `load_balancer/tests/test_health.py`
- **Purpose:** Timestamped state transitions and `HealthMonitor` probe/threshold loop.
- **Classes:** `TestTransitionRecording`, `TestHealthMonitor`.

### `load_balancer/tests/test_http_connection.py`
- **Purpose:** h11 request/response reading and serialization, body limits, framing.
- **Classes:** `TestReadRequest`, `TestReadResponse`, `TestSerializeRequest`, `TestSerializeResponse`.

### `load_balancer/tests/test_persistent_connection.py`
- **Purpose:** `HttpStreamReader` keep-alive sequencing and close semantics.
- **Classes:** `TestSequentialRequests`, `TestCloseSemantics`, `TestReaderOwnedSerialization`.

### `load_balancer/tests/test_failure_paths.py`
- **Purpose:** Upstream timeout → 502, write timeout, structured error logs and end-to-end error contract.
- **Classes:** `TestUpstreamTimeout`, `TestErrorContractE2E`, `TestWriteTimeout`, `TestStructuredLogs`.

### `load_balancer/tests/test_proxy.py`
- **Purpose:** Forwarding engine, persistent client connections, header/body review fixes, strategy integration, counters and failure paths.
- **Classes:** `TestForwarding`, `TestPersistentClientConnections`, `TestReviewFixes`, `TestStrategyIntegration`, `TestCounterAccounting`, `TestFailurePaths`.

### `load_balancer/tests/test_instrumentation.py`
- **Purpose:** Per-status counters, latency samples/percentiles, run snapshots, JSON/CSV export, wrk parsing, combined CSV and the atomic metrics dump loop.
- **Classes:** `TestStatusCounters`, `TestLatencySamples`, `TestPercentile`, `TestRunSnapshot`, `TestExport`, `TestWrkParser`, `TestCombinedCsv`, `TestDumpLoop`.

### `load_balancer/tests/test_haproxy_metrics.py`
- **Purpose:** HAProxy stats CSV parsing and snapshot synthesis, including transitional statuses, no-server responses and combined CSV output.
- **Classes:** `TestParseHaproxyStats`, `TestSynthesizeSnapshot`.

### `load_balancer/tests/test_backend_concurrency_limit.py`
- **Purpose:** Script-style manual probe of backend-3's 4-slot semaphore with 20 parallel `/work` requests.

### `load_balancer/tests/test_benchmark_runner.py`
- **Purpose:** Pure logic of the matrix runner (case expansion, wrk command, run IDs, medians, stats parsing, CPU deltas, case selection, port availability, sampler behavior).

### `load_balancer/tests/test_benchmark_charts.py`
- **Purpose:** Manifest↔combined join, median aggregation, resource means, failure-row parsing, SVG generators and 18×3 dataset invariants.

### `load_balancer/tests/test_failure_benchmark.py`
- **Purpose:** Failure-case coverage, victim transitions, status semantics, counter deltas, sticky recovery, watcher arming and victim restart.

### `load_balancer/tests/test_report_data.py`
- **Purpose:** Evenness/max-share math, UTF-16 decode, timeline parsing and report figure derivation.

### `load_balancer/tests/test_report_plots.py`
- **Purpose:** Failure-timeline markers, CPU grouping/aliases/caveat, and timeline-case derivation.

### `load_balancer/tests/test_report_validation.py`
- **Purpose:** Link extraction, missing artifact/question detection, untraceable glance figures and final-report validity.

### `load_balancer/tests/test_documentation.py`
- **Purpose:** Asserts every created source/config file has a section in this document.

### `load_balancer/tests/__init__.py`
- **Purpose:** Test package marker.

---

## Generated artifacts (catalogue)

- `results/structured/combined.csv` — 24-column wrk+proxy rows (`mode=custom|haproxy`); written by `wrk_parser.append_combined_csv` via `benchmark_matrix.py`/`parse_wrk.py`.
- `results/structured/backends.csv` — per-run per-backend requests/shares/health; written by `snapshot.append_backends_csv`.
- `results/structured/benchmark_runs.csv` — manifest linking each run id to its case and raw files; written by `benchmark_matrix.append_row`.
- `results/structured/resources.csv` — 2 s CPU/memory samples for LB and wrk containers; written by `benchmark_matrix.ResourceSampler`.
- `results/structured/failures.csv` — detection/failure/recovery figures for the 6 failure cases; written by `failure_benchmark.py`.
- `results/structured/baseline_medians.csv` — 18 median rows; written by `build_charts.py`.
- `results/structured/benchmark_environment.txt` — machine/version/config evidence; written by `build_charts.write_environment`.
- `results/structured/haproxy_environment_<run_id>.txt` — per-run HAProxy image/config/git evidence; written by `haproxy_acceptance.save_environment`.
- `results/structured/report_data.csv` — 251 report figures with evidence column; written by `build_report_data.py`.
- `results/plots/*.svg` — rps/p50/p75/p90/p99 by load, backend shares, failure detection/request charts, failure timeline and CPU comparison; written by `build_charts.py` and `build_report_plots.py`.
- `results/raw/*` — gitignored raw wrk output, snapshots, logs and failure timelines; retained locally so charts can be regenerated.

