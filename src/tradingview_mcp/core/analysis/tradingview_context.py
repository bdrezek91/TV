"""Bridges the existing TradingView MCP analysis functions into the new
Bybit-driven Strategy Engine.

Per the plan's absolute rule, this calls `core.services.screener_service`
functions **directly** (in-process function calls) — never HTTP requests
against this project's own MCP server. The real functions
(`analyze_coin`, `run_multi_timeframe_analysis`) hit TradingView's public
endpoints over the network, so `fetch_tradingview_context` takes an
injectable `analyzer` callable (defaulting to the real one) purely so
tests can supply a fixture-based fake and stay hermetic — mirroring the
same pattern Block 1 used for the Bybit WebSocket transport.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional


@dataclass(frozen=True)
class TradingViewContext:
    """Normalised view of what TradingView MCP's existing tools report for
    one symbol, used as the higher-timeframe / classic-TA half of setup
    evaluation (Bybit supplies the order-flow half)."""

    symbol: str
    trend_15m: Optional[str] = None       # "BULLISH" / "BEARISH" / "NEUTRAL"
    trend_1h: Optional[str] = None
    trend_4h: Optional[str] = None
    ema20: Optional[Decimal] = None
    ema50: Optional[Decimal] = None
    rsi: Optional[Decimal] = None
    macd_signal: Optional[str] = None     # "BULLISH" / "BEARISH" / "NEUTRAL"
    bollinger_position: Optional[str] = None  # "UPPER" / "MIDDLE" / "LOWER"
    volume_confirmation: Optional[bool] = None
    technical_levels: Dict[str, Decimal] = field(default_factory=dict)
    candidates: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)
    available: bool = True
    error: Optional[str] = None


def _default_analyzer(symbol: str, exchange: str) -> Dict[str, Any]:
    from tradingview_mcp.core.services.screener_service import (
        analyze_coin,
        run_multi_timeframe_analysis,
    )

    mtf = run_multi_timeframe_analysis(symbol, exchange)
    single = analyze_coin(symbol, exchange, "1h")
    return {"mtf": mtf, "single": single}


def _trend_label(value: Any) -> Optional[str]:
    if value is None:
        return None
    v = str(value).upper()
    if "BULL" in v or v in {"UP", "LONG", "BUY"}:
        return "BULLISH"
    if "BEAR" in v or v in {"DOWN", "SHORT", "SELL"}:
        return "BEARISH"
    return "NEUTRAL"


def parse_tradingview_response(symbol: str, raw: Dict[str, Any]) -> TradingViewContext:
    """Best-effort, defensive parsing: the existing services return
    loosely-typed dicts (tool-facing, not schema-pinned), so every lookup
    here is optional and missing/renamed fields degrade to `None` rather
    than raising — a parsing failure must never crash setup evaluation."""
    mtf = raw.get("mtf") or {}
    single = raw.get("single") or {}

    timeframes = mtf.get("timeframes") or mtf.get("timeframe_breakdown") or {}

    def tf_trend(key_candidates: List[str]) -> Optional[str]:
        for key in key_candidates:
            tf = timeframes.get(key)
            if isinstance(tf, dict):
                return _trend_label(tf.get("trend") or tf.get("recommendation") or tf.get("signal"))
        return None

    indicators = single.get("indicators") or single.get("technical_indicators") or {}
    levels = single.get("levels") or single.get("technical_levels") or {}
    technical_levels = {}
    for k, v in levels.items():
        try:
            technical_levels[k] = Decimal(str(v))
        except Exception:  # noqa: BLE001
            continue

    def dec_or_none(v: Any) -> Optional[Decimal]:
        try:
            return Decimal(str(v)) if v is not None else None
        except Exception:  # noqa: BLE001
            return None

    return TradingViewContext(
        symbol=symbol,
        trend_15m=tf_trend(["15m", "15"]),
        trend_1h=tf_trend(["1h", "60"]),
        trend_4h=tf_trend(["4h", "240"]),
        ema20=dec_or_none(indicators.get("EMA20") or indicators.get("ema20")),
        ema50=dec_or_none(indicators.get("EMA50") or indicators.get("ema50")),
        rsi=dec_or_none(indicators.get("RSI") or indicators.get("rsi")),
        macd_signal=_trend_label(indicators.get("MACD_signal") or indicators.get("macd")),
        bollinger_position=indicators.get("bollinger_position"),
        volume_confirmation=indicators.get("volume_confirmation"),
        technical_levels=technical_levels,
        candidates=list(single.get("candidates", []) or []),
        raw=raw,
        available=True,
    )


async def fetch_tradingview_context(
    symbol: str,
    exchange: str = "BYBIT",
    *,
    analyzer: Callable[[str, str], Dict[str, Any]] = _default_analyzer,
) -> TradingViewContext:
    """Runs the (synchronous, network-calling) analyzer off the event-loop
    thread and normalises its output. Never raises — a TradingView-side
    failure degrades to `available=False` so the Strategy Engine can still
    fall back to Bybit-only evaluation (with a lowered score, per the
    plan's conflict-handling rule) instead of crashing the cycle."""
    try:
        raw = await asyncio.to_thread(analyzer, symbol, exchange)
        return parse_tradingview_response(symbol, raw)
    except Exception as exc:  # noqa: BLE001
        return TradingViewContext(symbol=symbol, available=False, error=repr(exc))


@dataclass(frozen=True)
class ConflictResolution:
    has_conflict: bool
    score_penalty: int
    notes: List[str]
    reject: bool = False


def resolve_context_conflict(
    tv: TradingViewContext, bybit_direction_bias: Optional[str]
) -> ConflictResolution:
    """Compares TradingView's higher-timeframe trend read against Bybit's
    real-time order-flow bias (e.g. derived from CVD/delta sign) and
    decides how to handle disagreement.

    Per the plan: never let an LLM arbitrate the conflict. This is a fixed,
    deterministic rule: outright opposite reads lower the score and log the
    conflict; TradingView being unavailable is not treated as a conflict
    (Bybit-only evaluation proceeds, unpenalised beyond whatever the
    Bybit-side score components already reflect).
    """
    if not tv.available or bybit_direction_bias is None or tv.trend_1h is None:
        return ConflictResolution(has_conflict=False, score_penalty=0, notes=[])

    tv_bias = tv.trend_1h
    if tv_bias == "NEUTRAL" or bybit_direction_bias == "NEUTRAL":
        return ConflictResolution(has_conflict=False, score_penalty=0, notes=[])

    if tv_bias != bybit_direction_bias:
        return ConflictResolution(
            has_conflict=True,
            score_penalty=20,
            notes=[
                f"TradingView 1h trend ({tv_bias}) conflicts with Bybit order-flow bias "
                f"({bybit_direction_bias}); score penalised, setup not auto-rejected."
            ],
        )
    return ConflictResolution(has_conflict=False, score_penalty=0, notes=[])
