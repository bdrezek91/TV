"""Research-only Failed Breakout / Fakeout Reversal candidate.

The detector uses only price information available at the decision cutoff:
- a prior 20h reference range built from closed 1h candles excluding the
  most recent closed hour;
- the last four closed 15m candles to detect a sweep through the prior range
  edge followed by a close back inside the range.

No future candle, fill outcome, order-book history, or liquidation history is
used to create the setup.  W3/W4 are then applied unchanged.  This module does
not alter production Research V2 setup detection.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping, Optional

from tradingview_mcp.core.analysis import orderflow_confirmation_v2 as ofc
from tradingview_mcp.core.analysis import risk_manager_v2 as rm
from tradingview_mcp.core.backtest.comparison_v1 import NeutralSignal, Variant

REFERENCE_HOURS = 20
SWEEP_LOOKBACK_15M = 4
SWEEP_MIN_ATR = Decimal("0.10")
MAX_RECLAIM_DISTANCE_ATR = Decimal("0.75")
ENTRY_INSIDE_ATR = Decimal("0.15")
ENTRY_OUTSIDE_ATR = Decimal("0.05")
STOP_LEVEL_ATR = Decimal("0.50")
STOP_SWEEP_ATR = Decimal("0.10")
TARGET_R_MULTIPLIERS = (Decimal("1.5"), Decimal("2.5"), Decimal("4.0"))
ALLOWED_REGIMES = {"RANGE", "BREAKOUT_UP", "BREAKOUT_DOWN", "SQUEEZE", "NO_EDGE"}


def _d(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _no_setup(reason: str, *, regime: str | None = None) -> dict[str, Any]:
    return {
        "setup_type": "failed_breakout",
        "direction": "NO_SETUP",
        "entry_zone": None,
        "invalidation": None,
        "stop_loss": None,
        "targets": [],
        "confidence": 0.0,
        "reasons": [reason],
        "counterarguments": [],
        "regime_compatible": regime in ALLOWED_REGIMES if regime is not None else False,
        "price_only_mode": True,
        "orderflow_confirmed": False,
    }


def detect_failed_breakout_setup(chain: Mapping[str, Any]) -> dict[str, Any]:
    """Detect a failed range-edge breakout from decision-time closed candles."""
    regime = str((chain.get("regime") or {}).get("primary_regime") or "UNKNOWN")
    if regime not in ALLOWED_REGIMES:
        return _no_setup(f"regime {regime} outside predeclared fakeout scope", regime=regime)

    windows = chain.get("_windows")
    if windows is None:
        return _no_setup("historical windows unavailable", regime=regime)
    candles_1h = list(getattr(windows, "candles_1h", []) or [])
    candles_15m = list(getattr(windows, "candles_15m", []) or [])
    if len(candles_1h) < REFERENCE_HOURS + 1 or len(candles_15m) < SWEEP_LOOKBACK_15M:
        return _no_setup("insufficient closed 1h/15m history for fakeout reference", regime=regime)

    tf_1h = (((chain.get("features") or {}).get("tf") or {}).get("1h") or {})
    atr = _d(tf_1h.get("atr"))
    if atr is None or atr <= 0:
        return _no_setup("1h ATR unavailable", regime=regime)

    # Exclude the most recent closed 1h candle from the reference range so a
    # sweep occurring during that hour cannot move the goalpost it is tested
    # against. All reference candles were closed before the decision cutoff.
    reference = candles_1h[-(REFERENCE_HOURS + 1):-1]
    resistance = max(_d(row.get("high")) for row in reference)
    support = min(_d(row.get("low")) for row in reference)
    if resistance is None or support is None or support >= resistance:
        return _no_setup("invalid prior 20h reference range", regime=regime)

    candidates: list[dict[str, Any]] = []
    for candle in candles_15m[-SWEEP_LOOKBACK_15M:]:
        high = _d(candle.get("high"))
        low = _d(candle.get("low"))
        close = _d(candle.get("close"))
        if None in (high, low, close):
            continue
        upper = (
            high >= resistance + atr * SWEEP_MIN_ATR
            and close < resistance
            and close >= resistance - atr * MAX_RECLAIM_DISTANCE_ATR
        )
        lower = (
            low <= support - atr * SWEEP_MIN_ATR
            and close > support
            and close <= support + atr * MAX_RECLAIM_DISTANCE_ATR
        )
        if upper and lower:
            continue  # ambiguous two-sided expansion; do not force a direction
        if upper:
            candidates.append({"direction": "SHORT", "candle": candle, "extreme": high, "close": close})
        elif lower:
            candidates.append({"direction": "LONG", "candle": candle, "extreme": low, "close": close})

    if not candidates:
        return _no_setup("no 15m sweep-and-reclaim of the prior 20h range edge", regime=regime)

    # If more than one sweep exists in the lookback, use the latest knowable
    # one; timestamp ordering is historical input, not future outcome data.
    latest = max(candidates, key=lambda row: row["candle"].get("open_time"))
    direction = latest["direction"]
    extreme = latest["extreme"]
    close = latest["close"]

    if direction == "SHORT":
        zone_low = resistance - atr * ENTRY_INSIDE_ATR
        zone_high = resistance + atr * ENTRY_OUTSIDE_ATR
        stop = max(resistance + atr * STOP_LEVEL_ATR, extreme + atr * STOP_SWEEP_ATR)
        worst_entry = zone_low
        risk = stop - worst_entry
        targets = [worst_entry - risk * mult for mult in TARGET_R_MULTIPLIERS]
        level = resistance
        sweep_detail = f"upper sweep {extreme} above prior resistance {resistance}, close reclaimed inside at {close}"
    else:
        zone_low = support - atr * ENTRY_OUTSIDE_ATR
        zone_high = support + atr * ENTRY_INSIDE_ATR
        stop = min(support - atr * STOP_LEVEL_ATR, extreme - atr * STOP_SWEEP_ATR)
        worst_entry = zone_high
        risk = worst_entry - stop
        targets = [worst_entry + risk * mult for mult in TARGET_R_MULTIPLIERS]
        level = support
        sweep_detail = f"lower sweep {extreme} below prior support {support}, close reclaimed inside at {close}"

    if risk <= 0:
        return _no_setup("fakeout geometry produced non-positive risk", regime=regime)

    return {
        "setup_type": "failed_breakout",
        "direction": direction,
        "entry_zone": {"low": zone_low, "high": zone_high},
        "invalidation": stop,
        "stop_loss": stop,
        "targets": targets,
        "confidence": 0.55,
        "reasons": [
            f"failed breakout detected against a prior {REFERENCE_HOURS}h range",
            sweep_detail,
            "entry requires a retest near the reclaimed range edge; targets use the same 1.5R/2.5R/4R contract as ALIGNED_TARGETS",
        ],
        "counterarguments": [],
        "regime_compatible": True,
        "price_only_mode": True,
        "orderflow_confirmed": False,
        "reference_level": level,
        "sweep_extreme": extreme,
        "reference_support": support,
        "reference_resistance": resistance,
        "detection_version": "FAILED_BREAKOUT_PRICE_RECLAIM_V1",
    }


def evaluate_failed_breakout(chain: Mapping[str, Any], *, btc_regime: str | None = None) -> dict[str, Any]:
    """Run unchanged W3/W4 on the research-only fakeout setup."""
    setup = detect_failed_breakout_setup(chain)
    features = chain.get("features") or {}
    regime = chain.get("regime") or {}
    if setup.get("direction") not in {"LONG", "SHORT"}:
        return {"setup": setup, "confirmation": None, "risk": None, "signal": None}

    confirmation = ofc.confirm_setup(
        str(chain.get("symbol")),
        setup,
        regime,
        features.get("tf") or {},
        features.get("volume") or {},
        features.get("orderbook") or {},
        features.get("futures") or {},
        features.get("liquidations") or {},
        btc_regime=btc_regime,
        data_quality=chain.get("data_quality") or {},
    )
    portfolio = {
        "open_positions": [],
        "daily_realized_pnl_pct": Decimal("0"),
        "weekly_realized_pnl_pct": Decimal("0"),
        "btc_regime_flip_detected": False,
    }
    risk = rm.evaluate_risk(
        str(chain.get("symbol")),
        chain.get("as_of"),
        regime,
        setup,
        confirmation,
        features.get("orderbook") or {},
        chain.get("data_quality") or {},
        portfolio,
        rm.DEFAULT_RISK_CONFIG,
    )
    signal = None
    if risk.get("decision") in {"APPROVED", "APPROVED_REDUCED_SIZE"}:
        signal = NeutralSignal(
            variant=Variant.RESEARCH_V2,
            symbol=str(chain.get("symbol")),
            decision_time=chain.get("as_of"),
            setup_name="failed_breakout",
            direction=setup["direction"],
            entry_low=_d(setup["entry_zone"]["low"]),
            entry_high=_d(setup["entry_zone"]["high"]),
            stop_loss=_d(setup["stop_loss"]),
            targets=tuple(_d(t) for t in setup["targets"]),
            regime=regime.get("primary_regime"),
            score=Decimal(str(setup["confidence"])),
            metadata={
                "invalidation": setup["invalidation"],
                "confidence": setup["confidence"],
                "confirmation_status": confirmation.get("status"),
                "native_risk_decision": risk.get("decision"),
                "detection_version": setup["detection_version"],
                "reference_level": setup["reference_level"],
            },
        )
        signal.validate()
    return {"setup": setup, "confirmation": confirmation, "risk": risk, "signal": signal}
