"""Round-robin strategy: cycle through healthy backends in order.

The selector runs on the proxy's single asyncio event loop and never awaits
between reading and advancing the index, so a plain counter is safe without
locking. Unhealthy backends are filtered by the caller (the proxy passes the
healthy set), but the strategy also defends itself by ignoring unhealthy
entries it may still receive.
"""

from .base import LoadBalancingStrategy


class RoundRobinStrategy(LoadBalancingStrategy):
    def __init__(self) -> None:
        self._index = 0

    def select_backend(self, client_ip, healthy_backends):
        candidates = [b for b in healthy_backends if b.healthy]
        if not candidates:
            return None
        backend = candidates[self._index % len(candidates)]
        self._index += 1
        return backend
