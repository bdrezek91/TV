"""Standalone, real-Bybit-data backtest of the live Breakout+Retest strategy.

Why this exists: TV_connector's backtest tools (`backtest_strategy`,
`walk_forward_backtest_strategy`) run on Yahoo Finance data, not Bybit.
This script fetches genuine historical klines directly from Bybit's REST
API and replays the ACTUAL production `breakout_retest` strategy code
(not a generic canned strategy) against them, using the project's own
`ReplayEngine` (no look-ahead, real paper-broker fill logic).

Scope/honesty notes (read before trusting the numbers):

- Only `breakout_retest` is tested here. `trend_pullback` structurally
  requires TradingView's 4h/1h trend context
  (`tradingview_context.py`/`trend_pullback.py` both hard-`return None`
  without it), and there's no source of *historical* TradingView trend
  reads for arbitrary past dates — so trend_pullback cannot be
  backtested this way, only paper-traded forward from now.
- Uses 1h candles (not the live system's 15m) to keep the number of
  Bybit REST calls tractable within a single run. The strategy code
  itself is timeframe-agnostic (just operates on whatever candle list
  it's given), so this is a legitimate test at a coarser resolution, not
  a different strategy.
- Volume (CVD/z-score) and open-interest features are marked
  unavailable, since months of that history don't exist yet (the
  collector only started recently) — `breakout_retest` degrades
  gracefully without them (lower score components, not a blocked
  setup), so this tests the strategy's core price-action/structure
  logic specifically, not the full live feature fusion.
- Regime is classified from price action alone (no order-book spread
  history available historically either).

Run this ON THE VPS (only place with real Bybit network access):
    docker compose exec bybit-collector python /app/scripts/bybit_native_backtest.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
from decimal import Decimal

from tradingview_mcp.core.analysis.feature_engine import compute_price_action_features
from tradingview_mcp.core.analysis.regime_classifier import classify_regime
from tradingview_mcp.core.analysis.strategies import breakout_retest
from tradingview_mcp.core.analysis.tradingview_context import TradingViewContext
from tradingview_mcp.core.backtest.replay_engine import ReplayBar, ReplayEngine, ReplaySignal
from tradingview_mcp.core.market_data.bybit.client import BybitRestClient

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT", "NEARUSDT", "TRXUSDT",
]
INTERVAL = "60"          # 1h candles — see module docstring for why
TARGET_BARS = 2880       # ~120 days of 1h candles
WARMUP_BARS = 250        # min history before the strategy is even evaluated
STEP = 4                 # evaluate every 4th bar (~hourly cadence is already every bar; this just caps runtime)
FEE_RATE = Decimal("0.00055")
SLIPPAGE = Decimal("0")  # no slippage model applied here (kept simple/transparent)
RISK_PCT_OF_PRICE = Decimal("1")  # nominal 1-unit sizing; only ratios (win rate, R-multiple) matter, not absolute PnL


async def fetch_candles(client: BybitRestClient, symbol: str, interval: str, target: int) -> list[dict]:
    out: list[dict] = []
    end = None
    while len(out) < target:
        page_size = min(200, target - len(out))
        result = await client.get_kline(symbol, interval, category="linear", limit=page_size, end=end)
        rows = result.get("result", {}).get("list", [])
        if not rows:
            break
        for row in rows:
            out.append({
                "open_time": dt.datetime.fromtimestamp(int(row[0]) / 1000, tz=dt.timezone.utc),
                "open": row[1], "high": row[2], "low": row[3], "close": row[4],
                "volume": row[5], "turnover": row[6] if len(row) > 6 else 0,
            })
        end = int(rows[-1][0]) - 1
        if len(rows) < page_size:
            break
    out.sort(key=lambda c: c["open_time"])
    return out


def build_snapshot(window: list[dict]) -> dict:
    price_action = compute_price_action_features(window)
    return {
        "price_action": price_action,
        "volume": {"available": False},
        "orderbook": {},
        "futures": {"available": False},
        "liquidations": {"available": False},
        "is_tradeable": True,
    }


DUMMY_TV = TradingViewContext(symbol="", available=False)


def run_symbol(symbol: str, candles: list[dict]) -> dict:
    bars = [
        ReplayBar(
            timestamp=c["open_time"],
            open=Decimal(str(c["open"])), high=Decimal(str(c["high"])),
            low=Decimal(str(c["low"])), close=Decimal(str(c["close"])),
        )
        for c in candles
    ]
    signals: list[ReplaySignal] = []

    for i in range(WARMUP_BARS, len(candles) - 1, STEP):
        window = candles[max(0, i - WARMUP_BARS): i + 1]  # no look-ahead: only bars up to and including i
        snapshot = build_snapshot(window)
        if not snapshot["price_action"]["available"]:
            continue
        regime = classify_regime(snapshot)["regime"]
        setup = breakout_retest.evaluate(snapshot, DUMMY_TV, regime)
        if setup is None:
            continue
        signals.append(ReplaySignal(
            bar_index=i,
            symbol=symbol,
            setup_name=setup.setup_name,
            direction=setup.direction,
            market_regime=regime,
            entry_price=setup.entry_reference,
            stop_loss=setup.stop_loss,
            take_profit_1=setup.take_profit_1,
            take_profit_2=setup.take_profit_2,
            expires_at=bars[i].timestamp + dt.timedelta(hours=48),
            quantity=RISK_PCT_OF_PRICE / max(abs(setup.entry_reference - setup.stop_loss), Decimal("0.0001")),
        ))

    engine = ReplayEngine(bars, fee_rate=FEE_RATE, slippage=SLIPPAGE)
    report = engine.run(signals)
    return report.summary(None)


async def main() -> None:
    client = BybitRestClient(testnet=False)
    all_results = {}
    combined_trades = 0
    combined_wins = 0

    for symbol in SYMBOLS:
        print(f"fetching {symbol}...", file=sys.stderr)
        candles = await fetch_candles(client, symbol, INTERVAL, TARGET_BARS)
        if len(candles) < WARMUP_BARS + 10:
            all_results[symbol] = {"error": f"insufficient candles fetched ({len(candles)})"}
            continue
        summary = run_symbol(symbol, candles)
        all_results[symbol] = summary
        if summary.get("trades"):
            combined_trades += summary["trades"]
            combined_wins += round(float(summary["win_rate"]) * summary["trades"])
        print(f"  {symbol}: {summary.get('trades', 0)} trades, "
              f"win_rate={summary.get('win_rate')}, expectancy_r={summary.get('expectancy_r')}", file=sys.stderr)

    print(json.dumps({
        "interval": INTERVAL,
        "target_bars_per_symbol": TARGET_BARS,
        "results": all_results,
        "combined_total_trades": combined_trades,
        "combined_win_rate": round(combined_wins / combined_trades, 4) if combined_trades else None,
    }, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
