"""Environment-driven proxy configuration (Phase 2).

LB_BACKENDS  — comma-separated `backend_id:host:port` entries
LB_PROXY_HOST / LB_PROXY_PORT — the public listener (default 127.0.0.1:8080)
LB_STRATEGY  — round_robin | least_active_requests | ip_hash (Phase 4)
LB_HEALTH_INTERVAL / LB_HEALTH_TIMEOUT / LB_HEALTH_FAILURES /
LB_HEALTH_RECOVERIES — active health checks (Phase 5)

Malformed values fail fast at startup rather than misroute traffic later.
"""

import math
import os
from dataclasses import dataclass, field

from .algorithms.base import LoadBalancingStrategy
from .algorithms.round_robin import RoundRobinStrategy
from .models import Backend

DEFAULT_BACKENDS = "backend-1:127.0.0.1:8001"


@dataclass
class HealthConfig:
    """Active health-check knobs (Phase 5; defaults = documented rulings)."""

    interval: float = 1.0          # seconds between probes (LB_HEALTH_INTERVAL)
    timeout: float = 1.0           # per-probe timeout (LB_HEALTH_TIMEOUT)
    failure_threshold: int = 3     # consecutive failures -> DOWN (LB_HEALTH_FAILURES)
    recovery_threshold: int = 2    # consecutive successes -> UP (LB_HEALTH_RECOVERIES)

    def __post_init__(self):
        # isfinite: NaN passes <= 0 and would silently stall the probe
        # loop (sleep(nan)); inf is equally meaningless.
        if not math.isfinite(self.interval) or self.interval <= 0:
            raise ValueError("LB_HEALTH_INTERVAL must be a finite number > 0")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("LB_HEALTH_TIMEOUT must be a finite number > 0")
        if self.failure_threshold < 1:
            raise ValueError("LB_HEALTH_FAILURES must be >= 1")
        if self.recovery_threshold < 1:
            raise ValueError("LB_HEALTH_RECOVERIES must be >= 1")


@dataclass
class ProxyConfig:
    listen_host: str
    listen_port: int
    backends: list[Backend] = field(default_factory=list)
    strategy: LoadBalancingStrategy = None
    health: HealthConfig = field(default_factory=HealthConfig)

    def __post_init__(self):
        if self.strategy is None:
            self.strategy = RoundRobinStrategy()


def parse_backends(raw: str) -> list[Backend]:
    backends = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) != 3:
            raise ValueError(f"LB_BACKENDS entry must be id:host:port, got: {entry!r}")
        backend_id, host, port_raw = parts
        try:
            port = int(port_raw)
        except ValueError:
            raise ValueError(f"LB_BACKENDS entry has non-numeric port: {entry!r}") from None
        if not backend_id or not host:
            raise ValueError(f"LB_BACKENDS entry has empty id or host: {entry!r}")
        backends.append(Backend(backend_id=backend_id, host=host, port=port))
    ids = [b.backend_id for b in backends]
    if len(ids) != len(set(ids)):
        dupes = sorted(i for i in set(ids) if ids.count(i) > 1)
        raise ValueError(f"LB_BACKENDS has duplicate backend ids: {dupes}")
    if not backends:
        raise ValueError("LB_BACKENDS produced no backends")
    return backends


def _strategy_by_name(name: str) -> LoadBalancingStrategy:
    if name == "round_robin":
        from .algorithms.round_robin import RoundRobinStrategy

        return RoundRobinStrategy()
    if name == "least_active_requests":
        from .algorithms.least_active import LeastActiveRequestsStrategy

        return LeastActiveRequestsStrategy()
    if name == "ip_hash":
        from .algorithms.ip_hash import IpHashStrategy

        return IpHashStrategy()
    raise ValueError(f"unknown LB_STRATEGY: {name!r}")


def _health_config(env: dict) -> HealthConfig:
    return HealthConfig(
        interval=float(env.get("LB_HEALTH_INTERVAL", "1.0")),
        timeout=float(env.get("LB_HEALTH_TIMEOUT", "1.0")),
        failure_threshold=int(env.get("LB_HEALTH_FAILURES", "3")),
        recovery_threshold=int(env.get("LB_HEALTH_RECOVERIES", "2")),
    )


def load_config(env: dict | None = None) -> ProxyConfig:
    env = env if env is not None else os.environ
    return ProxyConfig(
        listen_host=env.get("LB_PROXY_HOST", "127.0.0.1"),
        listen_port=int(env.get("LB_PROXY_PORT", "8080")),
        backends=parse_backends(env.get("LB_BACKENDS", DEFAULT_BACKENDS)),
        strategy=_strategy_by_name(env.get("LB_STRATEGY", "round_robin")),
        health=_health_config(env),
    )
