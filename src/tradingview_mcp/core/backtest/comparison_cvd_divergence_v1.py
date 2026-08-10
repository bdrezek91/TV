"""Research-only CVD divergence reversal candidate.

Detects regular bullish/bearish divergence between confirmed 15m price swings
and cumulative taker-volume delta reconstructed from historical 1m trade
aggregates. Only data available at the decision cutoff is used.

Bullish: price makes a lower swing low while cumulative delta makes a higher low.
Bearish: price makes a higher swing high while cumulative delta makes a lower high.

The setup generator itself does not use W3 verdicts, orderbook or liquidation
history. W3/W4 are applied unchanged afterwards.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Mapping, Optional

from tradingview_mcp.core.analysis import orderflow_confirmation_v2 as ofc
from tradingview_mcp.core.analysis import risk_manager_v2 as rm
from tradingview_mcp.core.backtest.comparison_v1 import NeutralSignal, Variant

LOOKBACK_15M = 24
PIVOT_LEFT = 2
PIVOT_RIGHT = 2
MIN_PIVOT_SEPARATION = 3
MIN_PRICE_DIVERGENCE_ATR = Decimal("0.05")
ENTRY_ATR = Decimal("0.15")
STOP_ATR = Decimal("0.35")
TARGET_R_MULTIPLIERS = (Decimal("1.5"), Decimal("2.5"), Decimal("4.0"))
ALLOWED_REGIMES = {"RANGE", "NO_EDGE", "TREND_UP", "TREND_DOWN", "BREAKOUT_UP", "BREAKOUT_DOWN"}


def _d(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _ts(row: Mapping[str, Any], key: str) -> Optional[dt.datetime]:
    value = row.get(key)
    if isinstance(value, dt.datetime):
        return value
    if value is None:
        return None
    out = dt.datetime.fromisoformat(str(value))
    if out.tzinfo is None:
        out = out.replace(tzinfo=dt.timezone.utc)
    return out.astimezone(dt.timezone.utc)


def _no_setup(reason: str, regime: str | None = None) -> dict[str, Any]:
    return {
        "setup_type": "cvd_divergence_reversal",
        "direction": "NO_SETUP",
        "entry_zone": None,
        "invalidation": None,
        "stop_loss": None,
        "targets": [],
        "confidence": 0.0,
        "reasons": [reason],
        "counterarguments": [],
        "regime_compatible": regime in ALLOWED_REGIMES if regime else False,
        "price_only_mode": False,
        "orderflow_confirmed": False,
    }


def _bucket_cvd(candles: list[dict], trades: list[dict]) -> list[Decimal]:
    """Return cumulative delta sampled at each 15m candle close."""
    if not candles:
        return []
    starts = [_ts(c, "open_time") for c in candles]
    if any(x is None for x in starts):
        return []
    first = starts[0]
    last_end = starts[-1] + dt.timedelta(minutes=15)
    usable = []
    for row in trades:
        t = _ts(row, "bucket_start") or _ts(row, "open_time")
        if t is None or t < first or t >= last_end:
            continue
        delta = _d(row.get("delta"))
        if delta is not None:
            usable.append((t, delta))
    usable.sort(key=lambda x: x[0])
    out: list[Decimal] = []
    running = Decimal("0")
    j = 0
    for start in starts:
        end = start + dt.timedelta(minutes=15)
        while j < len(usable) and usable[j][0] < end:
            running += usable[j][1]
            j += 1
        out.append(running)
    return out


def _pivots(candles: list[dict], mode: str) -> list[int]:
    values = [(_d(c.get("low")) if mode == "low" else _d(c.get("high"))) for c in candles]
    if any(v is None for v in values):
        return []
    out: list[int] = []
    for i in range(PIVOT_LEFT, len(values) - PIVOT_RIGHT):
        center = values[i]
        left = values[i-PIVOT_LEFT:i]
        right = values[i+1:i+1+PIVOT_RIGHT]
        if mode == "low" and center < min(left) and center <= min(right):
            out.append(i)
        if mode == "high" and center > max(left) and center >= max(right):
            out.append(i)
    return out


def detect_cvd_divergence_setup(chain: Mapping[str, Any]) -> dict[str, Any]:
    regime = str((chain.get("regime") or {}).get("primary_regime") or "UNKNOWN")
    if regime not in ALLOWED_REGIMES:
        return _no_setup(f"regime {regime} outside predeclared CVD-divergence scope", regime)

    windows = chain.get("_windows")
    if windows is None:
        return _no_setup("historical windows unavailable", regime)
    candles = list(getattr(windows, "candles_15m", []) or [])[-LOOKBACK_15M:]
    trades = list(getattr(windows, "trades_1m", []) or [])
    if len(candles) < 12 or not trades:
        return _no_setup("insufficient 15m price / 1m trade history for CVD divergence", regime)

    tf_1h = (((chain.get("features") or {}).get("tf") or {}).get("1h") or {})
    atr = _d(tf_1h.get("atr"))
    if atr is None or atr <= 0:
        return _no_setup("1h ATR unavailable", regime)

    cvd = _bucket_cvd(candles, trades)
    if len(cvd) != len(candles):
        return _no_setup("could not align cumulative delta to 15m candles", regime)

    candidates: list[dict[str, Any]] = []
    lows = _pivots(candles, "low")
    highs = _pivots(candles, "high")

    if len(lows) >= 2:
        i1, i2 = lows[-2], lows[-1]
        if i2 - i1 >= MIN_PIVOT_SEPARATION:
            p1, p2 = _d(candles[i1]["low"]), _d(candles[i2]["low"])
            if p2 <= p1 - atr * MIN_PRICE_DIVERGENCE_ATR and cvd[i2] > cvd[i1]:
                candidates.append({"direction": "LONG", "i1": i1, "i2": i2, "price1": p1, "price2": p2, "cvd1": cvd[i1], "cvd2": cvd[i2]})

    if len(highs) >= 2:
        i1, i2 = highs[-2], highs[-1]
        if i2 - i1 >= MIN_PIVOT_SEPARATION:
            p1, p2 = _d(candles[i1]["high"]), _d(candles[i2]["high"])
            if p2 >= p1 + atr * MIN_PRICE_DIVERGENCE_ATR and cvd[i2] < cvd[i1]:
                candidates.append({"direction": "SHORT", "i1": i1, "i2": i2, "price1": p1, "price2": p2, "cvd1": cvd[i1], "cvd2": cvd[i2]})

    if not candidates:
        return _no_setup("no confirmed regular price/CVD divergence in recent 15m swings", regime)

    # Latest second pivot wins; if both directions share the same latest pivot,
    # the reading is ambiguous and deliberately skipped.
    latest_i = max(c["i2"] for c in candidates)
    latest = [c for c in candidates if c["i2"] == latest_i]
    if len({c["direction"] for c in latest}) != 1:
        return _no_setup("simultaneous bullish/bearish divergence is ambiguous", regime)
    div = latest[-1]
    direction = div["direction"]
    last_close = _d(candles[-1].get("close"))
    if last_close is None:
        return _no_setup("latest 15m close unavailable", regime)

    # Require that price has not already run far away from the second pivot.
    pivot_price = div["price2"]
    if abs(last_close - pivot_price) > atr * Decimal("1.25"):
        return _no_setup("divergence already too far from its second pivot", regime)

    if direction == "LONG":
        zone_low = last_close - atr * ENTRY_ATR
        zone_high = last_close + atr * Decimal("0.05")
        stop = min(pivot_price - atr * STOP_ATR, zone_low - atr * Decimal("0.10"))
        worst = zone_high
        risk = worst - stop
        targets = [worst + risk * m for m in TARGET_R_MULTIPLIERS]
    else:
        zone_low = last_close - atr * Decimal("0.05")
        zone_high = last_close + atr * ENTRY_ATR
        stop = max(pivot_price + atr * STOP_ATR, zone_high + atr * Decimal("0.10"))
        worst = zone_low
        risk = stop - worst
        targets = [worst - risk * m for m in TARGET_R_MULTIPLIERS]

    if risk <= 0:
        return _no_setup("CVD divergence geometry produced non-positive risk", regime)

    return {
        "setup_type": "cvd_divergence_reversal",
        "direction": direction,
        "entry_zone": {"low": zone_low, "high": zone_high},
        "invalidation": stop,
        "stop_loss": stop,
        "targets": targets,
        "confidence": 0.55,
        "reasons": [
            f"regular {direction.lower()} price/CVD divergence across confirmed 15m pivots",
            f"price pivot moved {div['price1']} -> {div['price2']} while cumulative delta moved {div['cvd1']} -> {div['cvd2']}",
            "pivots require two closed 15m candles on the right, so divergence is confirmed using only data known by decision time",
        ],
        "counterarguments": [],
        "regime_compatible": True,
        "price_only_mode": False,
        "orderflow_confirmed": False,
        "detection_version": "CVD_REGULAR_DIVERGENCE_CONFIRMED_PIVOTS_V1",
        "pivot_1_index": div["i1"],
        "pivot_2_index": div["i2"],
    }


def evaluate_cvd_divergence(chain: Mapping[str, Any], *, btc_regime: str | None = None) -> dict[str, Any]:
    setup = detect_cvd_divergence_setup(chain)
    features = chain.get("features") or {}
    regime = chain.get("regime") or {}
    if setup.get("direction") not in {"LONG", "SHORT"}:
        return {"setup": setup, "confirmation": None, "risk": None, "signal": None}

    # The setup already uses CVD to generate the thesis. The standard W3
    # anti-double-counting mechanism notices "CVD" in setup reasons and does
    # not count that same bucket as an independent confirmation again.
    confirmation = ofc.confirm_setup(
        str(chain.get("symbol")), setup, regime,
        features.get("tf") or {}, features.get("volume") or {},
        features.get("orderbook") or {}, features.get("futures") or {},
        features.get("liquidations") or {}, btc_regime=btc_regime,
        data_quality=chain.get("data_quality") or {},
    )
    portfolio = {"open_positions": [], "daily_realized_pnl_pct": Decimal("0"), "weekly_realized_pnl_pct": Decimal("0"), "btc_regime_flip_detected": False}
    risk = rm.evaluate_risk(
        str(chain.get("symbol")), chain.get("as_of"), regime, setup, confirmation,
        features.get("orderbook") or {}, chain.get("data_quality") or {}, portfolio,
        rm.DEFAULT_RISK_CONFIG,
    )
    signal = None
    if risk.get("decision") in {"APPROVED", "APPROVED_REDUCED_SIZE"}:
        signal = NeutralSignal(
            variant=Variant.RESEARCH_V2,
            symbol=str(chain.get("symbol")),
            decision_time=chain.get("as_of"),
            setup_name="cvd_divergence_reversal",
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
