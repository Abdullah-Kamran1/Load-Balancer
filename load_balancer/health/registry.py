"""Backend registry: owns the authoritative list of backends and their
health state. Active health checks (HealthMonitor, Phase 5) drive
mark_up()/mark_down() after their consecutive failure/success thresholds;
each actual state change is recorded on the Backend with a timestamp.
"""

import time

from ..models import Backend


class BackendRegistry:
    def __init__(self, backends: list[Backend]) -> None:
        self._backends = list(backends)

    def backends(self) -> list[Backend]:
        return list(self._backends)

    def healthy_backends(self) -> list[Backend]:
        return [b for b in self._backends if b.healthy]

    def mark_down(self, backend: Backend) -> None:
        if backend.healthy:
            backend.healthy = False
            backend.state_transitions.append(
                {"state": "down", "timestamp": time.time()}
            )

    def mark_up(self, backend: Backend) -> None:
        if not backend.healthy:
            backend.healthy = True
            backend.state_transitions.append(
                {"state": "up", "timestamp": time.time()}
            )

    def snapshot(self) -> list[dict]:
        return [
            {
                "backend_id": b.backend_id,
                "host": b.host,
                "port": b.port,
                "healthy": b.healthy,
                "active_requests": b.active_requests,
                "total_requests": b.total_requests,
                "state_transitions": list(b.state_transitions),
            }
            for b in self._backends
        ]
