"""Frozen development contract for V15 BTC-neutral residual reversion.

V15 is deliberately outside the V3-V14 family of single-leg directional
setups.  It trades an altcoin residual relative to BTC with two synchronized
legs.  Every feature is computed from candles closed by the decision time.

This module is research-only and rejects the reserved MR V2 holdout path.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from tradingview_mcp.core.backtest.comparison_v1 import TradeObservation, Variant

V15_NAME = "V15_BTC_NEUTRAL_RESIDUAL_REVERSION"
V15_SETUP = "btc_neutral_residual_reversion"
BTC_SYMBOL = "BTCUSDT"
ALT_SYMBOLS = ("ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT")
RESERVED_HOLDOUT_MARKERS = ("holdout_mrv2_120d",)

LOOKBACK_HOURS = 72
ENTRY_Z = Decimal("2.0")
EXIT_Z = Decimal("0.5")
STOP_Z = Decimal("3.5")
MAX_HOLD_HOURS = 24
CAPITAL = Decimal("10000")
PAIR_RISK_PCT = Decimal("1.0")
GROSS_EXPOSURE_PCT = Decimal("100")
TAKER_FEE_RATE_PCT = Decimal("0.055")
SLIPPAGE_BPS_EACH_SIDE = Decimal("2")
CONSERVATIVE_FUNDING_BPS_PER_8H = Decimal("1")


def validate_v15_development_cache_root(cache_root: Path) -> Path:
    """Reject every holdout reserved for another frozen hypothesis."""
    resolved = cache_root.resolve()
    lowered = str(resolved).lower()
    marker = next((item for item in RESERVED_HOLDOUT_MARKERS if item in lowered), None)
    if marker:
        raise ValueError(
            f"V15 development runner refuses reserved holdout cache: {marker}"
        )
    return resolved


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _closed_hourly(
    candles: Sequence[Mapping[str, Any]], cutoff: dt.datetime
) -> dict[dt.datetime, Decimal]:
    if cutoff.tzinfo is None:
        raise ValueError("cutoff must be timezone-aware")
    result: dict[dt.datetime, Decimal] = {}
    for candle in candles:
        opened = candle.get("open_time")
        if not isinstance(opened, dt.datetime) or opened.tzinfo is None:
            raise ValueError(
                "hourly candle timestamps must be timezone-aware datetimes"
            )
        if opened + dt.timedelta(hours=1) <= cutoff:
            close = _d(candle["close"])
            if close <= 0:
                raise ValueError("hourly close must be positive")
            result[opened] = close
    return result


def _aligned_prices(
    alt_candles: Sequence[Mapping[str, Any]],
    btc_candles: Sequence[Mapping[str, Any]],
    cutoff: dt.datetime,
) -> tuple[list[Decimal], list[Decimal]]:
    alt = _closed_hourly(alt_candles, cutoff)
    btc = _closed_hourly(btc_candles, cutoff)
    times = sorted(set(alt) & set(btc))[-(LOOKBACK_HOURS + 1) :]
    return [alt[t] for t in times], [btc[t] for t in times]


def _log_returns(prices: Sequence[Decimal]) -> list[float]:
    return [math.log(float(right / left)) for left, right in zip(prices, prices[1:])]


def detect_v15_pair_signal(
    alt_symbol: str,
    alt_candles_1h: Sequence[Mapping[str, Any]],
    btc_candles_1h: Sequence[Mapping[str, Any]],
    cutoff: dt.datetime,
) -> dict[str, Any]:
    """Return the frozen V15 decision using only closed, aligned 1h candles."""
    symbol = alt_symbol.upper()
    if symbol not in ALT_SYMBOLS:
        raise ValueError(f"unsupported V15 alt symbol: {symbol}")

    alt, btc = _aligned_prices(alt_candles_1h, btc_candles_1h, cutoff)
    if len(alt) < LOOKBACK_HOURS + 1:
        return {
            "candidate": V15_NAME,
            "eligible": False,
            "reason": "insufficient aligned closed 1h history",
        }

    alt_returns = _log_returns(alt)
    btc_returns = _log_returns(btc)
    btc_variance = statistics.pvariance(btc_returns)
    if btc_variance <= 0:
        return {
            "candidate": V15_NAME,
            "eligible": False,
            "reason": "BTC return variance is zero",
        }

    alt_mean = statistics.fmean(alt_returns)
    btc_mean = statistics.fmean(btc_returns)
    covariance = statistics.fmean(
        (a - alt_mean) * (b - btc_mean) for a, b in zip(alt_returns, btc_returns)
    )
    beta = covariance / btc_variance
    if not math.isfinite(beta) or beta <= 0:
        return {
            "candidate": V15_NAME,
            "eligible": False,
            "reason": "non-positive or invalid rolling beta",
        }

    spreads = [math.log(float(a)) - beta * math.log(float(b)) for a, b in zip(alt, btc)]
    spread_mean = statistics.fmean(spreads)
    spread_std = statistics.pstdev(spreads)
    if spread_std <= 0:
        return {
            "candidate": V15_NAME,
            "eligible": False,
            "reason": "residual spread variance is zero",
        }
    zscore = (spreads[-1] - spread_mean) / spread_std

    if zscore >= float(ENTRY_Z):
        pair_direction = "SHORT_ALT_LONG_BTC"
        alt_direction, btc_direction = "SHORT", "LONG"
    elif zscore <= -float(ENTRY_Z):
        pair_direction = "LONG_ALT_SHORT_BTC"
        alt_direction, btc_direction = "LONG", "SHORT"
    else:
        return {
            "candidate": V15_NAME,
            "eligible": False,
            "reason": "absolute residual z-score below frozen entry threshold",
            "zscore": zscore,
            "beta": beta,
        }

    return {
        "candidate": V15_NAME,
        "eligible": True,
        "decision_time": cutoff,
        "alt_symbol": symbol,
        "btc_symbol": BTC_SYMBOL,
        "pair_direction": pair_direction,
        "alt_direction": alt_direction,
        "btc_direction": btc_direction,
        "beta": beta,
        "spread_mean": spread_mean,
        "spread_std": spread_std,
        "zscore": zscore,
        "lookback_hours": LOOKBACK_HOURS,
        "future_data_used": False,
    }


@dataclass(frozen=True)
class PairExecutionResult:
    signal: Mapping[str, Any]
    observation: TradeObservation
    exit_time: dt.datetime | None
    exit_reason: str
    gross_pnl: Decimal
    net_pnl: Decimal
    fees: Decimal
    slippage: Decimal
    funding: Decimal


def _aligned_future(
    alt_candles: Sequence[Mapping[str, Any]],
    btc_candles: Sequence[Mapping[str, Any]],
    decision_time: dt.datetime,
) -> list[tuple[dt.datetime, Decimal, Decimal]]:
    def rows(
        candles: Sequence[Mapping[str, Any]],
    ) -> dict[dt.datetime, Mapping[str, Any]]:
        return {
            row["open_time"]: row
            for row in candles
            if isinstance(row.get("open_time"), dt.datetime)
            and row["open_time"] >= decision_time
        }

    alt, btc = rows(alt_candles), rows(btc_candles)
    timestamps = sorted(set(alt) & set(btc))
    return [(at, _d(alt[at]["open"]), _d(btc[at]["open"])) for at in timestamps]


def _signed_pnl(
    direction: str, entry: Decimal, exit_price: Decimal, quantity: Decimal
) -> Decimal:
    sign = Decimal("1") if direction == "LONG" else Decimal("-1")
    return sign * (exit_price - entry) * quantity


def _round_trip_costs(
    entry_notional: Decimal,
    exit_notional: Decimal,
    gross_notional: Decimal,
    held: dt.timedelta,
) -> tuple[Decimal, Decimal, Decimal]:
    fees = (entry_notional + exit_notional) * TAKER_FEE_RATE_PCT / Decimal("100")
    slippage = (
        (entry_notional + exit_notional) * SLIPPAGE_BPS_EACH_SIDE / Decimal("10000")
    )
    funding_intervals = int(
        held.total_seconds() // dt.timedelta(hours=8).total_seconds()
    )
    funding = (
        gross_notional
        * CONSERVATIVE_FUNDING_BPS_PER_8H
        / Decimal("10000")
        * funding_intervals
    )
    return fees, slippage, funding


def simulate_v15_pair(
    signal: Mapping[str, Any],
    alt_candles_1m: Sequence[Mapping[str, Any]],
    btc_candles_1m: Sequence[Mapping[str, Any]],
) -> PairExecutionResult:
    """Simulate synchronized market entry/exit and charge both legs.

    The rolling beta/mean/std are frozen at entry.  Exits are evaluated from
    common minute opens, avoiding asynchronous or intrabar look-ahead.
    """
    if not signal.get("eligible"):
        raise ValueError("cannot simulate an ineligible V15 signal")
    decision_time = signal["decision_time"]
    future = _aligned_future(alt_candles_1m, btc_candles_1m, decision_time)
    risk_amount = CAPITAL * PAIR_RISK_PCT / Decimal("100")
    if not future:
        observation = TradeObservation(
            variant=Variant.RESEARCH_V2,
            symbol=f"{signal['alt_symbol']}/{BTC_SYMBOL}",
            decision_time=decision_time,
            setup_name=V15_SETUP,
            direction=signal["pair_direction"],
            filled=False,
            r_multiple=None,
        )
        return PairExecutionResult(
            signal, observation, None, "NO_SYNCHRONIZED_ENTRY", *(Decimal("0"),) * 5
        )

    entry_time, alt_entry, btc_entry = future[0]
    if alt_entry <= 0 or btc_entry <= 0:
        raise ValueError("pair entry prices must be positive")
    beta = _d(signal["beta"])
    gross_notional = CAPITAL * GROSS_EXPOSURE_PCT / Decimal("100")
    alt_notional = gross_notional / (Decimal("1") + beta)
    btc_notional = gross_notional - alt_notional
    alt_qty, btc_qty = alt_notional / alt_entry, btc_notional / btc_entry
    entry_notional = alt_entry * alt_qty + btc_entry * btc_qty

    exit_time, alt_exit, btc_exit = future[-1]
    exit_reason = "MAX_HOLD"
    horizon = entry_time + dt.timedelta(hours=MAX_HOLD_HOURS)
    terminal = False
    for at, alt_price, btc_price in future[1:]:
        if alt_price <= 0 or btc_price <= 0:
            raise ValueError("pair execution prices must be positive")
        spread = math.log(float(alt_price)) - float(beta) * math.log(float(btc_price))
        zscore = (spread - float(signal["spread_mean"])) / float(signal["spread_std"])
        gross = _signed_pnl(signal["alt_direction"], alt_entry, alt_price, alt_qty)
        gross += _signed_pnl(signal["btc_direction"], btc_entry, btc_price, btc_qty)
        current_notional = alt_price * alt_qty + btc_price * btc_qty
        current_costs = _round_trip_costs(
            entry_notional, current_notional, gross_notional, at - entry_time
        )
        current_net = gross - sum(current_costs, Decimal("0"))
        if abs(zscore) <= float(EXIT_Z):
            exit_time, alt_exit, btc_exit, exit_reason = (
                at,
                alt_price,
                btc_price,
                "RESIDUAL_REVERTED",
            )
            terminal = True
            break
        if abs(zscore) >= float(STOP_Z) or current_net <= -risk_amount:
            exit_time, alt_exit, btc_exit, exit_reason = (
                at,
                alt_price,
                btc_price,
                "PAIR_STOP",
            )
            terminal = True
            break
        if at >= horizon:
            exit_time, alt_exit, btc_exit, exit_reason = (
                at,
                alt_price,
                btc_price,
                "MAX_HOLD",
            )
            terminal = True
            break

    if not terminal:
        raise ValueError(
            "insufficient synchronized future path for frozen 24h execution horizon"
        )

    gross_pnl = _signed_pnl(signal["alt_direction"], alt_entry, alt_exit, alt_qty)
    gross_pnl += _signed_pnl(signal["btc_direction"], btc_entry, btc_exit, btc_qty)
    exit_notional = alt_exit * alt_qty + btc_exit * btc_qty
    held = max(dt.timedelta(0), exit_time - entry_time)
    fees, slippage, funding = _round_trip_costs(
        entry_notional, exit_notional, gross_notional, held
    )
    net_pnl = gross_pnl - fees - slippage - funding
    observation = TradeObservation(
        variant=Variant.RESEARCH_V2,
        symbol=f"{signal['alt_symbol']}/{BTC_SYMBOL}",
        decision_time=decision_time,
        setup_name=V15_SETUP,
        direction=signal["pair_direction"],
        filled=True,
        r_multiple=net_pnl / risk_amount,
        pnl_net=net_pnl,
        fees=fees,
        slippage=slippage,
        funding=funding,
    )
    return PairExecutionResult(
        signal=signal,
        observation=observation,
        exit_time=exit_time,
        exit_reason=exit_reason,
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        fees=fees,
        slippage=slippage,
        funding=funding,
    )
