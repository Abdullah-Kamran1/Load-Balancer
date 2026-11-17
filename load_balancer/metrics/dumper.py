"""Periodic snapshot dumping for live runs (Phase 6).

A live proxy is a separate process, so there is no in-process caller to
collect a snapshot after a wrk run. When LB_METRICS_DUMP_PATH is set,
main.py runs dump_loop(): every interval it collects the run snapshot
and writes it atomically (temp file + os.replace), so a reader never
observes a partial JSON document.
"""

import asyncio
import os

from .snapshot import collect_run_snapshot, write_json


async def dump_loop(
    proxy,
    registry,
    strategy_name: str,
    path,
    interval: float = 1.0,
) -> None:
    """Write the run snapshot JSON to path every interval until cancelled."""
    tmp = str(path) + ".tmp"
    try:
        while True:
            snapshot = collect_run_snapshot(proxy, registry, strategy_name)
            write_json(snapshot, tmp)
            os.replace(tmp, path)  # atomic on POSIX and Windows
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        raise
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
