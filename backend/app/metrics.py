"""Backend-local request counters.

All counters are updated from coroutines on a single asyncio event loop, so
plain integer increments are safe (no await happens between read and write).

These counters are read through ``GET /stats`` and, in later phases, scraped
by the load balancer and the benchmark scripts.
"""

import time
from dataclasses import dataclass


@dataclass
class Snapshot:
    total_requests: int
    in_flight: int
    successful_requests: int
    error_responses: int
    busy_rejections: int
    uptime_seconds: float


class Metrics:
    def __init__(self) -> None:
        self._started_at = time.time()
        self._total_requests = 0
        self._in_flight = 0
        self._successful = 0
        self._errors = 0
        self._busy = 0

    def request_started(self) -> None:
        self._total_requests += 1
        self._in_flight += 1

    def request_finished(self) -> None:
        self._in_flight -= 1

    def record_success(self) -> None:
        self._successful += 1

    def record_error(self) -> None:
        self._errors += 1

    def record_busy(self) -> None:
        self._busy += 1

    def snapshot(self) -> Snapshot:
        return Snapshot(
            total_requests=self._total_requests,
            in_flight=self._in_flight,
            successful_requests=self._successful,
            error_responses=self._errors,
            busy_rejections=self._busy,
            uptime_seconds=round(time.time() - self._started_at, 3),
        )
