from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal
from pathlib import Path

import pytest

from tradingview_mcp.core.analysis import indicators as ind
from tradingview_mcp.core.backtest import comparison_orderflow_trigger_v17 as v17
from tradingview_mcp.core.backtest.comparison_orderflow_trigger_v17 import (
    ATR_MULTIPLE,
    V17RawInputs,
    detect_v17_signal,
    simulate_v17_trade,
    validate_v17_development_cache_root,
)

UTC = dt.timezone.utc


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _trending_series(
    start: dt.datetime,
    interval: dt.timedelta,
    count: int,
    *,
    base: float = 100.0,
    step: float = 0.6,
    amp: float = 2.0,
    period: int = 6,
) -> list[dict]:
    """A zig-zagging but net-upward-trending closed-candle series: enough
    to reliably produce swing_structure HH_HL and a rising EMA20 > EMA50 on
    both the 1h and 4h timeframes once >= 50 bars have closed."""
    out = []
    for i in range(count):
        val = base + i * step + amp * math.sin(i * (2 * math.pi / period))
        out.append(
            {
                "open_time": start + i * interval,
                "open": str(val),
                "high": str(val),
                "low": str(val),
                "close": str(val),
                "volume": "1000",
            }
        )
    return out


def _flat_series(start: dt.datetime, interval: dt.timedelta, count: int, price: float = 100.0) -> list[dict]:
    return [
        {
            "open_time": start + i * interval,
            "open": str(price),
            "high": str(price),
            "low": str(price),
            "close": str(price),
            "volume": "1000",
        }
        for i in range(count)
    ]


def _trades_series(from_ts: dt.datetime, to_ts: dt.datetime, *, bullish: bool = True) -> list[dict]:
    out = []
    t = from_ts
    while t < to_ts:
        row = (
            {"delta": "50", "buy_taker_volume": "100", "sell_taker_volume": "40"}
            if bullish
            else {"delta": "-50", "buy_taker_volume": "40", "sell_taker_volume": "100"}
        )
        out.append(
            {
                "bucket_start": t,
                "trade_count": "20",
                "avg_trade_size": "5",
                "large_trade_count": "4",
                "largest_trade_size": "60000",
                **row,
            }
        )
        t += dt.timedelta(minutes=1)
    return out


def _deriv_series(from_ts: dt.datetime, to_ts: dt.datetime, *, base: float = 100.0, bullish: bool = True) -> list[dict]:
    out = []
    t = from_ts
    price = base
    step = 0.02 if bullish else -0.02
    while t < to_ts:
        out.append(
            {
                "source_timestamp": t,
                "open_interest": "1000" if bullish else "2000",
                "mark_price": str(price),
                "index_price": str(price),
                "funding_rate": "0.0001",
                "long_short_ratio": "1.0",
            }
        )
        price += step
        t += dt.timedelta(minutes=5)
    return out


OFFSET_HOURS = 260  # >= 50 closed 4h bars (200h) so EMA50 on 4h is defined
TOTAL_HOURS = OFFSET_HOURS + 20
START = dt.datetime(2026, 1, 1, tzinfo=UTC)
CUTOFF = START + dt.timedelta(hours=OFFSET_HOURS)


def _bullish_inputs(*, extra_hours: int = 20) -> V17RawInputs:
    total = OFFSET_HOURS + extra_hours
    return V17RawInputs(
        candles_4h=_trending_series(START, dt.timedelta(hours=4), total // 4 + 5),
        candles_1h=_trending_series(START, dt.timedelta(hours=1), total),
        candles_15m=_trending_series(START, dt.timedelta(minutes=15), total * 4),
        trades_1m=_trades_series(CUTOFF - dt.timedelta(minutes=40), CUTOFF + dt.timedelta(hours=13), bullish=True),
        derivatives=_deriv_series(CUTOFF - dt.timedelta(hours=2), CUTOFF + dt.timedelta(hours=13), bullish=True),
    )


def _flat_inputs() -> V17RawInputs:
    return V17RawInputs(
        candles_4h=_flat_series(START, dt.timedelta(hours=4), TOTAL_HOURS // 4 + 5),
        candles_1h=_flat_series(START, dt.timedelta(hours=1), TOTAL_HOURS),
        candles_15m=_flat_series(START, dt.timedelta(minutes=15), TOTAL_HOURS * 4),
        trades_1m=_trades_series(CUTOFF - dt.timedelta(minutes=40), CUTOFF, bullish=True),
        derivatives=_deriv_series(CUTOFF - dt.timedelta(hours=2), CUTOFF, bullish=True),
    )


# ---------------------------------------------------------------------------
# Regime gate
# ---------------------------------------------------------------------------

def test_regime_gate_rejects_flat_no_edge_market() -> None:
    signal = detect_v17_signal("ETHUSDT", CUTOFF, _flat_inputs(), None)
    assert signal["eligible"] is False
    assert signal["primary_regime"] not in v17.ELIGIBLE_ENTRY_REGIMES
    assert "regime gate rejected" in signal["reason"]


# ---------------------------------------------------------------------------
# Entry trigger
# ---------------------------------------------------------------------------

def test_entry_fires_confirmed_long_in_trend_up_regime() -> None:
    signal = detect_v17_signal("ETHUSDT", CUTOFF, _bullish_inputs(), None)
    assert signal["eligible"] is True
    assert signal["primary_regime"] == "TREND_UP"
    assert signal["direction"] == "LONG"
    assert signal["net_independent_score"] >= 3
    assert signal["future_data_used"] is False


def test_entry_rejected_when_both_directions_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both-directions-CONFIRMED should be structurally near-impossible given
    the symmetric dimensions, but the code path must still reject it, so we
    force it via monkeypatch to exercise that branch directly."""

    def _always_confirmed(*args, **kwargs):
        return {
            "status": "CONFIRMED",
            "net_independent_score": 5,
            "available_dimensions": 6,
            "new_independent_confirmations": [],
            "contradictions": [],
        }

    monkeypatch.setattr(v17.ofc, "confirm_setup", _always_confirmed)
    signal = detect_v17_signal("ETHUSDT", CUTOFF, _bullish_inputs(), None)
    assert signal["eligible"] is False
    assert "ambiguous" in signal["reason"]


# ---------------------------------------------------------------------------
# ATR stop: independent from order-flow inputs
# ---------------------------------------------------------------------------

def test_atr_stop_matches_wilder_atr_and_is_independent_of_order_flow() -> None:
    bullish = _bullish_inputs()
    signal = detect_v17_signal("ETHUSDT", CUTOFF, bullish, None)
    assert signal["eligible"] is True

    closed_1h = [c for c in bullish.candles_1h if c["open_time"] + dt.timedelta(hours=1) <= CUTOFF]
    closed_1h = closed_1h[-v17.CANDLE_WINDOW :]  # _build_feature_bundle windows to the same trailing count
    expected_atr = ind.atr(closed_1h, 14)
    expected_entry = Decimal(str(closed_1h[-1]["close"]))
    assert signal["atr_1h"] == expected_atr
    assert signal["entry_price"] == expected_entry
    assert signal["stop_price"] == expected_entry - ATR_MULTIPLE * expected_atr

    # Swap every order-flow-only field (taker sides, OI/funding/long-short
    # ratio, even flipped to strongly bearish) while keeping candles
    # identical -- the price-only ATR/entry (and therefore the stop) must
    # not move at all, regardless of what that does to the order-flow
    # verdict itself. Checked at the feature-bundle level so the assertion
    # holds independent of whether the mutated order flow still confirms.
    mutated = V17RawInputs(
        candles_4h=bullish.candles_4h,
        candles_1h=bullish.candles_1h,
        candles_15m=bullish.candles_15m,
        trades_1m=[
            {**row, "buy_taker_volume": "10", "sell_taker_volume": "500", "delta": "-490"}
            for row in bullish.trades_1m
        ],
        derivatives=[
            {**row, "open_interest": "999999", "funding_rate": "-0.01", "long_short_ratio": "3.5"}
            for row in bullish.derivatives
        ],
    )
    mutated_bundle = v17._build_feature_bundle(mutated, CUTOFF)
    assert mutated_bundle["tf"]["1h"]["atr"] == signal["atr_1h"]
    assert mutated_bundle["tf"]["1h"]["last_close"] == signal["entry_price"]
    # Sanity: the mutation actually changed the order-flow read (proves this
    # isn't a no-op mutation), while the stop-relevant price data is untouched.
    assert mutated_bundle["volume"]["delta"] != v17._build_feature_bundle(bullish, CUTOFF)["volume"]["delta"]


# ---------------------------------------------------------------------------
# Exits
# ---------------------------------------------------------------------------

def test_atr_stop_exit_fires_on_intrabar_breach() -> None:
    inputs = _bullish_inputs()
    signal = detect_v17_signal("ETHUSDT", CUTOFF, inputs, None)
    assert signal["eligible"] is True and signal["direction"] == "LONG"

    crashed_candle = {
        "open_time": CUTOFF,
        "open": "200",
        "high": "200",
        "low": "1",
        "close": "150",
        "volume": "1000",
    }
    crashed_candles = [c for c in inputs.candles_1h if c["open_time"] != CUTOFF] + [crashed_candle]
    crashed_inputs = V17RawInputs(
        candles_4h=inputs.candles_4h,
        candles_1h=crashed_candles,
        candles_15m=inputs.candles_15m,
        trades_1m=inputs.trades_1m,
        derivatives=inputs.derivatives,
    )
    result = simulate_v17_trade(signal, crashed_inputs, None)
    assert result.exit_reason == "ATR_STOP"
    assert result.exit_time == CUTOFF + dt.timedelta(hours=1)
    assert result.observation.r_multiple is not None
    assert result.observation.r_multiple < 0


def test_orderflow_reversal_exit_fires_when_net_score_drops() -> None:
    flip = CUTOFF + dt.timedelta(hours=1)
    inputs = V17RawInputs(
        candles_4h=_trending_series(START, dt.timedelta(hours=4), TOTAL_HOURS // 4 + 5),
        candles_1h=_trending_series(START, dt.timedelta(hours=1), TOTAL_HOURS),
        candles_15m=_trending_series(START, dt.timedelta(minutes=15), TOTAL_HOURS * 4),
        trades_1m=(
            _trades_series(CUTOFF - dt.timedelta(minutes=40), flip, bullish=True)
            + _trades_series(flip, CUTOFF + dt.timedelta(hours=13), bullish=False)
        ),
        derivatives=(
            _deriv_series(CUTOFF - dt.timedelta(hours=2), flip, bullish=True)
            + _deriv_series(flip, CUTOFF + dt.timedelta(hours=13), bullish=False)
        ),
    )
    signal = detect_v17_signal("ETHUSDT", CUTOFF, inputs, None)
    assert signal["eligible"] is True and signal["direction"] == "LONG"

    result = simulate_v17_trade(signal, inputs, None)
    assert result.exit_reason == "ORDERFLOW_REVERSAL"
    assert result.exit_time is not None
    assert result.exit_time < CUTOFF + dt.timedelta(hours=v17.MAX_HOLD_HOURS)


def test_max_hold_exit_fires_after_twelve_hours() -> None:
    inputs = _bullish_inputs()
    signal = detect_v17_signal("ETHUSDT", CUTOFF, inputs, None)
    assert signal["eligible"] is True

    result = simulate_v17_trade(signal, inputs, None)
    assert result.exit_reason == "MAX_HOLD"
    assert result.exit_time == CUTOFF + dt.timedelta(hours=v17.MAX_HOLD_HOURS)
    assert result.observation.filled is True


# ---------------------------------------------------------------------------
# Cost / funding arithmetic
# ---------------------------------------------------------------------------

def test_costs_and_funding_match_frozen_formula() -> None:
    """Uses the real MAX_HOLD path (held exactly 12h -> one full 8h funding
    interval) so entry/stop/exit prices are genuine simulator output, then
    recomputes the frozen cost formula independently and compares."""
    inputs = _bullish_inputs()
    signal = detect_v17_signal("ETHUSDT", CUTOFF, inputs, None)
    assert signal["eligible"] is True

    result = simulate_v17_trade(signal, inputs, None)
    assert result.exit_reason == "MAX_HOLD"
    assert result.exit_time == CUTOFF + dt.timedelta(hours=v17.MAX_HOLD_HOURS)

    entry_price, stop_price = signal["entry_price"], signal["stop_price"]
    risk_amount = v17.CAPITAL * v17.RISK_PCT / Decimal("100")
    quantity = risk_amount / abs(entry_price - stop_price)
    exit_price = entry_price + (result.gross_pnl / quantity)  # LONG: gross = (exit-entry)*qty
    entry_notional = entry_price * quantity
    exit_notional = exit_price * quantity
    expected_fees = (entry_notional + exit_notional) * v17.TAKER_FEE_RATE_PCT / Decimal("100")
    expected_slippage = (entry_notional + exit_notional) * v17.SLIPPAGE_BPS_EACH_SIDE / Decimal("10000")
    held_hours = (result.exit_time - signal["decision_time"]).total_seconds() / 3600
    expected_funding_intervals = int(held_hours // 8)
    assert expected_funding_intervals == 1
    expected_funding = (
        entry_notional * v17.CONSERVATIVE_FUNDING_BPS_PER_8H / Decimal("10000") * expected_funding_intervals
    )

    assert result.fees == expected_fees
    assert result.slippage == expected_slippage
    assert result.funding == expected_funding
    assert result.fees > 0
    assert result.slippage > 0
    assert result.funding > 0
    assert result.net_pnl == result.gross_pnl - expected_fees - expected_slippage - expected_funding
    assert result.observation.r_multiple == result.net_pnl / risk_amount


def test_simulate_marks_unfilled_when_no_future_candles() -> None:
    entry_time = dt.datetime(2026, 3, 1, tzinfo=UTC)
    signal = {
        "eligible": True,
        "symbol": "ETHUSDT",
        "direction": "LONG",
        "decision_time": entry_time,
        "entry_price": Decimal("100"),
        "stop_price": Decimal("95"),
    }
    inputs = V17RawInputs(candles_4h=[], candles_1h=[], candles_15m=[], trades_1m=[], derivatives=[])
    result = simulate_v17_trade(signal, inputs, None)
    assert result.exit_reason == "NO_FUTURE_DATA"
    assert result.observation.filled is False
    assert result.observation.r_multiple is None


def test_simulate_rejects_ineligible_signal() -> None:
    inputs = V17RawInputs(candles_4h=[], candles_1h=[], candles_15m=[], trades_1m=[], derivatives=[])
    with pytest.raises(ValueError, match="ineligible"):
        simulate_v17_trade({"eligible": False}, inputs, None)


def test_v17_development_rejects_reserved_mr_v2_holdout(tmp_path: Path) -> None:
    reserved = tmp_path / "holdout_mrv2_120d" / "cache"
    with pytest.raises(ValueError, match="reserved holdout"):
        validate_v17_development_cache_root(reserved)
