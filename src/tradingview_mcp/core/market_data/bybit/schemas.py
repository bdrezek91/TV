"""Typed parsing of Bybit v5 public WebSocket payloads.

Reference shapes (Bybit v5 public WS, linear category):

    publicTrade.BTCUSDT:
        {"topic": "publicTrade.BTCUSDT", "type": "snapshot", "ts": 1700000000000,
         "data": [{"T": 1700000000000, "s": "BTCUSDT", "S": "Buy", "v": "0.01",
                    "p": "68000.5", "L": "ZeroPlusTick", "i": "trade-id", "BT": false}]}

    orderbook.50.BTCUSDT:
        {"topic": "orderbook.50.BTCUSDT", "type": "snapshot"|"delta", "ts": ...,
         "data": {"s": "BTCUSDT", "b": [["68000.0", "1.2"], ...],
                    "a": [["68001.0", "0.5"], ...], "u": 123, "seq": 456}}

    allLiquidation.BTCUSDT:
        {"topic": "allLiquidation.BTCUSDT", "ts": ..., "data": [
            {"T": ..., "s": "BTCUSDT", "S": "Buy", "v": "1.2", "p": "68000.0"}]}
        Per Bybit docs, `S` on a liquidation message is the *side of the
        liquidation order placed by the engine to close the position* — i.e.
        S="Buy" means a SHORT position was liquidated (forced buy-to-cover),
        and S="Sell" means a LONG position was liquidated (forced sell).

    tickers.BTCUSDT:
        {"topic": "tickers.BTCUSDT", "ts": ..., "data": {"symbol": "BTCUSDT",
         "markPrice": "...", "indexPrice": "...", "openInterest": "...",
         "fundingRate": "...", "nextFundingTime": "..."}}

    kline.1.BTCUSDT:
        {"topic": "kline.1.BTCUSDT", "ts": ..., "data": [{"start": ..., "end": ...,
         "interval": "1", "open": "...", "close": "...", "high": "...", "low": "...",
         "volume": "...", "turnover": "...", "confirm": true, "timestamp": ...}]}
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Literal, Optional


def ms_to_utc(ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc)


@dataclass(frozen=True)
class Trade:
    symbol: str
    side: Literal["Buy", "Sell"]
    price: Decimal
    size: Decimal
    trade_id: str
    trade_time: dt.datetime

    @property
    def is_buy_taker(self) -> bool:
        return self.side == "Buy"


def parse_trades(message: dict) -> List[Trade]:
    out: List[Trade] = []
    for row in message.get("data", []):
        out.append(
            Trade(
                symbol=row["s"],
                side=row["S"],
                price=Decimal(str(row["p"])),
                size=Decimal(str(row["v"])),
                trade_id=str(row.get("i", "")),
                trade_time=ms_to_utc(int(row["T"])),
            )
        )
    return out


@dataclass(frozen=True)
class Liquidation:
    symbol: str
    side: Literal["Buy", "Sell"]
    price: Decimal
    size: Decimal
    event_time: dt.datetime

    @property
    def liquidated_side(self) -> Literal["LONG", "SHORT"]:
        """Bybit's `S` field is the side of the forced order, which is the
        *opposite* of the position that got liquidated: a forced Sell closes
        a LONG, a forced Buy closes a SHORT."""
        return "LONG" if self.side == "Sell" else "SHORT"

    @property
    def value(self) -> Decimal:
        return self.price * self.size


def parse_liquidations(message: dict) -> List[Liquidation]:
    out: List[Liquidation] = []
    for row in message.get("data", []):
        out.append(
            Liquidation(
                symbol=row["s"],
                side=row["S"],
                price=Decimal(str(row["p"])),
                size=Decimal(str(row["v"])),
                event_time=ms_to_utc(int(row["T"])),
            )
        )
    return out


@dataclass(frozen=True)
class OrderbookDelta:
    symbol: str
    msg_type: Literal["snapshot", "delta"]
    bids: List[tuple]  # (price: Decimal, size: Decimal)
    asks: List[tuple]
    update_id: int
    seq: Optional[int]
    cross_seq: Optional[int]
    event_time: dt.datetime


def parse_orderbook_message(message: dict) -> OrderbookDelta:
    data = message["data"]
    return OrderbookDelta(
        symbol=data["s"],
        msg_type=message.get("type", "delta"),
        bids=[(Decimal(str(p)), Decimal(str(s))) for p, s in data.get("b", [])],
        asks=[(Decimal(str(p)), Decimal(str(s))) for p, s in data.get("a", [])],
        update_id=int(data.get("u", 0)),
        seq=data.get("seq"),
        cross_seq=data.get("cross_seq") or data.get("seq"),
        event_time=ms_to_utc(int(message.get("ts", 0))) if message.get("ts") else dt.datetime.now(dt.timezone.utc),
    )


@dataclass(frozen=True)
class TickerUpdate:
    symbol: str
    mark_price: Optional[Decimal]
    index_price: Optional[Decimal]
    open_interest: Optional[Decimal]
    funding_rate: Optional[Decimal]
    next_funding_time: Optional[dt.datetime]
    event_time: dt.datetime


def _dec_or_none(v) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    return Decimal(str(v))


def parse_ticker(message: dict) -> TickerUpdate:
    data = message.get("data", {})
    next_funding = data.get("nextFundingTime")
    return TickerUpdate(
        symbol=data.get("symbol", ""),
        mark_price=_dec_or_none(data.get("markPrice")),
        index_price=_dec_or_none(data.get("indexPrice")),
        open_interest=_dec_or_none(data.get("openInterest")),
        funding_rate=_dec_or_none(data.get("fundingRate")),
        next_funding_time=ms_to_utc(int(next_funding)) if next_funding else None,
        event_time=ms_to_utc(int(message.get("ts", 0))) if message.get("ts") else dt.datetime.now(dt.timezone.utc),
    )


@dataclass(frozen=True)
class KlineUpdate:
    symbol: str
    interval: str
    open_time: dt.datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    turnover: Decimal
    is_closed: bool


def parse_klines(message: dict, symbol: str) -> List[KlineUpdate]:
    out: List[KlineUpdate] = []
    for row in message.get("data", []):
        out.append(
            KlineUpdate(
                symbol=symbol,
                interval=str(row.get("interval", "1")),
                open_time=ms_to_utc(int(row["start"])),
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=Decimal(str(row["volume"])),
                turnover=Decimal(str(row.get("turnover", "0"))),
                is_closed=bool(row.get("confirm", False)),
            )
        )
    return out


def topic_symbol(topic: str) -> str:
    """`orderbook.50.BTCUSDT` -> `BTCUSDT`; `publicTrade.BTCUSDT` -> `BTCUSDT`."""
    return topic.rsplit(".", 1)[-1]


def build_subscribe_args(
    symbols: List[str],
    *,
    trades: bool = True,
    orderbook_depth: Optional[int] = 50,
    liquidations: bool = True,
    tickers: bool = True,
    kline_interval: Optional[str] = "1",
) -> List[str]:
    """Build the list of Bybit v5 topic strings to subscribe to."""
    args: List[str] = []
    for sym in symbols:
        if trades:
            args.append(f"publicTrade.{sym}")
        if orderbook_depth:
            args.append(f"orderbook.{orderbook_depth}.{sym}")
        if liquidations:
            args.append(f"allLiquidation.{sym}")
        if tickers:
            args.append(f"tickers.{sym}")
        if kline_interval:
            args.append(f"kline.{kline_interval}.{sym}")
    return args
