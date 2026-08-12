from __future__ import annotations

import datetime as dt
from decimal import Decimal

from tradingview_mcp.core.analysis.hard_gates import GateContext, evaluate_hard_gates
from tradingview_mcp.core.analysis.risk_engine import RiskEngineInput, evaluate_risk


def _ctx(**overrides):
    now = dt.datetime.now(dt.timezone.utc)
    base = dict(
        is_tradeable=True,
        orderbook_consistent=True,
        spread_bps=Decimal("5"),
        max_spread_bps=Decimal("8"),
        entry_price=Decimal("100"),
        stop_loss=Decimal("98"),
        risk_reward=Decimal("2.5"),
        min_risk_reward=Decimal("2.0"),
        expires_at=now + dt.timedelta(minutes=60),
        now=now,
        regime="TREND_UP",
        regime_allowed_for_strategy=True,
    )
    base.update(overrides)
    return GateContext(**base)


def test_hard_gates_pass_clean_setup():
    result = evaluate_hard_gates(_ctx())
    assert result.passed is True
    assert result.reasons == []


def test_hard_gate_stale_data():
    result = evaluate_hard_gates(_ctx(is_tradeable=False))
    assert result.passed is False
    assert any("stale" in r for r in result.reasons)


def test_hard_gate_inconsistent_orderbook():
    result = evaluate_hard_gates(_ctx(orderbook_consistent=False))
    assert result.passed is False
    assert any("inconsistent" in r for r in result.reasons)


def test_hard_gate_spread_too_wide():
    result = evaluate_hard_gates(_ctx(spread_bps=Decimal("20")))
    assert result.passed is False
    assert any("spread" in r for r in result.reasons)


def test_hard_gate_missing_entry_and_stop():
    result = evaluate_hard_gates(_ctx(entry_price=None, stop_loss=None))
    assert result.passed is False
    assert any("entry price" in r for r in result.reasons)
    assert any("stop loss" in r for r in result.reasons)


def test_hard_gate_rr_too_small():
    result = evaluate_hard_gates(_ctx(risk_reward=Decimal("1.0")))
    assert result.passed is False
    assert any("risk/reward" in r for r in result.reasons)


def test_hard_gate_expired_setup():
    now = dt.datetime.now(dt.timezone.utc)
    result = evaluate_hard_gates(_ctx(now=now, expires_at=now - dt.timedelta(minutes=1)))
    assert result.passed is False
    assert any("expired" in r for r in result.reasons)


def test_hard_gate_duplicate_active():
    result = evaluate_hard_gates(_ctx(duplicate_active_exists=True))
    assert result.passed is False
    assert any("duplicate" in r for r in result.reasons)


def test_hard_gate_daily_trade_limit():
    result = evaluate_hard_gates(_ctx(daily_trades_count=5, max_trades_per_day=5))
    assert result.passed is False
    assert any("daily trade limit" in r for r in result.reasons)


def test_hard_gate_consecutive_losses():
    result = evaluate_hard_gates(_ctx(consecutive_losses=3, max_consecutive_losses=3))
    assert result.passed is False
    assert any("consecutive-loss" in r for r in result.reasons)


def test_hard_gate_conflicting_position():
    result = evaluate_hard_gates(_ctx(conflicting_position_exists=True))
    assert result.passed is False
    assert any("conflicting open position" in r for r in result.reasons)


def test_hard_gate_chaotic_regime_never_tradeable():
    result = evaluate_hard_gates(_ctx(regime="CHAOTIC", regime_allowed_for_strategy=True))
    assert result.passed is False
    assert any("never tradeable" in r for r in result.reasons)


def test_hard_gate_regime_not_allowed_for_strategy():
    result = evaluate_hard_gates(_ctx(regime="RANGE", regime_allowed_for_strategy=False))
    assert result.passed is False
    assert any("allowed-regimes" in r for r in result.reasons)


def test_hard_gate_daily_loss_limit():
    result = evaluate_hard_gates(_ctx(daily_loss_limit_hit=True))
    assert result.passed is False
    assert any("daily loss limit" in r for r in result.reasons)


# -- Risk Engine ------------------------------------------------------------

def _risk_input(**overrides):
    base = dict(
        account_equity=Decimal("10000"),
        entry_price=Decimal("68000"),
        stop_loss=Decimal("67500"),
        risk_per_trade_pct=Decimal("0.25"),
        tick_size=Decimal("0.5"),
        qty_step=Decimal("0.001"),
        fee_rate=Decimal("0.0006"),
        slippage=Decimal("5"),
        max_leverage=Decimal("3"),
        min_risk_reward=Decimal("2.0"),
        risk_reward=Decimal("2.5"),
    )
    base.update(overrides)
    return RiskEngineInput(**base)


def test_risk_engine_approves_valid_setup():
    result = evaluate_risk(_risk_input())
    assert result.approved is True
    assert result.position_size > 0
    # risk_amount = 10000 * 0.25% = 25
    assert result.risk_amount == Decimal("25.00")
    assert result.max_loss_with_fees > result.risk_amount  # fees/slippage add on top


def test_risk_engine_position_sizing_formula():
    result = evaluate_risk(_risk_input())
    stop_distance = Decimal("500")  # 68000 - 67500
    expected_raw = Decimal("25.00") / stop_distance
    # position_size is floor-rounded to qty_step=0.001
    assert abs(result.position_size - expected_raw) < Decimal("0.002")


def test_risk_engine_rejects_zero_stop_distance():
    result = evaluate_risk(_risk_input(stop_loss=Decimal("68000")))
    assert result.approved is False
    assert any("stop distance" in r for r in result.rejection_reasons)


def test_risk_engine_rejects_rr_below_minimum():
    result = evaluate_risk(_risk_input(risk_reward=Decimal("1.0")))
    assert result.approved is False


def test_risk_engine_rejects_duplicate_position():
    result = evaluate_risk(_risk_input(has_open_position_same_symbol=True))
    assert result.approved is False
    assert any("duplicate" in r for r in result.rejection_reasons)


def test_risk_engine_rejects_daily_loss_limit_hit():
    result = evaluate_risk(_risk_input(daily_loss_limit_hit=True))
    assert result.approved is False


def test_risk_engine_rejects_consecutive_losses_no_martingale():
    result = evaluate_risk(_risk_input(consecutive_losses=3, max_consecutive_losses=3))
    assert result.approved is False
    assert any("martingale" in r for r in result.rejection_reasons)


def test_risk_engine_rejects_daily_trade_limit():
    result = evaluate_risk(_risk_input(daily_trades_count=5, max_trades_per_day=5))
    assert result.approved is False


def test_risk_engine_caps_leverage():
    # Deliberately tiny stop distance -> huge raw position size -> must be
    # capped by max_leverage rather than approved at absurd leverage.
    result = evaluate_risk(_risk_input(stop_loss=Decimal("67995"), risk_reward=Decimal("5")))
    assert result.effective_leverage <= Decimal("3.01")  # small tolerance for rounding


def test_risk_engine_max_loss_includes_fees_and_slippage():
    approved = evaluate_risk(_risk_input())
    zero_cost = evaluate_risk(_risk_input(fee_rate=Decimal("0"), slippage=Decimal("0")))
    assert approved.max_loss_with_fees > zero_cost.max_loss_with_fees
