from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

from tradingview_mcp.core.backtest.extended_history_loader_v1 import load_extended_bundle
from tradingview_mcp.core.backtest.extended_history_v1 import write_json_cache


UTC = dt.timezone.utc


def _write_cache(root):
    root.mkdir(parents=True)
    candle = {
        "open_time": dt.datetime(2026, 8, 1, tzinfo=UTC),
        "open": Decimal("100"), "high": Decimal("101"), "low": Decimal("99"),
        "close": Decimal("100"), "volume": Decimal("1"),
    }
    for name in ("kline_1m", "kline_15m", "kline_1h", "kline_4h"):
        write_json_cache(root / f"{name}.json", {"rows": [candle]})
    daily = root / "trades_1m_daily"
    write_json_cache(daily / "2026-08-01.json", {"rows": [{
        "bucket_start": dt.datetime(2026, 8, 1, tzinfo=UTC),
        "buy_taker_volume": Decimal("1"), "sell_taker_volume": Decimal("0.5"),
        "delta": Decimal("0.5"), "trade_count": 2,
        "avg_trade_size": Decimal("0.75"), "largest_trade_size": Decimal("1"),
        "large_trade_count": 0,
    }]})
    write_json_cache(root / "derivatives_reconstructed.json", {"rows": [{
        "source_timestamp": dt.datetime(2026, 8, 1, tzinfo=UTC),
        "open_interest": Decimal("10"), "open_interest_value": Decimal("1000"),
        "funding_rate": Decimal("0.0001"), "mark_price": Decimal("100"),
        "index_price": Decimal("99"), "basis": Decimal("1"), "basis_pct": Decimal("1.01"),
        "long_short_ratio": Decimal("1.2"),
    }]})
    manifest = {
        "sources": {
            "kline_1m": {"available": True}, "kline_15m": {"available": True},
            "kline_1h": {"available": True}, "kline_4h": {"available": True},
            "trades_1m": {"available": True}, "derivatives": {"available": True},
            "orderbook": {"available": False}, "liquidations": {"available": False},
        },
        "degradations": ["DEGRADED_NO_HISTORICAL_ORDERBOOK", "DEGRADED_NO_HISTORICAL_LIQUIDATIONS"],
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_degraded_cache_allows_research_but_forbids_exact_legacy(tmp_path):
    root = tmp_path / "BTCUSDT_range"
    _write_cache(root)
    bundle = load_extended_bundle(root)
    assert len(bundle.inputs.candles_1m) == 1
    assert len(bundle.inputs.trades_1m) == 1
    assert len(bundle.inputs.derivatives) == 1
    assert bundle.inputs.orderbook == []
    assert bundle.inputs.liquidations_1m == []
    assert bundle.capabilities["LEGACY_FIXED_TV_EXACT"] is False
    assert bundle.capabilities["LEGACY_AS_IS_EXACT"] is False
    assert bundle.capabilities["RESEARCH_V2_EXACT"] is False
    assert bundle.capabilities["RESEARCH_V2_DEGRADED"] is True
    assert bundle.capabilities["missing_orderbook"] is True
    assert bundle.capabilities["missing_liquidations"] is True
