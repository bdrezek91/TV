"""Bybit market-data service layer.

Block 1 scope: exposes the data-readiness helpers (warmup status) needed by
health checks and by the future (Block 2/3) Feature Engine / MCP tools.
This module intentionally does not call the collector's live in-memory
state directly — it reads persisted data via the repository layer, so it
works the same whether the collector process is local or on another host.

Nothing here is wired into `server.py` yet (no new MCP tools are added in
Block 1, per the plan) — this is the seam Block 3's `bybit_market_state`
tool will call into.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from tradingview_mcp.core.database.repositories.market_data_repository import count_candles
from tradingview_mcp.core.market_data.bybit.rest_poller import warmup_status


async def get_data_readiness(session: AsyncSession, symbol: str) -> dict[str, Any]:
    """Return WARMING_UP / READY based on how much candle history is
    persisted for *symbol* across the intervals the future Feature Engine
    needs (15m/1h/4h + base 1m)."""
    counts = {}
    for interval in ("1", "15", "60", "240"):
        counts[interval] = await count_candles(session, symbol, interval)
    return {"symbol": symbol, "candle_counts": counts, **warmup_status(counts)}
