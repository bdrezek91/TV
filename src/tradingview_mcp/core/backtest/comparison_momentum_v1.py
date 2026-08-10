"""Research-only Momentum Continuation candidate.

Fixed, outcome-independent detector using only closed candles available at the
historical decision cutoff.  It looks for a directional one-hour impulse made
of the last four 15m candles, then enters only if price finishes near the
impulse extreme instead of immediately rejecting it.

Order-flow is deliberately NOT used to create the setup; unchanged W3 evaluates
CVD/taker/OI/funding/BTC context afterwards.  This avoids double-counting and
keeps the experiment comparable with prior candidates.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping, Optional

from tradingview_mcp.core.analysis import orderflow_confirmation_v2 as ofc
from tradingview_mcp.core.analysis import risk_manager_v2 as rm
from tradingview_mcp.core.backtest.comparison_v1 import NeutralSignal, Variant

IMPULSE_CANDLES_15M = 4
IMPULSE_MIN_ATR = Decimal("0.90")
IMPULSE_MAX_ATR = Decimal("2.75")
MIN_DIRECTIONAL_CANDLES = 3
CLOSE_LOCATION_MIN = Decimal("0.70")
LAST_BODY_MIN_ATR = Decimal("0.10")
ENTRY_PULLBACK_ATR = Decimal("0.20")
ENTRY_EXTENSION_ATR = Decimal("0.05")
STOP_BUFFER_ATR = Decimal("0.10")
MAX_STOP_DISTANCE_ATR = Decimal("1.60")
TARGET_R_MULTIPLIERS = (Decimal("1.5"), Decimal("2.5"), Decimal("4.0"))
ALLOWED_REGIMES = {
    "TREND_UP", "TREND_DOWN", "BREAKOUT_UP", "BREAKOUT_DOWN", "NO_EDGE", "SQUEEZE"
}


def _d(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _no_setup(reason: str, *, regime: str | None = None) -> dict[str, Any]:
    return {
        "setup_type": "momentum_continuation",
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


def detect_momentum_continuation_setup(chain: Mapping[str, Any]) -> dict[str, Any]:
    regime = str((chain.get("regime") or {}).get("primary_regime") or "UNKNOWN")
    if regime not in ALLOWED_REGIMES:
        return _no_setup(f"regime {regime} outside predeclared momentum scope", regime=regime)

    windows = chain.get("_windows")
    if windows is None:
        return _no_setup("historical windows unavailable", regime=regime)
    candles = list(getattr(windows, "candles_15m", []) or [])
    if len(candles) < IMPULSE_CANDLES_15M:
        return _no_setup("insufficient closed 15m history for one-hour impulse", regime=regime)

    tf_1h = (((chain.get("features") or {}).get("tf") or {}).get("1h") or {})
    atr = _d(tf_1h.get("atr"))
    if atr is None or atr <= 0:
        return _no_setup("1h ATR unavailable", regime=regime)

    recent = candles[-IMPULSE_CANDLES_15M:]
    values = []
    for row in recent:
        o, h, l, c = (_d(row.get(k)) for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c):
            return _no_setup("incomplete OHLC in impulse candles", regime=regime)
        values.append((o, h, l, c))

    first_open = values[0][0]
    last_close = values[-1][3]
    move = last_close - first_open
    move_atr = abs(move) / atr
    if move_atr < IMPULSE_MIN_ATR:
        return _no_setup(f"one-hour move {move_atr:.2f} ATR below momentum threshold", regime=regime)
    if move_atr > IMPULSE_MAX_ATR:
        return _no_setup(f"one-hour move {move_atr:.2f} ATR too extended for continuation entry", regime=regime)

    direction = "LONG" if move > 0 else "SHORT"
    directional = sum(1 for o, _, _, c in values if (c > o if direction == "LONG" else c < o))
    if directional < MIN_DIRECTIONAL_CANDLES:
        return _no_setup(
            f"only {directional}/{IMPULSE_CANDLES_15M} candles align with impulse direction",
            regime=regime,
        )

    impulse_high = max(v[1] for v in values)
    impulse_low = min(v[2] for v in values)
    impulse_range = impulse_high - impulse_low
    if impulse_range <= 0:
        return _no_setup("invalid impulse range", regime=regime)
    close_location = (last_close - impulse_low) / impulse_range
    if direction == "LONG" and close_location < CLOSE_LOCATION_MIN:
        return _no_setup(f"LONG impulse failed to hold near high (location={close_location:.2f})", regime=regime)
    if direction == "SHORT" and close_location > Decimal("1") - CLOSE_LOCATION_MIN:
        return _no_setup(f"SHORT impulse failed to hold near low (location={close_location:.2f})", regime=regime)

    last_o, last_h, last_l, last_c = values[-1]
    last_body = abs(last_c - last_o)
    last_aligned = last_c > last_o if direction == "LONG" else last_c < last_o
    if not last_aligned or last_body < atr * LAST_BODY_MIN_ATR:
        return _no_setup("last 15m candle does not confirm continuation pressure", regime=regime)

    if direction == "LONG":
        zone_low = last_close - atr * ENTRY_PULLBACK_ATR
        zone_high = last_close + atr * ENTRY_EXTENSION_ATR
        stop = impulse_low - atr * STOP_BUFFER_ATR
        worst_entry = zone_high
        risk = worst_entry - stop
        if risk > atr * MAX_STOP_DISTANCE_ATR:
            return _no_setup("LONG continuation stop would be too wide relative to ATR", regime=regime)
        targets = [worst_entry + risk * mult for mult in TARGET_R_MULTIPLIERS]
    else:
        zone_low = last_close - atr * ENTRY_EXTENSION_ATR
        zone_high = last_close + atr * ENTRY_PULLBACK_ATR
        stop = impulse_high + atr * STOP_BUFFER_ATR
        worst_entry = zone_low
        risk = stop - worst_entry
        if risk > atr * MAX_STOP_DISTANCE_ATR:
            return _no_setup("SHORT continuation stop would be too wide relative to ATR", regime=regime)
        targets = [worst_entry - risk * mult for mult in TARGET_R_MULTIPLIERS]

    if risk <= 0:
        return _no_setup("momentum geometry produced non-positive risk", regime=regime)

    return {
        "setup_type": "momentum_continuation",
        "direction": direction,
        "entry_zone": {"low": zone_low, "high": zone_high},
        "invalidation": stop,
        "stop_loss": stop,
        "targets": targets,
        "confidence": 0.55,
        "reasons": [
            f"one-hour directional impulse {move_atr:.2f} ATR",
            f"{directional}/{IMPULSE_CANDLES_15M} recent 15m candles align with {direction}",
            f"impulse close location {close_location:.2f} holds near the directional extreme",
            "setup generation is price-only; W3 independently evaluates order-flow continuation",
        ],
        "counterarguments": [],
        "regime_compatible": True,
        "price_only_mode": False,
        "orderflow_confirmed": False,
        "impulse_move_atr": move_atr,
        "impulse_close_location": close_location,
        "directional_candles": directional,
        "detection_version": "MOMENTUM_CONTINUATION_IMPULSE_HOLD_V1",
    }


def evaluate_momentum_continuation(chain: Mapping[str, Any], *, btc_regime: str | None = None) -> dict[str, Any]:
    setup = detect_momentum_continuation_setup(chain)
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
            setup_name="momentum_continuation",
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
                "impulse_move_atr": setup["impulse_move_atr"],
            },
        )
        signal.validate()
    return {"setup": setup, "confirmation": confirmation, "risk": risk, "signal": signal}
