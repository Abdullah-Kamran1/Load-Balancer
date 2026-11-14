"""Backend state model for the load balancer (Phase 2).

A Backend carries its network identity plus the state the proxy and the
health monitor need: healthy/unhealthy flag, active proxied request count,
and the running total of requests sent to it.

All mutation happens from coroutines on a single asyncio event loop, so
plain attribute assignment is safe (no await between read and write).
"""

from dataclasses import dataclass, field


@dataclass
class Backend:
    backend_id: str
    host: str
    port: int
    healthy: bool = True
    active_requests: int = 0
    total_requests: int = 0

    @property
    def address(self) -> tuple[str, int]:
        return (self.host, self.port)

    @property
    def netloc(self) -> str:
        return f"{self.host}:{self.port}"
