from decimal import Decimal

from tradingview_mcp.core.backtest.comparison_mean_reversion_v2 import (
    MR_V2_NAME,
    estimated_full_fill_round_trip_cost_r,
    evaluate_mean_reversion_v2,
)


def _chain(*, structure_15m="HH_HL", structure_4h="CONTRACTING", oi="-0.25", futures_available=True):
    return {
        "setups": {
            "mean_reversion": {
                "setup_type": "mean_reversion",
                "direction": "LONG",
                "entry_zone": {"low": Decimal("99"), "high": Decimal("100")},
                "stop_loss": Decimal("98"),
                "targets": [Decimal("102"), Decimal("104")],
            }
        },
        "risk_decisions": {
            "mean_reversion": {"decision": "APPROVED"}
        },
        "features": {
            "tf": {
                "15m": {"structure": structure_15m},
                "4h": {"structure": structure_4h},
            },
            "futures": {
                "available": futures_available,
                "oi_change_1h_pct": oi,
            },
        },
    }


def test_v2_accepts_noncontracting_nonexpanding_range_with_nonpositive_oi_change():
    result = evaluate_mean_reversion_v2(_chain())
    assert result["candidate"] == MR_V2_NAME
    assert result["eligible"] is True
    assert result["future_data_used"] is False
    assert result["oi_change_1h_pct"] == Decimal("-0.25")


def test_v2_rejects_15m_contracting_structure():
    result = evaluate_mean_reversion_v2(_chain(structure_15m="CONTRACTING"))
    assert result["eligible"] is False
    assert any("15m structure is CONTRACTING" in reason for reason in result["rejection_reasons"])


def test_v2_rejects_4h_expanding_structure():
    result = evaluate_mean_reversion_v2(_chain(structure_4h="EXPANDING"))
    assert result["eligible"] is False
    assert any("4h structure is EXPANDING" in reason for reason in result["rejection_reasons"])


def test_v2_rejects_positive_oi_change():
    result = evaluate_mean_reversion_v2(_chain(oi="0.01"))
    assert result["eligible"] is False
    assert any("fresh leveraged exposure" in reason for reason in result["rejection_reasons"])


def test_v2_fails_closed_when_futures_are_unavailable():
    result = evaluate_mean_reversion_v2(_chain(futures_available=False))
    assert result["eligible"] is False
    assert any("fails closed" in reason for reason in result["rejection_reasons"])


def test_cost_diagnostic_is_geometry_based_and_not_a_gate():
    # LONG worst entry 100, SL distance 2. Round-trip fee+slippage fraction:
    # 2 * (0.055% + 2 bps) = 0.15%, so cost/R = 0.0015*100/2 = 0.075.
    value = estimated_full_fill_round_trip_cost_r(
        direction="LONG",
        entry_low="99",
        entry_high="100",
        stop_loss="98",
    )
    assert value == Decimal("0.07500")
