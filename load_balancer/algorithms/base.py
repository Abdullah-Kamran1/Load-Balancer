"""Minimal abstract base for load-balancing strategies (Phase 2/4)."""


class LoadBalancingStrategy:
    def select_backend(self, client_ip: str | None, healthy_backends: list):
        """Return a Backend chosen from healthy_backends, or None if none.

        client_ip is the TCP peer address of the client; strategies that do
        not use it (round robin, least requests) simply ignore it.
        """
        raise NotImplementedError
