"""Research-only V14 directional trend-continuation breakout candidate.

V14 is preregistered after V13 produced too few base setups. It replaces the
rare squeeze prerequisite with a fixed four-hour channel breakout, while
retaining the same point-in-time directional flow, OI and funding gates.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Optional

from tradingview_mcp.core.analysis import orderflow_confirmation_v2 as ofc
from tradingview_mcp.core.analysis import risk_manager_v2 as rm
from tradingview_mcp.core.backtest.comparison_breakout_v13 import evaluate_v13_gates
from tradingview_mcp.core.backtest.comparison_v1 import NeutralSignal, Variant

V14_NAME = "V14_DIRECTIONAL_TREND_CONTINUATION_BREAKOUT"
RESERVED_HOLDOUT_MARKER = "holdout_mrv2_120d"
CHANNEL_LOOKBACK_15M = 16
BREAKOUT_BUFFER_ATR = Decimal("0.05")
MIN_BODY_ATR = Decimal("0.20")
ENTRY_RETEST_ATR = Decimal("0.15")
STOP_ATR = Decimal("0.50")
TARGET_R_MULTIPLIERS = (Decimal("1.5"), Decimal("2.5"), Decimal("4.0"))
DIRECTIONAL_REGIMES = {"LONG": {"TREND_UP", "BREAKOUT_UP"}, "SHORT": {"TREND_DOWN", "BREAKOUT_DOWN"}}


def validate_v14_development_cache_root(cache_root: Path) -> Path:
    resolved = cache_root.resolve()
    if RESERVED_HOLDOUT_MARKER in str(resolved).lower():
        raise ValueError("V14 development runner refuses the reserved 120d holdout cache")
    return resolved


def _d(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _no_setup(reason: str, regime: str) -> dict[str, Any]:
    return {
        "setup_type": "trend_continuation_breakout",
        "direction": "NO_SETUP",
        "entry_zone": None,
        "invalidation": None,
        "stop_loss": None,
        "targets": [],
        "confidence": 0.0,
        "reasons": [reason],
        "counterarguments": [],
        "regime_compatible": False,
        "price_only_mode": True,
        "orderflow_confirmed": False,
    }


def detect_v14_setup(chain: Mapping[str, Any]) -> dict[str, Any]:
    regime = str((chain.get("regime") or {}).get("primary_regime") or "UNKNOWN")
    if regime in DIRECTIONAL_REGIMES["LONG"]:
        expected = "LONG"
    elif regime in DIRECTIONAL_REGIMES["SHORT"]:
        expected = "SHORT"
    else:
        return _no_setup(f"regime {regime} is not directional", regime)

    windows = chain.get("_windows")
    candles = list(getattr(windows, "candles_15m", []) or []) if windows is not None else []
    if len(candles) < CHANNEL_LOOKBACK_15M + 1:
        return _no_setup("insufficient 15m history for fixed channel", regime)

    atr = _d((((chain.get("features") or {}).get("tf") or {}).get("1h") or {}).get("atr"))
    if atr is None or atr <= 0:
        return _no_setup("1h ATR unavailable", regime)

    history = candles[-(CHANNEL_LOOKBACK_15M + 1):-1]
    trigger = candles[-1]
    highs = [_d(c.get("high")) for c in history]
    lows = [_d(c.get("low")) for c in history]
    o, h, l, c = [_d(trigger.get(k)) for k in ("open", "high", "low", "close")]
    if any(v is None for v in highs + lows) or None in (o, h, l, c):
        return _no_setup("invalid candle history", regime)

    resistance = max(highs)
    support = min(lows)
    body = abs(c - o)
    if body < atr * MIN_BODY_ATR:
        return _no_setup("trigger body too small", regime)

    if expected == "LONG" and c > resistance + atr * BREAKOUT_BUFFER_ATR:
        direction, level = "LONG", resistance
        zone_low = level - atr * ENTRY_RETEST_ATR
        zone_high = level + atr * Decimal("0.05")
        stop = min(level - atr * STOP_ATR, l - atr * Decimal("0.05"))
        worst = zone_high
        risk = worst - stop
        targets = [worst + risk * multiple for multiple in TARGET_R_MULTIPLIERS]
    elif expected == "SHORT" and c < support - atr * BREAKOUT_BUFFER_ATR:
        direction, level = "SHORT", support
        zone_low = level - atr * Decimal("0.05")
        zone_high = level + atr * ENTRY_RETEST_ATR
        stop = max(level + atr * STOP_ATR, h + atr * Decimal("0.05"))
        worst = zone_low
        risk = stop - worst
        targets = [worst - risk * multiple for multiple in TARGET_R_MULTIPLIERS]
    else:
        return _no_setup("channel did not break in regime direction", regime)

    if risk <= 0:
        return _no_setup("non-positive risk geometry", regime)
    return {
        "setup_type": "trend_continuation_breakout",
        "direction": direction,
        "entry_zone": {"low": zone_low, "high": zone_high},
        "invalidation": stop,
        "stop_loss": stop,
        "targets": targets,
        "confidence": 0.55,
        "reasons": ["fixed four-hour channel breakout", "breakout agrees with point-in-time directional regime"],
        "counterarguments": [],
        "regime_compatible": True,
        "price_only_mode": True,
        "orderflow_confirmed": False,
        "reference_level": level,
        "detection_version": V14_NAME,
    }


def evaluate_breakout_v14(chain: Mapping[str, Any], *, btc_regime: str | None = None) -> dict[str, Any]:
    setup = detect_v14_setup(chain)
    direction = str(setup.get("direction") or "NO_SETUP")
    gates = evaluate_v13_gates(chain, direction)
    gates["candidate"] = V14_NAME
    if direction not in {"LONG", "SHORT"} or not gates["eligible"]:
        return {"candidate": V14_NAME, "setup": setup, "confirmation": None, "risk": None, "gates": gates, "signal": None}

    features = chain.get("features") or {}
    regime = chain.get("regime") or {}
    confirmation = ofc.confirm_setup(
        str(chain.get("symbol")), setup, regime, features.get("tf") or {},
        features.get("volume") or {}, features.get("orderbook") or {},
        features.get("futures") or {}, features.get("liquidations") or {},
        btc_regime=btc_regime, data_quality=chain.get("data_quality") or {},
    )
    portfolio = {
        "open_positions": [], "daily_realized_pnl_pct": Decimal("0"),
        "weekly_realized_pnl_pct": Decimal("0"), "btc_regime_flip_detected": False,
    }
    risk = rm.evaluate_risk(
        str(chain.get("symbol")), chain.get("as_of"), regime, setup, confirmation,
        features.get("orderbook") or {}, chain.get("data_quality") or {},
        portfolio, rm.DEFAULT_RISK_CONFIG,
    )
    signal = None
    if risk.get("decision") in {"APPROVED", "APPROVED_REDUCED_SIZE"}:
        signal = NeutralSignal(
            variant=Variant.RESEARCH_V2, symbol=str(chain.get("symbol")),
            decision_time=chain.get("as_of"), setup_name="trend_continuation_breakout",
            direction=direction, entry_low=_d(setup["entry_zone"]["low"]),
            entry_high=_d(setup["entry_zone"]["high"]), stop_loss=_d(setup["stop_loss"]),
            targets=tuple(_d(target) for target in setup["targets"]),
            regime=regime.get("primary_regime"), score=Decimal(str(setup["confidence"])),
            metadata={"confirmation_status": confirmation.get("status"), "native_risk_decision": risk.get("decision"), "detection_version": V14_NAME},
        )
        signal.validate()
    return {"candidate": V14_NAME, "setup": setup, "confirmation": confirmation, "risk": risk, "gates": gates, "signal": signal}
