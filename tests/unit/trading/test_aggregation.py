from __future__ import annotations

import datetime as dt
from decimal import Decimal

from tradingview_mcp.core.market_data.bybit.aggregation import (
    LiquidationAggregator,
    TradeAggregator,
    TradeDeduplicator,
    bucket_start,
)
from tradingview_mcp.core.market_data.bybit.schemas import parse_liquidations, parse_trades

from .conftest import load_fixture


def _trades():
    return parse_trades(load_fixture("public_trades.json"))


def _liquidations():
    return parse_liquidations(load_fixture("liquidations.json"))


def test_trade_dedup_drops_repeat_trade_id():
    dedup = TradeDeduplicator()
    trades = _trades()
    seen = [t for t in trades if not dedup.is_duplicate(t.trade_id)]
    # Fixture has 4 trades, one is a duplicate id "t-1".
    assert len(trades) == 4
    assert len(seen) == 3
    assert [t.trade_id for t in seen] == ["t-1", "t-2", "t-3"]


def test_buy_sell_taker_mapping():
    trades = _trades()
    assert trades[0].is_buy_taker is True   # S="Buy"
    assert trades[1].is_buy_taker is False  # S="Sell"


def test_liquidation_side_mapping():
    """Bybit's `S` on allLiquidation is the forced-order side, which is the
    OPPOSITE of the liquidated position: S=Sell -> a LONG got liquidated,
    S=Buy -> a SHORT got liquidated."""
    liqs = _liquidations()
    assert liqs[0].side == "Sell" and liqs[0].liquidated_side == "LONG"
    assert liqs[1].side == "Sell" and liqs[1].liquidated_side == "LONG"
    assert liqs[2].side == "Buy" and liqs[2].liquidated_side == "SHORT"


def test_bucket_start_floors_to_boundary():
    ts = dt.datetime(2024, 1, 1, 0, 0, 37, tzinfo=dt.timezone.utc)
    assert bucket_start(ts, 60) == dt.datetime(2024, 1, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
    assert bucket_start(ts, 1) == dt.datetime(2024, 1, 1, 0, 0, 37, tzinfo=dt.timezone.utc)


def test_trade_aggregation_1s_and_1m_buckets():
    agg = TradeAggregator("BTCUSDT", bucket_seconds=(1, 60))
    dedup = TradeDeduplicator()
    for trade in _trades():
        if dedup.is_duplicate(trade.trade_id):
            continue
        agg.add_trade(trade)

    result = agg.force_flush_all()
    by_bucket = {(r["bucket_seconds"]): r for r in result}

    # All 3 unique trades land in the same 1s (and 1m) bucket since their
    # timestamps span only 300ms.
    one_s = by_bucket[1]
    assert one_s["trade_count"] == 3
    assert one_s["buy_taker_volume"] == Decimal("0.010") + Decimal("1.500")
    assert one_s["sell_taker_volume"] == Decimal("0.020")
    assert one_s["delta"] == one_s["buy_taker_volume"] - one_s["sell_taker_volume"]
    assert one_s["largest_trade_size"] == Decimal("1.500")

    one_m = by_bucket[60]
    assert one_m["trade_count"] == 3


def test_trade_aggregator_closes_bucket_when_new_bucket_starts():
    agg = TradeAggregator("BTCUSDT", bucket_seconds=(1,))
    trades = _trades()
    dedup = TradeDeduplicator()
    unique = [t for t in trades if not dedup.is_duplicate(t.trade_id)]

    agg.add_trade(unique[0])
    assert agg.flush_completed() == []  # still open

    # A trade a full second later closes out the previous 1s bucket.
    from tradingview_mcp.core.market_data.bybit.schemas import Trade

    later = Trade(
        symbol="BTCUSDT",
        side="Buy",
        price=Decimal("68010"),
        size=Decimal("0.1"),
        trade_id="t-later",
        trade_time=unique[0].trade_time + dt.timedelta(seconds=2),
    )
    agg.add_trade(later)
    completed = agg.flush_completed()
    assert len(completed) == 1
    assert completed[0]["trade_count"] == 1


def test_liquidation_aggregation_imbalance():
    agg = LiquidationAggregator("BTCUSDT", bucket_seconds=(1,))
    for liq in _liquidations():
        agg.add_liquidation(liq)
    result = agg.force_flush_all()
    assert len(result) == 1
    row = result[0]
    assert row["long_liq_count"] == 2
    assert row["short_liq_count"] == 1
    assert row["long_liq_value"] == Decimal("68000.0") * Decimal("1.0") + Decimal("67990.0") * Decimal("0.5")
    assert row["short_liq_value"] == Decimal("68010.0") * Decimal("2.0")
    assert row["largest_liq_value"] == Decimal("68010.0") * Decimal("2.0")
    # imbalance = (long - short) / (long + short); short dominates here.
    assert row["imbalance"] < 0
