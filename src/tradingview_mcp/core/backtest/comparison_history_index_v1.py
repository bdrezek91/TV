"""Indexed point-in-time windows for long BACKTEST_COMPARISON_V1 replays.

The original research helpers intentionally favoured clarity over speed and
filter each source linearly at every cutoff. That is fine for short local smoke
runs but wasteful for 30/90-day x 15-symbol replay. This module pre-sorts each
source by the instant it became knowable and uses bisect to select trailing
windows in O(log n) per source/cutoff.
"""
from __future__ import annotations

import bisect
import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional, Sequence

from tradingview_mcp.core.analysis import orderflow_confirmation_v2 as ofc
from tradingview_mcp.core.analysis import regime_classifier_v2 as rc
from tradingview_mcp.core.analysis import risk_manager_v2 as rm
from tradingview_mcp.core.analysis import setup_detector_v2 as sd
from tradingview_mcp.core.backtest.comparison_history_v1 import (
    HistoricalInputs,
    PointInTimeWindows,
    RESEARCH_HIST_MAX_AGES,
)
from tradingview_mcp.core.backtest.comparison_v1 import Variant
from tradingview_mcp.core.services import regime_service_v2 as rs


@dataclass(frozen=True)
class _IndexedSeries:
    rows: tuple[dict, ...]
    available_at: tuple[dt.datetime, ...]

    @classmethod
    def interval(cls, rows: Sequence[dict], key: str, duration: dt.timedelta):
        ordered = sorted((dict(r) for r in rows), key=lambda r: r[key])
        return cls(tuple(ordered), tuple(r[key] + duration for r in ordered))

    @classmethod
    def point(cls, rows: Sequence[dict], key: str):
        ordered = sorted((dict(r) for r in rows), key=lambda r: r[key])
        return cls(tuple(ordered), tuple(r[key] for r in ordered))

    def trailing(self, cutoff: dt.datetime, limit: Optional[int] = None) -> list[dict]:
        # available_at <= cutoff is knowable; bisect_right preserves equality.
        end = bisect.bisect_right(self.available_at, cutoff)
        start = max(0, end - limit) if limit is not None else 0
        return [dict(r) for r in self.rows[start:end]]


class HistoricalWindowIndex:
    def __init__(self, data: HistoricalInputs) -> None:
        self.c4 = _IndexedSeries.interval(data.candles_4h, "open_time", dt.timedelta(hours=4))
        self.c1 = _IndexedSeries.interval(data.candles_1h, "open_time", dt.timedelta(hours=1))
        self.c15 = _IndexedSeries.interval(data.candles_15m, "open_time", dt.timedelta(minutes=15))
        self.c1m = _IndexedSeries.interval(data.candles_1m, "open_time", dt.timedelta(minutes=1))
        self.trades = _IndexedSeries.interval(data.trades_1m, "bucket_start", dt.timedelta(minutes=1))
        self.liq = _IndexedSeries.interval(data.liquidations_1m, "bucket_start", dt.timedelta(minutes=1))
        self.ob = _IndexedSeries.point(data.orderbook, "source_timestamp")
        self.deriv = _IndexedSeries.point(data.derivatives, "source_timestamp")

    def trailing_trades(self, cutoff: dt.datetime, minutes: int) -> list[dict]:
        """Return an isolated longer trade window without changing W1/W3 inputs.

        The standard Research V2 feature build intentionally consumes only the
        last 30 one-minute trade buckets. Research detectors such as CVD
        divergence may require a longer thesis window; exposing it separately
        prevents those experiments from silently changing the base feature
        engine or W3 confirmation contract.
        """
        if minutes < 1:
            raise ValueError("minutes must be >= 1")
        return self.trades.trailing(cutoff, minutes)

    def windows(self, cutoff: dt.datetime, *, include_execution_1m: bool = False) -> PointInTimeWindows:
        liq_series = self.liq.trailing(cutoff, 15)
        return PointInTimeWindows(
            candles_4h=self.c4.trailing(cutoff, 120),
            candles_1h=self.c1.trailing(cutoff, 120),
            candles_15m=self.c15.trailing(cutoff, 120),
            candles_1m=self.c1m.trailing(cutoff, 5000) if include_execution_1m else [],
            trades_1m=self.trades.trailing(cutoff, 30),
            liq_1m=liq_series[-1:],
            liq_5m=liq_series[-5:],
            liq_15m=liq_series[-15:],
            orderbook=self.ob.trailing(cutoff, 20),
            derivatives=self.deriv.trailing(cutoff, 20),
        )


def build_research_v2_chain_indexed(
    symbol: str,
    index: HistoricalWindowIndex,
    cutoff: dt.datetime,
    *,
    btc_regime: Optional[str] = None,
    previous_state: Optional[dict] = None,
    portfolio_state: Optional[dict] = None,
) -> dict[str, Any]:
    """Same W1→W4 production logic as comparison_history_v1, faster windows."""
    w = index.windows(cutoff, include_execution_1m=False)
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
        "_windows": w,
        "_cvd_trades_1m": index.trailing_trades(cutoff, 360),
    }
