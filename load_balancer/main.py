"""Entrypoint for the custom load balancer proxy (Phase 2).

Usage:
    python -m load_balancer.main

Configuration comes from environment variables (see config.py):
    LB_PROXY_HOST, LB_PROXY_PORT, LB_BACKENDS, LB_STRATEGY
    LB_HEALTH_INTERVAL, LB_HEALTH_TIMEOUT, LB_HEALTH_FAILURES,
    LB_HEALTH_RECOVERIES (Phase 5 active health checks)
    LB_METRICS_DUMP_PATH (Phase 6: write a run-snapshot JSON here every
    1s while running, for benchmark capture)
"""

import asyncio
import logging
import os
import signal
import sys

from .config import load_config
from .health.monitor import HealthMonitor
from .health.registry import BackendRegistry
from .metrics.dumper import dump_loop
from .proxy import ProxyServer


async def serve() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    cfg = load_config()
    registry = BackendRegistry(cfg.backends)
    proxy = ProxyServer(
        registry=registry,
        strategy=cfg.strategy,
        host=cfg.listen_host,
        port=cfg.listen_port,
    )
    monitor = HealthMonitor(
        registry,
        interval=cfg.health.interval,
        timeout=cfg.health.timeout,
        failure_threshold=cfg.health.failure_threshold,
        recovery_threshold=cfg.health.recovery_threshold,
    )
    await proxy.start()
    await monitor.start()
    logger = logging.getLogger("lb.main")
    logger.info(
        "proxy listening on %s:%s with %s backends=%s strategy=%s "
        "health(interval=%s timeout=%s failures=%s recoveries=%s)",
        cfg.listen_host,
        cfg.listen_port,
        len(cfg.backends),
        [b.backend_id for b in cfg.backends],
        type(cfg.strategy).__name__,
        cfg.health.interval,
        cfg.health.timeout,
        cfg.health.failure_threshold,
        cfg.health.recovery_threshold,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows: KeyboardInterrupt delivers SIGINT instead.
            pass

    # Phase 6: live metrics dump for benchmark runs (LB_METRICS_DUMP_PATH).
    # LB_STRATEGY names the strategy in dump metadata (config keeps only
    # the instance).
    dump_task = None
    dump_path = os.environ.get("LB_METRICS_DUMP_PATH")
    if dump_path:
        dump_task = asyncio.create_task(
            dump_loop(proxy, registry, os.environ.get("LB_STRATEGY", "round_robin"),
                      dump_path, interval=1.0)
        )
        logger.info("metrics dump: %s every 1s", dump_path)

    try:
        await stop.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        if dump_task is not None:
            dump_task.cancel()
            try:
                await dump_task
            except asyncio.CancelledError:
                pass
        await proxy.close()
        await monitor.stop()
        logger.info("proxy stopped")


def main() -> None:
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
