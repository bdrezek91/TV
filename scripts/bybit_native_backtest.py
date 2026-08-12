"""Real-Bybit-data backtest of the 7 classic strategies (RSI, Bollinger,
MACD, EMA Cross, Supertrend, Donchian, Keltner Breakout), to find which
one actually performs best on the live system's 13 tracked symbols.

Why this exists: TV_connector's `backtest_strategy` / `compare_strategies`
/ `walk_forward_backtest_strategy` tools run on Yahoo Finance data, not
Bybit. This script fetches genuine historical klines directly from
Bybit's REST API and reuses the EXACT SAME strategy/backtest/walk-forward
logic from `core.services.backtest_service` (zero reimplementation) by
monkeypatching its internal `_fetch_ohlcv` to return real Bybit candles
instead of calling Yahoo Finance.

Skips `rsi_pullback` and `triple_ema` (need ~220-bar SMA200 warmup, which
at 1h resolution and a tractable REST call budget we don't comfortably
have room for here — same reason the Yahoo walk-forward tool needs
period='2y' for those two).

Runs BOTH a plain backtest and a walk-forward validation (out-of-sample)
for every strategy x symbol combination, so the result distinguishes a
real edge from curve-fitting -- exactly like the Yahoo-data research
earlier, just on real Bybit prices this time.

Must run on the VPS (only place with real Bybit network access):
    docker compose exec bybit-collector python /app/scripts/bybit_native_backtest.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys

from tradingview_mcp.core.market_data.bybit.client import BybitRestClient
from tradingview_mcp.core.services import backtest_service

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT", "NEARUSDT", "TRXUSDT",
]
STRATEGIES = ["rsi", "bollinger", "macd", "ema_cross", "supertrend", "donchian", "keltner_breakout"]
INTERVAL = "60"       # Bybit kline interval string for 1h
TARGET_BARS = 2200    # ~90 days of 1h candles - covers walk-forward's 3 folds comfortably


async def fetch_candles(client: BybitRestClient, symbol: str, interval: str, target: int) -> list[dict]:
    """Real Bybit REST klines, converted to backtest_service's expected
    candle dict shape: {date, open, high, low, close, volume}."""
    out: list[dict] = []
    end = None
    while len(out) < target:
        page_size = min(200, target - len(out))
        result = await client.get_kline(symbol, interval, category="linear", limit=page_size, end=end)
        rows = result.get("result", {}).get("list", [])
        if not rows:
            break
        for row in rows:
            ts = dt.datetime.fromtimestamp(int(row[0]) / 1000, tz=dt.timezone.utc)
            out.append({
                "date": ts.isoformat(),
                "open": float(row[1]), "high": float(row[2]), "low": float(row[3]), "close": float(row[4]),
                "volume": float(row[5]),
            })
        end = int(rows[-1][0]) - 1
        if len(rows) < page_size:
            break
    out.sort(key=lambda c: c["date"])
    return out


async def main() -> None:
    client = BybitRestClient(testnet=False)
    cache: dict[str, list[dict]] = {}

    for symbol in SYMBOLS:
        print(f"fetching {symbol}...", file=sys.stderr)
        candles = await fetch_candles(client, symbol, INTERVAL, TARGET_BARS)
        cache[symbol] = candles
        print(f"  got {len(candles)} real Bybit 1h candles "
              f"({candles[0]['date'] if candles else '?'} -> {candles[-1]['date'] if candles else '?'})",
              file=sys.stderr)

    def _fake_fetch_ohlcv(symbol: str, period: str, interval: str = "1d") -> list[dict]:
        return cache.get(symbol.upper().replace("-USD", "USDT"), [])

    backtest_service._fetch_ohlcv = _fake_fetch_ohlcv  # monkeypatch: real Bybit data in, everything else unchanged

    plain_results: dict[str, dict] = {}
    wf_results: dict[str, dict] = {}

    for symbol in SYMBOLS:
        yahoo_style_symbol = symbol  # _fake_fetch_ohlcv maps it back via cache lookup regardless
        plain_results[symbol] = {}
        wf_results[symbol] = {}
        for strategy in STRATEGIES:
            r = backtest_service.run_backtest(yahoo_style_symbol, strategy, period="1y", interval="1h")
            plain_results[symbol][strategy] = r
            wf = backtest_service.walk_forward_backtest(yahoo_style_symbol, strategy, period="2y", interval="1h", n_splits=3)
            wf_results[symbol][strategy] = wf
        print(f"backtested {symbol}", file=sys.stderr)

    # Aggregate: average robustness_score per strategy across all symbols
    # where a verdict was produced (skip error/no-data cases).
    agg: dict[str, list[float]] = {s: [] for s in STRATEGIES}
    for symbol, by_strategy in wf_results.items():
        for strategy, r in by_strategy.items():
            score = r.get("robustness_score")
            if isinstance(score, (int, float)):
                agg[strategy].append(score)

    leaderboard = sorted(
        (
            {
                "strategy": s,
                "avg_robustness_score": round(sum(v) / len(v), 3) if v else None,
                "symbols_tested": len(v),
                "robust_count": sum(1 for x in v if x >= 0.8),
                "overfitted_count": sum(1 for x in v if x < 0.2),
            }
            for s, v in agg.items()
        ),
        key=lambda x: (x["avg_robustness_score"] is None, -(x["avg_robustness_score"] or -999)),
    )

    print(json.dumps({
        "data_source": "Bybit REST API (real historical klines)",
        "interval": "1h",
        "bars_per_symbol": TARGET_BARS,
        "leaderboard_by_avg_robustness": leaderboard,
        "walk_forward_detail": wf_results,
        "plain_backtest_detail": plain_results,
    }, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
