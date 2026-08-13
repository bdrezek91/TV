"""Frozen development contract for V17 order-flow-triggered, regime-gated
single-instrument strategy.

Unlike V15 (two synchronized legs vs BTC), V17 trades each of the five
frozen symbols independently and single-leg. Order flow (11 dimensions from
`orderflow_confirmation_v2`) is the entry TRIGGER itself, not a secondary
confirmation of a price setup -- there is no upstream Warstwa 2 setup here,
so `confirm_setup` is called with an empty `regime_full`/`setup` (no
`reasons`/`counterarguments`), which deliberately leaves its `already_used`
buckets empty: every dimension counts as independent.

The key V15 lesson (entry and stop must never share a variable) is honored
by construction: entry is triggered by order-flow dimensions built from
volume/futures/liquidations/orderbook; the stop is `3.0 * ATR(14)` on 1h
candles -- pure price data, computed by `feature_engine.compute_price_action_
features` -> `indicators.atr`, which never reads volume/futures/orderbook/
liquidations at all.

V17 is a single-variable follow-up to V16 (same module below, cloned):
V16 used `ATR_MULTIPLE = 1.5`; V17 widens it to `ATR_MULTIPLE = 3.0` to test
whether V16's negative net result (driven by a small number of tight-stop
tail losses; see `V16_DEVELOPMENT_POSTMORTEM.md`) was a stop-placement
artifact rather than a flaw in the order-flow trigger itself. Every other
constant, gate, and formula in this module is intentionally identical to
V16.

This module is research-only, pure (no DB/network access), and reuses
`regime_classifier_v2` / `orderflow_confirmation_v2` / `feature_engine`
directly rather than re-implementing their dimension logic.

Known development-cache limitation (see V17_PREREGISTRATION.md): historical
orderbook and liquidations are marked `DEGRADED_NO_HISTORICAL_ORDERBOOK` /
`DEGRADED_NO_HISTORICAL_LIQUIDATIONS` in the extended-history cache used for
this development runner. `orderbook={"available": False}` and
`liquidations={"available": False}` are therefore always passed here -- this
necessarily caps `available_dimensions` in `confirm_setup` at 8/11 (the
three orderbook-bucket dimensions -- `orderbook_imbalance`, `spread_and_
depth`, `microprice` -- are always UNAVAILABLE) and removes any liquidation-
cascade evidence from both Warstwa 1 and Warstwa 3. This is a documented gap
of the *development* evaluation, not a bug to route around.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from tradingview_mcp.core.analysis import feature_engine as fe
from tradingview_mcp.core.analysis import orderflow_confirmation_v2 as ofc
from tradingview_mcp.core.analysis import regime_classifier_v2 as rc
from tradingview_mcp.core.backtest.comparison_holdout_v1 import REQUIRED_SYMBOLS
from tradingview_mcp.core.backtest.comparison_v1 import TradeObservation, Variant

V17_NAME = "V17_ORDERFLOW_TRIGGER_SINGLE_INSTRUMENT"
V17_SETUP = "orderflow_trigger_regime_gated"
BTC_SYMBOL = "BTCUSDT"
RESERVED_HOLDOUT_MARKERS = ("holdout_mrv2_120d",)

# Entry allowed only in these Warstwa 1 regimes -- excludes RANGE, SQUEEZE,
# HIGH_VOLATILITY, PANIC_LIQUIDATION_*, LOW_LIQUIDITY, UNSTABLE_DATA, NO_EDGE.
ELIGIBLE_ENTRY_REGIMES = frozenset(
    {"TREND_UP", "TREND_DOWN", "BREAKOUT_UP", "BREAKOUT_DOWN"}
)

ATR_PERIOD = 14
ATR_MULTIPLE = Decimal("3.0")
MAX_HOLD_HOURS = 12
CAPITAL = Decimal("10000")
RISK_PCT = Decimal("1.0")
TAKER_FEE_RATE_PCT = Decimal("0.055")
SLIPPAGE_BPS_EACH_SIDE = Decimal("2")
CONSERVATIVE_FUNDING_BPS_PER_8H = Decimal("1")

FOUR_HOURS = dt.timedelta(hours=4)
ONE_HOUR = dt.timedelta(hours=1)
FIFTEEN_MINUTES = dt.timedelta(minutes=15)
ONE_MINUTE = dt.timedelta(minutes=1)

CANDLE_WINDOW = 200
TRADE_AGG_WINDOW = 30
DERIVATIVES_WINDOW = 20

# Same replay philosophy as comparison_history_v1.RESEARCH_HIST_MAX_AGES:
# real historical freshness gates against the simulated cutoff, not disabled
# sentinels. Orderbook is intentionally NOT in this dict -- it is always
# absent by construction in this development cache (see module docstring),
# not a per-observation freshness signal, so there is no meaningful
# staleness threshold to define for it.
V17_MAX_AGES = {
    "candles": 3600 * 2 + 900,
    "trades": 60 * 15,
    "derivatives": 3600 * 6,
}


def validate_v17_development_cache_root(cache_root: Path) -> Path:
    """Reject every holdout reserved for another frozen hypothesis."""
    resolved = cache_root.resolve()
    lowered = str(resolved).lower()
    marker = next((item for item in RESERVED_HOLDOUT_MARKERS if item in lowered), None)
    if marker:
        raise ValueError(
            f"V17 development runner refuses reserved holdout cache: {marker}"
        )
    return resolved


@dataclass(frozen=True)
class V17RawInputs:
    """Raw, already-loaded historical rows for one symbol.

    Shapes match `extended_history_loader_v1.ExtendedReplayBundle.inputs`
    (a `comparison_history_v1.HistoricalInputs`), but this dataclass only
    keeps the fields V17 actually needs -- no 1m candles, no orderbook/
    liquidations rows (both are always empty in this cache; see docstring).
    """

    candles_4h: Sequence[Mapping[str, Any]]
    candles_1h: Sequence[Mapping[str, Any]]
    candles_15m: Sequence[Mapping[str, Any]]
    trades_1m: Sequence[Mapping[str, Any]]
    derivatives: Sequence[Mapping[str, Any]]


def _closed_before(
    rows: Sequence[Mapping[str, Any]],
    key: str,
    duration: dt.timedelta,
    cutoff: dt.datetime,
    limit: int,
) -> list[Mapping[str, Any]]:
    """Rows whose entire interval was knowable by `cutoff` -- no lookahead."""
    if cutoff.tzinfo is None:
        raise ValueError("cutoff must be timezone-aware")
    out = [row for row in rows if row[key] + duration <= cutoff]
    return out[-limit:] if limit else out


def _point_before(
    rows: Sequence[Mapping[str, Any]], key: str, cutoff: dt.datetime, limit: int
) -> list[Mapping[str, Any]]:
    out = [row for row in rows if row[key] <= cutoff]
    return out[-limit:] if limit else out


def _build_feature_bundle(inputs: V17RawInputs, cutoff: dt.datetime) -> dict[str, Any]:
    """Assemble tf/volume/orderbook/futures/liquidations/data_quality from
    closed-only historical rows -- the same shapes `regime_classifier_v2`
    and `orderflow_confirmation_v2` already consume, built directly from
    `feature_engine.compute_*_features` with zero reimplementation of that
    math."""
    c4 = _closed_before(inputs.candles_4h, "open_time", FOUR_HOURS, cutoff, CANDLE_WINDOW)
    c1 = _closed_before(inputs.candles_1h, "open_time", ONE_HOUR, cutoff, CANDLE_WINDOW)
    c15 = _closed_before(inputs.candles_15m, "open_time", FIFTEEN_MINUTES, cutoff, CANDLE_WINDOW)
    trades = _closed_before(inputs.trades_1m, "bucket_start", ONE_MINUTE, cutoff, TRADE_AGG_WINDOW)
    deriv = _point_before(inputs.derivatives, "source_timestamp", cutoff, DERIVATIVES_WINDOW)

    tf = {
        "4h": fe.compute_price_action_features(c4),
        "1h": fe.compute_price_action_features(c1),
        "15m": fe.compute_price_action_features(c15),
    }
    volume = fe.compute_volume_features(trades)
    futures = fe.compute_futures_features(deriv[-1] if deriv else {}, deriv)
    # Always unavailable in this development cache -- see module docstring.
    orderbook: dict[str, Any] = {"available": False}
    liquidations: dict[str, Any] = {"available": False}

    source_timestamps = {
        "candles": (c1[-1]["open_time"] + ONE_HOUR) if c1 else None,
        "trades": (trades[-1]["bucket_start"] + ONE_MINUTE) if trades else None,
        "orderbook": None,
        "derivatives": deriv[-1]["source_timestamp"] if deriv else None,
    }
    data_quality = fe.assess_data_quality(
        source_timestamps,
        V17_MAX_AGES,
        # Orderbook is permanently absent by construction here, not a live
        # quality fluctuation -- same non-required treatment
        # `regime_service_v2.build_features_from_windows` already applies
        # to orderbook in the live path.
        required_sources=("candles", "trades"),
        orderbook_consistent=True,
        now=cutoff,
    )
    return {
        "tf": tf,
        "volume": volume,
        "orderbook": orderbook,
        "futures": futures,
        "liquidations": liquidations,
        "data_quality": data_quality,
    }


def _btc_regime_at(
    symbol: str, cutoff: dt.datetime, btc_inputs: Optional[V17RawInputs]
) -> Optional[str]:
    """BTC's own regime is a fresh classification at `cutoff`, not carried
    over from an earlier bar -- matches `orderflow_confirmation_v2._dim_btc_
    alignment`'s "not applicable" treatment when the symbol IS BTCUSDT."""
    if symbol == BTC_SYMBOL or btc_inputs is None:
        return None
    btc_bundle = _build_feature_bundle(btc_inputs, cutoff)
    btc_regime = rc.classify_from_features(
        BTC_SYMBOL,
        cutoff,
        btc_bundle["tf"],
        btc_bundle["volume"],
        btc_bundle["orderbook"],
        btc_bundle["futures"],
        btc_bundle["liquidations"],
        btc_bundle["data_quality"],
        btc_regime=None,
    )
    return btc_regime["primary_regime"]


def detect_v17_signal(
    symbol: str,
    cutoff: dt.datetime,
    inputs: V17RawInputs,
    btc_inputs: Optional[V17RawInputs],
) -> dict[str, Any]:
    """Return the frozen V17 decision at `cutoff`, using only closed rows.

    Regime gate first (Warstwa 1), then order-flow entry trigger evaluated
    for both candidate directions (Warstwa 3 reused as the primary trigger,
    since V17 has no Warstwa 2 setup of its own). CONFIRMED in exactly one
    direction fires a signal; CONFIRMED in both is rejected as ambiguous.
    """
    symbol = symbol.upper()
    if symbol not in REQUIRED_SYMBOLS:
        raise ValueError(f"unsupported V17 symbol: {symbol}")

    btc_regime = _btc_regime_at(symbol, cutoff, btc_inputs)
    bundle = _build_feature_bundle(inputs, cutoff)
    regime_result = rc.classify_from_features(
        symbol,
        cutoff,
        bundle["tf"],
        bundle["volume"],
        bundle["orderbook"],
        bundle["futures"],
        bundle["liquidations"],
        bundle["data_quality"],
        btc_regime=btc_regime,
    )
    primary_regime = regime_result["primary_regime"]
    if primary_regime not in ELIGIBLE_ENTRY_REGIMES:
        return {
            "candidate": V17_NAME,
            "eligible": False,
            "reason": f"regime gate rejected primary_regime={primary_regime}",
            "symbol": symbol,
            "decision_time": cutoff,
            "primary_regime": primary_regime,
        }

    tf_1h = bundle["tf"]["1h"]
    entry_price = tf_1h.get("last_close") if tf_1h.get("available") else None
    atr = tf_1h.get("atr") if tf_1h.get("available") else None
    if entry_price is None or atr is None or atr <= 0:
        return {
            "candidate": V17_NAME,
            "eligible": False,
            "reason": "insufficient closed 1h history for entry price/ATR(14)",
            "symbol": symbol,
            "decision_time": cutoff,
            "primary_regime": primary_regime,
        }

    regime_full: dict[str, Any] = {}
    conf_long = ofc.confirm_setup(
        symbol, {"direction": "LONG"}, regime_full, bundle["tf"], bundle["volume"],
        bundle["orderbook"], bundle["futures"], bundle["liquidations"],
        btc_regime=btc_regime, data_quality=bundle["data_quality"],
    )
    conf_short = ofc.confirm_setup(
        symbol, {"direction": "SHORT"}, regime_full, bundle["tf"], bundle["volume"],
        bundle["orderbook"], bundle["futures"], bundle["liquidations"],
        btc_regime=btc_regime, data_quality=bundle["data_quality"],
    )
    long_confirmed = conf_long["status"] == "CONFIRMED"
    short_confirmed = conf_short["status"] == "CONFIRMED"

    if long_confirmed and short_confirmed:
        return {
            "candidate": V17_NAME,
            "eligible": False,
            "reason": "ambiguous: both directions CONFIRMED simultaneously",
            "symbol": symbol,
            "decision_time": cutoff,
            "primary_regime": primary_regime,
        }
    if long_confirmed:
        direction, confirmation = "LONG", conf_long
    elif short_confirmed:
        direction, confirmation = "SHORT", conf_short
    else:
        return {
            "candidate": V17_NAME,
            "eligible": False,
            "reason": "no confirmed order-flow direction",
            "symbol": symbol,
            "decision_time": cutoff,
            "primary_regime": primary_regime,
        }

    # ATR-based stop is computed purely from tf_1h (price only) -- it never
    # reads `confirmation`/volume/futures/orderbook/liquidations, keeping
    # entry and stop on two independent variables (the V15 lesson).
    stop_price = (
        entry_price - ATR_MULTIPLE * atr
        if direction == "LONG"
        else entry_price + ATR_MULTIPLE * atr
    )
    if stop_price <= 0:
        return {
            "candidate": V17_NAME,
            "eligible": False,
            "reason": "non-positive ATR stop price",
            "symbol": symbol,
            "decision_time": cutoff,
            "primary_regime": primary_regime,
        }

    return {
        "candidate": V17_NAME,
        "eligible": True,
        "decision_time": cutoff,
        "symbol": symbol,
        "direction": direction,
        "primary_regime": primary_regime,
        "btc_regime": btc_regime,
        "entry_price": entry_price,
        "atr_1h": atr,
        "stop_price": stop_price,
        "net_independent_score": confirmation["net_independent_score"],
        "available_dimensions": confirmation["available_dimensions"],
        "confirmed_dimensions": confirmation["new_independent_confirmations"],
        "contradicting_dimensions": confirmation["contradictions"],
        "future_data_used": False,
    }


@dataclass(frozen=True)
class V17ExecutionResult:
    signal: Mapping[str, Any]
    observation: TradeObservation
    exit_time: Optional[dt.datetime]
    exit_reason: str
    gross_pnl: Decimal
    net_pnl: Decimal
    fees: Decimal
    slippage: Decimal
    funding: Decimal


def _round_trip_costs(
    entry_notional: Decimal, exit_notional: Decimal, held: dt.timedelta
) -> tuple[Decimal, Decimal, Decimal]:
    fees = (entry_notional + exit_notional) * TAKER_FEE_RATE_PCT / Decimal("100")
    slippage = (entry_notional + exit_notional) * SLIPPAGE_BPS_EACH_SIDE / Decimal("10000")
    funding_intervals = int(held.total_seconds() // dt.timedelta(hours=8).total_seconds())
    funding = entry_notional * CONSERVATIVE_FUNDING_BPS_PER_8H / Decimal("10000") * funding_intervals
    return fees, slippage, funding


def simulate_v17_trade(
    signal: Mapping[str, Any],
    inputs: V17RawInputs,
    btc_inputs: Optional[V17RawInputs],
) -> V17ExecutionResult:
    """Walk forward on closed 1h candles only, applying the frozen exit
    priority ATR_STOP > ORDERFLOW_REVERSAL > MAX_HOLD at each bar."""
    if not signal.get("eligible"):
        raise ValueError("cannot simulate an ineligible V17 signal")

    symbol = signal["symbol"]
    direction = signal["direction"]
    entry_time: dt.datetime = signal["decision_time"]
    entry_price: Decimal = signal["entry_price"]
    stop_price: Decimal = signal["stop_price"]

    risk_amount = CAPITAL * RISK_PCT / Decimal("100")
    distance = abs(entry_price - stop_price)
    if distance <= 0:
        raise ValueError("entry/stop distance must be positive")
    quantity = risk_amount / distance
    entry_notional = entry_price * quantity

    future = sorted(
        (c for c in inputs.candles_1h if c["open_time"] >= entry_time),
        key=lambda c: c["open_time"],
    )
    horizon = entry_time + dt.timedelta(hours=MAX_HOLD_HOURS)

    exit_time: Optional[dt.datetime] = None
    exit_price: Optional[Decimal] = None
    exit_reason: Optional[str] = None
    for candle in future:
        close_time = candle["open_time"] + ONE_HOUR
        high = Decimal(str(candle["high"]))
        low = Decimal(str(candle["low"]))
        close = Decimal(str(candle["close"]))

        stop_hit = (direction == "LONG" and low <= stop_price) or (
            direction == "SHORT" and high >= stop_price
        )
        if stop_hit:
            exit_time, exit_price, exit_reason = close_time, stop_price, "ATR_STOP"
            break

        btc_regime_now = _btc_regime_at(symbol, close_time, btc_inputs)
        bundle_now = _build_feature_bundle(inputs, close_time)
        reversal = ofc.confirm_setup(
            symbol, {"direction": direction}, {}, bundle_now["tf"], bundle_now["volume"],
            bundle_now["orderbook"], bundle_now["futures"], bundle_now["liquidations"],
            btc_regime=btc_regime_now, data_quality=bundle_now["data_quality"],
        )
        if reversal["net_independent_score"] <= 0:
            exit_time, exit_price, exit_reason = close_time, close, "ORDERFLOW_REVERSAL"
            break

        if close_time >= horizon:
            exit_time, exit_price, exit_reason = close_time, close, "MAX_HOLD"
            break

    if exit_time is None or exit_price is None or exit_reason is None:
        observation = TradeObservation(
            variant=Variant.RESEARCH_V2,
            symbol=symbol,
            decision_time=entry_time,
            setup_name=V17_SETUP,
            direction=direction,
            filled=False,
            r_multiple=None,
        )
        return V17ExecutionResult(
            signal, observation, None, "NO_FUTURE_DATA", *(Decimal("0"),) * 5
        )

    held = max(dt.timedelta(0), exit_time - entry_time)
    exit_notional = exit_price * quantity
    fees, slippage, funding = _round_trip_costs(entry_notional, exit_notional, held)

    sign = Decimal("1") if direction == "LONG" else Decimal("-1")
    gross_pnl = sign * (exit_price - entry_price) * quantity
    net_pnl = gross_pnl - fees - slippage - funding

    observation = TradeObservation(
        variant=Variant.RESEARCH_V2,
        symbol=symbol,
        decision_time=entry_time,
        setup_name=V17_SETUP,
        direction=direction,
        filled=True,
        r_multiple=net_pnl / risk_amount,
        pnl_net=net_pnl,
        fees=fees,
        slippage=slippage,
        funding=funding,
    )
    return V17ExecutionResult(
        signal=signal,
        observation=observation,
        exit_time=exit_time,
        exit_reason=exit_reason,
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        fees=fees,
        slippage=slippage,
        funding=funding,
    )
