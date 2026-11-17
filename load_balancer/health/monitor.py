"""Active health checks (Phase 5).

One background asyncio task per backend probes GET /health once per
interval and drives the registry's mark_up()/mark_down().

Semantics (documented rulings):
- Success = a valid HTTP response with status 200 received within the
  probe timeout. A non-200 status, a malformed response, a timeout, and
  a connection failure are ALL failures.
- A backend is marked DOWN after `failure_threshold` consecutive
  failures (default 3, from LB_HEALTH_FAILURES).
- A DOWN backend is marked UP again only after `recovery_threshold`
  consecutive successful checks (default 2, from LB_HEALTH_RECOVERIES).
  The plan requires a "successful recovery check" and explicitly permits
  a threshold: 2 consecutive successes avoid flipping a backend back
  into rotation on a single lucky response while it is crash-looping
  (anti-flapping).
- Detection time for an instantly-failing backend is approximately
  failure_threshold * interval: the loop probes immediately on start and
  then every interval, so the Nth consecutive failure happens at the
  (N-1)th sleep boundary.

The monitor is started/stopped explicitly by main.py (and integration
tests); ProxyServer stays decoupled from it.
"""

import asyncio
import logging

from ..http_connection import read_response, serialize_request
from ..models import Backend
from .registry import BackendRegistry

logger = logging.getLogger("lb.health")


class HealthMonitor:
    """Probes GET /health on every backend and drives registry state."""

    def __init__(
        self,
        registry: BackendRegistry,
        *,
        interval: float = 1.0,
        timeout: float = 1.0,
        failure_threshold: int = 3,
        recovery_threshold: int = 2,
        max_body_bytes: int = 65536,
    ) -> None:
        self._registry = registry
        self._interval = interval
        self._timeout = timeout
        self._failure_threshold = failure_threshold
        self._recovery_threshold = recovery_threshold
        self._max_body_bytes = max_body_bytes
        self._tasks: dict[str, asyncio.Task] = {}
        self._consecutive_failures: dict[str, int] = {}
        self._consecutive_successes: dict[str, int] = {}

    async def start(self) -> None:
        """Spawn one probe task per backend (immediate first check)."""
        for backend in self._registry.backends():
            self._tasks[backend.backend_id] = asyncio.create_task(
                self._probe_loop(backend),
                name=f"health-{backend.backend_id}",
            )

    async def stop(self) -> None:
        """Cancel all probe tasks and wait for them to finish."""
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _probe_loop(self, backend: Backend) -> None:
        try:
            while True:
                await self.check_backend(backend)
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            raise

    async def check_backend(self, backend: Backend) -> bool:
        """One probe cycle: request, evaluate, update counters and state.

        Returns True when the probe succeeded (status 200 in time).
        """
        success = await self._probe(backend)

        backend_id = backend.backend_id
        if success:
            self._consecutive_successes[backend_id] = (
                self._consecutive_successes.get(backend_id, 0) + 1
            )
            self._consecutive_failures[backend_id] = 0
            if (
                not backend.healthy
                and self._consecutive_successes[backend_id] >= self._recovery_threshold
            ):
                self._registry.mark_up(backend)
                logger.info(
                    "health: %s recovered after %d consecutive successes",
                    backend_id,
                    self._consecutive_successes[backend_id],
                )
                self._consecutive_successes[backend_id] = 0
        else:
            self._consecutive_failures[backend_id] = (
                self._consecutive_failures.get(backend_id, 0) + 1
            )
            self._consecutive_successes[backend_id] = 0
            if (
                backend.healthy
                and self._consecutive_failures[backend_id] >= self._failure_threshold
            ):
                self._registry.mark_down(backend)
                logger.info(
                    "health: %s DOWN after %d consecutive failures",
                    backend_id,
                    self._consecutive_failures[backend_id],
                )
                self._consecutive_failures[backend_id] = 0
        return success

    async def _probe(self, backend: Backend) -> bool:
        """GET /health with probe timeout; True iff status 200."""
        try:
            async with asyncio.timeout(self._timeout):
                reader, writer = await asyncio.open_connection(*backend.address)
                try:
                    writer.write(
                        serialize_request(
                            "GET",
                            "/health",
                            [(b"host", backend.netloc.encode("ascii"))],
                            b"",
                        )
                    )
                    await writer.drain()
                    status, _, _ = await read_response(reader, self._max_body_bytes)
                finally:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except (ConnectionError, OSError):
                        pass
        except asyncio.CancelledError:
            raise
        except Exception:
            # connection refused/reset, timeout, protocol garbage: failure
            return False
        return status == 200
