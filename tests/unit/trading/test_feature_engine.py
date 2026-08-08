from __future__ import annotations

import datetime as dt
from decimal import Decimal

from tradingview_mcp.core.analysis.feature_engine import (
    assess_data_quality,
    build_feature_snapshot,
    compute_futures_features,
    compute_liquidation_features,
    compute_orderbook_features,
    compute_price_action_features,
    compute_volume_features,
)


def _candles(n=30, start=100, step=1):
    now = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    out = []
    price = Decimal(start)
    for i in range(n):
        price += Decimal(step)
        out.append(
            {
                "open_time": now + dt.timedelta(minutes=15 * i),
                "open": price - 1, "high": price + 1, "low": price - 2, "close": price,
                "volume": "10",
            }
        )
    return out


def test_price_action_features_basic():
    feats = compute_price_action_features(_candles(30))
    assert feats["available"] is True
    assert feats["last_close"] == Decimal("130")  # 100 + 30*1 (loop starts by adding 1 first)
    assert feats["pct_change"] is not None
    assert feats["support"] is not None and feats["resistance"] is not None


def test_price_action_empty_candles():
    feats = compute_price_action_features([])
    assert feats == {"available": False}


def test_volume_features_delta_and_cvd():
    aggs = [
        {"bucket_start": dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc), "buy_taker_volume": "10", "sell_taker_volume": "4", "delta": "6", "trade_count": 5, "avg_trade_size": "2", "largest_trade_size": "3", "large_trade_count": 0},
        {"bucket_start": dt.datetime(2024, 1, 1, 0, 1, tzinfo=dt.timezone.utc), "buy_taker_volume": "5", "sell_taker_volume": "8", "delta": "-3", "trade_count": 4, "avg_trade_size": "1.5", "largest_trade_size": "2", "large_trade_count": 0},
    ]
    feats = compute_volume_features(aggs)
    assert feats["available"] is True
    assert feats["delta"] == Decimal("3")   # 6 + (-3)
    assert feats["cvd"] == Decimal("3")     # cumulative sum of deltas
    assert feats["buy_taker_volume"] == Decimal("15")
    assert feats["sell_taker_volume"] == Decimal("12")


def test_volume_features_empty():
    assert compute_volume_features([]) == {"available": False}


def test_orderbook_features_imbalance_and_microprice_passthrough():
    latest = {
        "best_bid": "100", "best_ask": "100.1", "spread": "0.1", "spread_bps": "10",
        "mid_price": "100.05", "microprice": "100.04", "bid_depth": "10", "ask_depth": "8",
        "imbalance": "0.111", "depth_bands": {}, "is_consistent": True,
        "source_timestamp": dt.datetime.now(dt.timezone.utc),
    }
    previous = dict(latest, imbalance="0.05")
    feats = compute_orderbook_features(latest, previous, [previous, latest])
    assert feats["imbalance"] == "0.111"
    assert feats["imbalance_change"] == Decimal("0.111") - Decimal("0.05")
    assert feats["microprice"] == "100.04"
    assert feats["is_consistent"] is True


def test_orderbook_features_missing():
    assert compute_orderbook_features({}) == {"available": False}


def test_orderbook_large_wall_flag():
    latest = {
        "best_bid": "100", "best_ask": "100.1", "bid_depth": "100", "ask_depth": "5",
        "imbalance": "0.9", "is_consistent": True, "source_timestamp": dt.datetime.now(dt.timezone.utc),
    }
    feats = compute_orderbook_features(latest)
    assert feats["large_wall_present"] is True


def test_futures_features_basis_and_oi_change():
    now = dt.datetime.now(dt.timezone.utc)
    history = [
        {"source_timestamp": now - dt.timedelta(minutes=10), "open_interest": "1000", "mark_price": "100"},
        {"source_timestamp": now, "open_interest": "1100", "mark_price": "105"},
    ]
    latest = history[-1] | {"funding_rate": "0.0001", "index_price": "104.5", "basis": "0.5"}
    feats = compute_futures_features(latest, history)
    assert feats["available"] is True
    assert feats["oi_change_5m_pct"] is not None
    assert feats["price_to_oi_relation"] == "PRICE_UP_OI_UP"


def test_liquidation_features_imbalance_and_cascade():
    liq_1m = [
        {"long_liq_value": "150000", "short_liq_value": "0"},
        {"long_liq_value": "150000", "short_liq_value": "0"},
        {"long_liq_value": "150000", "short_liq_value": "0"},
    ]
    feats = compute_liquidation_features(liq_1m, liq_1m, liq_1m)
    assert feats["cascade_detected"] is True
    assert feats["imbalance_15m"] == Decimal("1")  # all long liqs


def test_data_quality_all_fresh_is_tradeable():
    now = dt.datetime.now(dt.timezone.utc)
    quality = assess_data_quality(
        {"candles": now, "trades": now, "orderbook": now},
        {"candles": 180, "trades": 10, "orderbook": 5},
        orderbook_consistent=True,
        now=now,
    )
    assert quality["is_tradeable"] is True
    assert quality["data_quality_score"] == 100
    assert quality["missing_fields"] == []
    assert quality["stale_fields"] == []


def test_data_quality_stale_trades_blocks_tradeable():
    now = dt.datetime.now(dt.timezone.utc)
    quality = assess_data_quality(
        {"candles": now, "trades": now - dt.timedelta(seconds=60), "orderbook": now},
        {"candles": 180, "trades": 10, "orderbook": 5},
        orderbook_consistent=True,
        now=now,
    )
    assert quality["is_tradeable"] is False
    assert "trades" in quality["stale_fields"]


def test_data_quality_inconsistent_orderbook_blocks_tradeable():
    now = dt.datetime.now(dt.timezone.utc)
    quality = assess_data_quality(
        {"candles": now, "trades": now, "orderbook": now},
        {"candles": 180, "trades": 10, "orderbook": 5},
        orderbook_consistent=False,
        now=now,
    )
    assert quality["is_tradeable"] is False


def test_data_quality_missing_source_blocks_tradeable():
    now = dt.datetime.now(dt.timezone.utc)
    quality = assess_data_quality(
        {"candles": now, "trades": None, "orderbook": now},
        {"candles": 180, "trades": 10, "orderbook": 5},
        orderbook_consistent=True,
        now=now,
    )
    assert quality["is_tradeable"] is False
    assert "trades" in quality["missing_fields"]


def test_build_feature_snapshot_assembles_all_groups():
    now = dt.datetime.now(dt.timezone.utc)
    candles = _candles(30)
    for i, c in enumerate(candles):
        c["open_time"] = now - dt.timedelta(minutes=15 * (30 - i))
    trade_aggs = [
        {"bucket_start": now, "buy_taker_volume": "10", "sell_taker_volume": "5", "delta": "5", "trade_count": 3, "avg_trade_size": "5", "largest_trade_size": "6", "large_trade_count": 0}
    ]
    ob = {
        "best_bid": "100", "best_ask": "100.1", "spread": "0.1", "spread_bps": "10",
        "mid_price": "100.05", "microprice": "100.04", "bid_depth": "10", "ask_depth": "8",
        "imbalance": "0.1", "depth_bands": {}, "is_consistent": True, "source_timestamp": now,
    }
    deriv = {"source_timestamp": now, "open_interest": "1000", "mark_price": "100", "funding_rate": "0.0001"}
    snapshot = build_feature_snapshot(
        "BTCUSDT",
        candles_15m=candles,
        trade_aggregates_1m=trade_aggs,
        orderbook_latest=ob,
        orderbook_previous=None,
        orderbook_recent=[ob],
        derivatives_latest=deriv,
        derivatives_history=[deriv],
        liq_1m=[], liq_5m=[], liq_15m=[],
        max_ages={"candles": 999999, "trades": 999999, "orderbook": 999999, "derivatives": 999999},
        now=now,
    )
    assert snapshot["symbol"] == "BTCUSDT"
    assert snapshot["is_tradeable"] is True
    assert snapshot["price_action"]["available"] is True
    assert snapshot["volume"]["available"] is True
    assert snapshot["orderbook"]["available"] is True
    assert snapshot["futures"]["available"] is True
