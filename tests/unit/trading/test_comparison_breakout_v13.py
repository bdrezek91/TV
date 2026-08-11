from __future__ import annotations

from decimal import Decimal

from tradingview_mcp.core.backtest.comparison_breakout_v13 import evaluate_v13_gates


def _chain(
    *,
    regime: str = "TREND_UP",
    cvd: str = "5",
    buy: str = "70",
    sell: str = "30",
    relation: str = "PRICE_UP_OI_UP",
    funding: str = "0.0001",
) -> dict:
    return {
        "regime": {"primary_regime": regime},
        "features": {
            "volume": {
                "available": True,
                "cvd_slope_15m": Decimal(cvd),
                "buy_taker_volume": Decimal(buy),
                "sell_taker_volume": Decimal(sell),
            },
            "futures": {
                "available": True,
                "price_to_oi_relation": relation,
                "funding_rate": Decimal(funding),
            },
        },
    }


def test_long_requires_all_directional_gates() -> None:
    result = evaluate_v13_gates(_chain(), "LONG")
    assert result["eligible"] is True
    assert result["future_data_used"] is False


def test_rejects_breakout_against_regime() -> None:
    result = evaluate_v13_gates(_chain(regime="TREND_DOWN"), "LONG")
    assert result["eligible"] is False
    assert any("regime" in reason for reason in result["rejection_reasons"])


def test_rejects_oi_not_expanding_with_price() -> None:
    result = evaluate_v13_gates(_chain(relation="PRICE_UP_OI_DOWN"), "LONG")
    assert result["eligible"] is False
    assert any("price/OI" in reason for reason in result["rejection_reasons"])


def test_rejects_extreme_funding_crowded_with_direction() -> None:
    result = evaluate_v13_gates(_chain(funding="0.0005"), "LONG")
    assert result["eligible"] is False
    assert any("funding" in reason for reason in result["rejection_reasons"])


def test_short_uses_signed_flow_and_short_oi_relation() -> None:
    result = evaluate_v13_gates(
        _chain(
            regime="BREAKOUT_DOWN",
            cvd="-5",
            buy="30",
            sell="70",
            relation="PRICE_DOWN_OI_UP",
            funding="-0.0001",
        ),
        "SHORT",
    )
    assert result["eligible"] is True


def test_missing_derivatives_fails_closed() -> None:
    chain = _chain()
    chain["features"]["futures"] = {"available": False}
    result = evaluate_v13_gates(chain, "LONG")
    assert result["eligible"] is False
    assert "derivatives features unavailable" in result["rejection_reasons"]

