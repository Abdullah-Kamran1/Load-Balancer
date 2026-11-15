"""IP-hash strategy: map the client's TCP peer address deterministically
onto the healthy backend set (Phase 4).

The hash is SHA-256 of the peer IP, so the mapping is stable across
process restarts (unlike Python's salted str hash) and needs no state.

Documented property: the mapping is hash of (IP, healthy set). When the
healthy set changes — a backend goes down, a new one is admitted — some
clients are remapped to a different backend. This is inherent to hashing
on a live set (consistent hashing would reduce it; not in this plan).

No forwarded headers are consulted: the proxy hands us the socket peer
address only, so arbitrary clients cannot choose their backend via
X-Forwarded-For. A trusted-upstream XFF scheme is out of plan scope.
"""

import hashlib

from .base import LoadBalancingStrategy


class IpHashStrategy(LoadBalancingStrategy):
    def select_backend(self, client_ip, healthy_backends):
        candidates = [b for b in healthy_backends if b.healthy]
        if not candidates:
            return None
        if client_ip is None:
            # Ruling (ledgered): no peer address -> first healthy backend.
            return candidates[0]
        digest = hashlib.sha256(client_ip.encode("ascii", errors="replace")).digest()
        index = int.from_bytes(digest, "big") % len(candidates)
        return candidates[index]
