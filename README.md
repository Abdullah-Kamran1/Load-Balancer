# README — asyncio load balancer

An educational L7 HTTP reverse proxy built with Python `asyncio` + `h11`,
compared against HAProxy under controlled benchmarks. The implementation
plan lives in `../load_balancer_project_plan.md`.

## Quick start

```powershell
docker compose up -d --build       # start the backend farm
curl http://localhost:8001/health  # verify backend-1
```

## Backend farm (Phase 1)

All backends run the same FastAPI app from `backend/` and serve identical
endpoints:

- `GET /` — backend ID, request ID, timestamp
- `GET /health` — HTTP 200 while alive
- `GET /work[?delay_ms=N]` — artificial work; defaults to the configured delay
- `GET /stats` — local counters (total / in-flight / successful / busy / errors)

Capacity is modelled by two knobs configured per backend via environment
variables:

| Backend   | Host port | `DEFAULT_WORK_DELAY_MS` | `MAX_CONCURRENCY` | Profile |
|-----------|-----------|------------------------:|------------------:|---------|
| backend-1 | 8001      | 25                      | 16                | fast    |
| backend-2 | 8002      | 100                     | 8                 | medium  |
| backend-3 | 8003      | 300                     | 4                 | slow    |
| backend-4 | 8004      | 50                      | 6                 | mixed   |

Behaviour notes:

- Over an `asyncio.Semaphore` limit, `/work` answers **503** (`busy`) rather
  than queueing, so saturation is observable by clients and later by the
  load balancer's health checks.
- Capacity differences come only from configuration — the request path is
  identical for every backend.
- `docker stop lb-backend-2` kills one instance without affecting the rest;
  `docker start lb-backend-2` brings it back.

## Environment variables (backend)

| Variable                | Default  | Meaning                                |
|-------------------------|----------|----------------------------------------|
| `BACKEND_ID`            | backend-1 | Identity reported on every response   |
| `DEFAULT_WORK_DELAY_MS` | 50       | Delay used when `?delay_ms` is absent  |
| `MAX_DELAY_MS`          | 60000    | Upper clamp for `?delay_ms`            |
| `MAX_CONCURRENCY`       | 8        | Semaphore slots for `/work`            |

See `.env.example` for the copy-paste template.
