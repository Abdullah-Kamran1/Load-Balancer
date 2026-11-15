"""Least-active-requests strategy: pick the healthy backend with the
fewest in-flight proxied requests (Phase 4).

active_requests lives on the Backend model and is owned by the proxy:
incremented when a request is assigned, decremented in a finally block
after completion or failure (Phase 2/3). The strategy only reads it.

Tie-break: round robin across the currently tied backends, so equal-load
farms still distribute evenly instead of hammering the first entry.
"""

from .base import LoadBalancingStrategy


class LeastActiveRequestsStrategy(LoadBalancingStrategy):
    def __init__(self) -> None:
        self._index = 0

    def select_backend(self, client_ip, healthy_backends):
        candidates = [b for b in healthy_backends if b.healthy]
        if not candidates:
            return None
        smallest = min(b.active_requests for b in candidates)
        tied = [b for b in candidates if b.active_requests == smallest]
        backend = tied[self._index % len(tied)]
        self._index += 1
        return backend
