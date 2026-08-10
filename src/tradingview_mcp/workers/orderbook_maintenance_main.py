"""Periodic order-book retention worker.

Runs independently from the live collector so maintenance failures cannot stop
market-data ingestion. The worker keeps recent high-resolution snapshots,
downsamples older history, and eventually expires very old rows.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal

from tradingview_mcp.core.database.orderbook_retention import (
    OrderbookRetentionPolicy,
    run_orderbook_retention_once,
)
from tradingview_mcp.core.database.session import dispose_engine, session_scope

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)r}',
)
logger = logging.getLogger("tradingview_mcp.orderbook_maintenance")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value is None or not value.strip() else int(value)


def policy_from_env() -> OrderbookRetentionPolicy:
    policy = OrderbookRetentionPolicy(
        enabled=_env_bool("ORDERBOOK_RETENTION_ENABLED", True),
        high_res_days=_env_int("ORDERBOOK_HIGH_RES_RETENTION_DAYS", 30),
        archive_days=_env_int("ORDERBOOK_ARCHIVE_RETENTION_DAYS", 180),
        downsample_seconds=_env_int("ORDERBOOK_DOWNSAMPLE_SECONDS", 60),
        batch_size=_env_int("ORDERBOOK_RETENTION_BATCH_SIZE", 5000),
    )
    policy.validate()
    return policy


async def _run_once(policy: OrderbookRetentionPolicy) -> None:
    async with session_scope() as session:
        result = await run_orderbook_retention_once(session, policy)
    logger.info("orderbook retention pass: %s", json.dumps(result, default=str, separators=(",", ":")))


async def main_async() -> None:
    policy = policy_from_env()
    interval = _env_int("ORDERBOOK_MAINTENANCE_INTERVAL_SECONDS", 21600)
    if interval < 300:
        raise ValueError("ORDERBOOK_MAINTENANCE_INTERVAL_SECONDS must be >= 300")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _stop() -> None:
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    logger.info(
        "starting orderbook maintenance enabled=%s high_res_days=%d archive_days=%d downsample_seconds=%d interval_seconds=%d",
        policy.enabled,
        policy.high_res_days,
        policy.archive_days,
        policy.downsample_seconds,
        interval,
    )

    try:
        while not stop_event.is_set():
            try:
                await _run_once(policy)
            except Exception:
                logger.exception("orderbook retention pass failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    finally:
        await dispose_engine()


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
