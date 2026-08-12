"""Diagnostics shared by BACKTEST_COMPARISON_V1 runners and reports.

These helpers do not generate trades. They answer two audit questions:
1. are two engines agreeing at the same symbol/decision timestamp?
2. is the requested decision window actually covered by every required source?
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Mapping, Sequence

from tradingview_mcp.core.backtest.comparison_v1 import NeutralSignal


REQUIRED_EXACT_SOURCES = (
    "4h",
    "1h",
    "15m",
    "candles_1m",
    "trades_1m",
    "liquidations_1m",
    "orderbook",
    "derivatives",
)


@dataclass(frozen=True)
class CoverageDecision:
    complete: bool
    overlap_start: dt.datetime | None
    overlap_end: dt.datetime | None
    requested_start: dt.datetime
    requested_end: dt.datetime
    missing_sources: tuple[str, ...]
    outside_overlap: bool
    reasons: tuple[str, ...]


def signal_set_category(
    legacy_signals: Sequence[NeutralSignal],
    research_signals: Sequence[NeutralSignal],
) -> str:
    """Compare all actionable directions at one symbol/cutoff.

    A set containing both LONG and SHORT is intentionally classified as a
    conflict rather than "agreement"; two engines both being internally
    contradictory is not useful confirmation.
    """
    legacy_dirs = {s.direction for s in legacy_signals if s.direction in {"LONG", "SHORT"}}
    research_dirs = {s.direction for s in research_signals if s.direction in {"LONG", "SHORT"}}
    if not legacy_dirs and not research_dirs:
        return "BOTH_NO_TRADE"
    if legacy_dirs and not research_dirs:
        return "ONLY_LEGACY"
    if research_dirs and not legacy_dirs:
        return "ONLY_RESEARCH_V2"
    if len(legacy_dirs) == 1 and legacy_dirs == research_dirs:
        return "BOTH_SIGNAL_SAME_DIRECTION"
    return "DIRECTION_CONFLICT"


def paired_setup_families(
    legacy_signals: Sequence[NeutralSignal],
    research_signals: Sequence[NeutralSignal],
) -> dict[str, str]:
    """Matched-family comparison without pretending every setup has a twin."""
    legacy_by = {s.setup_name: s for s in legacy_signals}
    research_by = {s.setup_name: s for s in research_signals}
    out: dict[str, str] = {}

    legacy_trend = legacy_by.get("trend_pullback")
    research_trend = research_by.get("trend_pullback")
    if legacy_trend or research_trend:
        out["trend_pullback"] = _pair_one(legacy_trend, research_trend)

    legacy_breakout = legacy_by.get("breakout_retest")
    research_breakout = research_by.get("breakout")
    if legacy_breakout or research_breakout:
        out["breakout_family"] = _pair_one(legacy_breakout, research_breakout)
    return out


def _pair_one(legacy: NeutralSignal | None, research: NeutralSignal | None) -> str:
    if legacy is None and research is None:
        return "BOTH_NO_TRADE"
    if legacy is not None and research is None:
        return "ONLY_LEGACY"
    if research is not None and legacy is None:
        return "ONLY_RESEARCH_V2"
    assert legacy is not None and research is not None
    return "BOTH_SIGNAL_SAME_DIRECTION" if legacy.direction == research.direction else "DIRECTION_CONFLICT"


def validate_requested_overlap(
    coverage: Mapping[str, Mapping[str, object]],
    requested_start: dt.datetime,
    requested_end: dt.datetime,
    *,
    required_sources: Sequence[str] = REQUIRED_EXACT_SOURCES,
) -> CoverageDecision:
    """Strict common overlap across every required source for one symbol.

    ``coverage`` is the per-symbol structure written by the CLI coverage
    command. A missing source is a hard incomplete result, never silently
    dropped from the intersection.
    """
    if requested_start.tzinfo is None or requested_end.tzinfo is None:
        raise ValueError("requested_start/requested_end must be timezone-aware")
    if requested_end < requested_start:
        raise ValueError("requested_end < requested_start")

    starts: list[dt.datetime] = []
    ends: list[dt.datetime] = []
    missing: list[str] = []
    for name in required_sources:
        span = coverage.get(name) or {}
        earliest = span.get("earliest")
        latest = span.get("latest")
        if not earliest or not latest:
            missing.append(name)
            continue
        starts.append(dt.datetime.fromisoformat(str(earliest)))
        ends.append(dt.datetime.fromisoformat(str(latest)))

    reasons: list[str] = []
    if missing:
        reasons.append("missing required sources: " + ", ".join(missing))
        return CoverageDecision(
            complete=False,
            overlap_start=None,
            overlap_end=None,
            requested_start=requested_start,
            requested_end=requested_end,
            missing_sources=tuple(missing),
            outside_overlap=True,
            reasons=tuple(reasons),
        )

    overlap_start, overlap_end = max(starts), min(ends)
    if overlap_end < overlap_start:
        reasons.append("required sources have no common time intersection")
        return CoverageDecision(False, None, None, requested_start, requested_end, (), True, tuple(reasons))

    outside = requested_start < overlap_start or requested_end > overlap_end
    if outside:
        reasons.append(
            f"requested window {requested_start.isoformat()}..{requested_end.isoformat()} "
            f"exceeds exact overlap {overlap_start.isoformat()}..{overlap_end.isoformat()}"
        )
    return CoverageDecision(
        complete=not outside,
        overlap_start=overlap_start,
        overlap_end=overlap_end,
        requested_start=requested_start,
        requested_end=requested_end,
        missing_sources=(),
        outside_overlap=outside,
        reasons=tuple(reasons),
    )
