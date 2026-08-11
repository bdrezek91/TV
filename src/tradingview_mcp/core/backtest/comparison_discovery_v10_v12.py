"""Pre-registered final discovery batch: V10-V12.

Research-only setup detectors for the last 30-day discovery pass:
- V10 Trade Flow Impulse Continuation
- V11 Basis/Premium Dislocation Reversal
- V12 Volume-Weighted Time-Series Momentum

Thresholds are fixed before observing V10-V12 outcomes. All inputs are taken
from point-in-time historical windows and no production module is mutated.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Callable, Mapping, Optional

from tradingview_mcp.core.analysis import orderflow_confirmation_v2 as ofc
from tradingview_mcp.core.analysis import risk_manager_v2 as rm
from tradingview_mcp.core.backtest.comparison_v1 import NeutralSignal, Variant

TARGET_R_MULTIPLIERS = (Decimal("1.5"), Decimal("2.5"), Decimal("4.0"))

# V10: aggressive taker-flow continuation.
V10_FLOW_WINDOW_MINUTES = 60
V10_RECENT_MINUTES = 15
V10_MIN_COVERED_MINUTES = 45
V10_IMBALANCE_60_MIN = Decimal("0.08")
V10_IMBALANCE_15_MIN = Decimal("0.12")
V10_VOLUME_ACCELERATION_MIN = Decimal("1.10")
V10_PRICE_MOVE_MIN_ATR = Decimal("0.35")
V10_PRICE_MOVE_MAX_ATR = Decimal("2.00")
V10_ENTRY_PULLBACK_ATR = Decimal("0.15")
V10_ENTRY_EXTENSION_ATR = Decimal("0.05")
V10_STOP_BUFFER_ATR = Decimal("0.10")
V10_MAX_STOP_ATR = Decimal("1.50")
V10_ALLOWED_REGIMES = {
    "TREND_UP", "TREND_DOWN", "BREAKOUT_UP", "BREAKOUT_DOWN",
    "NO_EDGE", "SQUEEZE", "RANGE",
}

# V11: basis/funding dislocation reversal.
V11_LOOKBACK_HOURS = 24
V11_MIN_DERIVATIVE_ROWS = 144
V11_BASIS_Z_MIN = Decimal("1.50")
V11_MIN_FUNDING_ABS = Decimal("0.0001")
V11_TRIGGER_BODY_MIN_ATR = Decimal("0.10")
V11_MAX_DERIVATIVE_AGE_MINUTES = 10
V11_ENTRY_RETRACE_ATR = Decimal("0.10")
V11_STOP_BUFFER_ATR = Decimal("0.25")
V11_MAX_STOP_ATR = Decimal("1.25")
V11_ALLOWED_REGIMES = {"RANGE", "NO_EDGE", "SQUEEZE", "TREND_UP", "TREND_DOWN"}

# V12: volume-weighted time-series momentum.
V12_RECENT_15M_CANDLES = 8
V12_BASELINE_15M_CANDLES = 8
V12_MIN_VOLUME_EXPANSION = Decimal("1.20")
V12_MOVE_MIN_ATR = Decimal("0.50")
V12_MOVE_MAX_ATR = Decimal("2.50")
V12_MIN_DIRECTIONAL_CANDLES = 5
V12_ENTRY_PULLBACK_ATR = Decimal("0.20")
V12_ENTRY_EXTENSION_ATR = Decimal("0.05")
V12_STOP_BUFFER_ATR = Decimal("0.10")
V12_MAX_STOP_ATR = Decimal("1.60")
V12_ALLOWED_REGIMES = {
    "TREND_UP", "TREND_DOWN", "BREAKOUT_UP", "BREAKOUT_DOWN",
    "NO_EDGE", "SQUEEZE",
}


def _d(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _ts(value: Any) -> Optional[dt.datetime]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        out = value
    else:
        out = dt.datetime.fromisoformat(str(value))
    if out.tzinfo is None:
        out = out.replace(tzinfo=dt.timezone.utc)
    return out.astimezone(dt.timezone.utc)


def _no_setup(name: str, reason: str, *, regime: str, price_only: bool) -> dict[str, Any]:
    return {
        "setup_type": name,
        "direction": "NO_SETUP",
        "entry_zone": None,
        "invalidation": None,
        "stop_loss": None,
        "targets": [],
        "confidence": 0.0,
        "reasons": [reason],
        "counterarguments": [],
        "regime_compatible": False,
        "price_only_mode": price_only,
        "orderflow_confirmed": False,
    }


def _atr(chain: Mapping[str, Any]) -> Optional[Decimal]:
    return _d(((((chain.get("features") or {}).get("tf") or {}).get("1h") or {}).get("atr")))


def _geometry(
    direction: str,
    last_close: Decimal,
    atr: Decimal,
    anchor_low: Decimal,
    anchor_high: Decimal,
    *,
    pullback_atr: Decimal,
    extension_atr: Decimal,
    stop_buffer_atr: Decimal,
    max_stop_atr: Decimal,
) -> Optional[dict[str, Any]]:
    if direction == "LONG":
        zone_low = last_close - atr * pullback_atr
        zone_high = last_close + atr * extension_atr
        stop = min(anchor_low - atr * stop_buffer_atr, zone_low - atr * Decimal("0.10"))
        worst = zone_high
        risk = worst - stop
        targets = [worst + risk * m for m in TARGET_R_MULTIPLIERS]
    else:
        zone_low = last_close - atr * extension_atr
        zone_high = last_close + atr * pullback_atr
        stop = max(anchor_high + atr * stop_buffer_atr, zone_high + atr * Decimal("0.10"))
        worst = zone_low
        risk = stop - worst
        targets = [worst - risk * m for m in TARGET_R_MULTIPLIERS]
    if risk <= 0 or risk > atr * max_stop_atr:
        return None
    return {
        "entry_zone": {"low": zone_low, "high": zone_high},
        "invalidation": stop,
        "stop_loss": stop,
        "targets": targets,
        "risk_atr": risk / atr,
    }


def _trade_time(row: Mapping[str, Any]) -> Optional[dt.datetime]:
    return _ts(row.get("bucket_start") or row.get("open_time"))


def _flow_stats(rows: list[dict[str, Any]]) -> Optional[dict[str, Decimal]]:
    if not rows:
        return None
    buy = sum((_d(r.get("buy_taker_volume")) or Decimal("0") for r in rows), Decimal("0"))
    sell = sum((_d(r.get("sell_taker_volume")) or Decimal("0") for r in rows), Decimal("0"))
    total = buy + sell
    if total <= 0:
        return None
    return {
        "buy": buy,
        "sell": sell,
        "total": total,
        "imbalance": (buy - sell) / total,
    }


def detect_trade_flow_impulse_continuation(chain: Mapping[str, Any]) -> dict[str, Any]:
    """V10: continue strong aggressive taker flow when price and new OI agree."""
    name = "trade_flow_impulse_continuation"
    regime = str((chain.get("regime") or {}).get("primary_regime") or "UNKNOWN")
    if regime not in V10_ALLOWED_REGIMES:
        return _no_setup(name, f"regime {regime} outside V10 scope", regime=regime, price_only=False)

    cutoff = _ts(chain.get("as_of"))
    trades = list(chain.get("_trade_flow_trades_1m") or [])
    if cutoff is None:
        return _no_setup(name, "decision timestamp unavailable", regime=regime, price_only=False)
    start = cutoff - dt.timedelta(minutes=V10_FLOW_WINDOW_MINUTES)
    trades = [r for r in trades if (t := _trade_time(r)) is not None and start <= t < cutoff]
    if len(trades) < V10_MIN_COVERED_MINUTES:
        return _no_setup(name, "insufficient 60m public-trade coverage", regime=regime, price_only=False)

    recent_start = cutoff - dt.timedelta(minutes=V10_RECENT_MINUTES)
    recent = [r for r in trades if (_trade_time(r) or cutoff) >= recent_start]
    prior = [r for r in trades if (_trade_time(r) or cutoff) < recent_start]
    all_stats, recent_stats, prior_stats = _flow_stats(trades), _flow_stats(recent), _flow_stats(prior)
    if not all_stats or not recent_stats or not prior_stats or not recent or not prior:
        return _no_setup(name, "could not compute V10 taker-flow windows", regime=regime, price_only=False)

    recent_avg = recent_stats["total"] / Decimal(len(recent))
    prior_avg = prior_stats["total"] / Decimal(len(prior))
    if prior_avg <= 0:
        return _no_setup(name, "prior taker-volume baseline is zero", regime=regime, price_only=False)
    volume_accel = recent_avg / prior_avg

    windows = chain.get("_windows")
    candles = list(getattr(windows, "candles_15m", []) or []) if windows is not None else []
    if len(candles) < 4:
        return _no_setup(name, "insufficient closed 15m candles for V10 price impulse", regime=regime, price_only=False)
    atr = _atr(chain)
    if atr is None or atr <= 0:
        return _no_setup(name, "1h ATR unavailable", regime=regime, price_only=False)
    recent_candles = candles[-4:]
    first_open = _d(recent_candles[0].get("open"))
    last_close = _d(recent_candles[-1].get("close"))
    if first_open is None or last_close is None:
        return _no_setup(name, "incomplete V10 price impulse candles", regime=regime, price_only=False)
    move = last_close - first_open
    move_atr = abs(move) / atr
    if move_atr < V10_PRICE_MOVE_MIN_ATR or move_atr > V10_PRICE_MOVE_MAX_ATR:
        return _no_setup(name, f"V10 one-hour price move {move_atr:.3f} ATR outside pre-registered band", regime=regime, price_only=False)

    direction = "LONG" if move > 0 else "SHORT"
    sign = Decimal("1") if direction == "LONG" else Decimal("-1")
    if all_stats["imbalance"] * sign < V10_IMBALANCE_60_MIN:
        return _no_setup(name, "60m taker imbalance does not confirm price direction", regime=regime, price_only=False)
    if recent_stats["imbalance"] * sign < V10_IMBALANCE_15_MIN:
        return _no_setup(name, "recent 15m taker imbalance is not strong enough", regime=regime, price_only=False)
    if volume_accel < V10_VOLUME_ACCELERATION_MIN:
        return _no_setup(name, "recent taker volume is not accelerating versus prior 45m", regime=regime, price_only=False)

    futures = (chain.get("features") or {}).get("futures") or {}
    relation = futures.get("price_to_oi_relation")
    required = "PRICE_UP_OI_UP" if direction == "LONG" else "PRICE_DOWN_OI_UP"
    if relation != required:
        return _no_setup(name, f"open interest relation {relation} is not new-position trend confirmation", regime=regime, price_only=False)

    lows = [_d(c.get("low")) for c in recent_candles]
    highs = [_d(c.get("high")) for c in recent_candles]
    if any(v is None for v in lows + highs):
        return _no_setup(name, "incomplete V10 impulse range", regime=regime, price_only=False)
    geom = _geometry(
        direction, last_close, atr, min(lows), max(highs),
        pullback_atr=V10_ENTRY_PULLBACK_ATR,
        extension_atr=V10_ENTRY_EXTENSION_ATR,
        stop_buffer_atr=V10_STOP_BUFFER_ATR,
        max_stop_atr=V10_MAX_STOP_ATR,
    )
    if geom is None:
        return _no_setup(name, "V10 stop geometry exceeds pre-registered ATR budget", regime=regime, price_only=False)

    return {
        "setup_type": name,
        "direction": direction,
        **geom,
        "confidence": 0.55,
        "reasons": [
            f"taker volume imbalance confirms {direction}: 60m={all_stats['imbalance']:.4f}, 15m={recent_stats['imbalance']:.4f}",
            f"volume acceleration recent15/prior45={volume_accel:.3f}",
            f"open interest confirms new positions with {relation}",
            f"one-hour price impulse {move_atr:.3f} ATR aligns with aggressive flow",
        ],
        "counterarguments": [],
        "regime_compatible": True,
        "price_only_mode": False,
        "orderflow_confirmed": False,
        "flow_imbalance_60m": all_stats["imbalance"],
        "flow_imbalance_15m": recent_stats["imbalance"],
        "volume_acceleration": volume_accel,
        "price_move_atr": move_atr,
        "detection_version": "V10_TRADE_FLOW_IMPULSE_CONTINUATION_PREREG_V1",
    }


def _basis_zscore(rows: list[dict[str, Any]]) -> Optional[tuple[Decimal, Decimal, Decimal]]:
    values = [_d(r.get("basis_pct")) for r in rows]
    values = [v for v in values if v is not None]
    if len(values) < V11_MIN_DERIVATIVE_ROWS:
        return None
    mean = sum(values, Decimal("0")) / Decimal(len(values))
    variance = sum(((v - mean) ** 2 for v in values), Decimal("0")) / Decimal(len(values))
    if variance <= 0:
        return None
    std = variance.sqrt()
    return (values[-1] - mean) / std, mean, std


def detect_basis_dislocation_reversal(chain: Mapping[str, Any]) -> dict[str, Any]:
    """V11: fade a statistically extreme perp/index basis with funding crowding."""
    name = "basis_premium_dislocation_reversal"
    regime = str((chain.get("regime") or {}).get("primary_regime") or "UNKNOWN")
    if regime not in V11_ALLOWED_REGIMES:
        return _no_setup(name, f"regime {regime} outside V11 scope", regime=regime, price_only=False)

    cutoff = _ts(chain.get("as_of"))
    rows = list(chain.get("_derivatives_24h") or [])
    if cutoff is None:
        return _no_setup(name, "decision timestamp unavailable", regime=regime, price_only=False)
    start = cutoff - dt.timedelta(hours=V11_LOOKBACK_HOURS)
    rows = [r for r in rows if (t := _ts(r.get("source_timestamp"))) is not None and start <= t <= cutoff]
    if len(rows) < V11_MIN_DERIVATIVE_ROWS:
        return _no_setup(name, "insufficient 24h derivative snapshot coverage", regime=regime, price_only=False)

    latest = rows[-1]
    latest_ts = _ts(latest.get("source_timestamp"))
    if latest_ts is None or cutoff - latest_ts > dt.timedelta(minutes=V11_MAX_DERIVATIVE_AGE_MINUTES):
        return _no_setup(name, "latest derivative snapshot is stale for V11", regime=regime, price_only=False)

    z_result = _basis_zscore(rows)
    if z_result is None:
        return _no_setup(name, "basis_pct variance/coverage insufficient for V11 z-score", regime=regime, price_only=False)
    basis_z, basis_mean, basis_std = z_result
    funding = _d(latest.get("funding_rate"))
    basis_pct = _d(latest.get("basis_pct"))
    if funding is None or basis_pct is None:
        return _no_setup(name, "basis_pct/funding unavailable", regime=regime, price_only=False)
    if abs(basis_z) < V11_BASIS_Z_MIN:
        return _no_setup(name, f"basis z-score {basis_z:.3f} below pre-registered dislocation threshold", regime=regime, price_only=False)
    if abs(funding) < V11_MIN_FUNDING_ABS or funding * basis_z <= 0:
        return _no_setup(name, "funding does not confirm the side of the basis crowding", regime=regime, price_only=False)

    direction = "SHORT" if basis_z > 0 else "LONG"
    windows = chain.get("_windows")
    candles = list(getattr(windows, "candles_15m", []) or []) if windows is not None else []
    if not candles:
        return _no_setup(name, "closed 15m trigger candle unavailable", regime=regime, price_only=False)
    trigger = candles[-1]
    o, h, l, c = (_d(trigger.get(k)) for k in ("open", "high", "low", "close"))
    atr = _atr(chain)
    if None in (o, h, l, c) or atr is None or atr <= 0:
        return _no_setup(name, "V11 trigger candle/ATR incomplete", regime=regime, price_only=False)
    body = abs(c - o)
    counter_crowd = c < o if direction == "SHORT" else c > o
    if not counter_crowd or body < atr * V11_TRIGGER_BODY_MIN_ATR:
        return _no_setup(name, "basis dislocation lacks a counter-crowd 15m price trigger", regime=regime, price_only=False)

    geom = _geometry(
        direction, c, atr, l, h,
        pullback_atr=V11_ENTRY_RETRACE_ATR,
        extension_atr=Decimal("0.05"),
        stop_buffer_atr=V11_STOP_BUFFER_ATR,
        max_stop_atr=V11_MAX_STOP_ATR,
    )
    if geom is None:
        return _no_setup(name, "V11 stop geometry exceeds pre-registered ATR budget", regime=regime, price_only=False)

    ratio = _d(latest.get("long_short_ratio"))
    ratio_text = f"; long/short ratio={ratio}" if ratio is not None else ""
    return {
        "setup_type": name,
        "direction": direction,
        **geom,
        "confidence": 0.55,
        "reasons": [
            f"basis dislocation z={basis_z:.3f} over trailing 24h (basis_pct={basis_pct}, mean={basis_mean}, std={basis_std})",
            f"funding {funding} is aligned with the crowded basis side{ratio_text}",
            f"closed 15m candle triggered {direction} against the crowded side",
        ],
        "counterarguments": [],
        "regime_compatible": True,
        "price_only_mode": False,
        "orderflow_confirmed": False,
        "basis_zscore_24h": basis_z,
        "basis_pct": basis_pct,
        "funding_rate": funding,
        "long_short_ratio": ratio,
        "detection_version": "V11_BASIS_PREMIUM_DISLOCATION_PREREG_V1",
    }


def _signed_return(prev_close: Decimal, close: Decimal) -> Decimal:
    if prev_close == 0:
        return Decimal("0")
    return (close - prev_close) / prev_close


def detect_volume_weighted_tsmom(chain: Mapping[str, Any]) -> dict[str, Any]:
    """V12: multi-horizon time-series momentum strengthened by expanding volume."""
    name = "volume_weighted_tsmom"
    regime = str((chain.get("regime") or {}).get("primary_regime") or "UNKNOWN")
    if regime not in V12_ALLOWED_REGIMES:
        return _no_setup(name, f"regime {regime} outside V12 scope", regime=regime, price_only=True)

    windows = chain.get("_windows")
    c15 = list(getattr(windows, "candles_15m", []) or []) if windows is not None else []
    c1h = list(getattr(windows, "candles_1h", []) or []) if windows is not None else []
    required_15m = V12_RECENT_15M_CANDLES + V12_BASELINE_15M_CANDLES
    if len(c15) < required_15m or len(c1h) < 5:
        return _no_setup(name, "insufficient closed 15m/1h history for V12", regime=regime, price_only=True)
    atr = _atr(chain)
    if atr is None or atr <= 0:
        return _no_setup(name, "1h ATR unavailable", regime=regime, price_only=True)

    baseline = c15[-required_15m:-V12_RECENT_15M_CANDLES]
    recent = c15[-V12_RECENT_15M_CANDLES:]
    baseline_vol = sum((_d(c.get("volume")) or Decimal("0") for c in baseline), Decimal("0"))
    recent_vol = sum((_d(c.get("volume")) or Decimal("0") for c in recent), Decimal("0"))
    if baseline_vol <= 0 or recent_vol <= 0:
        return _no_setup(name, "15m candle volume unavailable for V12", regime=regime, price_only=True)
    volume_expansion = recent_vol / baseline_vol
    if volume_expansion < V12_MIN_VOLUME_EXPANSION:
        return _no_setup(name, "recent 2h volume does not exceed prior 2h baseline", regime=regime, price_only=True)

    closes = [_d(c.get("close")) for c in recent]
    opens = [_d(c.get("open")) for c in recent]
    highs = [_d(c.get("high")) for c in recent]
    lows = [_d(c.get("low")) for c in recent]
    vols = [_d(c.get("volume")) for c in recent]
    if any(v is None for v in closes + opens + highs + lows + vols):
        return _no_setup(name, "incomplete V12 OHLCV candles", regime=regime, price_only=True)

    first_open, last_close = opens[0], closes[-1]
    move = last_close - first_open
    move_atr = abs(move) / atr
    if move_atr < V12_MOVE_MIN_ATR or move_atr > V12_MOVE_MAX_ATR:
        return _no_setup(name, f"V12 two-hour move {move_atr:.3f} ATR outside pre-registered band", regime=regime, price_only=True)
    direction = "LONG" if move > 0 else "SHORT"
    sign = Decimal("1") if direction == "LONG" else Decimal("-1")

    directional = sum(1 for o, c in zip(opens, closes) if (c > o if direction == "LONG" else c < o))
    if directional < V12_MIN_DIRECTIONAL_CANDLES:
        return _no_setup(name, "too few 15m candles align with V12 direction", regime=regime, price_only=True)

    weighted_return = Decimal("0")
    for o, c, v in zip(opens, closes, vols):
        if o == 0:
            continue
        weighted_return += ((c - o) / o) * (v / recent_vol)
    if weighted_return * sign <= 0:
        return _no_setup(name, "volume-weighted 15m returns disagree with V12 direction", regime=regime, price_only=True)

    h1_start = _d(c1h[-5].get("close"))
    h1_end = _d(c1h[-1].get("close"))
    if h1_start is None or h1_end is None or h1_start == 0:
        return _no_setup(name, "4h time-series momentum unavailable", regime=regime, price_only=True)
    momentum_4h = _signed_return(h1_start, h1_end)
    if momentum_4h * sign <= 0:
        return _no_setup(name, "4h time-series momentum disagrees with recent 2h direction", regime=regime, price_only=True)

    geom = _geometry(
        direction, last_close, atr, min(lows), max(highs),
        pullback_atr=V12_ENTRY_PULLBACK_ATR,
        extension_atr=V12_ENTRY_EXTENSION_ATR,
        stop_buffer_atr=V12_STOP_BUFFER_ATR,
        max_stop_atr=V12_MAX_STOP_ATR,
    )
    if geom is None:
        return _no_setup(name, "V12 stop geometry exceeds pre-registered ATR budget", regime=regime, price_only=True)

    return {
        "setup_type": name,
        "direction": direction,
        **geom,
        "confidence": 0.55,
        "reasons": [
            f"two-hour volume expansion recent/prior={volume_expansion:.3f}",
            f"volume-weighted signed 15m return {weighted_return} aligns with {direction}",
            f"two-hour move {move_atr:.3f} ATR with {directional}/{V12_RECENT_15M_CANDLES} aligned candles",
            f"four-hour time-series momentum {momentum_4h} aligns with {direction}",
        ],
        "counterarguments": [],
        "regime_compatible": True,
        "price_only_mode": True,
        "orderflow_confirmed": False,
        "volume_expansion": volume_expansion,
        "weighted_return": weighted_return,
        "momentum_4h": momentum_4h,
        "price_move_atr": move_atr,
        "detection_version": "V12_VOLUME_WEIGHTED_TSMOM_PREREG_V1",
    }


def evaluate_discovery_setup(
    chain: Mapping[str, Any],
    detector: Callable[[Mapping[str, Any]], dict[str, Any]],
    setup_name: str,
    *,
    btc_regime: str | None = None,
) -> dict[str, Any]:
    setup = detector(chain)
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
            setup_name=setup_name,
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
            },
        )
        signal.validate()
    return {"setup": setup, "confirmation": confirmation, "risk": risk, "signal": signal}
