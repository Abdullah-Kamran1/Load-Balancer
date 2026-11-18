# HAProxy reference setup (Phase 7)

HAProxy 3.4.7 runs as `lb-haproxy` (`haproxy:3.4-alpine`, digest
`sha256:b9fe316f718d5015c5cfebd8ca019591f3e44656f61d93cb8b59a1028961e530`)
on the same Docker network and backend farm as the custom proxy.

## Run it

```powershell
# farm + HAProxy (profile-gated: a plain `docker compose up -d` starts only
# the farm, so the custom proxy environment is unchanged)
docker compose --profile haproxy up -d

# stop just HAProxy (a profile service is not stopped by plain compose stop)
docker stop lb-haproxy

# version + checks
docker exec lb-haproxy haproxy -vv
docker exec lb-haproxy haproxy -c -f /usr/local/etc/haproxy/haproxy.cfg
```

| Port | Frontend | Balance directive |
|---:|---|---|
| 8091 | `fe_roundrobin` → `be_roundrobin` | `balance roundrobin` |
| 8092 | `fe_leastconn` → `be_leastconn` | `balance leastconn` |
| 8093 | `fe_source` → `be_source` | `balance source` |
| 8404 | stats page `/stats`, CSV `/stats;csv` | — |

Only the frontend under test receives load, so one load balancer handles a
run at a time. Config: `haproxy/haproxy.cfg`.

## Algorithm names and semantics (confirmed for 3.4.7)

The Alpine image ships no documentation, so the names/semantics were
confirmed in the 3.4.7 manual (<https://docs.haproxy.org/3.4/configuration.html>)
and all three directives were accepted by `haproxy -c` in the image:

- `roundrobin` — "Each server is used in turns, according to their
  weights." Dynamic; server weights can change on the fly.
- `leastconn` — "The server with the lowest number of connections receives
  the connection. Round-robin is performed within groups of servers of the
  same load... It will also consider the number of queued connections."
- `source` — "The source IP address is hashed and divided by the total
  weight of the running servers to designate which server will receive the
  request. This ensures that the same client IP address will always reach
  the same server as long as no server goes down or up."

**Not equivalent to the custom strategies** (plan rule: do not claim
equivalence where semantics differ):

| Custom proxy | HAProxy | Difference |
|---|---|---|
| `round_robin` | `roundrobin` | Same intent; HAProxy is weighted and re-admits a recovered server after a very short delay. |
| `least_active_requests` (in-flight requests) | `leastconn` (server-side connections, plus queued) | Different unit. With HTTP keep-alive + `http-server-close`, a connection ≈ one request here, but the definitions are not identical. |
| `ip_hash` (SHA-256 of client IP modulo healthy servers) | `source` (map-based hash of source IP) | Both sticky; different hash functions and remapping behaviour when the server set changes. |

## Health checks and timeouts (matched to the custom proxy)

| Custom proxy setting | HAProxy directive |
|---|---|
| `GET /health`, success = HTTP 200 | `option httpchk GET /health` + `http-check expect status 200` |
| `LB_HEALTH_INTERVAL=1.0` | `default-server inter 1s` |
| `LB_HEALTH_TIMEOUT=1.0` | `timeout check 1s` |
| `LB_HEALTH_FAILURES=3` → DOWN | `fall 3` |
| `LB_HEALTH_RECOVERIES=2` → UP | `rise 2` |
| `connect_timeout=5.0` | `timeout connect 5s` |
| `request_timeout=30.0` (read + idle) | `timeout http-request 30s`, `timeout client 30s`, `timeout http-keep-alive 30s`, `timeout server 30s` |
| one upstream connection per request | `option http-server-close` (client keep-alive preserved) |
| no upstream retry | `retries 0` |

HAProxy reports transitional health as `UP 2/3`, `UP 1/3` while the fall
counter drains; only `DOWN` means removed from rotation (`DOWN 1/2` is the
mirror during recovery). Acceptance logic must test `status == "UP"` /
`status.startswith("DOWN")`, not merely "not UP".

Observed on this Windows/Docker host: a killed backend's address silently
drops connects, so each failed probe consumes `timeout check 1s` and then
waits `inter 1s`. DOWN therefore lands at ≈ `3 x (1s + 1s)`, the same
mechanism documented for the custom proxy. Measured acceptance run:
**4.09 s** kill → DOWN, 3 failed client requests in the window, re-admitted
2.47 s after the restarted container served `/health`, then ~25 % share.

## Recorded configuration/measurement differences

These cannot be made identical and are reported with benchmark results
(plan fair-comparison rule):

- **Error status**: HAProxy answers `503` when no server is reachable; the
  custom proxy answers `502`.
- **Threading**: HAProxy runs its natural multi-threaded configuration
  (default `nbthread` = detected CPUs); the custom proxy is one asyncio
  thread. CPU numbers reflect this.
- **Status detail**: HAProxy exposes class counters (`hrsp_2xx`…`hrsp_5xx`),
  the custom proxy exact statuses. Comparisons use class aggregates
  (`docs/metric_definitions.md`).
- **Client aborts count as 4xx**: in the demo run (wrk `-t2 -c10 -d8s`,
  11 135 requests) HAProxy reported `4xx: 5` with `cli_abrt: 5` on the
  backend row and no corresponding server-side 4xx. These are wrk's
  end-of-run connection closes, not application errors; wrk itself
  reported no `Non-2xx` line. Do not read HAProxy `4xx` as "failed
  requests" without checking `cli_abrt`/wrk's own error lines.
- **Stats are cumulative since HAProxy start**. Restart `lb-haproxy`
  before a benchmark run so a run's row contains only that run, or
  subtract a pre-run reading (Phase 8 must do one of these).
- **Latency**: HAProxy's stats expose averages (`qtime`/`ctime`/`rtime`/
  `ttime`), not percentiles. The synthesized snapshot leaves proxy
  percentiles empty; cross-implementation latency uses wrk-side numbers.
- **Host header**: the custom proxy rewrites `Host` to the backend; HAProxy
  forwards the client's `Host` unchanged. The test backends ignore it.
- **Frontend `stot` is connections**, while server `stot` is
  requests/sessions under `http-server-close`. The snapshot synthesizer
  takes `connections_total` from the FRONTEND row and request counts from
  the server rows.

## Acceptance + evidence

`scripts/haproxy_acceptance.py` starts the stack, checks each frontend's
routing contract, kills `lb-backend-2` under traffic on `:8091`, waits for
stats `DOWN`, checks post-DOWN routing, restarts the container, waits for
`UP` and sustained ~25 % traffic, then writes:

- `results/structured/haproxy_environment.txt` — image, exact version,
  full `haproxy -vv`, config hash, verbatim config, Docker/git versions
  (tracked with benchmark results);
- `results/raw/haproxy_acceptance_<run_id>.txt` — timeline and failures;
- `results/raw/haproxy_snapshot_<run_id>.json` — synthesized snapshot.

Benchmark row pipeline (mode `haproxy`):

```powershell
# restart HAProxy first so its counters start at zero
docker restart lb-haproxy
$runId = Get-Date -Format "yyyyMMdd-HHmmss"
docker run --rm lb/wrk -t2 -c10 -d60s --latency `
  "http://host.docker.internal:8091/work?delay_ms=5" > "results/raw/haproxy_wrk_$runId.txt"

.\.venv\Scripts\python.exe -c @"
import sys; sys.path.insert(0, '.')
from pathlib import Path
from scripts.haproxy_acceptance import get_stats
from load_balancer.metrics.haproxy_stats import synthesize_snapshot
from load_balancer.metrics import write_json, append_backends_csv
snap = synthesize_snapshot(get_stats(), 'roundrobin', 'be_roundrobin',
                           frontend_pxname='fe_roundrobin')
write_json(snap, Path('results/raw/haproxy_snapshot_$runId.json'))
append_backends_csv(snap, Path('results/structured/backends.csv'), run_id='$runId')
"@

.\.venv\Scripts\python.exe scripts\parse_wrk.py `
  --wrk "results/raw/haproxy_wrk_$runId.txt" `
  --snapshot "results/raw/haproxy_snapshot_$runId.json" `
  --run-id $runId --mode haproxy
```
