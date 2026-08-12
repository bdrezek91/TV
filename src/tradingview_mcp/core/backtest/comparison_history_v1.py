"""Point-in-time signal generators for BACKTEST_COMPARISON_V1.

This module rebuilds both deterministic signal pipelines from *already loaded*
historical rows.  It performs no I/O and, critically, never includes a candle
whose interval had not closed at the decision cutoff.

TradingView history is not available as a true point-in-time API snapshot in
this project.  ``LEGACY_FIXED_TV`` therefore uses an explicitly labelled
``TV_RECONSTRUCTED`` context that reproduces only the current timeframe-bias
rules needed by the legacy strategy engine. ``LEGACY_AS_IS`` preserves the
known parser mismatch by presenting the empty trend fields that the current
parser effectively yields from today's response schema.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Optional, Sequence

from tradingview_mcp.core.analysis import feature_engine as fe
from tradingview_mcp.core.analysis import indicators as ind
from tradingview_mcp.core.analysis import orderflow_confirmation_v2 as ofc
from tradingview_mcp.core.analysis import regime_classifier_v2 as rc
from tradingview_mcp.core.analysis import risk_manager_v2 as rm
from tradingview_mcp.core.analysis import setup_detector_v2 as sd
from tradingview_mcp.core.analysis.tradingview_context import TradingViewContext
from tradingview_mcp.core.backtest.comparison_v1 import NeutralSignal, TvHistoryKind, Variant
from tradingview_mcp.core.services import regime_service_v2 as rs
from tradingview_mcp.core.services import strategy_engine_service as legacy_engine


FOUR_HOURS = dt.timedelta(hours=4)
ONE_HOUR = dt.timedelta(hours=1)
FIFTEEN_MINUTES = dt.timedelta(minutes=15)
ONE_MINUTE = dt.timedelta(minutes=1)

# Same replay philosophy as audit_regime_classifier: these are real historical
# freshness gates against the simulated cutoff, not disabled 1e12 sentinels.
RESEARCH_HIST_MAX_AGES = {
    "candles": 3600 * 2 + 900,
    "trades": 60 * 15,
    "orderbook": 3600 * 6,
    "derivatives": 3600 * 6,
}

# Legacy Feature Engine measures candle age from open_time rather than close
# time. Decision points are on exact hour boundaries, so the latest closed 15m
# candle normally opened 15 minutes earlier. Keep the historical gate explicit.
LEGACY_HIST_MAX_AGES = {
    "candles": 60 * 20,
    "trades": 60 * 15,
    "orderbook": 3600 * 6,
    "derivatives": 3600 * 6,
}


@dataclass(frozen=True)
class HistoricalInputs:
    candles_4h: Sequence[dict]
    candles_1h: Sequence[dict]
    candles_15m: Sequence[dict]
    candles_1m: Sequence[dict]
    trades_1m: Sequence[dict]
    liquidations_1m: Sequence[dict]
    orderbook: Sequence[dict]
    derivatives: Sequence[dict]


@dataclass(frozen=True)
class PointInTimeWindows:
    candles_4h: list[dict]
    candles_1h: list[dict]
    candles_15m: list[dict]
    candles_1m: list[dict]
    trades_1m: list[dict]
    liq_1m: list[dict]
    liq_5m: list[dict]
    liq_15m: list[dict]
    orderbook: list[dict]
    derivatives: list[dict]


def closed_interval_rows(
    rows: Sequence[dict],
    *,
    timestamp_key: str,
    duration: dt.timedelta,
    cutoff: dt.datetime,
) -> list[dict]:
    """Only rows whose entire interval was knowable by ``cutoff``."""
    return [dict(r) for r in rows if r[timestamp_key] + duration <= cutoff]


def point_snapshot_rows(
    rows: Sequence[dict], *, timestamp_key: str, cutoff: dt.datetime
) -> list[dict]:
    """Point-in-time snapshots (orderbook/OI) need no close-duration rule."""
    return [dict(r) for r in rows if r[timestamp_key] <= cutoff]


def windows_as_of(data: HistoricalInputs, cutoff: dt.datetime) -> PointInTimeWindows:
    c4 = closed_interval_rows(data.candles_4h, timestamp_key="open_time", duration=FOUR_HOURS, cutoff=cutoff)[-120:]
    c1 = closed_interval_rows(data.candles_1h, timestamp_key="open_time", duration=ONE_HOUR, cutoff=cutoff)[-120:]
    c15 = closed_interval_rows(data.candles_15m, timestamp_key="open_time", duration=FIFTEEN_MINUTES, cutoff=cutoff)[-120:]
    c1m = closed_interval_rows(data.candles_1m, timestamp_key="open_time", duration=ONE_MINUTE, cutoff=cutoff)[-5000:]
    trades = closed_interval_rows(data.trades_1m, timestamp_key="bucket_start", duration=ONE_MINUTE, cutoff=cutoff)[-30:]
    liq_series = closed_interval_rows(data.liquidations_1m, timestamp_key="bucket_start", duration=ONE_MINUTE, cutoff=cutoff)
    ob = point_snapshot_rows(data.orderbook, timestamp_key="source_timestamp", cutoff=cutoff)[-20:]
    deriv = point_snapshot_rows(data.derivatives, timestamp_key="source_timestamp", cutoff=cutoff)[-20:]
    return PointInTimeWindows(
        candles_4h=c4,
        candles_1h=c1,
        candles_15m=c15,
        candles_1m=c1m,
        trades_1m=trades,
        liq_1m=liq_series[-1:],
        liq_5m=liq_series[-5:],
        liq_15m=liq_series[-15:],
        orderbook=ob,
        derivatives=deriv,
    )


def _trend_15m(candles: Sequence[dict]) -> Optional[str]:
    closes = [Decimal(str(c["close"])) for c in candles]
    ema9, ema20 = ind.ema(closes, 9), ind.ema(closes, 20)
    if ema9 is None or ema20 is None:
        return None
    return "BULLISH" if ema9 > ema20 else "BEARISH"


def _trend_1h(candles: Sequence[dict]) -> Optional[str]:
    closes = [Decimal(str(c["close"])) for c in candles]
    ema20 = ind.ema(closes, 20)
    if not closes or ema20 is None:
        return None
    # Mirrors analyze_timeframe_context(): price above EMA20 => Bullish,
    # otherwise Bearish (there is no neutral equality branch there).
    return "BULLISH" if closes[-1] > ema20 else "BEARISH"


def _trend_4h(candles: Sequence[dict]) -> Optional[str]:
    closes = [Decimal(str(c["close"])) for c in candles]
    ema20, ema50 = ind.ema(closes, 20), ind.ema(closes, 50)
    if not closes or ema20 is None or ema50 is None:
        return None
    close = closes[-1]
    if close > ema20 > ema50:
        return "BULLISH"
    if close < ema20 < ema50:
        return "BEARISH"
    return "NEUTRAL"


def reconstruct_tv_context(symbol: str, windows: PointInTimeWindows) -> TradingViewContext:
    """Approximate current TV bias rules from closed Bybit candles only.

    This is deliberately NOT called a historical TradingView snapshot. The raw
    metadata makes the approximation explicit in every generated signal.
    """
    closes_1h = [Decimal(str(c["close"])) for c in windows.candles_1h]
    ema20 = ind.ema(closes_1h, 20)
    ema50 = ind.ema(closes_1h, 50)
    return TradingViewContext(
        symbol=symbol,
        trend_15m=_trend_15m(windows.candles_15m),
        trend_1h=_trend_1h(windows.candles_1h),
        trend_4h=_trend_4h(windows.candles_4h),
        ema20=ema20,
        ema50=ema50,
        raw={"history_kind": TvHistoryKind.RECONSTRUCTED.value},
        available=True,
    )


def legacy_as_is_tv_context(symbol: str) -> TradingViewContext:
    """Preserve the effective consequence of the known current parser bug."""
    return TradingViewContext(
        symbol=symbol,
        available=True,
        raw={
            "history_kind": TvHistoryKind.RECONSTRUCTED.value,
            "legacy_as_is_parser_mismatch_preserved": True,
        },
    )


def _legacy_snapshot(symbol: str, windows: PointInTimeWindows, cutoff: dt.datetime) -> dict:
    ob_latest = windows.orderbook[-1] if windows.orderbook else {}
    ob_prev = windows.orderbook[-2] if len(windows.orderbook) >= 2 else None
    deriv_latest = windows.derivatives[-1] if windows.derivatives else {}
    return fe.build_feature_snapshot(
        symbol,
        candles_15m=windows.candles_15m,
        trade_aggregates_1m=windows.trades_1m,
        orderbook_latest=ob_latest,
        orderbook_previous=ob_prev,
        orderbook_recent=windows.orderbook,
        derivatives_latest=deriv_latest,
        derivatives_history=windows.derivatives,
        liq_1m=windows.liq_1m,
        liq_5m=windows.liq_5m,
        liq_15m=windows.liq_15m,
        max_ages=LEGACY_HIST_MAX_AGES,
        now=cutoff,
    )


def build_legacy_chain(
    symbol: str,
    data: HistoricalInputs,
    cutoff: dt.datetime,
    *,
    fixed_tv: bool,
) -> dict[str, Any]:
    """Evaluate legacy Block 1-3 at one historical decision point."""
    w = windows_as_of(data, cutoff)
    snapshot = _legacy_snapshot(symbol, w, cutoff)
    tv = reconstruct_tv_context(symbol, w) if fixed_tv else legacy_as_is_tv_context(symbol)
    result = legacy_engine.evaluate_symbol(
        symbol,
        snapshot,
        tv,
        account_equity=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.25"),
        tick_size=Decimal("0.1"),
        qty_step=Decimal("0.001"),
        fee_rate=Decimal("0.00055"),
        slippage=Decimal("10"),
        max_leverage=Decimal("3"),
        min_risk_reward=Decimal("2.0"),
        signal_expiry_minutes=60,
        enable_liquidation_reversal=False,
        now=cutoff,
    )
    return {
        "symbol": symbol,
        "as_of": cutoff,
        "variant": Variant.LEGACY_FIXED_TV if fixed_tv else Variant.LEGACY_AS_IS,
        "tv_history_kind": TvHistoryKind.RECONSTRUCTED.value,
        "tv_context": tv,
        "feature_snapshot": snapshot,
        "result": result,
    }


def build_research_v2_chain(
    symbol: str,
    data: HistoricalInputs,
    cutoff: dt.datetime,
    *,
    btc_regime: Optional[str] = None,
    previous_state: Optional[dict] = None,
    portfolio_state: Optional[dict] = None,
) -> dict[str, Any]:
    """Run W1->W4 at one historical cutoff from closed/past-only windows."""
    w = windows_as_of(data, cutoff)
    built = rs.build_features_from_windows(
        w.candles_4h,
        w.candles_1h,
        w.candles_15m,
        w.trades_1m,
        w.liq_1m,
        w.liq_5m,
        w.liq_15m,
        w.orderbook,
        w.derivatives,
        now=cutoff,
        max_ages=RESEARCH_HIST_MAX_AGES,
    )
    regime = rc.classify_from_features(
        symbol,
        cutoff,
        built["tf"], built["volume"], built["orderbook"], built["futures"],
        built["liquidations"], built["data_quality"],
        btc_regime=btc_regime,
        previous_state=previous_state,
    )
    state = regime.pop("_state", None)
    setup_result = sd.detect_setups(
        symbol, cutoff, regime, built["tf"], built["volume"], built["orderbook"],
        built["futures"], built["liquidations"],
    )
    setups = setup_result["setups"]
    confirmations: dict[str, dict] = {}
    risk_decisions: dict[str, dict] = {}
    portfolio = portfolio_state or {
        "open_positions": [],
        "daily_realized_pnl_pct": Decimal("0"),
        "weekly_realized_pnl_pct": Decimal("0"),
        "btc_regime_flip_detected": False,
    }
    for setup_type, setup in setups.items():
        confirmation = ofc.confirm_setup(
            symbol, setup, regime, built["tf"], built["volume"], built["orderbook"],
            built["futures"], built["liquidations"],
            btc_regime=btc_regime,
            data_quality=built["data_quality"],
        )
        confirmations[setup_type] = confirmation
        risk_decisions[setup_type] = rm.evaluate_risk(
            symbol, cutoff, regime, setup, confirmation, built["orderbook"],
            built["data_quality"], portfolio, rm.DEFAULT_RISK_CONFIG,
        )
    return {
        "symbol": symbol,
        "as_of": cutoff,
        "variant": Variant.RESEARCH_V2,
        "regime": regime,
        "setups": setups,
        "confirmations": confirmations,
        "risk_decisions": risk_decisions,
        "data_quality": built["data_quality"],
        "features": built,
        "_state": state,
    }


def neutral_signals_from_research(
    chain: Mapping[str, Any], *, approved_only: bool = True
) -> list[NeutralSignal]:
    out: list[NeutralSignal] = []
    for setup_type, setup in chain["setups"].items():
        if setup.get("direction") not in {"LONG", "SHORT"}:
            continue
        risk = chain["risk_decisions"][setup_type]
        if approved_only and risk.get("decision") not in {"APPROVED", "APPROVED_REDUCED_SIZE"}:
            continue
        targets = tuple(Decimal(str(t)) for t in setup.get("targets", []))
        signal = NeutralSignal(
            variant=Variant.RESEARCH_V2,
            symbol=chain["symbol"],
            decision_time=chain["as_of"],
            setup_name=setup_type,
            direction=setup["direction"],
            entry_low=Decimal(str(setup["entry_zone"]["low"])),
            entry_high=Decimal(str(setup["entry_zone"]["high"])),
            stop_loss=Decimal(str(setup["stop_loss"])),
            targets=targets,
            regime=chain["regime"].get("primary_regime"),
            score=Decimal(str(setup.get("confidence", 0))),
            metadata={
                "invalidation": setup.get("invalidation"),
                "confidence": setup.get("confidence"),
                "price_only_mode": setup.get("price_only_mode"),
                "confirmation_status": chain["confirmations"][setup_type].get("status"),
                "native_risk_decision": risk.get("decision"),
            },
        )
        signal.validate()
        out.append(signal)
    return out


def neutral_signals_from_legacy(chain: Mapping[str, Any]) -> list[NeutralSignal]:
    out: list[NeutralSignal] = []
    result = chain["result"]
    for evaluated in result.get("signals", []):
        setup = evaluated.setup
        variant = chain["variant"]
        signal = NeutralSignal(
            variant=variant,
            symbol=chain["symbol"],
            decision_time=chain["as_of"],
            setup_name=setup.setup_name,
            direction=setup.direction,
            entry_low=Decimal(str(setup.entry_min)),
            entry_high=Decimal(str(setup.entry_max)),
            stop_loss=Decimal(str(setup.stop_loss)),
            targets=(Decimal(str(setup.take_profit_1)), Decimal(str(setup.take_profit_2))),
            regime=evaluated.regime,
            score=Decimal(str(evaluated.score)) if evaluated.score is not None else None,
            metadata={
                "invalidation": None,
                "tv_history_kind": chain["tv_history_kind"],
                "native_status": evaluated.status.value if hasattr(evaluated.status, "value") else str(evaluated.status),
            },
        )
        signal.validate()
        out.append(signal)
    return out
