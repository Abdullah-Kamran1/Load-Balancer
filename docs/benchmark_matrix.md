# Phase 8 benchmark matrix

This document records how the Phase 8 dataset was produced and how to
regenerate it. The plan's baseline matrix, failure test and metric list are
in `load_balancer_project_plan.md` (lines 503-562).

## Environment

| Item | Value |
|---|---|
| Host | 12th Gen Intel Core i7-12700H, 20 logical CPUs, 15.7 GiB RAM |
| Python | 3.15.2 (`.venv`), asyncio single-thread custom proxy |
| Docker | 29.9.3 client/server, Desktop on Windows 11 |
| HAProxy | `haproxy:3.4-alpine@sha256:b9fe316f…` → 3.4.7-8872a26 (per-run `haproxy_environment_<run_id>.txt`) |
| Backend farm | same four containers as Phases 1-7: 25 ms/16, 100 ms/8, 300 ms/4, 50 ms/6 slots |
| Workload | `GET /work?delay_ms=25` (fixed path + fixed artificial delay) |
| Duration | 60 s measured per run, 10 s warm-up per run, 3 runs per case, median reported |

`results/structured/benchmark_environment.txt` is regenerated with the
dataset and holds the exact versions/config hashes used.

## Cases and controls

18 baseline cases: {custom proxy, HAProxy} × {3 algorithms} ×
{50, 200, 500 clients}. Algorithms: custom `round_robin`,
`least_active_requests`, `ip_hash`; HAProxy `roundrobin`, `leastconn`,
`source` (:8091/:8092/:8093). Their semantics are **not** claimed equivalent
(see `docs/haproxy_setup.md`).

- One load balancer serves traffic at a time; each case starts from a fresh
  process/container so proxy counters begin at zero. For HAProxy a warm-up
  run is followed by a restart before the measured run (stats are cumulative).
- wrk runs in the local `lb/wrk` container against `host.docker.internal`.
  Threads: 4 at 50/200 clients, 8 at 500. Its CPU is sampled alongside the
  LB's; low wrk CPU (<100% of one core) confirms the generator is not the
  bottleneck.
- Fixed request path/payload/delay, same machine and Docker configuration for
  both LBs, identical health checks/timeouts (see `docs/haproxy_setup.md`).
- CPU is sampled every 2 s: the custom proxy reports its own
  `process_time()`/RSS in its 1 s metrics dump (Windows reports 0 CPU for
  harness-spawned processes via Get-Process/WMI); HAProxy/wrk use
  `docker stats`. The units are both "% of one core" and MiB but the
  accounting mechanisms differ; compare cautiously.
- Raw wrk output, proxy snapshots and resources stay on disk under
  `results/raw/` (gitignored) so charts can be regenerated; summaries and
  per-run environment evidence are tracked under `results/structured/`.

## Reproduce

```powershell
# farm must be up; Docker Desktop must be running
.\.venv\Scripts\python.exe scripts\benchmark_matrix.py --dry-run
.\.venv\Scripts\python.exe scripts\benchmark_matrix.py --phase baseline --runs 3

# a single case/run (cases are listed by --dry-run, 1-based --start)
.\.venv\Scripts\python.exe scripts\benchmark_matrix.py --phase baseline --start 2 --limit 1 --runs 1

# timed failure + recovery: 60 s load, backend-2 killed at t=30 s
.\.venv\Scripts\python.exe scripts\failure_benchmark.py

# medians, environment and SVG charts
.\.venv\Scripts\python.exe scripts\build_charts.py
```

Outputs:
- `results/structured/benchmark_runs.csv` — manifest linking each run id to
  its case and raw files;
- `results/structured/combined.csv`, `backends.csv` — per-run proxy metrics;
- `results/structured/resources.csv` — LB/wrk CPU and memory samples;
- `results/structured/baseline_medians.csv` — one median row per case;
- `results/structured/failures.csv` — detection/failure/recovery per case;
- `results/plots/*.svg` — comparison charts (tracked deliverables);
- `results/raw/benchmark_baseline.log`, `benchmark_errors.csv`,
  `failure_<run_id>.txt`, `wrk_<run_id>.txt`, `snapshot_<run_id>.json`.

## Failure and recovery test

For each of the six mode/algorithm pairs at 200 clients, a 60 s wrk run is
started, `lb-backend-2` is killed at exactly t=30 s, and the load continues.
A watcher records the first DOWN/UP from the proxy's health state (custom
metrics dump) or the HAProxy stats CSV; HAProxy transitional states
(`UP 2/3`, `DOWN 1/2`) are handled as documented in `docs/haproxy_setup.md`.
Failed requests are the delta of 5xx/502/503 counters across the kill, with
wrk's `Non-2xx` count recorded for cross-checking. After the run the backend
is restarted in a separate recovery phase: direct `/health`, LB health UP,
and 12 follow-up requests confirm re-admission (sticky algorithms are judged
by health state because one client IP need not map to the recovered server).
The victim and timing are identical across both LBs.

## Results

All values are medians of three 60 s runs. "5xx median" is the proxy's
5xx-class response count: for both implementations these are overwhelmingly
**backend 503s** (the small farm's 34 concurrency slots saturate under wrk),
not load-balancer crashes. p50/p95/p99 are wrk-side client latencies.

| Mode | Strategy | Clients | RPS median | p50 ms | p95 ms | p99 ms | 5xx median | LB CPU% | wrk CPU% |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| custom | ip_hash | 50 | 360.05 | 128.43 | 195.71 | 296.47 | 14009 | 86.8 | 2.09 |
| custom | ip_hash | 200 | 389.8 | 479.99 | 715.98 | 1020.0 | 16280 | 79.11 | 2.62 |
| custom | ip_hash | 500 | 129.35 | 236.27 | 286.06 | 388.07 | 5170 | 16.2 | 20.52 |
| custom | least_active_requests | 50 | 518.95 | 99.41 | 182.46 | 289.76 | 20276 | 86.72 | 2.62 |
| custom | least_active_requests | 200 | 488.57 | 128.64 | 649.14 | 1050.0 | 23012 | 78.93 | 3.03 |
| custom | least_active_requests | 500 | 180.24 | 265.47 | 321.61 | 430.22 | 7122 | 18.69 | 22.32 |
| custom | round_robin | 50 | 943.07 | 31.7 | 103.73 | 198.42 | 48945 | 84.31 | 5.3 |
| custom | round_robin | 200 | 242.68 | 553.23 | 600.58 | 644.85 | 2592 | 59.34 | 4.48 |
| custom | round_robin | 500 | 171.1 | 257.25 | 337.65 | 801.57 | 6220 | 20.74 | 27.16 |
| haproxy | leastconn | 50 | 3585.22 | 8.29 | 20.22 | 31.49 | 152629 | 170.31 | 33.13 |
| haproxy | leastconn | 200 | 3726.16 | 48.25 | 65.46 | 72.65 | 150851 | 203.99 | 39.14 |
| haproxy | leastconn | 500 | 3599.87 | 133.05 | 148.88 | 158.84 | 143832 | 194.45 | 41.27 |
| haproxy | roundrobin | 50 | 3698.23 | 14.14 | 27.43 | 30.0 | 146566 | 164.57 | 33.57 |
| haproxy | roundrobin | 200 | 4137.79 | 44.93 | 86.45 | 124.04 | 173841 | 186.15 | 36.7 |
| haproxy | roundrobin | 500 | 4107.09 | 97.61 | 279.18 | 394.0 | 172164 | 186.25 | 40.21 |
| haproxy | source | 50 | 1784.65 | 22.49 | 24.54 | 47.66 | 88809 | 56.27 | 13.04 |
| haproxy | source | 200 | 1904.72 | 101.84 | 109.8 | 125.47 | 96068 | 57.67 | 13.32 |
| haproxy | source | 500 | 1790.7 | 270.48 | 285.85 | 298.16 | 89282 | 58.16 | 14.12 |

Backend shares (median row per case in `baseline_medians.csv`) show round robin
and least-active/leastconn near 25% each, while custom `ip_hash` pins one
client IP (96.7% on one backend at 200 clients; other clients map elsewhere
over the run) and HAProxy `source` pins 100% to backend-2. These are different
hash functions; no equivalence is claimed.

### Failure and recovery (50 clients, backend-2 killed at t=30 s)

| Mode | Strategy | Detection s | Failed (5xx delta) | wrk non-2xx | Recovery s | Re-admitted |
|---|---|---:|---:|---:|---:|---|
| custom | round_robin | 6.744 | 26383 | 26823 | >60 | True |
| custom | least_active_requests | 6.795 | 27278 | 28355 | 56.245 | True |
| custom | ip_hash | 5.88 | 11301 | 13629 | 3.389 | True |
| haproxy | roundrobin | 4.249 | 52848 | 115979 | 2.544 | True |
| haproxy | leastconn | 4.6 | 67092 | 136795 | 2.457 | True |
| haproxy | source | 4.458 | 44382 | 86406 | 2.339 | True |

Detection is measured from the kill to the first DOWN state after the kill;
failed requests are the delta of 5xx/502/503 counters across that moment.
wrk's `non-2xx` totals cover the whole 60 s run (mostly capacity 503s) and are
only a cross-check. Re-admission was verified with 40 requests after the
backend restarted; sticky strategies are judged by health state because one
client IP need not map to the recovered server. The two long custom recovery
times reflect health probes queued behind the load's aftermath; all six cases
did eventually serve the recovered backend.

### Interpretation limits

- The farm is intentionally small. At every tested concurrency wrk pushes
  more traffic than the backends can serve, so throughput mostly measures how
  quickly 503s are returned. This dataset is a **saturation** comparison;
  Phase 9 should not read it as a pure proxy-efficiency comparison.
- HAProxy is multi-threaded and runs in a container; the custom proxy is one
  asyncio host process. CPU percentages are indicative, not directly
  comparable (self-reported `process_time()` vs `docker stats`).
- wrk CPU stayed below ~42% of one core, so the load generator was not the
  bottleneck. Python/Docker/HAProxy versions, machine specs and config hashes
  are in `results/structured/benchmark_environment.txt`.

### Charts

`results/plots/`: `rps_by_load.svg`, `p50_by_load.svg`, `p95_by_load.svg`,
`p99_by_load.svg`, `backend_shares.svg`, `failure_detection.svg`,
`failure_requests.svg`.
