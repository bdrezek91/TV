"""Backtest Comparison V1 — research-only helpers.

This module deliberately does NOT mutate live/paper state.  It provides the
neutral comparison primitives used to evaluate the legacy TradingView+Bybit
pipeline against Research V2 on identical decision timestamps and execution
assumptions.

Key invariants:
- no production DB writes;
- no exchange order endpoints;
- explicit LEGACY_AS_IS vs LEGACY_FIXED_TV distinction;
- historical TradingView is labelled TV_RECONSTRUCTED, never claimed to be a
  point-in-time TradingView snapshot;
- Europe/Warsaw decision clock is DST-safe;
- bootstrap/statistical helpers are deterministic.
"""
from __future__ import annotations

import datetime as dt
import random
import statistics
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from tradingview_mcp.core.analysis.tradingview_context import TradingViewContext

WARSAW = ZoneInfo("Europe/Warsaw")
DEFAULT_SCAN_HOURS = (7, 9, 11, 13, 15, 17, 19, 21)


class Variant(str, Enum):
    LEGACY_AS_IS = "LEGACY_AS_IS"
    LEGACY_FIXED_TV = "LEGACY_FIXED_TV"
    RESEARCH_V2 = "RESEARCH_V2"
    RESEARCH_V2_TV = "RESEARCH_V2_TV"


class TvHistoryKind(str, Enum):
    LIVE_CURRENT = "TRUE_LIVE_CURRENT"
    RECONSTRUCTED = "TV_RECONSTRUCTED"
    APPROXIMATE_RECONSTRUCTED = "APPROXIMATE_TV_RECONSTRUCTION"


@dataclass(frozen=True)
class NeutralSignal:
    variant: Variant
    symbol: str
    decision_time: dt.datetime
    setup_name: str
    direction: str
    entry_low: Decimal
    entry_high: Decimal
    stop_loss: Decimal
    targets: tuple[Decimal, ...]
    regime: Optional[str] = None
    score: Optional[Decimal] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if self.direction not in {"LONG", "SHORT"}:
            raise ValueError(f"unsupported direction: {self.direction}")
        if self.entry_low > self.entry_high:
            raise ValueError("entry_low > entry_high")
        if not self.targets:
            raise ValueError("at least one target is required")
        if self.decision_time.tzinfo is None:
            raise ValueError("decision_time must be timezone-aware")


@dataclass(frozen=True)
class TradeObservation:
    variant: Variant
    symbol: str
    decision_time: dt.datetime
    setup_name: str
    direction: str
    filled: bool
    r_multiple: Optional[Decimal]
    pnl_net: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    slippage: Decimal = Decimal("0")
    funding: Decimal = Decimal("0")
    mae_r: Optional[Decimal] = None
    mfe_r: Optional[Decimal] = None


# ---------------------------------------------------------------------------
# Decision clock
# ---------------------------------------------------------------------------

def decision_times(
    start: dt.datetime,
    end: dt.datetime,
    *,
    hours: Sequence[int] = DEFAULT_SCAN_HOURS,
    timezone: ZoneInfo = WARSAW,
) -> list[dt.datetime]:
    """Return DST-safe decision timestamps in UTC for a local schedule.

    `start` and `end` must be timezone-aware. The interval is inclusive.
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start/end must be timezone-aware")
    if end < start:
        return []

    start_local = start.astimezone(timezone)
    end_local = end.astimezone(timezone)
    day = start_local.date()
    out: list[dt.datetime] = []
    while day <= end_local.date():
        for hour in sorted(set(int(h) for h in hours)):
            local = dt.datetime.combine(day, dt.time(hour=hour), tzinfo=timezone)
            utc = local.astimezone(dt.timezone.utc)
            if start <= utc <= end:
                out.append(utc)
        day += dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# Legacy TradingView parser audit / fixed adapter
# ---------------------------------------------------------------------------

def legacy_parser_mismatch(raw: Mapping[str, Any]) -> dict[str, bool]:
    """Detect the known legacy schema mismatch without changing production code.

    Current TradingView service responses expose MTF `bias` and top-level
    single-symbol sections (`rsi`, `macd`, `ema`, `bollinger_bands`, ...),
    while the legacy parser expects MTF trend/recommendation/signal and nested
    `single['indicators']` / `single['levels']`.
    """
    mtf = raw.get("mtf") or {}
    single = raw.get("single") or {}
    timeframes = mtf.get("timeframes") or mtf.get("timeframe_breakdown") or {}
    has_bias_only = any(
        isinstance(v, Mapping)
        and v.get("bias") is not None
        and not any(v.get(k) is not None for k in ("trend", "recommendation", "signal"))
        for v in timeframes.values()
    )
    has_top_level_sections = any(k in single for k in ("rsi", "macd", "ema", "bollinger_bands", "support_resistance"))
    lacks_legacy_nested = not isinstance(single.get("indicators"), Mapping)
    return {
        "mtf_bias_not_consumed_by_legacy_parser": has_bias_only,
        "single_has_current_top_level_schema": has_top_level_sections,
        "single_lacks_legacy_indicators_wrapper": lacks_legacy_nested,
    }


def _trend_label(value: Any) -> Optional[str]:
    if value is None:
        return None
    v = str(value).strip().upper()
    if "BULL" in v or v in {"UP", "LONG", "BUY"}:
        return "BULLISH"
    if "BEAR" in v or v in {"DOWN", "SHORT", "SELL"}:
        return "BEARISH"
    return "NEUTRAL"


def _dec(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _section_value(section: Any, *keys: str) -> Any:
    if not isinstance(section, Mapping):
        return None
    for key in keys:
        if section.get(key) is not None:
            return section.get(key)
    return None


def parse_current_tv_response_fixed(symbol: str, raw: Mapping[str, Any]) -> TradingViewContext:
    """Parse the *current* screener_service response shape for research.

    This function intentionally lives in the backtest namespace. It does not
    replace `analysis.tradingview_context.parse_tradingview_response`, so the
    AS-IS benchmark remains reproducible.
    """
    mtf = raw.get("mtf") or {}
    single = raw.get("single") or {}
    timeframes = mtf.get("timeframes") or mtf.get("timeframe_breakdown") or {}

    def tf_bias(*keys: str) -> Optional[str]:
        for key in keys:
            row = timeframes.get(key)
            if isinstance(row, Mapping):
                return _trend_label(row.get("bias") or row.get("trend") or row.get("recommendation") or row.get("signal"))
        return None

    ema = single.get("ema") or {}
    rsi = single.get("rsi") or {}
    macd = single.get("macd") or {}
    boll = single.get("bollinger_bands") or {}
    levels = single.get("support_resistance") or {}
    volume = single.get("volume_analysis") or single.get("volume") or {}

    technical_levels: dict[str, Decimal] = {}
    if isinstance(levels, Mapping):
        for k, v in levels.items():
            d = _dec(v)
            if d is not None:
                technical_levels[str(k)] = d

    macd_signal = _section_value(macd, "crossover", "signal")
    boll_position = _section_value(boll, "position")
    volume_signal = str(_section_value(volume, "signal") or "").upper()
    volume_confirmation = None if not volume_signal else volume_signal in {"ABOVE AVERAGE", "HIGH", "VERY HIGH"}

    return TradingViewContext(
        symbol=symbol,
        trend_15m=tf_bias("15m", "15"),
        trend_1h=tf_bias("1h", "60"),
        trend_4h=tf_bias("4h", "240"),
        ema20=_dec(_section_value(ema, "ema20")),
        ema50=_dec(_section_value(ema, "ema50")),
        rsi=_dec(_section_value(rsi, "value", "rsi")),
        macd_signal=_trend_label(macd_signal),
        bollinger_position=str(boll_position).upper() if boll_position is not None else None,
        volume_confirmation=volume_confirmation,
        technical_levels=technical_levels,
        candidates=list(single.get("candidates", []) or []),
        raw=dict(raw),
        available=not bool(single.get("error") or mtf.get("error")),
        error=str(single.get("error") or mtf.get("error")) if (single.get("error") or mtf.get("error")) else None,
    )


# ---------------------------------------------------------------------------
# Paired signal analysis
# ---------------------------------------------------------------------------

def paired_signal_category(
    legacy_direction: Optional[str], research_direction: Optional[str]
) -> str:
    ld = legacy_direction if legacy_direction in {"LONG", "SHORT"} else None
    rd = research_direction if research_direction in {"LONG", "SHORT"} else None
    if ld and rd:
        return "BOTH_SIGNAL_SAME_DIRECTION" if ld == rd else "DIRECTION_CONFLICT"
    if ld:
        return "ONLY_LEGACY"
    if rd:
        return "ONLY_RESEARCH_V2"
    return "BOTH_NO_TRADE"


# ---------------------------------------------------------------------------
# Statistics / sample labels
# ---------------------------------------------------------------------------

def sample_status(n: int) -> str:
    if n < 30:
        return "INSUFFICIENT_SAMPLE"
    if n < 100:
        return "PRELIMINARY"
    if n < 300:
        return "MODERATE_SAMPLE"
    return "STRONGER_SAMPLE"


def _quantile(values: Sequence[float], q: float) -> float:
    if not values:
        raise ValueError("empty values")
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def bootstrap_mean_ci(
    values: Iterable[Decimal | float | int],
    *,
    resamples: int = 1000,
    confidence: float = 0.95,
    seed: int = 20260810,
) -> Optional[dict[str, float]]:
    xs = [float(v) for v in values]
    if not xs:
        return None
    if resamples < 100:
        raise ValueError("resamples must be >= 100")
    rng = random.Random(seed)
    n = len(xs)
    means = [statistics.fmean(rng.choices(xs, k=n)) for _ in range(resamples)]
    alpha = (1.0 - confidence) / 2.0
    return {
        "mean": statistics.fmean(xs),
        "low": _quantile(means, alpha),
        "high": _quantile(means, 1.0 - alpha),
        "confidence": confidence,
        "resamples": resamples,
        "seed": seed,
    }


def summarize_observations(rows: Sequence[TradeObservation]) -> dict[str, Any]:
    filled = [r for r in rows if r.filled]
    rs = [r.r_multiple for r in filled if r.r_multiple is not None]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    gross_profit = sum(wins, Decimal("0"))
    gross_loss = -sum(losses, Decimal("0"))
    n = len(rs)
    expectancy = sum(rs, Decimal("0")) / Decimal(n) if n else None
    win_rate = Decimal(len(wins)) / Decimal(n) if n else None
    pf = gross_profit / gross_loss if gross_loss > 0 else None
    return {
        "signals": len(rows),
        "filled": len(filled),
        "trades_with_r": n,
        "sample_status": sample_status(n),
        "win_rate": str(win_rate) if win_rate is not None else None,
        "expectancy_r": str(expectancy) if expectancy is not None else None,
        "profit_factor": str(pf) if pf is not None else None,
        "pnl_net": str(sum((r.pnl_net for r in filled), Decimal("0"))),
        "fees": str(sum((r.fees for r in filled), Decimal("0"))),
        "slippage": str(sum((r.slippage for r in filled), Decimal("0"))),
        "funding": str(sum((r.funding for r in filled), Decimal("0"))),
        "expectancy_ci": bootstrap_mean_ci(rs) if rs else None,
    }
