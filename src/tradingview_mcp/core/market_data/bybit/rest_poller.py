"""REST poller: open interest, funding, mark/index price, basis, long/short
ratio, and historical candle backfill.

Idempotent (safe to re-run after a crash/restart), rate-limit aware (fixed
polling intervals, one in-flight request per endpoint), retry with backoff,
and never overwrites newer data with older data (delegates that rule to the
repository layer).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from decimal import Decimal
from typing import Awaitable, Callable, Optional

from tradingview_mcp.core.market_data.bybit.client import BybitRestClient
from tradingview_mcp.core.market_data.bybit.reconnect import BackoffPolicy
from tradingview_mcp.core.observability import metrics as m

logger = logging.getLogger("tradingview_mcp.bybit_rest_poller")

# Minimum closed candles needed for each downstream indicator, at the
# interval it's computed on. Used to decide WARMING_UP vs ready.
MIN_CANDLES_FOR_WARMUP = {
    "1": 210,     # covers EMA200 on the base interval if ever needed
    "15": 210,    # EMA200 / ATR / RSI / Bollinger on 15m
    "60": 210,    # 1h analysis
    "240": 210,   # 4h analysis
}


def warmup_status(candle_counts: dict) -> dict:
    """Given ``{interval: count}``, return either a ready status or the
    plan-required WARMING_UP envelope."""
    missing = {
        interval: required
        for interval, required in MIN_CANDLES_FOR_WARMUP.items()
        if candle_counts.get(interval, 0) < required
    }
    if missing:
        return {
            "status": "WARMING_UP",
            "reason": "insufficient_history",
            "missing": missing,
        }
    return {"status": "READY"}


class RestPoller:
    """Polls Bybit REST endpoints for one symbol on independent intervals."""

    def __init__(
        self,
        client: BybitRestClient,
        symbol: str,
        *,
        category: str = "linear",
        oi_poll_interval_seconds: float = 300,
        funding_poll_interval_seconds: float = 300,
        max_retries: int = 3,
        backoff: Optional[BackoffPolicy] = None,
        on_derivatives_snapshot: Optional[Callable[[dict], Awaitable[None]]] = None,
        on_candle: Optional[Callable[[dict], Awaitable[None]]] = None,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.client = client
        self.symbol = symbol
        self.category = category
        self.oi_poll_interval_seconds = oi_poll_interval_seconds
        self.funding_poll_interval_seconds = funding_poll_interval_seconds
        self.max_retries = max_retries
        self.backoff = backoff or BackoffPolicy(base_delay_seconds=1.0, max_delay_seconds=30.0)
        self.on_derivatives_snapshot = on_derivatives_snapshot
        self.on_candle = on_candle
        self._sleep = sleep_fn
        self._stopping = False

    async def _with_retry(self, endpoint: str, coro_fn: Callable[[], Awaitable[dict]]) -> Optional[dict]:
        last_exc = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return await coro_fn()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                m.rest_poll_errors_total.labels(endpoint=endpoint).inc()
                delay = self.backoff.delay_for_attempt(attempt)
                logger.warning("%s poll failed (attempt %d/%d): %r; retrying in %.2fs", endpoint, attempt, self.max_retries, exc, delay)
                await self._sleep(delay)
        logger.error("%s poll exhausted retries: %r", endpoint, last_exc)
        return None

    async def poll_derivatives_once(self) -> Optional[dict]:
        """Fetch tickers (mark/index/OI/funding) in one call and persist a
        single derivatives snapshot row."""
        result = await self._with_retry("tickers", lambda: self.client.get_tickers(self.symbol, category=self.category))
        if result is None:
            return None
        rows = result.get("result", {}).get("list", [])
        if not rows:
            return None
        row = rows[0]
        now = dt.datetime.now(dt.timezone.utc)

        def dec(key: str) -> Optional[Decimal]:
            v = row.get(key)
            return Decimal(str(v)) if v not in (None, "") else None

        mark = dec("markPrice")
        index = dec("indexPrice")
        basis = (mark - index) if (mark is not None and index is not None) else None
        basis_pct = (basis / index * 100) if (basis is not None and index) else None

        next_funding_raw = row.get("nextFundingTime")
        next_funding = None
        if next_funding_raw:
            try:
                next_funding = dt.datetime.fromtimestamp(int(next_funding_raw) / 1000, tz=dt.timezone.utc)
            except (ValueError, TypeError):
                next_funding = None

        snapshot = {
            "symbol": self.symbol,
            "open_interest": dec("openInterest"),
            "open_interest_value": dec("openInterestValue"),
            "funding_rate": dec("fundingRate"),
            "next_funding_time": next_funding,
            "mark_price": mark,
            "index_price": index,
            "basis": basis,
            "basis_pct": basis_pct,
            "long_short_ratio": None,
            "source_timestamp": now,
        }
        if self.on_derivatives_snapshot is not None:
            await self.on_derivatives_snapshot(snapshot)
        return snapshot

    async def backfill_candles(
        self,
        interval: str,
        *,
        target_count: int,
        already_have: int = 0,
    ) -> int:
        """Idempotent backfill: fetches only what's missing (``target_count -
        already_have``), in pages of up to 200 candles, oldest data will
        naturally not overwrite newer rows thanks to the repository's
        source_timestamp guard.

        Returns the number of candles fetched (not necessarily written --
        that's up to the on_candle sink's idempotency).
        """
        remaining = max(0, target_count - already_have)
        fetched = 0
        end: Optional[int] = None
        while remaining > 0 and not self._stopping:
            page_size = min(200, remaining)
            result = await self._with_retry(
                "kline",
                lambda: self.client.get_kline(self.symbol, interval, category=self.category, limit=page_size, end=end),
            )
            if result is None:
                break
            rows = result.get("result", {}).get("list", [])
            if not rows:
                break
            for row in rows:
                # Bybit kline REST rows: [start, open, high, low, close, volume, turnover]
                open_time = dt.datetime.fromtimestamp(int(row[0]) / 1000, tz=dt.timezone.utc)
                candle = {
                    "symbol": self.symbol,
                    "interval": interval,
                    "open_time": open_time,
                    "open": row[1],
                    "high": row[2],
                    "low": row[3],
                    "close": row[4],
                    "volume": row[5],
                    "turnover": row[6] if len(row) > 6 else 0,
                    "is_closed": True,
                    "source": "rest_backfill",
                    "source_timestamp": open_time,
                }
                if self.on_candle is not None:
                    await self.on_candle(candle)
                fetched += 1
            remaining -= len(rows)
            oldest_ms = int(rows[-1][0])
            end = oldest_ms - 1
            if len(rows) < page_size:
                break
        return fetched

    def stop(self) -> None:
        self._stopping = True
