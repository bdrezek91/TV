"""Trade and liquidation aggregation over fixed time buckets (1s / 1m / 5m),
plus trade-id deduplication.

These aggregators are pure, deterministic, and hold no network/DB state, so
they're fully unit-testable by feeding in `Trade` / `Liquidation` objects
built from fixture JSON.
"""
from __future__ import annotations

import datetime as dt
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Deque, Dict, List, Optional

from tradingview_mcp.core.market_data.bybit.schemas import Liquidation, Trade

BUCKET_SECONDS = (1, 60, 300)
LARGE_TRADE_THRESHOLD_USD = Decimal("50000")


def bucket_start(ts: dt.datetime, bucket_seconds: int) -> dt.datetime:
    """Floor *ts* to the start of its bucket, in UTC."""
    epoch = int(ts.timestamp())
    floored = epoch - (epoch % bucket_seconds)
    return dt.datetime.fromtimestamp(floored, tz=dt.timezone.utc)


class TradeDeduplicator:
    """Bounded-memory de-duplication by trade id (handles reconnect replay
    and any duplicate delivery from the exchange)."""

    def __init__(self, max_size: int = 20_000) -> None:
        self._seen: "deque[str]" = deque(maxlen=max_size)
        self._seen_set: set = set()

    def is_duplicate(self, trade_id: str) -> bool:
        if trade_id in self._seen_set:
            return True
        if len(self._seen) == self._seen.maxlen:
            oldest = self._seen[0]
            self._seen_set.discard(oldest)
        self._seen.append(trade_id)
        self._seen_set.add(trade_id)
        return False


@dataclass
class _TradeBucket:
    symbol: str
    bucket_seconds: int
    bucket_start: dt.datetime
    buy_taker_volume: Decimal = Decimal("0")
    sell_taker_volume: Decimal = Decimal("0")
    trade_count: int = 0
    total_size: Decimal = Decimal("0")
    largest_trade_size: Decimal = Decimal("0")
    large_trade_count: int = 0

    def add(self, trade: Trade) -> None:
        if trade.is_buy_taker:
            self.buy_taker_volume += trade.size
        else:
            self.sell_taker_volume += trade.size
        self.trade_count += 1
        self.total_size += trade.size
        if trade.size > self.largest_trade_size:
            self.largest_trade_size = trade.size
        if trade.price * trade.size >= LARGE_TRADE_THRESHOLD_USD:
            self.large_trade_count += 1

    def to_dict(self) -> dict:
        avg = self.total_size / self.trade_count if self.trade_count else Decimal("0")
        return {
            "symbol": self.symbol,
            "bucket_seconds": self.bucket_seconds,
            "bucket_start": self.bucket_start,
            "buy_taker_volume": self.buy_taker_volume,
            "sell_taker_volume": self.sell_taker_volume,
            "delta": self.buy_taker_volume - self.sell_taker_volume,
            "trade_count": self.trade_count,
            "avg_trade_size": avg,
            "largest_trade_size": self.largest_trade_size,
            "large_trade_count": self.large_trade_count,
        }


class TradeAggregator:
    """Maintains rolling 1s/1m/5m trade aggregates for one symbol.

    Call :meth:`add_trade` for every incoming (de-duplicated) trade, and
    :meth:`flush_completed` periodically (or on shutdown) to pop buckets
    whose window has closed, ready for persistence.
    """

    def __init__(self, symbol: str, bucket_seconds: tuple = BUCKET_SECONDS) -> None:
        self.symbol = symbol
        self.bucket_seconds = bucket_seconds
        self._current: Dict[int, _TradeBucket] = {}
        self._completed: List[dict] = []

    def add_trade(self, trade: Trade) -> None:
        for bs in self.bucket_seconds:
            bstart = bucket_start(trade.trade_time, bs)
            current = self._current.get(bs)
            if current is None or current.bucket_start != bstart:
                if current is not None:
                    self._completed.append(current.to_dict())
                current = _TradeBucket(self.symbol, bs, bstart)
                self._current[bs] = current
            current.add(trade)

    def flush_completed(self) -> List[dict]:
        """Pop and return all buckets that have been closed out by a newer
        trade arriving. Does not force-close the still-open bucket."""
        out, self._completed = self._completed, []
        return out

    def force_flush_all(self) -> List[dict]:
        """Flush everything, including in-progress buckets (used on
        graceful shutdown)."""
        out = self.flush_completed()
        for bucket in self._current.values():
            out.append(bucket.to_dict())
        self._current = {}
        return out


@dataclass
class _LiqBucket:
    symbol: str
    bucket_seconds: int
    bucket_start: dt.datetime
    long_liq_value: Decimal = Decimal("0")
    short_liq_value: Decimal = Decimal("0")
    long_liq_count: int = 0
    short_liq_count: int = 0
    largest_liq_value: Decimal = Decimal("0")

    def add(self, liq: Liquidation) -> None:
        value = liq.value
        if liq.liquidated_side == "LONG":
            self.long_liq_value += value
            self.long_liq_count += 1
        else:
            self.short_liq_value += value
            self.short_liq_count += 1
        if value > self.largest_liq_value:
            self.largest_liq_value = value

    def to_dict(self) -> dict:
        total = self.long_liq_value + self.short_liq_value
        imbalance = (
            (self.long_liq_value - self.short_liq_value) / total if total else Decimal("0")
        )
        return {
            "symbol": self.symbol,
            "bucket_seconds": self.bucket_seconds,
            "bucket_start": self.bucket_start,
            "long_liq_value": self.long_liq_value,
            "short_liq_value": self.short_liq_value,
            "long_liq_count": self.long_liq_count,
            "short_liq_count": self.short_liq_count,
            "largest_liq_value": self.largest_liq_value,
            "imbalance": imbalance,
        }


class LiquidationAggregator:
    """Same rolling-bucket approach as :class:`TradeAggregator`, for
    liquidation events."""

    def __init__(self, symbol: str, bucket_seconds: tuple = BUCKET_SECONDS) -> None:
        self.symbol = symbol
        self.bucket_seconds = bucket_seconds
        self._current: Dict[int, _LiqBucket] = {}
        self._completed: List[dict] = []

    def add_liquidation(self, liq: Liquidation) -> None:
        for bs in self.bucket_seconds:
            bstart = bucket_start(liq.event_time, bs)
            current = self._current.get(bs)
            if current is None or current.bucket_start != bstart:
                if current is not None:
                    self._completed.append(current.to_dict())
                current = _LiqBucket(self.symbol, bs, bstart)
                self._current[bs] = current
            current.add(liq)

    def flush_completed(self) -> List[dict]:
        out, self._completed = self._completed, []
        return out

    def force_flush_all(self) -> List[dict]:
        out = self.flush_completed()
        for bucket in self._current.values():
            out.append(bucket.to_dict())
        self._current = {}
        return out
