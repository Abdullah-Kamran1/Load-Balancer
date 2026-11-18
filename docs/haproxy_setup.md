# HAProxy reference setup (Phase 7)

HAProxy 3.4.7 runs as `lb-haproxy` (`haproxy:3.4-alpine`, pinned by digest
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

The image reference is pinned by digest in `docker-compose.yml` so every
run uses the exact build whose version/config are recorded. Note that the
Compose healthcheck only syntax-checks the *mounted* file; it cannot tell
that a running HAProxy process still has the previous config loaded.
`scripts/haproxy_acceptance.py` handles that: if `lb-haproxy` started
before `haproxy.cfg` was last modified, it restarts the container before
testing (and therefore also starts from zeroed counters). After editing the
config by hand, run `docker restart lb-haproxy`.

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
| `GET /health`, success = HTTP 200 | `option httpchk GET /health HTTP/1.1` + `http-check expect status 200` (explicit HTTP/1.1, matching the custom monitor's h11 request) |
| `LB_HEALTH_INTERVAL=1.0` | `default-server inter 1s` |
| `LB_HEALTH_TIMEOUT=1.0` | `timeout check 1s` |
| `LB_HEALTH_FAILURES=3` → DOWN | `fall 3` |
| `LB_HEALTH_RECOVERIES=2` → UP | `rise 2` |
| `connect_timeout=5.0` | `timeout connect 5s` |
| `request_timeout=30.0` (read + idle) | `timeout http-request 30s`, `timeout client 30s`, `timeout http-keep-alive 30s`, `timeout server 30s` |
| one upstream connection per request | `option http-server-close` (client keep-alive preserved) |
| no upstream retry | `retries 0` |

HAProxy reports transitional health as `UP 2/3`, `UP 1/3` while the fall
counter drains — the server is still in rotation and receives traffic — and
`DOWN 1/2` while it recovers without yet receiving traffic. So:
`healthy` (snapshot/backends.csv) is `status.startswith("UP")`, acceptance
DOWN-detection is `status.startswith("DOWN")`, and UP-waiting is
`status == "UP"`. "Not UP" is not the same as DOWN.

Observed on this Windows/Docker host: a killed backend's address silently
drops connects, so each failed probe consumes `timeout check 1s` and then
waits `inter 1s`; HAProxy's check scheduling adds up to one `inter` of
phase, so DOWN lands roughly 4–6 s after the kill. Measured acceptance
runs: **4.09 s, 5.34 s, 5.52 s** kill → DOWN (request-assignment phase
changes the value run to run), with the victim still receiving traffic
only until the health check flips it. Re-admission: ~2.4 s after the
restarted container served `/health`, then ~25 % share.

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
- **No internal-error counter**: `proxy_errors` is left empty for HAProxy
  runs (the custom proxy counts client-vanished/internal errors). The CSV
  column is blank, not `0`.
- **Client aborts count as 4xx**: in the recorded demo run (wrk
  `-t2 -c10 -d8s`, 10 519 requests) the frontend row reported `4xx: 2`
  while the backend row reported `4xx: 1` with `cli_abrt: 1`, and no server
  row had a 4xx. These are wrk's end-of-run connection closes, not
  application errors; wrk itself reported no `Non-2xx` line. Do not read
  HAProxy `4xx` as "failed requests" without checking `cli_abrt`/wrk's own
  error lines.
- **Stats are cumulative since HAProxy start**. Restart `lb-haproxy`
  before a benchmark run so a run's row contains only that run, or
  subtract a pre-run reading (Phase 8 must do one of these).
- **Latency**: HAProxy's stats expose averages (`qtime`/`ctime`/`rtime`/
  `ttime`), not percentiles. The synthesized snapshot leaves proxy
  percentiles empty; cross-implementation latency uses wrk-side numbers.
- **Host header**: the custom proxy rewrites `Host` to the backend; HAProxy
  forwards the client's `Host` unchanged. The test backends ignore it.
- **Connection/request counters**: a FRONTEND row's `stot` counts client
  connections (e.g. 11 for a 10+1-connection wrk run) while server `stot`
  counts requests/sessions under `http-server-close`. `connections_total`
  therefore comes from the FRONTEND row; `requests_total` and the status
  classes come from that row's `req_tot`/`hrsp_*` (or the BACKEND row when
  no frontend is named), because a request that found no server (all DOWN/
  queued) never appears on a server row. Shares use the server rows.
- **Topology**: the custom proxy is a host process reached at
  `127.0.0.1:8000+`; HAProxy is a container on `lb-net` reached via a
  published port. Client→LB hop and LB→backend network path differ
  accordingly. `backends.csv` `host`/`port` therefore also differ in
  meaning: custom rows record the published backend endpoints, HAProxy rows
  the container IP:port HAProxy talks to. Shares/counts are unaffected.
- **Health-check volume**: one HAProxy container runs three backends'
  check sections, so each backend sees three probes per second (one per
  frontend) vs one per second from the custom monitor. `/health` bypasses
  the request semaphore, so capacity is unaffected.
- **Backend address resolution is static**: `server backend-N
  backend-N:8000` is resolved when HAProxy starts. `docker kill`/`start`
  keeps a container's IP, so failure/recovery works; a **recreated**
  backend (`compose up` after a config change, `--force-recreate`) gets a
  new IP and HAProxy keeps probing the old one. Restart `lb-haproxy` after
  recreating backends (Phase 8 must do this).
- **Run one load balancer at a time**: profile-gating keeps the compose
  environment unchanged, but if the custom proxy is running on the host,
  stop it before an HAProxy run so only one LB serves the backends.

## Acceptance + evidence

`scripts/haproxy_acceptance.py` starts the stack, checks each frontend's
routing contract, kills `lb-backend-2` under traffic on `:8091`, waits for
stats `DOWN`, checks post-DOWN routing, restarts the container, waits for
`UP` and sustained ~25 % traffic, then writes:

- `results/structured/haproxy_environment_<run_id>.txt` — image reference
  + id + digest, exact version, full `haproxy -vv`, config hash, verbatim
  config, Docker/git versions (tracked with benchmark results; one file per
  run so earlier evidence is never overwritten);
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
from scripts.haproxy_acceptance import get_stats, save_environment
from load_balancer.metrics.haproxy_stats import synthesize_snapshot
from load_balancer.metrics import write_json, append_backends_csv
snap = synthesize_snapshot(get_stats(), strategy_name='roundrobin',
                           pxname='be_roundrobin',
                           frontend_pxname='fe_roundrobin')
write_json(snap, Path('results/raw/haproxy_snapshot_$runId.json'))
append_backends_csv(snap, Path('results/structured/backends.csv'), run_id='$runId')
save_environment('$runId')
"@

.\.venv\Scripts\python.exe scripts\parse_wrk.py `
  --wrk "results/raw/haproxy_wrk_$runId.txt" `
  --snapshot "results/raw/haproxy_snapshot_$runId.json" `
  --run-id $runId --mode haproxy
```

For a `leastconn` or `source` run, use that frontend's port and the matching
names: `strategy_name='leastconn', pxname='be_leastconn',
frontend_pxname='fe_leastconn'` (`8092`), or `strategy_name='source',
pxname='be_source', frontend_pxname='fe_source'` (`8093`). The synthesizer
raises `ValueError` on an unknown pxname, so a typo cannot silently record
zero counters.
