"""Backend registry: owns the authoritative list of backends and their
health state (Phase 2). Active health checks (Phase 5) will drive
mark_up()/mark_down(); for now the proxy flips state passively.
"""

from ..models import Backend


class BackendRegistry:
    def __init__(self, backends: list[Backend]) -> None:
        self._backends = list(backends)

    def backends(self) -> list[Backend]:
        return list(self._backends)

    def healthy_backends(self) -> list[Backend]:
        return [b for b in self._backends if b.healthy]

    def mark_down(self, backend: Backend) -> None:
        backend.healthy = False

    def mark_up(self, backend: Backend) -> None:
        backend.healthy = True

    def snapshot(self) -> list[dict]:
        return [
            {
                "backend_id": b.backend_id,
                "host": b.host,
                "port": b.port,
                "healthy": b.healthy,
                "active_requests": b.active_requests,
                "total_requests": b.total_requests,
            }
            for b in self._backends
        ]
