"""Backend configuration, loaded from environment variables.

Capacity is modelled by two independent knobs (see the project plan):
- DEFAULT_WORK_DELAY_MS: artificial processing delay per /work request.
- MAX_CONCURRENCY: how many /work requests may execute simultaneously.

Capacity differences between backends must come from this configuration,
never from the request path itself: every backend serves the same URLs.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    backend_id: str
    default_delay_ms: int
    max_delay_ms: int
    max_concurrency: int


def _parse_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def load_settings() -> Settings:
    return Settings(
        backend_id=os.getenv("BACKEND_ID", "backend-1"),
        default_delay_ms=_parse_int("DEFAULT_WORK_DELAY_MS", 50),
        max_delay_ms=_parse_int("MAX_DELAY_MS", 60_000),
        max_concurrency=_parse_int("MAX_CONCURRENCY", 8),
    )
