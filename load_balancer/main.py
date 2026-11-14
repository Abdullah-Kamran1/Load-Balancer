"""Entrypoint for the custom load balancer proxy (Phase 2).

Usage:
    python -m load_balancer.main

Configuration comes from environment variables (see config.py):
    LB_PROXY_HOST, LB_PROXY_PORT, LB_BACKENDS, LB_STRATEGY
"""

import asyncio
import logging
import signal
import sys

from .config import load_config
from .health.registry import BackendRegistry
from .proxy import ProxyServer


async def serve() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    cfg = load_config()
    proxy = ProxyServer(
        registry=BackendRegistry(cfg.backends),
        strategy=cfg.strategy,
        host=cfg.listen_host,
        port=cfg.listen_port,
    )
    await proxy.start()
    logger = logging.getLogger("lb.main")
    logger.info(
        "proxy listening on %s:%s with %s backends=%s strategy=%s",
        cfg.listen_host,
        cfg.listen_port,
        len(cfg.backends),
        [b.backend_id for b in cfg.backends],
        type(cfg.strategy).__name__,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows: KeyboardInterrupt delivers SIGINT instead.
            pass

    try:
        await stop.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        await proxy.close()
        logger.info("proxy stopped")


def main() -> None:
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
