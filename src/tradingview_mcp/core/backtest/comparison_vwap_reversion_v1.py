"""Research-only rolling VWAP deviation reversion candidate.

Uses a 24h rolling VWAP built from the last 96 closed 15m candles.  A setup is
only emitted when price is materially displaced from VWAP and the most recent
closed 15m candle shows rejection back toward VWAP.  No future data, orderbook
history or liquidation history is used by the detector.  W3/W4 are applied
unchanged afterwards.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping, Optional

from tradingview_mcp.core.analysis import orderflow_confirmation_v2 as ofc
from tradingview_mcp.core.analysis import risk_manager_v2 as rm
from tradingview_mcp.core.backtest.comparison_v1 import NeutralSignal, Variant

VWAP_LOOKBACK_15M = 96
MIN_DEVIATION_ATR = Decimal("1.25")
MAX_DEVIATION_ATR = Decimal("4.00")
ENTRY_ATR = Decimal("0.15")
STOP_ATR = Decimal("0.35")
TARGET_R_MULTIPLIERS = (Decimal("1.5"), Decimal("2.5"), Decimal("4.0"))
ALLOWED_REGIMES = {"RANGE", "NO_EDGE", "SQUEEZE", "TREND_UP", "TREND_DOWN"}


def _d(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _no_setup(reason: str, regime: str | None = None) -> dict[str, Any]:
    return {
        "setup_type": "vwap_deviation_reversion",
        "direction": "NO_SETUP",
        "entry_zone": None,
        "invalidation": None,
        "stop_loss": None,
        "targets": [],
        "confidence": 0.0,
        "reasons": [reason],
        "counterarguments": [],
        "regime_compatible": regime in ALLOWED_REGIMES if regime else False,
        "price_only_mode": True,
        "orderflow_confirmed": False,
    }


def _rolling_vwap(candles: list[dict]) -> Optional[Decimal]:
    pv = Decimal("0")
    volume = Decimal("0")
    for row in candles:
        high, low, close, vol = (_d(row.get(k)) for k in ("high", "low", "close", "volume"))
        if None in (high, low, close, vol) or vol < 0:
            return None
        typical = (high + low + close) / Decimal("3")
        pv += typical * vol
        volume += vol
    return pv / volume if volume > 0 else None


def detect_vwap_deviation_reversion_setup(chain: Mapping[str, Any]) -> dict[str, Any]:
    regime = str((chain.get("regime") or {}).get("primary_regime") or "UNKNOWN")
    if regime not in ALLOWED_REGIMES:
        return _no_setup(f"regime {regime} outside predeclared VWAP-reversion scope", regime)

    windows = chain.get("_windows")
    candles = list(getattr(windows, "candles_15m", []) or []) if windows is not None else []
    if len(candles) < VWAP_LOOKBACK_15M:
        return _no_setup("insufficient 15m history for 24h rolling VWAP", regime)
    window = candles[-VWAP_LOOKBACK_15M:]
    vwap = _rolling_vwap(window)
    tf_1h = (((chain.get("features") or {}).get("tf") or {}).get("1h") or {})
    atr = _d(tf_1h.get("atr"))
    if vwap is None or atr is None or atr <= 0:
        return _no_setup("VWAP or 1h ATR unavailable", regime)

    last = window[-1]
    o, h, l, c = (_d(last.get(k)) for k in ("open", "high", "low", "close"))
    if None in (o, h, l, c):
        return _no_setup("latest 15m OHLC incomplete", regime)

    deviation = (c - vwap) / atr
    if abs(deviation) < MIN_DEVIATION_ATR:
        return _no_setup(f"VWAP deviation {deviation:.2f} ATR below threshold {MIN_DEVIATION_ATR}", regime)
    if abs(deviation) > MAX_DEVIATION_ATR:
        return _no_setup(f"VWAP deviation {deviation:.2f} ATR exceeds max hypothesis {MAX_DEVIATION_ATR}", regime)

    candle_range = h - l
    if candle_range <= 0:
        return _no_setup("latest 15m candle has zero range", regime)
    close_location = (c - l) / candle_range

    if deviation < 0:
        # Below VWAP: require a bullish rejection candle closing in upper half.
        if not (c > o and close_location >= Decimal("0.60")):
            return _no_setup("below-VWAP displacement lacks bullish 15m rejection", regime)
        direction = "LONG"
        zone_low = c - atr * ENTRY_ATR
        zone_high = c + atr * ENTRY_ATR
        stop = min(l - atr * STOP_ATR, zone_low - atr * Decimal("0.10"))
        worst = zone_high
        risk = worst - stop
        reasons = [
            f"price {abs(deviation):.2f} ATR below rolling 24h VWAP",
            "latest closed 15m candle rejects lower prices and closes in its upper range",
        ]
    else:
        # Above VWAP: require bearish rejection closing in lower half.
        if not (c < o and close_location <= Decimal("0.40")):
            return _no_setup("above-VWAP displacement lacks bearish 15m rejection", regime)
        direction = "SHORT"
        zone_low = c - atr * ENTRY_ATR
        zone_high = c + atr * ENTRY_ATR
        stop = max(h + atr * STOP_ATR, zone_high + atr * Decimal("0.10"))
        worst = zone_low
        risk = stop - worst
        reasons = [
            f"price {abs(deviation):.2f} ATR above rolling 24h VWAP",
            "latest closed 15m candle rejects higher prices and closes in its lower range",
        ]

    if risk <= 0:
        return _no_setup("VWAP reversion geometry produced non-positive risk", regime)
    targets = [
        worst + risk * mult if direction == "LONG" else worst - risk * mult
        for mult in TARGET_R_MULTIPLIERS
    ]
    return {
        "setup_type": "vwap_deviation_reversion",
        "direction": direction,
        "entry_zone": {"low": zone_low, "high": zone_high},
        "invalidation": stop,
        "stop_loss": stop,
        "targets": targets,
        "confidence": 0.55,
        "reasons": reasons + ["targets use fixed 1.5R/2.5R/4R W4-compatible geometry"],
        "counterarguments": [],
        "regime_compatible": True,
        "price_only_mode": True,
        "orderflow_confirmed": False,
        "rolling_vwap": vwap,
        "deviation_atr": deviation,
        "detection_version": "ROLLING_24H_VWAP_REJECTION_V1",
    }


def evaluate_vwap_deviation_reversion(chain: Mapping[str, Any], *, btc_regime: str | None = None) -> dict[str, Any]:
    setup = detect_vwap_deviation_reversion_setup(chain)
    features = chain.get("features") or {}
    regime = chain.get("regime") or {}
    if setup.get("direction") not in {"LONG", "SHORT"}:
        return {"setup": setup, "confirmation": None, "risk": None, "signal": None}

    confirmation = ofc.confirm_setup(
        str(chain.get("symbol")), setup, regime,
        features.get("tf") or {}, features.get("volume") or {},
        features.get("orderbook") or {}, features.get("futures") or {},
        features.get("liquidations") or {}, btc_regime=btc_regime,
        data_quality=chain.get("data_quality") or {},
    )
    portfolio = {
        "open_positions": [],
        "daily_realized_pnl_pct": Decimal("0"),
        "weekly_realized_pnl_pct": Decimal("0"),
        "btc_regime_flip_detected": False,
    }
    risk = rm.evaluate_risk(
        str(chain.get("symbol")), chain.get("as_of"), regime, setup, confirmation,
        features.get("orderbook") or {}, chain.get("data_quality") or {},
        portfolio, rm.DEFAULT_RISK_CONFIG,
    )
    signal = None
    if risk.get("decision") in {"APPROVED", "APPROVED_REDUCED_SIZE"}:
        signal = NeutralSignal(
            variant=Variant.RESEARCH_V2,
            symbol=str(chain.get("symbol")),
            decision_time=chain.get("as_of"),
            setup_name="vwap_deviation_reversion",
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
                "rolling_vwap": setup["rolling_vwap"],
                "deviation_atr": setup["deviation_atr"],
            },
        )
        signal.validate()
    return {"setup": setup, "confirmation": confirmation, "risk": risk, "signal": signal}
