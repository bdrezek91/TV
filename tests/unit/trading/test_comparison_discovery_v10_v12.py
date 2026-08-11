from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

from tradingview_mcp.core.backtest.comparison_discovery_v10_v12 import (
    detect_basis_dislocation_reversal,
    detect_trade_flow_impulse_continuation,
    detect_volume_weighted_tsmom,
)

UTC = dt.timezone.utc
CUTOFF = dt.datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _c15(i: int, o: str, h: str, l: str, c: str, volume: str = "100") -> dict:
    return {
        "open_time": CUTOFF - dt.timedelta(minutes=15 * (16 - i)),
        "open": Decimal(o),
        "high": Decimal(h),
        "low": Decimal(l),
        "close": Decimal(c),
        "volume": Decimal(volume),
    }


def _trade(i: int, buy: str, sell: str) -> dict:
    return {
        "bucket_start": CUTOFF - dt.timedelta(minutes=60 - i),
        "buy_taker_volume": Decimal(buy),
        "sell_taker_volume": Decimal(sell),
        "delta": Decimal(buy) - Decimal(sell),
        "trade_count": 10,
        "avg_trade_size": Decimal("1"),
        "large_trade_count": 0,
        "largest_trade_size": Decimal("1"),
    }


def test_v10_detects_long_trade_flow_continuation_with_new_oi() -> None:
    trades = [_trade(i, "55", "45") for i in range(45)]
    trades += [_trade(i, "90", "30") for i in range(45, 60)]
    candles = [
        _c15(12, "100.0", "100.6", "99.8", "100.4"),
        _c15(13, "100.4", "101.0", "100.2", "100.8"),
        _c15(14, "100.8", "101.4", "100.6", "101.2"),
        _c15(15, "101.2", "101.8", "101.0", "101.6"),
    ]
    chain = {
        "as_of": CUTOFF,
        "regime": {"primary_regime": "TREND_UP"},
        "features": {
            "tf": {"1h": {"atr": Decimal("2")}},
            "futures": {"price_to_oi_relation": "PRICE_UP_OI_UP"},
        },
        "_windows": SimpleNamespace(candles_15m=candles),
        "_trade_flow_trades_1m": trades,
    }

    setup = detect_trade_flow_impulse_continuation(chain)

    assert setup["direction"] == "LONG"
    assert setup["flow_imbalance_60m"] > Decimal("0.08")
    assert setup["flow_imbalance_15m"] > Decimal("0.12")
    assert setup["volume_acceleration"] >= Decimal("1.10")
    assert setup["stop_loss"] < setup["entry_zone"]["low"]
    assert setup["detection_version"] == "V10_TRADE_FLOW_IMPULSE_CONTINUATION_PREREG_V1"


def test_v10_rejects_when_oi_does_not_show_new_positions() -> None:
    trades = [_trade(i, "55", "45") for i in range(45)]
    trades += [_trade(i, "90", "30") for i in range(45, 60)]
    candles = [
        _c15(12, "100.0", "100.6", "99.8", "100.4"),
        _c15(13, "100.4", "101.0", "100.2", "100.8"),
        _c15(14, "100.8", "101.4", "100.6", "101.2"),
        _c15(15, "101.2", "101.8", "101.0", "101.6"),
    ]
    chain = {
        "as_of": CUTOFF,
        "regime": {"primary_regime": "TREND_UP"},
        "features": {
            "tf": {"1h": {"atr": Decimal("2")}},
            "futures": {"price_to_oi_relation": "PRICE_UP_OI_DOWN"},
        },
        "_windows": SimpleNamespace(candles_15m=candles),
        "_trade_flow_trades_1m": trades,
    }

    setup = detect_trade_flow_impulse_continuation(chain)
    assert setup["direction"] == "NO_SETUP"
    assert "open interest relation" in setup["reasons"][0]


def _derivatives(latest_funding: str = "0.0002") -> list[dict]:
    rows = []
    for i in range(200):
        ts = CUTOFF - dt.timedelta(minutes=5 * (199 - i))
        basis = Decimal("0.01") if i % 2 else Decimal("-0.01")
        if i == 199:
            basis = Decimal("0.20")
        rows.append({
            "source_timestamp": ts,
            "basis_pct": basis,
            "funding_rate": Decimal(latest_funding if i == 199 else "0.0001"),
            "long_short_ratio": Decimal("1.20"),
        })
    return rows


def test_v11_detects_short_basis_dislocation_with_counter_crowd_trigger() -> None:
    trigger = {
        "open_time": CUTOFF - dt.timedelta(minutes=15),
        "open": Decimal("101"),
        "high": Decimal("101.2"),
        "low": Decimal("100.3"),
        "close": Decimal("100.5"),
        "volume": Decimal("100"),
    }
    chain = {
        "as_of": CUTOFF,
        "regime": {"primary_regime": "RANGE"},
        "features": {"tf": {"1h": {"atr": Decimal("2")}}},
        "_windows": SimpleNamespace(candles_15m=[trigger]),
        "_derivatives_24h": _derivatives(),
    }

    setup = detect_basis_dislocation_reversal(chain)

    assert setup["direction"] == "SHORT"
    assert setup["basis_zscore_24h"] > Decimal("1.5")
    assert setup["funding_rate"] > 0
    assert setup["stop_loss"] > setup["entry_zone"]["high"]
    assert setup["detection_version"] == "V11_BASIS_PREMIUM_DISLOCATION_PREREG_V1"


def test_v11_rejects_when_funding_opposes_basis_crowding() -> None:
    trigger = {
        "open_time": CUTOFF - dt.timedelta(minutes=15),
        "open": Decimal("101"),
        "high": Decimal("101.2"),
        "low": Decimal("100.3"),
        "close": Decimal("100.5"),
        "volume": Decimal("100"),
    }
    chain = {
        "as_of": CUTOFF,
        "regime": {"primary_regime": "RANGE"},
        "features": {"tf": {"1h": {"atr": Decimal("2")}}},
        "_windows": SimpleNamespace(candles_15m=[trigger]),
        "_derivatives_24h": _derivatives("-0.0002"),
    }

    setup = detect_basis_dislocation_reversal(chain)
    assert setup["direction"] == "NO_SETUP"
    assert "funding" in setup["reasons"][0]


def test_v12_detects_volume_weighted_time_series_momentum() -> None:
    baseline = [_c15(i, "100", "100.2", "99.8", "100", "100") for i in range(8)]
    recent = []
    price = Decimal("100")
    for i in range(8, 16):
        o = price
        c = price + Decimal("0.25")
        recent.append(_c15(i, str(o), str(c + Decimal("0.1")), str(o - Decimal("0.1")), str(c), "160"))
        price = c

    c1h = []
    for i, close in enumerate(("99", "99.5", "100", "101", "102")):
        c = Decimal(close)
        c1h.append({
            "open_time": CUTOFF - dt.timedelta(hours=5 - i),
            "open": c - Decimal("0.2"),
            "high": c + Decimal("0.2"),
            "low": c - Decimal("0.3"),
            "close": c,
            "volume": Decimal("500"),
        })

    chain = {
        "regime": {"primary_regime": "TREND_UP"},
        "features": {"tf": {"1h": {"atr": Decimal("2")}}},
        "_windows": SimpleNamespace(candles_15m=baseline + recent, candles_1h=c1h),
    }

    setup = detect_volume_weighted_tsmom(chain)

    assert setup["direction"] == "LONG"
    assert setup["volume_expansion"] >= Decimal("1.20")
    assert setup["weighted_return"] > 0
    assert setup["momentum_4h"] > 0
    assert setup["price_only_mode"] is True
    assert setup["detection_version"] == "V12_VOLUME_WEIGHTED_TSMOM_PREREG_V1"


def test_v12_rejects_without_volume_expansion() -> None:
    baseline = [_c15(i, "100", "100.2", "99.8", "100", "100") for i in range(8)]
    recent = []
    price = Decimal("100")
    for i in range(8, 16):
        o = price
        c = price + Decimal("0.25")
        recent.append(_c15(i, str(o), str(c + Decimal("0.1")), str(o - Decimal("0.1")), str(c), "100"))
        price = c
    c1h = [
        {"open_time": CUTOFF - dt.timedelta(hours=5 - i), "open": Decimal("100"), "high": Decimal("103"),
         "low": Decimal("99"), "close": Decimal(str(99 + i)), "volume": Decimal("500")}
        for i in range(5)
    ]
    chain = {
        "regime": {"primary_regime": "TREND_UP"},
        "features": {"tf": {"1h": {"atr": Decimal("2")}}},
        "_windows": SimpleNamespace(candles_15m=baseline + recent, candles_1h=c1h),
    }

    setup = detect_volume_weighted_tsmom(chain)
    assert setup["direction"] == "NO_SETUP"
    assert "volume" in setup["reasons"][0]
