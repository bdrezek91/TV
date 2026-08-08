"""Aggregated system health for the trading-system extension.

Consumed by the (Block 3) `trading_system_health` MCP tool and by the
worker's own `/health` endpoint. Kept in `core/services/` alongside the
existing TradingView services per the plan's proposed module layout, and
following the same "server.py stays thin, logic lives in services" rule.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from tradingview_mcp.core.config.trading_settings import TradingSettingsError, get_trading_settings
from tradingview_mcp.core.database.session import check_database_connection


async def check_redis_connection(redis_url: str) -> bool:
    try:
        import redis.asyncio as aioredis
    except ImportError:
        return False
    try:
        client = aioredis.from_url(redis_url, socket_connect_timeout=2)
        try:
            return bool(await client.ping())
        finally:
            await client.aclose()
    except Exception:
        return False


async def build_health_report(
    *,
    collector_health: Optional[dict] = None,
    database_url: Optional[str] = None,
    redis_url: Optional[str] = None,
) -> dict[str, Any]:
    """Compose the full health report combining config validity, DB, Redis,
    and (if provided) the live collector's per-symbol state."""
    now = dt.datetime.now(dt.timezone.utc)
    report: dict[str, Any] = {
        "generated_at": now.isoformat(),
        "config": {"valid": True, "error": None},
        "database": {"connected": False},
        "redis": {"connected": False},
        "collector": collector_health or {},
        "overall_status": "UNKNOWN",
    }

    settings = None
    try:
        settings = get_trading_settings()
    except Exception as exc:  # pydantic ValidationError wraps TradingSettingsError
        report["config"] = {"valid": False, "error": str(exc)}

    db_url = database_url or (settings.database_url if settings else None)
    redis_url_eff = redis_url or (settings.redis_url if settings else None)

    if db_url:
        report["database"]["connected"] = await check_database_connection(db_url)
    if redis_url_eff:
        report["redis"]["connected"] = await check_redis_connection(redis_url_eff)

    collector_ok = True
    if collector_health:
        for sym_state in collector_health.values():
            if not sym_state.get("connected") or sym_state.get("is_stale") or not sym_state.get(
                "orderbook_consistent", True
            ):
                collector_ok = False

    if not report["config"]["valid"]:
        report["overall_status"] = "CONFIG_INVALID"
    elif not report["database"]["connected"]:
        report["overall_status"] = "DATABASE_DOWN"
    elif collector_health and not collector_ok:
        report["overall_status"] = "DEGRADED"
    else:
        report["overall_status"] = "HEALTHY"

    return report
