# Asyncio Load Balancer Project — Implementation Plan

## 1. Recommended technology stack

Use **FastAPI for the backend services**, but build the load balancer as a separate Python asyncio application. Keep the proxy independent of FastAPI: FastAPI is an application framework, while the proxy's job is to handle HTTP connections, choose an upstream, forward traffic, and manage backend health.

| Component | Recommended technology | Why |
|---|---|---|
| Backend services | Python + FastAPI + Uvicorn | Easy health endpoint, configurable delay, and controllable backend capacity |
| Custom load balancer | Python `asyncio` + `h11` | You own the TCP sockets and HTTP/1.1 request/response lifecycle without writing an HTTP parser from scratch |
| Containerization | Docker + Docker Compose | Reproducible 3–4 backend instances and isolated test environments |
| Reference load balancer | HAProxy | Mature, production-grade comparison |
| Load generation | `wrk` (primary), `hey` (secondary) | Repeatable concurrent HTTP load and latency/throughput measurements |
| Metrics | Python logging + CSV; optionally Prometheus later | Simple, reproducible measurements without adding unnecessary infrastructure |
| Packet inspection | Wireshark / `tcpdump` | Observe TCP connections, keep-alive, and failure behaviour |
| Tests | `pytest` + `pytest-asyncio` | Test algorithms, health transitions, and proxy behaviour |

### Why `asyncio` + `h11` for the proxy?

I recommend this over writing the proxy with FastAPI or using Flask:

- `asyncio.start_server()` gives the project direct control over accepted TCP connections.
- `h11` handles HTTP/1.1 message framing and protocol state. You still implement the connection lifecycle, forwarding, routing, and error handling.
- It keeps the proxy genuinely educational: you can see the difference between a TCP connection and multiple HTTP requests carried over a persistent connection.
- It avoids spending the project debugging a hand-written HTTP parser.

Do **not** write HTTP parsing manually. Correctly handling content lengths, chunked bodies, keep-alive, and protocol edge cases is a project of its own.

FastAPI is for the backends, not the load balancer. The proxy should be a standalone asyncio process.

### Important scope decision

The core project is an **L7 HTTP reverse proxy**. It accepts HTTP requests, reads the request metadata, chooses a healthy backend, and forwards the request. Do not attempt L4 TCP balancing in the initial implementation. Add it only as an optional extension after the L7 proxy and HAProxy comparison work.

---

## 2. High-level architecture

```text
                         Load generator
                         wrk / hey
                              |
                              | HTTP/1.1
                              v
                    +--------------------+
                    |  Custom LB          |
                    |  asyncio + h11      |
                    |                    |
                    |  - Round robin     |
                    |  - Least requests  |
                    |  - IP hash         |
                    |  - Health monitor  |
                    |  - Metrics         |
                    +--------------------+
                       |      |      |
                HTTP   |      |      |   HTTP
                       v      v      v
                    +-----+ +-----+ +-----+     (+ optional 4th)
                    | B1  | | B2  | | B3  |
                    |FastAPI|FastAPI|FastAPI|
                    +-----+ +-----+ +-----+
                       Docker Compose network

        Repeat the same benchmark with HAProxy in front of the same backends.
```

Each backend should expose:

- `GET /` — returns its backend ID, request ID, and useful diagnostic information.
- `GET /health` — returns HTTP 200 when healthy.
- `GET /work?delay_ms=...` — performs configurable artificial work and returns the backend ID.
- Optional `GET /stats` — returns backend-local request counters.

The load balancer should expose its own administrative endpoints, bound to a management port:

- `GET /lb/health` — proxy process health.
- `GET /lb/backends` — backend state, active request count, and request totals.
- `GET /lb/stats` — aggregate counters and algorithm currently selected.

Keep management endpoints separate from the public proxy listener where practical.

---

## 3. Repository structure

Create the following structure before implementation. It keeps the backend, proxy, deployment, and experiments separate.

```text
async-load-balancer/
|
|-- backend/
|   |-- app/
|   |   |-- main.py
|   |   |-- config.py
|   |   `-- metrics.py
|   |-- Dockerfile
|   `-- requirements.txt
|
|-- load_balancer/
|   |-- main.py
|   |-- config.py
|   |-- models.py
|   |-- http_connection.py
|   |-- proxy.py
|   |-- algorithms/
|   |   |-- base.py
|   |   |-- round_robin.py
|   |   |-- least_connections.py
|   |   `-- ip_hash.py
|   |-- health/
|   |   |-- checker.py
|   |   `-- registry.py
|   |-- metrics/
|   |   |-- collector.py
|   |   `-- exporter.py
|   `-- tests/
|       |-- test_algorithms.py
|       |-- test_health.py
|       `-- test_proxy.py
|
|-- haproxy/
|   `-- haproxy.cfg
|
|-- scripts/
|   |-- run_benchmark.sh
|   |-- kill_backend.sh
|   `-- collect_results.py
|
|-- results/
|   |-- raw/
|   `-- plots/
|
|-- docker-compose.yml
|-- .env.example
|-- README.md
`-- requirements-dev.txt
```

You can adjust filenames as the implementation develops, but preserve the separation of responsibilities.

---

# Implementation phases

## Phase 0 — Environment and project skeleton

**Goal:** Get the development environment and Docker network working before writing proxy logic.

### Steps

1. Install Python 3.11 or newer.
2. Install Docker Desktop and verify Docker Compose works.
3. Install `wrk`. On Windows, use WSL2 for the Linux-based benchmark tools if needed.
4. Create the repository structure above.
5. Create a Python virtual environment for local development.
6. Add dependencies:
   - Backend: `fastapi`, `uvicorn[standard]`
   - Proxy: `h11`
   - Tests: `pytest`, `pytest-asyncio`
   - Optional later: `httpx` for health-check requests
7. Create a minimal Docker Compose file with one FastAPI backend.
8. Verify that the backend is reachable from both the host and another container.

### Deliverable

A running FastAPI container reachable through Docker networking.

### Acceptance test

- `GET /health` returns HTTP 200.
- The service starts using Docker Compose.
- The backend is reachable by its Compose service name from another container.

---

## Phase 1 — Build configurable FastAPI backends

**Goal:** Build 3–4 backend instances whose capacities can be controlled and measured.

### Steps

1. Implement `GET /health`.
2. Implement `GET /` returning:
   - backend ID (for example, `backend-1`)
   - request ID
   - current timestamp
3. Implement `GET /work` with configurable artificial delay.
4. Add a concurrency limit using an `asyncio.Semaphore` so each backend can model a finite amount of simultaneous work.
5. Add counters:
   - total requests received
   - requests currently being processed
   - successful requests
   - request errors
6. Make backend ID, delay, and concurrency limit configurable through environment variables.
7. Add 3 or 4 backend services to Docker Compose.
8. Give them intentionally unequal capacities. For example:
   - Backend 1: low delay, higher concurrency
   - Backend 2: medium delay, medium concurrency
   - Backend 3: high delay, lower concurrency
   - Backend 4 (optional): another capacity profile
9. Keep the test workload identical across backends. Use backend configuration to create the capacity difference rather than changing the request URL for different backends.

### Important design note

An artificial sleep alone does not model all forms of server capacity. Use both:
- configurable processing delay; and
- a concurrency limit.

Document each backend's configured capacity so benchmark results can be interpreted.

### Deliverable

A Docker Compose backend farm with 3–4 independently configurable FastAPI services.

### Acceptance test

- Each backend responds with its own ID.
- Each backend's health endpoint works.
- `/work` produces the configured delay.
- Backend-local counters change as requests arrive.
- Stopping one backend does not stop the others.

---

## Phase 2 — Implement the basic asyncio HTTP reverse proxy

**Goal:** Accept HTTP/1.1 requests and forward them to a backend. Start with one request per client connection; add persistent connections after basic forwarding works.

### Steps

1. Implement the TCP listener using `asyncio.start_server()`.
2. Use `h11` to parse incoming HTTP/1.1 requests and serialize responses.
3. Implement a backend configuration model containing:
   - backend ID
   - host and port
   - healthy/unhealthy state
   - active request count
   - total requests sent
4. Implement a simple round-robin selector.
5. For each incoming request:
   - parse the request;
   - select a healthy backend;
   - open or acquire an upstream connection;
   - forward the request;
   - stream the response back to the client;
   - update counters and release resources.
6. Return HTTP 503 when no healthy backend is available.
7. Handle connection failures and timeouts without crashing the proxy.
8. Add structured logs containing request ID, client IP, selected backend, status code, and duration.
9. Initially close the client and upstream connection after one request. This deliberately limits scope while you validate the forwarding path.

### HTTP correctness requirements

- Preserve the HTTP method, path, query string, and relevant headers.
- Do not forward hop-by-hop headers blindly (`Connection`, `Keep-Alive`, `Transfer-Encoding`, `Upgrade`, and headers named by `Connection`).
- Correctly handle request and response bodies, including streaming.
- Avoid reading an unbounded body into memory.
- Set sensible connection and request timeouts.
- Ensure error paths close or release sockets and decrement active counters.

### Deliverable

A working L7 reverse proxy using round robin.

### Acceptance test

- Requests through the proxy reach all healthy backends.
- Responses contain the correct backend ID.
- A stopped backend does not crash the proxy.
- No-backend condition returns HTTP 503.
- Repeated requests complete without steadily increasing open sockets or active counters.

---

## Phase 3 — Add HTTP/1.1 persistent connections

**Goal:** Understand and support multiple HTTP requests over one client TCP connection.

### Steps

1. Replace the one-request-per-connection behaviour with a per-connection request loop.
2. Use `h11` connection state to determine when a request is complete and whether the client intends to keep the connection alive.
3. Support multiple sequential requests on the same client connection.
4. Ensure request bodies are fully consumed before parsing the next request.
5. Handle `Connection: close` correctly.
6. Decide and document whether the upstream connection is:
   - opened for each request initially; or
   - pooled and reused in the advanced version.
7. Test with `wrk` using keep-alive and compare against a run that closes connections frequently.
8. Use Wireshark to verify that multiple HTTP requests can travel over one TCP connection.

### Important distinction

With persistent HTTP/1.1, a client can reuse one TCP connection for many sequential requests. Therefore, **active TCP connections and active HTTP requests are not the same metric**.

For this project's least-connections algorithm, define the metric explicitly. The recommended core definition is **least active proxied requests**. This makes the algorithm meaningful even when a client keeps its TCP connection open while idle. Call it “least active requests” in the report, and explain that it is the L7 analogue of least connections. If you later implement true connection-count balancing, report it as a separate variant.

### Deliverable

A proxy that correctly handles persistent client connections.

### Acceptance test

- Multiple sequential requests succeed over one client connection.
- `Connection: close` closes the connection.
- Keep-alive tests do not cause request parsing or response framing errors.
- Wireshark confirms TCP connection reuse.

---

## Phase 4 — Implement all three balancing algorithms

**Goal:** Make backend selection pluggable so algorithms can be compared without changing proxy code.

Create a common selector interface such as:

```python
class LoadBalancingStrategy:
    def select_backend(self, client_ip, healthy_backends):
        raise NotImplementedError
```

### A. Round robin

1. Maintain a shared index.
2. Select the next healthy backend.
3. Advance the index safely when concurrent requests arrive.
4. Skip unhealthy backends.
5. Test that requests are distributed evenly when all backends have equal capacity.

### B. Least active requests

1. Read each healthy backend's active proxied request count.
2. Select the backend with the smallest count.
3. Define a deterministic tie-breaker, such as round robin among tied backends.
4. Increment the count when a request is assigned.
5. Decrement it in a `finally` block after completion or failure.
6. Test that counters never become negative, including when a client disconnects or an upstream fails.

### C. IP hash

1. Extract the client IP from the actual TCP peer address.
2. Hash the IP deterministically.
3. Map the hash to a healthy backend.
4. Document that the mapping can change when the healthy backend set changes.
5. Do not trust `X-Forwarded-For` from arbitrary clients. Only use forwarded client IP headers if a trusted upstream proxy is explicitly configured.

### Steps

1. Implement each algorithm in its own module.
2. Add an environment variable or command-line option to choose the strategy.
3. Add unit tests using a fixed backend list.
4. Add integration tests through the running proxy.
5. Log the selected backend for every request.

### Deliverable

Round robin, least active requests, and IP hash selectable at startup.

### Acceptance test

- Each strategy selects only healthy backends.
- Round robin cycles through available backends.
- Least active requests responds to different active request counts.
- IP hash is deterministic for the same client IP and backend set.

---

## Phase 5 — Active health checks and backend state management

**Goal:** Detect failed backends, remove them from rotation, and re-admit them when they recover.

### Steps

1. Implement a background health-check task for every backend.
2. Send `GET /health` once per second.
3. Use a short health-check timeout.
4. Maintain consecutive success and failure counters.
5. Mark a backend **DOWN after 3 consecutive failures**.
6. Mark a DOWN backend **UP after a successful recovery check**. Consider requiring 2 consecutive successes to avoid flapping; if you do, document this recovery threshold.
7. Ensure every load-balancing strategy sees only healthy backends.
8. Record state transitions with timestamps.
9. Measure failure detection time from the moment the backend is killed until the proxy marks it DOWN.
10. Test recovery by restarting the backend.

### State model

```text
             3 consecutive failures
       +--------------------------------+
       |                                v
    +------+                         +------+
    | UP   |                         | DOWN |
    +------+                         +------+
       ^                                |
       |                                |
       +--------------------------------+
             successful recovery check
```

### Deliverable

Active health checks with automatic backend removal and re-admission.

### Acceptance test

- Kill one backend while traffic is running.
- It is marked DOWN after the configured failure threshold.
- New requests stop being assigned to it.
- Restart it and verify it returns to rotation after the recovery rule is met.
- Record how many requests fail during the detection window.

---

## Phase 6 — Add observability and benchmark instrumentation

**Goal:** Collect enough data to explain performance, not just produce a requests-per-second number.

### Metrics to collect

**Per run**
- algorithm / HAProxy mode
- concurrency level
- test duration
- request rate
- total requests
- successful and failed requests
- p50, p95, and p99 latency
- proxy CPU and memory
- backend CPU and memory where practical

**Per backend**
- requests received
- active requests over time
- share of total requests
- health state and state transition timestamps

**During failure tests**
- backend kill timestamp
- health-check DOWN timestamp
- failure detection time
- failed requests during the detection window
- requests routed to the failed backend after it was killed

### Steps

1. Add aggregate request and error counters to the proxy.
2. Record per-request latency using a monotonic clock.
3. Export snapshots to JSON or CSV.
4. Add a script that parses `wrk` output and combines it with proxy/backend counters.
5. Save every raw run separately. Do not overwrite earlier results.
6. Use the same metric definitions for the custom proxy and HAProxy.

### Deliverable

A repeatable results directory containing raw benchmark output and structured CSV files.

---

## Phase 7 — Configure HAProxy as the reference implementation

**Goal:** Compare your proxy with a mature load balancer under equivalent conditions.

### Steps

1. Add HAProxy to Docker Compose.
2. Configure the same backend services used by the custom proxy.
3. Configure HTTP mode and equivalent health checks:
   - check `/health`;
   - interval of approximately 1 second;
   - mark DOWN after 3 failures;
   - configure recovery checks and thresholds consistently.
4. Configure separate HAProxy frontends or configurations for:
   - round robin;
   - least connections;
   - source/IP hashing.
5. Confirm the exact HAProxy algorithm names and semantics in the installed HAProxy version's documentation.
6. Keep timeouts, backend list, health checks, and test conditions as similar as possible.
7. Expose HAProxy statistics for diagnostics if useful.

### Fair-comparison rules

- Use the same backend containers and backend capacity settings.
- Run one load balancer at a time where possible.
- Use the same client machine, load generator, request path, payload, and duration.
- Keep HTTP keep-alive behaviour consistent.
- Record configuration differences that cannot be made identical.
- Do not claim that two algorithms are equivalent if their semantics differ. In particular, HAProxy's least-connection behaviour counts server-side connections, while your core L7 selector is defined as least active requests.

### Deliverable

A working HAProxy configuration with comparable balancing and health-check settings.

### Acceptance test

- HAProxy routes requests to all healthy backends.
- It removes a failed backend from rotation.
- It recovers a backend after restart.
- Its configuration and version are saved with benchmark results.

---

## Phase 8 — Run the benchmark matrix

**Goal:** Compare algorithms under controlled load and observe failure behaviour.

### Baseline test matrix

Run each algorithm at each concurrency level:

| Load balancer | Algorithm | Concurrent clients | Duration |
|---|---|---:|---:|
| Custom proxy | Round robin | 50 | 60 s |
| Custom proxy | Round robin | 200 | 60 s |
| Custom proxy | Round robin | 500 | 60 s |
| Custom proxy | Least active requests | 50 / 200 / 500 | 60 s each |
| Custom proxy | IP hash | 50 / 200 / 500 | 60 s each |
| HAProxy | Round robin | 50 / 200 / 500 | 60 s each |
| HAProxy | Least connections | 50 / 200 / 500 | 60 s each |
| HAProxy | Source hash | 50 / 200 / 500 | 60 s each |

Run each case at least 3 times and report the median across runs. If time permits, do 5 runs.

### Failure test

For each selected algorithm:

1. Start a 60-second load test.
2. Kill one backend at `t = 30 s`.
3. Keep the load generator running.
4. Capture health-state transitions and request failures.
5. Restart the backend after the test and verify re-admission in a separate recovery test.

Use the same failure timing and backend for the custom proxy and HAProxy.

### Metrics to report

- Requests per second
- p50 latency
- p95 latency
- p99 latency
- Requests received by each backend
- Failed requests during backend failure
- Failure detection time
- Proxy / HAProxy CPU usage
- Proxy / HAProxy memory usage

### Important benchmark controls

- Use a fixed request path and payload.
- Use a fixed artificial workload.
- Ensure the load generator is not the bottleneck.
- Run tests from the same machine and Docker configuration.
- Warm up the system before recording a run.
- Record CPU, memory, Python version, Docker version, HAProxy version, and machine specs.
- Separate normal-operation tests from backend-failure tests.
- Keep raw outputs so charts can be regenerated.

### Deliverable

A complete benchmark dataset and comparison charts.

---

## Phase 9 — Analysis and final report

**Goal:** Explain what the measurements show and connect them to networking concepts.

### Questions your report should answer

1. Does round robin distribute requests evenly?
2. Does even distribution produce good performance when backends have unequal capacity?
3. Does least active requests improve latency or throughput for unequal backends?
4. How does IP hash affect distribution and repeatability?
5. What happens to IP-hash distribution when a backend is removed?
6. How long does each load balancer take to detect a backend failure?
7. How many requests fail during the health-check detection window?
8. Does HTTP keep-alive change throughput, latency, or connection counts?
9. Why can active TCP connections differ from active HTTP requests?
10. Where does the custom proxy fall behind HAProxy, and why?

### Required plots

- Requests/s by algorithm and concurrency.
- p50 / p95 / p99 latency by algorithm.
- Per-backend request distribution.
- Failure timeline showing backend state and request failures.
- CPU usage comparison.
- Optional: throughput or latency over time during the backend-kill test.

### Deliverable

A report with methodology, configurations, results, charts, limitations, and conclusions.

---

# Optional advanced extensions

Do these only after all core phases and benchmarks are complete.

1. **Dynamic weights:** adjust backend weights using measured response time or capacity. Smooth measurements to avoid unstable weight oscillation.
2. **Consistent hashing:** compare it with basic modulo-based IP hashing, especially when a backend is added or removed.
3. **Connection draining:** stop assigning new requests to a backend while allowing in-flight requests to finish before shutdown.
4. **TLS termination:** terminate HTTPS at the proxy and forward HTTP to backends; document certificate handling and security implications.
5. **L4 vs L7 comparison:** build a separate TCP pass-through proxy and compare it with the HTTP-aware L7 proxy. Do not describe the L7 proxy as L4 balancing.
6. **Upstream connection pooling:** reuse backend TCP connections and measure the effect on connection setup overhead.
7. **Prometheus + Grafana:** add dashboards if the basic CSV instrumentation is already correct.

---

# Recommended implementation order

Follow this order rather than implementing everything at once:

1. Docker Compose + one FastAPI backend.
2. Three configurable FastAPI backends.
3. Basic asyncio + h11 proxy with round robin.
4. Correct HTTP/1.1 request/response forwarding.
5. Persistent client connections.
6. Least active requests and IP hash.
7. Active health checks and recovery.
8. Metrics and automated benchmark scripts.
9. HAProxy configuration.
10. Benchmark matrix and failure tests.
11. Report and optional extensions.

## Definition of done

The core project is complete when:

- 3–4 unequal-capacity FastAPI backends run in Docker Compose.
- The custom asyncio proxy supports round robin, least active requests, and IP hash.
- HTTP/1.1 persistent client connections work.
- Health checks run every second, mark a backend DOWN after 3 consecutive failures, and re-admit it after recovery.
- HAProxy runs against the same backends with documented comparable settings.
- 50, 200, and 500-client tests run for 60 seconds.
- A backend is killed at 30 seconds during a failure test.
- Throughput, p50/p95/p99 latency, per-backend request counts, failure detection time, failed requests, and CPU usage are recorded.
- The final report explains the results and the differences between TCP connections and HTTP requests.

## Final recommendation

Keep the **core implementation focused on an L7 HTTP/1.1 reverse proxy**. Use FastAPI/Uvicorn for the backend farm, `asyncio` + `h11` for the custom proxy, Docker Compose for deployment, HAProxy as the reference, and `wrk` for load generation. This combination is educational, achievable, and directly aligned with the assignment's networking objectives.
"""

