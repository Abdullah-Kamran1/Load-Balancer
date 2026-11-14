"""Environment-driven proxy configuration (Phase 2).

LB_BACKENDS  — comma-separated `backend_id:host:port` entries
LB_PROXY_HOST / LB_PROXY_PORT — the public listener (default 127.0.0.1:8080)
LB_STRATEGY  — round_robin (only strategy implemented in Phase 2)

Malformed values fail fast at startup rather than misroute traffic later.
"""

import os
from dataclasses import dataclass, field

from .algorithms.base import LoadBalancingStrategy
from .algorithms.round_robin import RoundRobinStrategy
from .models import Backend

DEFAULT_BACKENDS = "backend-1:127.0.0.1:8001"


@dataclass
class ProxyConfig:
    listen_host: str
    listen_port: int
    backends: list[Backend] = field(default_factory=list)
    strategy: LoadBalancingStrategy = None

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
    if not backends:
        raise ValueError("LB_BACKENDS produced no backends")
    return backends


def _strategy_by_name(name: str) -> LoadBalancingStrategy:
    if name == "round_robin":
        return RoundRobinStrategy()
    raise ValueError(f"unknown LB_STRATEGY: {name!r}")


def load_config(env: dict | None = None) -> ProxyConfig:
    env = env if env is not None else os.environ
    return ProxyConfig(
        listen_host=env.get("LB_PROXY_HOST", "127.0.0.1"),
        listen_port=int(env.get("LB_PROXY_PORT", "8080")),
        backends=parse_backends(env.get("LB_BACKENDS", DEFAULT_BACKENDS)),
        strategy=_strategy_by_name(env.get("LB_STRATEGY", "round_robin")),
    )
