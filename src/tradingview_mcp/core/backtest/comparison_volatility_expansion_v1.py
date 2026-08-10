"""Research-only volatility expansion / squeeze breakout candidate."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping, Optional

from tradingview_mcp.core.analysis import orderflow_confirmation_v2 as ofc
from tradingview_mcp.core.analysis import risk_manager_v2 as rm
from tradingview_mcp.core.backtest.comparison_v1 import NeutralSignal, Variant

LOOKBACK_15M = 20
SQUEEZE_LOOKBACK = 16
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


def _no_setup(reason: str, regime: str | None = None) -> dict[str, Any]:
    return {"setup_type":"volatility_expansion","direction":"NO_SETUP","entry_zone":None,"invalidation":None,"stop_loss":None,"targets":[],"confidence":0.0,"reasons":[reason],"counterarguments":[],"regime_compatible":regime in ALLOWED_REGIMES if regime else False,"price_only_mode":True,"orderflow_confirmed":False}


def detect_volatility_expansion_setup(chain: Mapping[str, Any]) -> dict[str, Any]:
    regime = str((chain.get("regime") or {}).get("primary_regime") or "UNKNOWN")
    if regime not in ALLOWED_REGIMES:
        return _no_setup(f"regime {regime} outside volatility-expansion scope", regime)
    windows = chain.get("_windows")
    candles = list(getattr(windows, "candles_15m", []) or []) if windows is not None else []
    if len(candles) < LOOKBACK_15M + 1:
        return _no_setup("insufficient 15m history", regime)
    atr = _d(((((chain.get("features") or {}).get("tf") or {}).get("1h") or {}).get("atr")))
    if atr is None or atr <= 0:
        return _no_setup("1h ATR unavailable", regime)

    history = candles[-(LOOKBACK_15M+1):-1]
    recent = history[-SQUEEZE_LOOKBACK:]
    trigger = candles[-1]
    highs = [_d(c.get("high")) for c in recent]
    lows = [_d(c.get("low")) for c in recent]
    if any(v is None for v in highs+lows):
        return _no_setup("invalid candle history", regime)
    range_now = max(highs)-min(lows)
    older = history[:-SQUEEZE_LOOKBACK] or recent
    older_highs=[_d(c.get("high")) for c in older]; older_lows=[_d(c.get("low")) for c in older]
    older_range=max(older_highs)-min(older_lows)
    if older_range <= 0 or range_now > older_range * RANGE_COMPRESSION_RATIO:
        return _no_setup("no clear pre-breakout range compression", regime)

    resistance=max(highs); support=min(lows)
    o,h,l,c=[_d(trigger.get(k)) for k in ("open","high","low","close")]
    if None in (o,h,l,c):
        return _no_setup("trigger candle incomplete", regime)
    body=abs(c-o)
    if body < atr*MIN_BODY_ATR:
        return _no_setup("trigger body too small for volatility expansion", regime)

    if c > resistance + atr*BREAKOUT_BUFFER_ATR:
        direction="LONG"; level=resistance
        zone_low=level-atr*ENTRY_RETEST_ATR; zone_high=level+atr*Decimal("0.05")
        stop=min(level-atr*STOP_ATR, l-atr*Decimal("0.05"))
        worst=zone_high; risk=worst-stop
        targets=[worst+risk*m for m in TARGET_R_MULTIPLIERS]
    elif c < support - atr*BREAKOUT_BUFFER_ATR:
        direction="SHORT"; level=support
        zone_low=level-atr*Decimal("0.05"); zone_high=level+atr*ENTRY_RETEST_ATR
        stop=max(level+atr*STOP_ATR, h+atr*Decimal("0.05"))
        worst=zone_low; risk=stop-worst
        targets=[worst-risk*m for m in TARGET_R_MULTIPLIERS]
    else:
        return _no_setup("compressed range has not broken with a decisive close", regime)
    if risk <= 0:
        return _no_setup("non-positive risk geometry", regime)
    return {"setup_type":"volatility_expansion","direction":direction,"entry_zone":{"low":zone_low,"high":zone_high},"invalidation":stop,"stop_loss":stop,"targets":targets,"confidence":0.55,"reasons":["pre-breakout 15m range compression","decisive close outside compressed range","price-only detector; W3 provides independent confirmation"],"counterarguments":[],"regime_compatible":True,"price_only_mode":False,"orderflow_confirmed":False,"reference_level":level,"compression_range":range_now,"older_range":older_range,"detection_version":"VOLATILITY_EXPANSION_SQUEEZE_V1"}


def evaluate_volatility_expansion(chain: Mapping[str, Any], *, btc_regime: str | None=None) -> dict[str, Any]:
    setup=detect_volatility_expansion_setup(chain)
    if setup.get("direction") not in {"LONG","SHORT"}:
        return {"setup":setup,"confirmation":None,"risk":None,"signal":None}
    features=chain.get("features") or {}; regime=chain.get("regime") or {}
    confirmation=ofc.confirm_setup(str(chain.get("symbol")),setup,regime,features.get("tf") or {},features.get("volume") or {},features.get("orderbook") or {},features.get("futures") or {},features.get("liquidations") or {},btc_regime=btc_regime,data_quality=chain.get("data_quality") or {})
    portfolio={"open_positions":[],"daily_realized_pnl_pct":Decimal("0"),"weekly_realized_pnl_pct":Decimal("0"),"btc_regime_flip_detected":False}
    risk=rm.evaluate_risk(str(chain.get("symbol")),chain.get("as_of"),regime,setup,confirmation,features.get("orderbook") or {},chain.get("data_quality") or {},portfolio,rm.DEFAULT_RISK_CONFIG)
    signal=None
    if risk.get("decision") in {"APPROVED","APPROVED_REDUCED_SIZE"}:
        signal=NeutralSignal(variant=Variant.RESEARCH_V2,symbol=str(chain.get("symbol")),decision_time=chain.get("as_of"),setup_name="volatility_expansion",direction=setup["direction"],entry_low=_d(setup["entry_zone"]["low"]),entry_high=_d(setup["entry_zone"]["high"]),stop_loss=_d(setup["stop_loss"]),targets=tuple(_d(t) for t in setup["targets"]),regime=regime.get("primary_regime"),score=Decimal(str(setup["confidence"])),metadata={"confirmation_status":confirmation.get("status"),"native_risk_decision":risk.get("decision"),"detection_version":setup["detection_version"]})
        signal.validate()
    return {"setup":setup,"confirmation":confirmation,"risk":risk,"signal":signal}
