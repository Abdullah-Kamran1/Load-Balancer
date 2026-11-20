# Runbook — commands, configuration and testing

This is the operational guide. For architecture see
[ARCHITECTURE_FLOW.md](ARCHITECTURE_FLOW.md); for per-file details see
[FILE_REFERENCE.md](FILE_REFERENCE.md). All commands are PowerShell, run from
the repository root (`D:\Work\LoadBalancer`).

## Prerequisites

- Windows 10/11 with **Docker Desktop** (Linux containers, WSL2 integration).
  WSL → Windows-host traffic is blocked in this environment, which is why wrk
  runs in a container.
- Python 3.11+ (the recorded runs used 3.15.2) and Git.
- 4 CPU cores / 8 GiB RAM recommended (recorded host: i7-12700H, 20 logical
  CPUs, 15.7 GiB).

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
docker compose up -d --build
docker build -t lb/wrk docker/wrk
```

Verify the farm:

```powershell
docker compose ps
curl http://127.0.0.1:8001/health
```

## Running

### Custom proxy (host process)

```powershell
$env:LB_STRATEGY = 'round_robin'
.\.venv\Scripts\python.exe -m load_balancer.main
```

Or use the interactive launcher (menu: strategy, port, custom backend string):

```powershell
.\run.ps1
.\run.ps1 -Status
.\run.ps1 -Stop
```

### HAProxy reference (container)

```powershell
docker compose --profile haproxy up -d
docker exec lb-haproxy haproxy -vv
curl http://127.0.0.1:8404/stats
```

Ports: custom `:8080`; HAProxy `:8091` round robin, `:8092` least connections,
`:8093` source hash, stats `:8404`. **Run one load balancer at a time.**

### A single wrk run

The example below assumes the **HAProxy** reference environment is running;
change the port to `:8080` for the custom proxy started in the previous section.

```powershell
$runId = Get-Date -Format "yyyyMMdd-HHMMSS"
docker run --rm lb/wrk -t2 -c10 -d8s --latency `
  "http://host.docker.internal:8091/work?delay_ms=5" > "results\raw\wrk_$runId.txt"
```

### Acceptance, benchmarks and reports

```powershell
.\.venv\Scripts\python.exe scripts\phase2_acceptance.py
.\.venv\Scripts\python.exe scripts\phase4_acceptance.py --live --strategy ip_hash
.\.venv\Scripts\python.exe scripts\phase5_acceptance.py --live
.\.venv\Scripts\python.exe scripts\haproxy_acceptance.py

.\.venv\Scripts\python.exe scripts\benchmark_matrix.py --dry-run
.\.venv\Scripts\python.exe scripts\benchmark_matrix.py --phase baseline --runs 3
.\.venv\Scripts\python.exe scripts\failure_benchmark.py
.\.venv\Scripts\python.exe scripts\build_charts.py
.\.venv\Scripts\python.exe scripts\build_report_data.py
.\.venv\Scripts\python.exe scripts\build_report_plots.py
.\.venv\Scripts\python.exe scripts\validate_report.py
```

Combine a manual wrk output with a saved snapshot:

```powershell
.\.venv\Scripts\python.exe scripts\parse_wrk.py `
  --wrk "results\raw\wrk_$runId.txt" `
  --snapshot "results\raw\snapshot_$runId.json" `
  --run-id $runId --mode custom
```

Useful matrix flags: `--start N --limit M` to select cases (order from
`--dry-run`), `--smoke` for 8 s runs without warm-up, `--duration` and
`--warmup` to override timings. `haproxy_acceptance.py --no-start` skips
starting the stack. `build_report_data.py` and `build_report_plots.py` accept
`--out` to change their output location; `parse_wrk.py --csv` overrides the
destination CSV.

## Configuration

### Custom proxy (`load_balancer/config.py`, `load_balancer/main.py`)

| Variable | Default | Meaning |
|---|---|---|
| `LB_PROXY_HOST` | `127.0.0.1` | Listen address. |
| `LB_PROXY_PORT` | `8080` | Listen port. |
| `LB_BACKENDS` | `backend-1:127.0.0.1:8001` | Comma-separated `id:host:port` list. Use all four ports for the full farm. |
| `LB_STRATEGY` | `round_robin` | `round_robin`, `least_active_requests` or `ip_hash`. |
| `LB_HEALTH_INTERVAL` | `1.0` | Seconds between `/health` probes. |
| `LB_HEALTH_TIMEOUT` | `1.0` | Per-probe timeout (seconds). |
| `LB_HEALTH_FAILURES` | `3` | Consecutive failures before DOWN. |
| `LB_HEALTH_RECOVERIES` | `2` | Consecutive successes before UP. |
| `LB_METRICS_DUMP_PATH` | unset | If set, writes a run snapshot JSON every second (atomic replace). |

Full-farm example:

```powershell
$env:LB_BACKENDS = 'backend-1:127.0.0.1:8001,backend-2:127.0.0.1:8002,backend-3:127.0.0.1:8003,backend-4:127.0.0.1:8004'
```

### Backends (Docker Compose / `.env.example`)

| Variable | Default (Compose backend-1) | Meaning |
|---|---|---|
| `BACKEND_ID` | `backend-1` | Identity in JSON responses. |
| `DEFAULT_WORK_DELAY_MS` | `25` | Default `/work` delay. |
| `MAX_DELAY_MS` | `60000` | Upper clamp for `?delay_ms=`. |
| `MAX_CONCURRENCY` | `16` | Semaphore slots; 503 when full (8/4/6 on backends 2/3/4). |

### HAProxy

`haproxy/haproxy.cfg` is bind-mounted read-only; the image is pinned by digest
in `docker-compose.yml`. Change the config, then restart the container
(`docker restart lb-haproxy`): HAProxy resolves backend names at startup and
Compose does not recreate a service when only a mounted file changes.

## Testing

```powershell
# full suite (pytest.ini: asyncio auto, session loop)
.\.venv\Scripts\python.exe -m pytest -q

# a single area
.\.venv\Scripts\python.exe -m pytest load_balancer\tests\test_proxy.py -q
.\.venv\Scripts\python.exe -m pytest load_balancer\tests\test_haproxy_metrics.py -q

# documentation/evidence gates
.\.venv\Scripts\python.exe scripts\validate_report.py
```

Test layers: algorithm/config/unit tests; h11 framing and keep-alive tests;
proxy forwarding, 400/413/502/503 and counters; health thresholds and
transitions; instrumentation/export; HAProxy stats parsing; benchmark runner,
charts, failure, report and documentation artifact tests. Live acceptance
scripts exercise the real farm (they briefly kill/restart `lb-backend-2`).

## Troubleshooting

- **Docker daemon not reachable** — start Docker Desktop; `docker info` must succeed.
- **Port 8080 bind fails after a wrk run** — Windows `TIME_WAIT`; the runner now
  waits for a fresh bind (up to 90 s). Manually: wait ~30–60 s or restart.
- **HAProxy says "could not resolve address backend-2"** — a backend container was
  stopped when HAProxy restarted; `docker start lb-backend-2`, then
  `docker restart lb-haproxy`. The failure runner does this automatically.
- **`UP 2/3` / `DOWN 1/2`** — transitional health states; only plain `DOWN` means
  out of rotation and only plain `UP` means fully re-admitted.
- **wrk reports connection refused** — check that the selected frontend port is
  listening before starting wrk; the runners now wait for HTTP readiness.
- **5xx counts look huge** — the farm has 34 concurrency slots; wrk exceeds it at
  every tested concurrency, so most 5xx are backend 503s, not proxy failures.
- **No p95 chart** — wrk `--latency` emits p50/p75/p90/p99 only; custom snapshots
  have an internal p95 but HAProxy has no equivalent, so p90 is the closest
  comparable percentile.
- **Raw files look garbled in PowerShell** — older wrk captures are UTF-16; the
  `parse_wrk.py`/report scripts decode them automatically.
