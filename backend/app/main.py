"""Configurable FastAPI backend (Phase 1).

Each backend instance models a finite amount of capacity via:
- an artificial per-request processing delay (/work), and
- a concurrency limit enforced with an asyncio.Semaphore.

When all capacity is in use, /work answers 503 instead of queueing forever.
This lets load-balancing algorithms be compared against backends that saturate.

Environment configuration lives in config.py; counters live in metrics.py.

Endpoints:
- GET /        -> backend ID, request ID, timestamp
- GET /health  -> HTTP 200 while the process is alive
- GET /work    -> artificial work with configurable delay
- GET /stats   -> backend-local counters
"""

import asyncio
import time

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from .config import Settings, load_settings
from .metrics import Metrics

settings = load_settings()
metrics = Metrics()

app = FastAPI(title="async-load-balancer backend", version="1.0")

# One unit of work = one request inside the semaphore. Acquiring the last
# slot takes the counter to zero; further requests are rejected as busy.
_work_slots = asyncio.Semaphore(settings.max_concurrency)


@app.get("/")
async def root():
    metrics.request_started()
    try:
        snap = metrics.snapshot()
        return {
            "backend_id": settings.backend_id,
            "request_id": snap.total_requests,
            "timestamp": time.time(),
            "uptime_seconds": snap.uptime_seconds,
        }
    finally:
        metrics.request_finished()
        metrics.record_success()


@app.get("/health")
async def health():
    return {"status": "ok", "backend_id": settings.backend_id}


@app.get("/work")
async def work(
    delay_ms: int | None = Query(
        default=None,
        ge=0,
        description="Artificial work duration in milliseconds. "
        "Defaults to the backend's configured DEFAULT_WORK_DELAY_MS.",
    ),
):
    delay = settings.default_delay_ms if delay_ms is None else delay_ms
    delay = min(max(delay, 0), settings.max_delay_ms)

    metrics.request_started()

    # No await between the locked() check and the acquire, so on a single
    # event loop this is effectively atomic: a busy backend rejects, it does
    # not queue. That is what models finite capacity.
    if _work_slots.locked():
        metrics.request_finished()
        metrics.record_busy()
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "1"},
            content={
                "backend_id": settings.backend_id,
                "error": "busy",
                "detail": "Concurrency limit reached; retry later.",
            },
        )

    started = time.perf_counter()
    try:
        async with _work_slots:
            await asyncio.sleep(delay / 1000.0)
    except Exception:
        metrics.request_finished()
        metrics.record_error()
        raise

    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    metrics.request_finished()
    metrics.record_success()
    return {
        "backend_id": settings.backend_id,
        "requested_delay_ms": delay,
        "actual_ms": elapsed_ms,
    }


@app.get("/stats")
async def stats():
    snap = metrics.snapshot()
    return {
        "backend_id": settings.backend_id,
        "total_requests": snap.total_requests,
        "in_flight": snap.in_flight,
        "successful_requests": snap.successful_requests,
        "error_responses": snap.error_responses,
        "busy_rejections": snap.busy_rejections,
        "uptime_seconds": snap.uptime_seconds,
        "configured": {
            "default_delay_ms": settings.default_delay_ms,
            "max_delay_ms": settings.max_delay_ms,
            "max_concurrency": settings.max_concurrency,
        },
    }
