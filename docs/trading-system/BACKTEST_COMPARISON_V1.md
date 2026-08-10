# BACKTEST_COMPARISON_V1

Status: `IMPLEMENTATION_IN_PROGRESS — LOCAL_SMOKE_PENDING_VPS_DATA`

This research track compares the old Block 1–3 TradingView+Bybit engine with
Research V2 without changing the live paper portfolio or production decisions.

## Variants

- `LEGACY_AS_IS` — current legacy parser behaviour preserved.
- `LEGACY_FIXED_TV` — same legacy engine with the current TradingView response
  schema interpreted correctly in a research-only adapter.
- `RESEARCH_V2` — current W1→W4 signal/risk chain.
- `RESEARCH_V2_TV` — reserved for the external-TV shadow-confirmation variant;
  not reported until a point-in-time/reconstructed confirmation adapter exists.

## Primary signal-quality execution contract

All signal generators are evaluated with the same neutral W5 adapter:

- reference capital: 10,000;
- fixed risk: 1.0% of capital per signal;
- position sizing from the worst entry-zone edge, matching W4's conservative
  sizing basis;
- identical fees/slippage/funding model;
- entry requires an actual 1m candle touch of the entry zone;
- `NEXT_CANDLE` exit policy after fill, because OHLC cannot prove whether an
  SL/TP touch inside the fill candle happened before or after the fill;
- target fractions are normalised across the targets that actually exist.

The production behaviour remains available as a sensitivity/diagnostic mode;
it is not silently rewritten.

## No-look-ahead

At decision cutoff T:

- 4h candle is available only when `open_time + 4h <= T`;
- 1h candle only when `open_time + 1h <= T`;
- 15m candle only when `open_time + 15m <= T`;
- 1m trade/liquidation buckets only after their minute closed;
- orderbook and derivatives point snapshots require `source_timestamp <= T`;
- the 1m candle opening exactly at T belongs to the future execution path, not
  to the decision feature window.

TradingView historical context is explicitly labelled `TV_RECONSTRUCTED`; the
project does not claim to possess true point-in-time TradingView snapshots.

## Known W5 production quirks found during benchmark review

These are documented/reproduced but are **not** fixed in production by this PR.
Changing production W5 while benchmarking it would contaminate the baseline.

1. **Two-target residual position** — default W5 TP fractions are 50/30/20,
   while some Research V2 setups have only two targets. Hitting both can leave
   20% open. The primary comparison rescales the available 50:30 weights to
   62.5:37.5; `PRODUCTION_AS_IS` remains available for sensitivity testing.
2. **Entry fee omitted from realised PnL/balance** — entry fee is recorded in
   `fees_paid`, but current `realized_pnl` starts at zero and only exit fees are
   deducted later. Comparison output therefore stores both production PnL and
   corrected net PnL.
3. **Fill-candle reused for exit simulation** — current assembly can feed the
   same 1m OHLC window to `simulate_entry` and then `simulate_exit`. The primary
   historical comparison starts exits from the next candle.
4. **Repeated partial fill overwrite** — a later partial entry call replaces
   `filled_size`/`remaining_size` instead of accumulating the previous partial
   fill. A regression reproducer documents current behaviour.

These findings should be addressed in a separate production-fix PR after the
benchmark baseline and current paper state are protected.

## Paired diagnostics

The research code now has explicit comparison categories for the same
symbol/decision timestamp:

- `BOTH_SIGNAL_SAME_DIRECTION`
- `ONLY_LEGACY`
- `ONLY_RESEARCH_V2`
- `DIRECTION_CONFLICT`
- `BOTH_NO_TRADE`

Matched setup families are also kept separate:

- legacy `trend_pullback` ↔ Research V2 `trend_pullback`
- legacy `breakout_retest` ↔ Research V2 `breakout`

`mean_reversion` and `liquidation_reversal` are not forced into fake legacy
counterparts.

## Local Postgres smoke test

The read-only smoke runner is:

```bash
python scripts/research/backtest_comparison_v1.py coverage
python scripts/research/backtest_comparison_v1.py audit-tv
python scripts/research/backtest_comparison_v1.py smoke \
  --start 2026-08-09T00:00:00+00:00 \
  --end 2026-08-10T12:00:00+00:00 \
  --symbols BTCUSDT,ETHUSDT,SOLUSDT
```

Outputs are written only under:

`/app/artifacts/research/backtest_comparison/`

The smoke result is always tagged `SMOKE_LOCAL_OVERLAP` and is not permitted to
produce a final statistical winner.

## Before final 30/90-day benchmark

Still required:

- wire the strict coverage validator and paired categories into the smoke JSON;
- production-as-is execution sensitivity report;
- native full-system/portfolio simulation separate from fixed-risk alpha test;
- extended Bybit history cache with explicit degradation when liquidation or
  orderbook history is unavailable;
- `RESEARCH_V2_TV` historical/shadow adapter;
- final report with sample-size labels and bootstrap confidence intervals.
