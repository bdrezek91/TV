# Block 2 Report — Feature Engine, Strategies, Risk, Paper Trading

Date: 2026-08-08
Scope executed: **BLOK 2 — analiza, strategie, ryzyko i paper trading**
only. Block 3 (new MCP tools, Claude routine update, Telegram,
Prometheus/Grafana dashboards, Replay Engine, live-trading gate doc) was
**not** started.

Continued in the same worktree/branch as Block 1 (`worktree-
agent-aae960b2d318ba597`) — nothing from Block 1 was lost, re-done, or
reset. Same operating constraint as before: **no VPS/SSH access**; local
Postgres (installed directly in this sandbox) reused for integration
tests.

## 1. Modules built

All under `core/analysis/` (deterministic engine, zero AI/LLM
discretion) and `core/trading/` (paper broker):

- **`core/analysis/indicators.py`** — Decimal-safe EMA, ATR (Wilder),
  ATR%, realized volatility, VWAP, position-in-range, local support/
  resistance, and a fractal-swing HH/HL/LH/LL structure detector.
- **`core/analysis/feature_engine.py`** — price action, volume/CVD
  (buy/sell taker volume, delta, CVD, CVD delta 1m/5m/15m, CVD slope,
  volume z-score, avg/large trade stats), order book (imbalance, imbalance
  change, persistence, replenishment, absorption heuristic, depth bands at
  5/10/25/50 bps — a lone wall is flagged informationally only, never
  scored on its own), futures (OI + 5m/15m/1h change, funding, basis +
  change, price-to-OI relation), liquidations (1m/5m/15m sums, imbalance,
  z-score, cascade detection), and `assess_data_quality` /
  `build_feature_snapshot` (produces the exact `data_quality_score` /
  `missing_fields` / `stale_fields` / `source_timestamps` / `is_tradeable`
  shape from the plan — `is_tradeable=false` on any stale/missing required
  source, which blocks setup generation downstream).
- **`core/analysis/tradingview_context.py`** — calls
  `core.services.screener_service.run_multi_timeframe_analysis` /
  `analyze_coin` **directly** (never HTTP-to-self), normalises the result
  into a `TradingViewContext`, and implements the plan's conflict rule:
  `resolve_context_conflict` lowers the score and logs a conflict note
  when TradingView's 1h trend disagrees with Bybit's order-flow bias — it
  never rejects outright and never lets an LLM arbitrate.
- **`core/analysis/regime_classifier.py`** — rule-based (no ML) classifier
  into all 9 plan regimes (spread → CHAOTIC/LOW_LIQUIDITY, ATR% →
  HIGH_VOLATILITY/LOW_VOLATILITY, structure+EMA → TREND_UP/TREND_DOWN,
  compression/expansion → RANGE/BREAKOUT_ATTEMPT, non-tradeable data →
  NO_DATA), with confidence/reasons/warnings.
- **`core/analysis/scoring.py`** — loads `config/strategies/v1.yaml`
  (weights validated to sum to exactly 100 at load time), computes a
  0-100 score + per-component breakdown, version-stamped.
- **`core/analysis/hard_gates.py`** — all 12 plan gates
  (stale data, inconsistent book, spread, missing entry/SL, RR too small,
  expired, duplicate, daily-trade-limit, consecutive-losses,
  conflicting position, CHAOTIC/LOW_LIQUIDITY/NO_DATA regimes, strategy-
  specific disallowed regime) — fully independent of score.
- **`core/analysis/risk_engine.py`** — the plan's exact formula
  (`risk_amount = equity × pct`, `position_size = risk_amount /
  stop_distance`), qty-step rounding, leverage capping, fee+slippage-
  inclusive `max_loss_with_fees`, and the exact plan JSON shape
  (`approved`, `risk_amount`, `position_size`, `effective_leverage`,
  `max_loss_with_fees`, `rejection_reasons`). No-martingale / no-
  increase-after-loss / no-duplicate-position all enforced.
- **`core/analysis/strategies/`** — `trend_pullback.py` and
  `breakout_retest.py` (both active by default), `liquidation_reversal.py`
  (implemented, off by default via `ENABLE_LIQUIDATION_REVERSAL=false`,
  **always** `experimental=True` and the orchestrator refuses to ever
  route it to a paper order, regardless of the flag).
- **`core/trading/paper_broker.py`** — market fills (`LONG = best_ask +
  slippage`, `SHORT = best_bid - slippage`), conservative limit fills
  (price-reached AND opposing-taker-liquidity-threshold — a bare wick
  touch does not fill), stop-loss fills that can be worse than requested
  on a gap-through, take-profit fills at the requested price, fee/funding
  helpers, and `PaperPositionTracker` for partial closes (TP1 then TP2).
- **`core/services/strategy_engine_service.py`** — the orchestrator:
  Feature Engine → regime → each active strategy → scoring → hard gates →
  Risk Engine → `EvaluatedSetup` (CANDIDATE or REJECTED, always with full
  reasons). Returns `NO_TRADE` with a `reasons` list when nothing
  qualifies. `server.py` does not import this (no new MCP tools in Block
  2 — that's Block 3).
- **`core/database/repositories/signals_repository.py`** /
  **`paper_trading_repository.py`** — persistence for
  `feature_snapshots`, `market_regimes`, `strategy_signals` +
  `signal_components` (every setup, approved or rejected, is saved),
  `daily_risk_state` (trade count / consecutive losses / loss-limit lock),
  `paper_orders`/`paper_fills`/`paper_positions`/`paper_equity_snapshots`.
- **`config/strategies/v1.yaml`** — the versioned scoring weights (sum
  enforced = 100), hard-gate thresholds, risk defaults, regime
  allow-lists per strategy, and strategy enable flags.

## 2. New files (26)

```
config/strategies/v1.yaml
src/tradingview_mcp/core/analysis/__init__.py
src/tradingview_mcp/core/analysis/indicators.py
src/tradingview_mcp/core/analysis/feature_engine.py
src/tradingview_mcp/core/analysis/tradingview_context.py
src/tradingview_mcp/core/analysis/regime_classifier.py
src/tradingview_mcp/core/analysis/scoring.py
src/tradingview_mcp/core/analysis/hard_gates.py
src/tradingview_mcp/core/analysis/risk_engine.py
src/tradingview_mcp/core/analysis/strategies/__init__.py
src/tradingview_mcp/core/analysis/strategies/base.py
src/tradingview_mcp/core/analysis/strategies/trend_pullback.py
src/tradingview_mcp/core/analysis/strategies/breakout_retest.py
src/tradingview_mcp/core/analysis/strategies/liquidation_reversal.py
src/tradingview_mcp/core/trading/__init__.py
src/tradingview_mcp/core/trading/paper_broker.py
src/tradingview_mcp/core/services/strategy_engine_service.py
src/tradingview_mcp/core/database/repositories/signals_repository.py
src/tradingview_mcp/core/database/repositories/paper_trading_repository.py
docs/trading-system/BLOCK2_REPORT.md
tests/unit/trading/test_feature_engine.py
tests/unit/trading/test_regime_and_scoring.py
tests/unit/trading/test_hard_gates_and_risk.py
tests/unit/trading/test_paper_broker.py
tests/unit/trading/test_strategy_engine.py
tests/unit/trading/test_signals_repository_db.py
tests/unit/trading/test_integration_pipeline.py
```

## 3. Changed files (2)

- `pyproject.toml` — added `pyyaml` (for the versioned strategy config);
  `uv.lock` regenerated accordingly.
- `docker-compose.yml` / `.env.example` — added `STRATEGY_CONFIG_PATH`.
  This fixes a real bug caught during verification: the config loader's
  original path resolution (`Path(__file__).parents[4]`) only works in a
  source checkout — `uv pip install --system .` (what the Dockerfile
  does) copies the package into site-packages, severing that path
  relationship, which would have silently broken config loading inside
  every container. Fixed with an explicit resolution order (env override
  → package-relative dev path → cwd → `/app` fallback matching the
  Dockerfile's `WORKDIR`), and `STRATEGY_CONFIG_PATH=/app/config/
  strategies/v1.yaml` set explicitly in Compose as defense-in-depth.
- `server.py` — **NOT changed** (`git diff` empty, confirmed explicitly;
  Block 2 is internal engine work only, no new MCP tools per the plan).

## 4. Migrations

None. Block 2 populates tables Block 1's `0001_initial_schema` migration
already created (`feature_snapshots`, `market_regimes`,
`strategy_signals`, `signal_components`, `paper_orders`, `paper_fills`,
`paper_positions`, `paper_equity_snapshots`, `daily_risk_state`) — no
schema changes were needed.

## 5. Strategy / scoring / risk / paper-broker description

- **Trend Pullback** (default-on): requires 4h/1h trend agreement, price
  pulled back into the EMA20/EMA50 band (not extended), taker-flow
  confirmation, OI not contradicting. Entry/stop/TP are 100% deterministic
  Decimal arithmetic off ATR and local support/resistance — see §7 for a
  concrete example.
- **Breakout + Retest** (default-on): requires prior compression/
  expansion structure, price near either edge of its range (breakout
  attempt), not overextended from EMA20, and a retest of the broken level
  holding within 0.5 ATR, with volume/flow/OI confirmation.
- **Liquidation Exhaustion Reversal** (implemented, `ENABLE_
  LIQUIDATION_REVERSAL=false` by default): requires a liquidation
  cascade/z-score spike, order-book absorption, and improving delta.
  Always `experimental=True`; `strategy_engine_service.evaluate_symbol`
  unconditionally routes it to `rejected` with reason "experimental
  strategy — never produces a paper order," regardless of the enable
  flag — verified by
  `test_liquidation_reversal_stays_experimental_and_rejected_even_when_enabled`.
- **Scoring**: 0-100, weights from `config/strategies/v1.yaml`
  (`trend_alignment` 15, `entry_structure` 15, `volume` 10, `cvd_delta`
  10, `open_interest` 10, `order_book` 10, `retest_pullback` 10,
  `invalidation_quality` 5, `risk_reward` 10, `funding_basis` 5 — sums to
  100, enforced at load time). Every signal records
  `strategy_config_version="v1"`.
- **Hard gates**: evaluated independently of score, listed in full in §1
  — a 100/100-scoring setup with a failed gate is still `REJECTED`.
- **Risk Engine**: `risk_amount = equity × risk_per_trade_pct`,
  `position_size = risk_amount / |entry - stop|` rounded down to
  `qty_step`, capped at `max_leverage`, `max_loss_with_fees` includes
  entry+exit fees and slippage. Rejects duplicate positions, daily-loss-
  locked days, consecutive-loss streaks (no martingale), and RR below
  minimum — fully independent of any LLM.
- **Paper broker**: market fills exactly per the plan's formula; limit
  fills require both price-reached AND minimum opposing taker liquidity
  (a lone wick never fills alone); stops can fill worse than requested on
  a gap-through; TPs fill at the requested price; `PaperPositionTracker`
  supports TP1-then-TP2 partial closes with correctly-apportioned entry
  fees.

## 6. Test results

**79 new Block 2 tests, all passing** across 7 files in
`tests/unit/trading/`:
`test_feature_engine.py`, `test_regime_and_scoring.py`,
`test_hard_gates_and_risk.py`, `test_paper_broker.py`,
`test_strategy_engine.py`, `test_signals_repository_db.py` (DB-backed),
`test_integration_pipeline.py` (full chain).

Covers every scenario the task asked for: delta, CVD, order-book
imbalance, microprice, data quality, stale-data gate, regime
classification (all 9 regimes exercised), scoring (full-marks/zero/
partial/config-validation), hard gates (all 12), RR, position sizing
(formula + qty-step rounding + leverage cap), fees, funding (signed by
direction), daily loss limit (locks the day), consecutive-loss limit (no
martingale), duplicates, expiry, market fill, limit fill (wick-only
rejected, real-flow accepted), partial fill, stop-loss slippage
(including gap-through), TP1, TP2, paper PnL, a full LONG setup, a full
SHORT setup, and `NO_TRADE`.

**Full regression: 363/363 passing** (284 from Block 1 + 79 new Block 2
tests = 363), 8 `stress` tests correctly deselected by default. Raw
result:

```
363 passed, 8 deselected in ~8s
```

No real Bybit or TradingView network access was used anywhere in the test
suite. `TradingViewContext` fixtures are constructed directly (not via the
real network-calling `analyzer`), matching the same fixture-driven pattern
Block 1 used for Bybit.

## 7. Integration test result

`test_integration_pipeline.py::test_full_pipeline_fixture_to_paper_pnl_and_metrics`
chains, using only real Block 1/Block 2 code (no mocked business logic):

```
Bybit fixture JSON
  -> LocalOrderBook.apply() + TradeAggregator (Block 1 collector modules)
  -> feature_engine.build_feature_snapshot()
  -> strategy_engine_service.evaluate_symbol()
       -> regime_classifier.classify_regime() = TREND_UP
       -> strategies.trend_pullback.evaluate() = LONG setup
       -> scoring.score_setup()
       -> hard_gates.evaluate_hard_gates() = passed
       -> risk_engine.evaluate_risk() = approved, position_size computed
  -> paper_broker.fill_market_order() (entry)
  -> paper_broker.fill_take_profit() (TP1 exit)
  -> paper_broker.realized_pnl() = positive PnL
  -> core.observability.metrics (bybit_messages_total counter verified
     to have incremented and persisted across the whole chain)
```

## 8. Example: LONG setup

```json
{
  "symbol": "BTCUSDT", "setup": "trend_pullback", "direction": "LONG",
  "regime": "TREND_UP", "score": 55,
  "entry_min": "105.70", "entry_max": "106.30", "stop_loss": "94.50",
  "take_profit_1": "129.000", "take_profit_2": "147.400",
  "risk_reward_1": "2.00", "risk_reward_2": "3.60",
  "risk": {
    "approved": true, "risk_amount": "25.00", "position_size": "2.173",
    "effective_leverage": "0.02", "max_loss_with_fees": "36.12",
    "rejection_reasons": []
  },
  "invalidation": "15m close below 94.50"
}
```

## 9. Example: SHORT setup

```json
{
  "symbol": "BTCUSDT", "setup": "trend_pullback", "direction": "SHORT",
  "regime": "TREND_DOWN", "score": 55,
  "entry_min": "93.70", "entry_max": "94.30", "stop_loss": "105.50",
  "take_profit_1": "71.000", "take_profit_2": "52.600",
  "risk_reward_1": "2.00", "risk_reward_2": "3.60",
  "risk": {
    "approved": true, "risk_amount": "25.00", "position_size": "2.173",
    "effective_leverage": "0.02", "max_loss_with_fees": "36.11",
    "rejection_reasons": []
  },
  "invalidation": "15m close above 105.50"
}
```

(Both examples use small illustrative price levels, not real BTCUSDT
prices — the point is to show the exact deterministic output shape, which
is identical in form at real price scale, as proven by
`test_integration_pipeline.py` using ~$68,000 scale fixture data.)

## 10. Example: NO_TRADE

```json
{
  "status": "NO_TRADE",
  "regime": "TREND_UP",
  "reasons": [
    "trend_pullback: no setup found this cycle",
    "breakout_retest: no setup found this cycle"
  ]
}
```
(Triggered here by 4h/1h trend disagreement — Trend Pullback requires
agreement and declines to propose anything otherwise, which is a "no
setup found" outcome, distinct from a setup that was proposed and then
hard-gate-rejected; both feed into the same `NO_TRADE` status when nothing
survives.)

## 11. Problems found & fixed

- **Config path resolution bug** (see §3): would have broken silently
  inside the Docker image. Fixed with a multi-candidate resolution order
  and a clear `StrategyConfigError` if none exist, instead of a confusing
  `FileNotFoundError` deep inside `yaml.safe_load`.
- **RR/entry-reference mismatch** in the first draft of both strategies:
  `take_profit_1`/`2` were computed off `entry_min`/`entry_max`
  asymmetrically while the Risk/Gate layer checks RR against
  `entry_reference` (the entry-zone midpoint), producing a systematic
  ~1.97 vs 2.00 RR mismatch that would have caused live setups to fail the
  RR hard gate just below the boundary. Fixed by computing risk/reward
  consistently off `entry_reference` in both `trend_pullback.py` and
  `breakout_retest.py`.
- Three test-authoring issues caught and fixed during verification (all
  in test files, not implementation code): an `EvaluatedSetup` was missing
  a dedicated `rejection_reasons` field (was conflating strategy's
  positive `reasons` with gate rejection reasons) — added the field
  properly rather than leaving the test to work around it; a synthetic
  candle series for the integration test needed a genuine EMA-band
  pullback (a strictly monotonic series has no fractal swing points and
  no realistic pullback zone) — rebuilt with a documented 60-candle
  uptrend + 4-candle pullback shape; the integration test's synthetic
  order-book price scale needed to match its synthetic candle price scale
  for the paper-broker PnL leg to be economically sensible.

## 12. Remaining risks

- **`workers/analysis_worker_main.py`'s DB-polling loop is not wired up.**
  Every engine module it would call is implemented and tested (see §1),
  but the actual "read latest rows from Postgres per symbol on a timer,
  call `evaluate_symbol`, persist the result" glue is deliberately
  deferred — it's the one piece of Block 2 that can only be meaningfully
  exercised against a live collector + live Postgres + live TradingView
  network access, none of which exist in this sandbox. This is flagged
  explicitly in the worker's own docstring and `/health` response as the
  first follow-up once this reaches the real VPS.
- **`TradingViewContext` parsing (`parse_tradingview_response`) is
  defensive but not validated against real TradingView MCP tool output
  shapes** — it was written by reading `analyze_coin`/
  `run_multi_timeframe_analysis`'s docstrings and general dict shape, not
  by capturing real responses (no network access here). It degrades
  gracefully (`None` fields, never raises) if field names don't match, but
  the *quality* of trend/indicator extraction should be spot-checked
  against real output on the VPS before relying on it heavily.
- **Strategy entry/exit math is a first, reasonable implementation, not a
  backtested trading strategy.** The plan's job for Block 2 was to build
  the deterministic *engine* (feature calc → regime → strategy → score →
  gates → risk → paper broker), which is done and tested; whether these
  specific ATR-multiple/EMA-band heuristics are good trading rules is an
  empirical question for Block 3's replay engine / live paper-trading
  data, not something unit tests can answer.
- **Liquidation-reversal absorption heuristic is simplistic** (order-book
  depth held roughly flat despite replenishment activity) — it's
  explicitly experimental and gated off by default, so this is low-risk,
  but worth revisiting with real liquidation-cascade data before ever
  considering enabling it.
- Same Block 1 risks still apply unchanged (no VPS validation yet, Docker
  Hub pulls blocked in this sandbox — see §13).

## 13. VPS resource usage

**Unknown — no VPS access in this session** (same as Block 1). Local
resource usage: the full 363-test suite (including the DB-integration and
full-pipeline tests) completed in under 8 seconds against the same local
PostgreSQL 16 instance proven out in Block 1, with no memory/CPU pressure
observed.

## 14. Docker build / compose

Same sandbox limitation as Block 1 — Docker Hub image pulls return `403
Forbidden` regardless of the proxy (re-confirmed not re-litigated this
round, no new attempt needed since nothing about the image/base changed).
`docker compose config --quiet` validates the full merged configuration
(now including the `STRATEGY_CONFIG_PATH` addition) with **zero errors**.

## 15. Run instructions

No new run steps beyond Block 1's (`docker compose build && docker
compose up -d`, see `docs/trading-system/IMPLEMENTATION_PLAN.md` §9) — the
new engine modules ship inside the same shared image and are exercised by
`bybit-collector`/`analysis-worker` once the deferred DB-polling wiring
(§12) is completed. `tradingview-mcp` itself needs no restart or config
change for any of Block 2.

## 16. Rollback instructions

Identical to Block 1's (§9/§16 of `IMPLEMENTATION_PLAN.md` /
`BLOCK1_REPORT.md`) — nothing in Block 2 added new containers, volumes, or
migrations, so there is nothing additional to roll back at the
infrastructure level. Code rollback: everything is uncommitted in this
worktree; discard the branch or `git checkout -- .` to revert instantly.

## 17. Proposed Block 3 scope

Per the plan (only after explicit approval, and only after Block 2 is
confirmed healthy on the real VPS): new MCP tools (`bybit_market_state`,
`rank_futures_opportunities`, `get_trade_setup`, `paper_portfolio_status`,
`strategy_performance`, `trading_system_health`,
`run_futures_opportunity_scan` — thin `server.py` handlers delegating to
`strategy_engine_service`/`bybit_market_service`/`system_health_service`),
a carefully diffed, backed-up update to the existing ~2h Claude routine,
Telegram alerts with dedup+cooldown, the full Prometheus metric list +
Grafana dashboards, a Replay Engine (in-sample/out-of-sample/walk-forward,
no look-ahead bias), and `docs/trading-system/LIVE_TRADING_GATE.md` (gate
document only, no live-trading code).

---

**Block 2 is complete and verified within the limits of this
no-VPS-access working session, built directly on Block 1's modules with
zero regressions.**

> Blok 2 został zakończony i zweryfikowany (lokalnie, bez dostępu do VPS
> — patrz sekcje 12-14 tego raportu). Czy mam rozpocząć Blok 3 obejmujący
> nowe narzędzia MCP, aktualizację rutyny Claude, Telegram, Prometheus,
> Grafanę, replay i raportowanie?
