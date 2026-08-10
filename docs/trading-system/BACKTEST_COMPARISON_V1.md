# BACKTEST_COMPARISON_V1

Status: `CODE_READY_FOR_VPS_PREFLIGHT — PERFORMANCE_RESULTS_PENDING`

This research track compares the old Block 1–3 TradingView+Bybit engine with Research V2 without changing the live paper portfolio or production decisions.

## Validation status

GitHub Actions `Backtest Comparison CI` passes **52/52 tests** and validates all five research CLIs via `--help` on Python 3.11.

This proves code-level invariants only. It is **not** a trading-performance result. No strategy winner is declared until the scripts are run against the actual VPS Postgres/cache data.

## Variants

- `LEGACY_AS_IS` — current legacy parser behaviour preserved.
- `LEGACY_FIXED_TV` — same legacy engine with the current TradingView response schema interpreted correctly in a research-only adapter.
- `RESEARCH_V2` — current W1→W4 signal/risk chain.
- `RESEARCH_V2 + TV_SHADOW` — Research V2 decisions are unchanged; reconstructed classic-TA context only tags each existing signal for later outcome stratification.

## Two validation tiers

### Tier A — EXACT local overlap

`scripts/research/backtest_comparison_guarded_v1.py`

Uses only real sources retained in the VPS Postgres and fails closed unless all selected symbols have a common point-in-time window containing:

- 4h candles;
- 1h candles;
- 15m candles;
- 1m execution candles;
- 1m trade aggregates;
- 1m liquidation aggregates;
- orderbook snapshots;
- derivatives snapshots.

The runner additionally requires 50-bar candle warm-up, 10 trade-bucket warm-up and 12 hours of future 1m execution path after the last decision cutoff.

Tier A compares `LEGACY_AS_IS`, `LEGACY_FIXED_TV`, `RESEARCH_V2`, and Research V2 outcomes stratified by `TV_RECONSTRUCTED_CLASSIC_TA_V1`. The TV shadow cannot affect W4/W5.

### Tier B — EXTENDED degraded history

`scripts/research/backfill_extended_history_v1.py` and `scripts/research/backtest_comparison_extended_v1.py`.

The backfill uses official Bybit historical market endpoints/public trade archives and stores data only below `/app/artifacts/research/backtest_comparison/cache/`.

Safe default is 30 evaluation days + 10 warm-up days for BTC, ETH, SOL, XRP and BNB. `--all-symbols` is required deliberately for the full configured universe.

Extended history currently reconstructs:

- 1m/15m/1h/4h trade-price candles;
- mark/index price history;
- open interest;
- funding;
- long/short account ratio;
- public trades aggregated through the same 1m `TradeAggregator` logic used live;
- derivative snapshots using only values available at or before each OI timestamp.

It does **not** fabricate historical liquidations or orderbook snapshots. Therefore Tier B is explicitly labelled `DEGRADED_NO_HISTORICAL_ORDERBOOK` and `DEGRADED_NO_HISTORICAL_LIQUIDATIONS` and may not be used to declare a final legacy-vs-V2 winner.

The legacy Feature Engine requires orderbook as a primary data-quality source, so running legacy on an empty synthetic orderbook would systematically force NO_TRADE and create a false comparison. The loader forbids that interpretation instead of pretending missing data is neutral.

## Primary signal-quality execution contract

All generators in the exact comparison use the same neutral W5 adapter:

- reference capital: 10,000;
- fixed risk: 1.0% per signal;
- sizing from the worst edge of the entry zone;
- identical fees/slippage/funding model;
- entry requires an actual 1m touch of the entry zone;
- exits begin on the next 1m candle after the fill in the primary comparison;
- target fractions are normalised across targets that actually exist.

A separate `W5_PRODUCTION_AS_IS_SENSITIVITY` result preserves current W5 execution behaviour so execution quirks can be measured rather than hidden.

## No-look-ahead

At decision cutoff T:

- 4h candle is available only when `open_time + 4h <= T`;
- 1h candle only when `open_time + 1h <= T`;
- 15m candle only when `open_time + 15m <= T`;
- 1m trade/liquidation buckets only after their interval closed;
- orderbook/derivatives point snapshots require `source_timestamp <= T`;
- the 1m candle opening exactly at T belongs to execution future, not decision features.

For long 30/90-day runs `HistoricalWindowIndex` pre-indexes those availability instants and uses bisect lookup; tests confirm it matches the reference point-in-time selector.

## TradingView history contract

The project does not possess a true historical TradingView snapshot API. Historical TV-style information is explicitly labelled `TV_RECONSTRUCTED_CLASSIC_TA_V1` and must never be described as `TRUE_TRADINGVIEW_HISTORY`.

TV shadow evaluates four grouped dimensions: HTF structure (1h/4h), momentum (RSI + MACD), location (Bollinger) and volatility (ATR). Dimension groups are used instead of counting many correlated indicators as independent confirmations, and HTF is explicitly marked as overlapping with existing regime/setup logic.

Shadow statuses are `CONFIRMED`, `WEAK_CONFIRMATION`, `NEUTRAL`, `CONTRADICTED` and `INSUFFICIENT_DATA`.

The exact and extended runners predeclare two counterfactual reports: drop only `TV CONTRADICTED`, and keep only `TV CONFIRMED`/`WEAK_CONFIRMATION`. Those filters do not alter historical decisions; they stratify the same executed baseline outcomes.

## Known W5 production quirks discovered during review

These are reproduced/documented but intentionally not fixed in the production paper engine by this benchmark PR:

1. **Two-target residual** — production TP fractions are 50/30/20, while some setups have only two targets, potentially leaving 20% open after both.
2. **Entry fee omitted from realised PnL** — entry fee is recorded in `fees_paid` but is not included in the current `realized_pnl` settlement.
3. **Fill candle reused for exit** — current assembly can give the same 1m OHLC to entry and exit simulation; historical OHLC cannot prove intrabar order.
4. **Repeated partial fill overwrite** — a later partial fill replaces previous filled/remaining size rather than accumulating it.

The primary comparison uses corrected/normalised assumptions and also writes a production-as-is sensitivity result. Production W5 fixes belong in a separate PR after the current paper baseline is protected.

## Paired diagnostics

For every exact symbol/decision timestamp the runner records `BOTH_SIGNAL_SAME_DIRECTION`, `ONLY_LEGACY`, `ONLY_RESEARCH_V2`, `DIRECTION_CONFLICT` or `BOTH_NO_TRADE`.

Matched setup families are legacy `trend_pullback` ↔ Research V2 `trend_pullback`, and legacy `breakout_retest` ↔ Research V2 `breakout`. Research-only `mean_reversion` and `liquidation_reversal` are never forced into fake legacy equivalents.

## Automatic Markdown report

`scripts/research/render_backtest_comparison_report_v1.py` reads exact/extended JSON and writes `comparison_report.md`.

The renderer is fail-closed: smoke/degraded runs cannot be promoted to a strategy winner. Until a fuller risk-adjusted contract exists, it reports `NO_STATISTICALLY_MEANINGFUL_WINNER` rather than selecting on a single attractive metric.

## VPS commands — exact preflight

After checking out/building this research branch on the VPS:

```bash
python scripts/research/backtest_comparison_v1.py coverage
python scripts/research/backtest_comparison_v1.py audit-tv
python scripts/research/backtest_comparison_guarded_v1.py --auto-clamp --symbols BTCUSDT,ETHUSDT,SOLUSDT
python scripts/research/render_backtest_comparison_report_v1.py
```

The guarded runner writes only under `/app/artifacts/research/backtest_comparison/` and cannot declare a final winner from a small local smoke sample.

## VPS commands — extended research

Safe five-symbol 30-day backfill:

```bash
python scripts/research/backfill_extended_history_v1.py --days 30
python scripts/research/backtest_comparison_extended_v1.py
python scripts/research/render_backtest_comparison_report_v1.py
```

Later, only if the smoke run is clean:

```bash
python scripts/research/backfill_extended_history_v1.py --days 90 --all-symbols
python scripts/research/backtest_comparison_extended_v1.py
python scripts/research/render_backtest_comparison_report_v1.py
```

Do not interpret Tier B as an exact legacy comparison until real historical orderbook and liquidation adapters are validated.

## Remaining work before a final statistical verdict

1. Run exact local preflight on the VPS and inspect actual source overlap.
2. Run the safe 5-symbol extended backfill/replay and validate real Bybit cache shapes against the synthetic tests.
3. Only then expand to 90 days/full configured universe if storage and data availability are sane.
4. Generate the final comparison report from actual trades, including sample size, expectancy CI, PF and drawdown.
5. Keep the answer `NO_STATISTICALLY_MEANINGFUL_WINNER` when sample size or confidence intervals do not justify a stronger conclusion.
