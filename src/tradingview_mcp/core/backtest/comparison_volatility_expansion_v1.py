"""Research-only volatility expansion / squeeze breakout candidate."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping, Optional

from tradingview_mcp.core.analysis import orderflow_confirmation_v2 as ofc
from tradingview_mcp.core.analysis import risk_manager_v2 as rm
from tradingview_mcp.core.backtest.comparison_v1 import NeutralSignal, Variant

PRE_COMPRESSION_LOOKBACK_15M = 16
COMPRESSION_LOOKBACK_15M = 16
HISTORY_LOOKBACK_15M = PRE_COMPRESSION_LOOKBACK_15M + COMPRESSION_LOOKBACK_15M
RANGE_COMPRESSION_RATIO = Decimal("0.65")
BREAKOUT_BUFFER_ATR = Decimal("0.10")
MIN_BODY_ATR = Decimal("0.45")
ENTRY_RETEST_ATR = Decimal("0.20")
STOP_ATR = Decimal("0.45")
TARGET_R_MULTIPLIERS = (Decimal("1.5"), Decimal("2.5"), Decimal("4.0"))
ALLOWED_REGIMES = {"NO_EDGE", "RANGE", "SQUEEZE", "BREAKOUT_UP", "BREAKOUT_DOWN"}


def _d(v: Any) -> Optional[Decimal]:
    if v is None:
        return None
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _no_setup(
    reason: str,
    regime: str | None = None,
    allowed_regimes: Optional[set[str]] = None,
) -> dict[str, Any]:
    regime_scope = ALLOWED_REGIMES if allowed_regimes is None else allowed_regimes
    return {
        "setup_type": "volatility_expansion",
        "direction": "NO_SETUP",
        "entry_zone": None,
        "invalidation": None,
        "stop_loss": None,
        "targets": [],
        "confidence": 0.0,
        "reasons": [reason],
        "counterarguments": [],
        "regime_compatible": regime in regime_scope if regime else False,
        "price_only_mode": True,
        "orderflow_confirmed": False,
    }


def detect_volatility_expansion_setup(
    chain: Mapping[str, Any],
    *,
    allowed_regimes: Optional[set[str]] = None,
) -> dict[str, Any]:
    regime_scope = ALLOWED_REGIMES if allowed_regimes is None else allowed_regimes
    regime = str((chain.get("regime") or {}).get("primary_regime") or "UNKNOWN")
    if regime not in regime_scope:
        return _no_setup(f"regime {regime} outside volatility-expansion scope", regime, regime_scope)

    windows = chain.get("_windows")
    candles = list(getattr(windows, "candles_15m", []) or []) if windows is not None else []
    if len(candles) < HISTORY_LOOKBACK_15M + 1:
        return _no_setup("insufficient 15m history for equal pre-compression/compression windows", regime, regime_scope)

    atr = _d(((((chain.get("features") or {}).get("tf") or {}).get("1h") or {}).get("atr")))
    if atr is None or atr <= 0:
        return _no_setup("1h ATR unavailable", regime, regime_scope)

    # Compare equal-length windows. V1 accidentally compared a 16-candle
    # recent range with only four older candles, making the compression test
    # structurally almost impossible to pass.
    history = candles[-(HISTORY_LOOKBACK_15M + 1):-1]
    older = history[:PRE_COMPRESSION_LOOKBACK_15M]
    recent = history[PRE_COMPRESSION_LOOKBACK_15M:]
    trigger = candles[-1]

    recent_highs = [_d(c.get("high")) for c in recent]
    recent_lows = [_d(c.get("low")) for c in recent]
    older_highs = [_d(c.get("high")) for c in older]
    older_lows = [_d(c.get("low")) for c in older]
    if any(v is None for v in recent_highs + recent_lows + older_highs + older_lows):
        return _no_setup("invalid candle history", regime, regime_scope)

    range_now = max(recent_highs) - min(recent_lows)
    older_range = max(older_highs) - min(older_lows)
    if older_range <= 0 or range_now > older_range * RANGE_COMPRESSION_RATIO:
        return _no_setup("no clear pre-breakout range compression", regime, regime_scope)

    resistance = max(recent_highs)
    support = min(recent_lows)
    o, h, l, c = [_d(trigger.get(k)) for k in ("open", "high", "low", "close")]
    if None in (o, h, l, c):
        return _no_setup("trigger candle incomplete", regime, regime_scope)

    body = abs(c - o)
    if body < atr * MIN_BODY_ATR:
        return _no_setup("trigger body too small for volatility expansion", regime, regime_scope)

    if c > resistance + atr * BREAKOUT_BUFFER_ATR:
        direction = "LONG"
        level = resistance
        zone_low = level - atr * ENTRY_RETEST_ATR
        zone_high = level + atr * Decimal("0.05")
        stop = min(level - atr * STOP_ATR, l - atr * Decimal("0.05"))
        worst = zone_high
        risk = worst - stop
        targets = [worst + risk * m for m in TARGET_R_MULTIPLIERS]
    elif c < support - atr * BREAKOUT_BUFFER_ATR:
        direction = "SHORT"
        level = support
        zone_low = level - atr * Decimal("0.05")
        zone_high = level + atr * ENTRY_RETEST_ATR
        stop = max(level + atr * STOP_ATR, h + atr * Decimal("0.05"))
        worst = zone_low
        risk = stop - worst
        targets = [worst - risk * m for m in TARGET_R_MULTIPLIERS]
    else:
        return _no_setup("compressed range has not broken with a decisive close", regime, regime_scope)

    if risk <= 0:
        return _no_setup("non-positive risk geometry", regime, regime_scope)

    return {
        "setup_type": "volatility_expansion",
        "direction": direction,
        "entry_zone": {"low": zone_low, "high": zone_high},
        "invalidation": stop,
        "stop_loss": stop,
        "targets": targets,
        "confidence": 0.55,
        "reasons": [
            "equal-window 15m range compression versus the preceding four hours",
            "decisive close outside compressed range",
            "price-only detector; W3 provides independent confirmation",
        ],
        "counterarguments": [],
        "regime_compatible": True,
        "price_only_mode": True,
        "orderflow_confirmed": False,
        "reference_level": level,
        "compression_range": range_now,
        "older_range": older_range,
        "detection_version": "VOLATILITY_EXPANSION_SQUEEZE_V2_EQUAL_WINDOWS",
    }


def evaluate_volatility_expansion(
    chain: Mapping[str, Any],
    *,
    btc_regime: str | None = None,
    allowed_regimes: Optional[set[str]] = None,
) -> dict[str, Any]:
    setup = detect_volatility_expansion_setup(chain, allowed_regimes=allowed_regimes)
    if setup.get("direction") not in {"LONG", "SHORT"}:
        return {"setup": setup, "confirmation": None, "risk": None, "signal": None}

    features = chain.get("features") or {}
    regime = chain.get("regime") or {}
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
            setup_name="volatility_expansion",
            direction=setup["direction"],
            entry_low=_d(setup["entry_zone"]["low"]),
            entry_high=_d(setup["entry_zone"]["high"]),
            stop_loss=_d(setup["stop_loss"]),
            targets=tuple(_d(t) for t in setup["targets"]),
            regime=regime.get("primary_regime"),
            score=Decimal(str(setup["confidence"])),
            metadata={
                "confirmation_status": confirmation.get("status"),
                "native_risk_decision": risk.get("decision"),
                "detection_version": setup["detection_version"],
            },
        )
        signal.validate()
    return {"setup": setup, "confirmation": confirmation, "risk": risk, "signal": signal}
