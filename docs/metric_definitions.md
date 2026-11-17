# Metric definitions

One definition per metric, used identically for the custom proxy and for
HAProxy (Phase 7). The CSV schemas in `load_balancer/metrics/`
(`COMBINED_HEADER` in `wrk_parser.py`, `SUMMARY_HEADER` /
`BACKENDS_HEADER` in `snapshot.py`) are the machine-readable form of
this document.

## Run identity

| Metric | Definition |
|---|---|
| `run_id` | `YYYYMMDD-HHMMSS` at run start (local time); unique per run. Every raw evidence file carries it in its filename; raw files are **never** overwritten, structured CSVs are append-only. |
| `mode` | `custom` (this project's proxy) or `haproxy` (Phase 7). Same row schema for both. |
| `strategy` | `round_robin` \| `least_active_requests` \| `ip_hash` for custom; the equivalent HAProxy balance directive name for HAProxy runs. |
| `connections` / `threads` | wrk's `-c` / `-t` values, as parsed from wrk output. Concurrency level of the run. |
| `duration_s` | The wall-clock length of the load window as reported by wrk (`requests in <d>s`), seconds. |

## Request outcomes (per run)

| Metric | Definition |
|---|---|
| `wrk_requests` | Total requests wrk completed in the window (client view). |
| `wrk_requests_per_sec` | wrk's `Requests/sec` (client-observed throughput). |
| `wrk_socket_errors` | wrk's `Socket errors: connect/read/write/timeout` counts; `0;0;0;0` flattened when absent. |
| `wrk_non_2xx` | wrk's `Non-2xx or 3xx responses` count when present (HTTP-level failures wrk saw). |
| `proxy_requests_total` | Requests the proxy answered (including error responses). Custom: `counters["requests_total"]`. HAProxy: from its statistics page (cumulative since start minus a pre-run reading, or the HC `-D`/stats delta for the window). |
| `proxy_status_counts` | Exact count per HTTP status the proxy returned (`200:3398;502:4` flattened). Custom: `counters["status_counts"]`. HAProxy: sum of per-frontend/per-backend HTTP response status counters over the window (its stats expose 2xx/4xx/5xx classes; class counts are the comparable form — see notes below). |
| `proxy_errors` | Internal proxy errors that did not become a status (client vanished mid-flight). Custom: `counters["errors"]`. HAProxy: has no direct equivalent; left empty for HAProxy runs. |

### Status-class note (fair comparison)

The custom proxy counts exact statuses; HAProxy exposes 2xx/3xx/4xx/5xx
classes. For side-by-side tables use the **class aggregates**: `2xx =
status_counts[200..299] summed`, `5xx = status_counts[500..599] summed`
(custom) vs. HAProxy's `hrsp_2xx` / `hrsp_5xx`. The per-status detail is
kept for custom-run diagnosis only.

## Latency (per run)

| Metric | Definition |
|---|---|
| `wrk_p50/p75/p90/p99_ms` | Latency distribution wrk prints with `--latency`, normalized to milliseconds. Client-side, includes DNS-free connect + request + response on persistent connections. |
| `wrk_thread_latency_avg_ms` | wrk's Thread Stats Latency Avg, ms. |
| `proxy_p50/p95/p99_ms` | Full request lifetime measured **at the proxy** with a monotonic clock (`time.perf_counter`): from the start of reading the request to the response fully drained to the client. Stored per request (`proxy.latencies_ms`), exported as linear-interpolation percentiles: `idx = (p/100) * (n-1)` on the sorted samples; empty sample set → empty field. HAProxy runs: HAProxy's stats `timer` percentiles or, for strict comparability, wrk-side percentiles only — the proxy-internal measurement has no HAProxy equivalent. |

Both latency families are reported side by side; **comparisons across
implementations use the wrk-side numbers** (same generator, same clock,
same vantage point).

## Per backend

| Metric | Definition |
|---|---|
| `requests` | Requests delivered to the backend (custom: `Backend.total_requests`; HAProxy: backend `stot`/request counters over the window). |
| `share_pct` | A backend's delivered requests / total delivered across **all** backends (a backend that served traffic before dying keeps its true share), rounded to 2 decimals. |
| `active_requests` | Requests currently in flight at the backend (custom: `Backend.active_requests` at snapshot time). |
| `healthy` | Boolean rotation membership at snapshot time. |
| `transitions` | Health state changes `state@unix_timestamp` (`down@1731812345.123|up@...`), recorded only on actual state change by `BackendRegistry`. HAProxy equivalent for comparison: its server UP/DOWN events in logs (`Server X is DOWN/UP`). |

## Failure-test metrics (phase acceptance scripts)

| Metric | Definition |
|---|---|
| `backend kill timestamp` | The time the kill command was issued (`t0`). |
| `health-check DOWN timestamp` | When the registry recorded the backend DOWN (`state_transitions` entry); custom only. HAProxy: time of its DOWN log line. |
| `failure detection time` | DOWN timestamp − kill timestamp (custom). For HAProxy: DOWN log timestamp − kill timestamp. Active-check detection ≈ `failures × (timeout + interval)` when the host silently drops connects, ≈ `failures × interval` when refuses are instant. |
| `failed requests during detection window` | Requests issued in `(t0, DOWN]` that did not return 200 (client view, from the acceptance script's request log). |
| `requests routed to the failed backend after it was killed` | Requests in the window that returned non-200 due to the dead backend (custom: all non-200s in the window are such, since no retry exists). |

## CPU and memory

`proxy CPU / memory`, `backend CPU / memory`: sampled with external
tools during the window (custom proxy: host process; backends: `docker
stats` snapshot at midpoint and end, logged into the run's raw file).
Not yet automated — captured manually per run until the Phase 7
comparison needs them, then scripted with the same definitions for both
implementations.
