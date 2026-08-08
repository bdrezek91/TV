# Block 3 Report — MCP Tools, Alerts, Monitoring, Replay Engine

Date: 2026-08-08
Scope executed: **BLOK 3 — wykonaj dopiero po zgodzie użytkownika** (full
scope — this is the plan's final block; there is no Block 4).

Continued in the same worktree/branch as Blocks 1-2
(`worktree-agent-aae960b2d318ba597`) — nothing from either was lost or
redone. Same operating constraint throughout: **no VPS/SSH access**, local
Postgres reused for all integration tests, same Docker Hub pull
restriction re-confirmed (not re-litigated) in this sandbox.

## 0. Live routine — explicitly confirmed untouched

**No routine/scheduled-trigger management tool was called at any point in
this session.** The ~2h Claude market-scan routine is not part of this git
repository and this sandbox has no tool that could reach it. Per
instructions, the proposed new routine prompt text was written to
`docs/trading-system/PROPOSED_ROUTINE_PROMPT.md`, clearly marked
**PROPOSED ONLY — NOT APPLIED**, with an explicit explanation of why
applying it now (before this code is deployed) would break the user's
live routine.

## 1. Final architecture

```
src/tradingview_mcp/
├── server.py                          # +7 new tools, thin handlers, 44 tools total
├── core/
│   ├── config/ database/ market_data/ observability/   (Block 1)
│   ├── analysis/ trading/                                (Block 2)
│   ├── services/
│   │   ├── bybit_market_service.py                       (Block 1)
│   │   ├── system_health_service.py                      (Block 1, extended)
│   │   ├── strategy_engine_service.py                     (Block 2)
│   │   └── trading_query_service.py      ← NEW (Block 3): backs all 7 new MCP tools
│   ├── notifications/
│   │   └── telegram.py                   ← NEW: dedup+cooldown alerting
│   └── backtest/
│       └── replay_engine.py              ← NEW: no-look-ahead backtest engine
└── workers/                              (Block 1, unchanged in Block 3)
config/strategies/v1.yaml                 (Block 2)
grafana/provisioning/                     ← NEW: datasource + dashboard JSON
docs/trading-system/
├── IMPLEMENTATION_PLAN.md, BLOCK1_REPORT.md, BLOCK2_REPORT.md
├── PROPOSED_ROUTINE_PROMPT.md            ← NEW, proposed only
├── LIVE_TRADING_GATE.md                  ← NEW, requirements checklist only
└── BLOCK3_REPORT.md                      ← this file
```

## 2. New modules

- **`core/database/repositories/query_repository.py`** — read-only SELECT
  helpers (latest order-book/derivatives snapshot, recent trade/liquidation
  aggregates, latest regime/feature snapshot, signal lookups, open/closed
  paper positions, latest equity snapshot, daily risk state) backing every
  new MCP tool.
- **`core/database/repositories/signal_components_repository.py`** —
  fetches a signal's persisted score components for `get_trade_setup`.
- **`core/services/trading_query_service.py`** — the orchestration layer
  for all 7 new tools: `bybit_market_state`, `rank_futures_opportunities`,
  `get_trade_setup`, `paper_portfolio_status`, `strategy_performance`,
  `trading_system_health`, `run_futures_opportunity_scan`. Every function
  only reads what Block 1/2's collector and Strategy Engine already
  computed and persisted — none of them run live analysis inline, and
  `run_futures_opportunity_scan` explicitly pulls the precomputed ranking
  rather than evaluating anything itself.
- **`core/notifications/telegram.py`** — `AlertType` (all 11 plan alert
  types), `build_dedup_key` (`symbol|setup|direction|time_bucket`, exactly
  the plan's formula), `AlertDeduplicator` (cooldown-based suppression),
  `send_alert` (never raises, `TELEGRAM_ENABLED=false` by default, real
  HTTP only via `HttpxTelegramTransport` which is never invoked by tests —
  `FakeTelegramTransport` is used throughout `test_telegram.py`).
- **`core/backtest/replay_engine.py`** — `ReplayEngine`/`ReplayBar`/
  `ReplaySignal`/`ReplayReport`. Strict no-look-ahead (a signal at bar
  index `i` only ever simulates against `bars[i+1:]`), reuses the exact
  same `core.trading.paper_broker` fill functions live paper trading uses
  (not a parallel reimplementation), applies fees/slippage/funding/
  expiry, reports win rate/avg R/expectancy/profit factor/max drawdown/
  auxiliary Sharpe/after-cost PnL/MAE/MFE/by-setup/by-regime/LONG-vs-
  SHORT/by-hour/by-weekday, and separates in-sample/out-of-sample/
  walk-forward. **No auto-optimizer**: verified by a dedicated test
  (`test_no_auto_optimizer_exists_in_replay_module`) that greps the
  module's public API for optimizer-like names.

## 3. New MCP tools (7, added to `server.py`)

All thin (parameter validation + delegate to `trading_query_service`),
`readOnlyHint=True`, `destructiveHint=False` — none of them can place an
order, paper or real:

1. `bybit_market_state(symbol)`
2. `rank_futures_opportunities(symbols, minimum_score, maximum_results, include_rejected)`
3. `get_trade_setup(signal_id)`
4. `paper_portfolio_status()`
5. `strategy_performance(days, setup_name, symbol, market_regime)`
6. `trading_system_health()`
7. `run_futures_opportunity_scan(minimum_score, maximum_results)`

Verified registered and callable in-process: `await server.mcp.list_tools()`
returns 44 tools (37 pre-existing + 7 new); all 7 new names present.
**Cannot verify the live MCP connector sees them without VPS/connector
access** — that must be checked after deployment (see §12 run
instructions).

## 4. Routine update

**Proposed only, not applied.** See §0 above and
`docs/trading-system/PROPOSED_ROUTINE_PROMPT.md` for the full text and
rationale.

## 5. New containers

None. Telegram/Prometheus-metrics/Replay-Engine are code additions inside
the existing `bybit-collector`/`analysis-worker` image; Grafana already
existed as a container from Block 1 — Block 3 only mounts a provisioning
directory (`./grafana/provisioning:/etc/grafana/provisioning:ro`) into it,
containing a datasource config and one dashboard JSON.

## 6. Migrations

None. Block 3 is read-only against tables Block 1/2 already created.

## 7. Test results

**42 new Block 3 tests** across `test_trading_query_service.py` (11, DB-
integrated against the real local Postgres), `test_server_tools_block3.py`
(10, exercises the `server.py` handlers directly), `test_telegram.py` (11),
`test_replay_engine.py` (10).

**Full regression: 405/405 passing** (363 from Blocks 1-2 + 42 new), 8
`stress` tests correctly deselected. No real Telegram/Bybit/TradingView
network calls anywhere in the suite.

```
405 passed, 8 deselected in ~11s
```

`server.py`'s pre-existing 37 tools are all still registered and
unmodified — verified both structurally (`test_a_sample_of_pre_existing_
tools_still_registered` checks 11 representative tool names across every
category: top_gainers, coin_analysis, multi_timeframe_analysis,
bollinger_scan, backtest_strategy, market_snapshot, bitcoin_market_pulse,
futures_market_overview, stock_screener, financial_news, top_losers) and
by the full pre-existing 227-test suite continuing to pass unchanged.

## 8. Healthchecks

No new containers, so no new healthcheck definitions were needed.
`trading_system_health` (the new MCP tool) itself extends Block 1's
`system_health_service.build_health_report` with per-symbol order-book
consistency/freshness and paper-broker state — tested against the real
local Postgres (`test_trading_system_health_reports_database_connected`).

## 9. Sample MCP tool outputs (all real, generated by calling the actual
tool handlers against the real local Postgres — not hand-written)

**`trading_system_health()`** (abbreviated):
```json
{
  "generated_at": "2026-08-08T15:24:16Z",
  "config": {"valid": true, "error": null},
  "database": {"connected": true},
  "redis": {"connected": false},
  "overall_status": "HEALTHY",
  "paper_broker": {"has_equity_data": false, "open_positions_count": 0},
  "strategy_engine": {"config_version": "v1"}
}
```
(`redis: false` here reflects this sandbox has no Redis running — Redis
is provisioned but not yet used by any Block 1-3 code path, see Block 1/2
risk notes; it does not currently gate `overall_status`.)

## 10. Example: OPPORTUNITIES_FOUND (`run_futures_opportunity_scan`)

Generated by seeding one real `strategy_signals` row (via the actual
`signals_repository.save_signal` Block 2 wrote) and calling the real tool:

```json
{
  "generated_at": "2026-08-08T15:24:50.528Z",
  "market": "BYBIT_LINEAR",
  "status": "OPPORTUNITIES_FOUND",
  "data_quality": "GOOD",
  "opportunities": [
    {
      "signal_id": "d0ec4225-90a2-40d8-8757-3c2e1d997b2c",
      "symbol": "BTCUSDT",
      "setup": "trend_pullback",
      "direction": "LONG",
      "regime": "TREND_UP",
      "score": 82,
      "status": "CANDIDATE",
      "entry_zone": ["68200.00000000", "68400.00000000"],
      "stop_loss": "67550.00000000",
      "take_profit_1": "69600.00000000",
      "take_profit_2": "70800.00000000",
      "risk_reward_1": "2.0000",
      "risk_reward_2": "3.6000",
      "expires_at": "2026-08-08T16:24:49.880Z",
      "invalidation": "15m close below 67550"
    }
  ],
  "rejected_summary": {"count": 0},
  "system_warnings": []
}
```

`get_trade_setup(signal_id)` on the same signal additionally returns
`score_components`, `tradingview_context`, `bybit_context`,
`risk_context`, and `rejection_reasons` — all real persisted values, no
placeholders.

## 11. Example: NO_TRADE

Against an empty (unseeded) database — the honest default state before any
collector has run on a real VPS:

```json
{
  "generated_at": "2026-08-08T15:24:17.029Z",
  "market": "BYBIT_LINEAR",
  "status": "NO_TRADE",
  "data_quality": "GOOD",
  "opportunities": [],
  "reasons": [
    "No signal reached the minimum score in the lookback window",
    "minimum_score=75, symbols=['BTCUSDT']"
  ],
  "system_warnings": []
}
```

## 12. Paper trading stats

Not available — no real paper trades have ever executed (no live VPS, no
live collector running against real Bybit prices). `strategy_performance`
against the empty local DB correctly returns `{"trades": 0, "win_rate":
null, ..., "note": "no closed paper trades in this window"}`, proven by
`test_strategy_performance_empty_window`.
`test_strategy_performance_computes_stats_from_closed_positions` proves
the aggregation math itself (win rate, profit factor, LONG-vs-SHORT split)
against synthetic seeded rows.

## 13. No live-trading code path — verification

Grepped the entire `core/`/`workers/` tree for order-placement-shaped
calls (`place_order`, `create_order`, `submit_order`, `.buy(`, `.sell(`,
`execute_trade`, etc.), excluding matches inside comments/docstrings that
explicitly say "no live trading":

- **Zero matches** in any Block 1-3 code.
- **One pre-existing match**: `core/portfolio.py::execute_trade` — a
  legacy module from the *original* repo (commit `be19006`, before any of
  our work), SQLite-based, float-typed (not our system's Decimal
  convention), does local paper-bookkeeping only, calls no exchange API,
  and is **not imported by `server.py`** (confirmed via grep — no tool
  wires it up). Untouched by Blocks 1-3 per the "don't touch unrelated
  files" rule, and unrelated to the new Bybit/paper-trading system this
  plan built (it predates it and uses an entirely separate storage/schema).
- `core/market_data/bybit/client.py`'s `BybitRestClient` (Block 1) only
  calls read-only pybit endpoints: `get_open_interest`, `get_tickers`,
  `get_funding_rate_history`, `get_long_short_ratio`, `get_kline` — no
  `place_order`/`amend_order`/`cancel_order` anywhere.
- `TRADING_MODE` is hard-validated to `"paper"` at startup
  (`TradingSettings._validate_trading_mode`); this was true since Block 1
  and unchanged.

## 14. Problems found & fixed

- None new in Block 3's own code beyond the usual iterative test-fixing
  during development (documented for transparency): the initial
  `trading_query_service` integration tests failed with a Postgres DNS
  error (`Name or service not known`) because `system_health_service.
  build_health_report()`'s own connectivity probe uses
  `get_trading_settings().database_url`, which defaults to the Docker
  Compose `postgres` hostname — meaningless outside a container. Fixed by
  adding an autouse test fixture that points the cached settings singleton
  at the local `TEST_DATABASE_URL` for the test session (mirrors how
  Block 1/2 already handled this for other DB-touching code paths).

## 15. Remaining risks

- **Redis is provisioned but still unused everywhere** — `trading_system_
  health` reports its connectivity but nothing (dedup, caching, cross-
  worker coordination) actually uses it yet. Low risk (it degrades
  gracefully — `redis: {"connected": false}` doesn't currently block
  `overall_status`), but worth deciding whether Redis should become
  load-bearing or be dropped from the stack.
- **`avg_r` is `None` in `strategy_performance`** — the current
  `paper_positions` schema doesn't store a per-trade R-multiple (only raw
  PnL), so the live-data version of this stat is unavailable until either
  the schema gains an `r_multiple` column or it's computed from the
  linked `strategy_signals.stop_loss`/`entry` at query time (a small,
  well-scoped follow-up). The Replay Engine, by contrast, *does* compute
  `avg_r` correctly (it has the risk distance available at simulation
  time) — this asymmetry is worth resolving before leaning on
  `strategy_performance`'s R-based stats specifically.
- **Grafana dashboard JSON was authored, not rendered** — validated as
  syntactically correct JSON and mounted via provisioning, but this
  sandbox cannot pull the Grafana image to actually load and visually
  confirm the dashboard (same Docker Hub restriction as everything else).
  Panel queries reference real, tested metric names, but layout/readability
  should be eyeballed once Grafana actually runs on the VPS.
- **The Replay Engine has no live historical data to run against yet** —
  it's fully unit-tested on synthetic bars (fills, expiry, same-bar
  stop/target ambiguity, fee impact, sample-splitting all verified), but
  has never processed real accumulated market data because none exists
  outside a live VPS deployment.
- Same unresolved Block 1/2 risks still apply: `workers/analysis_worker_
  main.py`'s DB-polling loop remains unwired (flagged again in Block 2's
  report — Block 3 did not change this); no VPS validation of any kind
  yet; Docker Hub pulls blocked in this sandbox.

## 16. VPS resource usage

**Unknown — no VPS access in this session** (unchanged from Blocks 1-2).

## 17. Run instructions

Same as Block 1 (`docs/trading-system/IMPLEMENTATION_PLAN.md` §9): `docker
compose build && docker compose up -d` on the real VPS. New in Block 3:
after the stack is up, confirm the connector sees the 7 new tools (call
`trading_system_health` once through the actual Claude connector — if it
returns "tool not found," the deployed image is stale; rebuild) before
applying `docs/trading-system/PROPOSED_ROUTINE_PROMPT.md`'s routine text
(see that file's own pre-flight checklist).

## 18. Upgrade instructions

No migrations to run (Block 3 added no schema). Redeploy: `docker compose
build && docker compose up -d` picks up the new `server.py` tools and the
Grafana provisioning mount. `tradingview-mcp` itself does need a rebuild
this time (its own `server.py` changed to add the 7 tools) — unlike
Blocks 1-2, this is the first block where that container's image content
actually changes; its port/transport/existing-tool behavior does not.

## 19. Rollback instructions

- **Code rollback**: everything across all three blocks is uncommitted in
  this worktree; discard the branch or `git checkout -- .` reverts
  instantly.
- **Tool rollback without a full code revert**: since `server.py`'s new
  tools are purely additive and read-only, there is no destructive state
  to unwind — removing the 7 new `@mcp.tool()` blocks (or reverting
  `server.py` alone) returns to Block 2's exact tool surface with no other
  side effects.
- **Routine rollback**: not applicable — it was never applied (§0).

## 20. Information needed for further strategy validation

To move past this report toward real validation (not live trading — see
`LIVE_TRADING_GATE.md`, which this section feeds): (1) VPS access to run
Docker builds and actually start `bybit-collector` against real Bybit data
for a meaningful period; (2) enough accumulated real market data
(candles/trade-aggregates/order-book snapshots/derivatives snapshots) for
the Replay Engine to run against; (3) enough real paper trades for
`strategy_performance` to report non-null win-rate/expectancy/profit-factor
numbers; (4) a human review of the actual TradingView-context field
mapping in `core/analysis/tradingview_context.py` against real
`analyze_coin`/`run_multi_timeframe_analysis` output shapes (written
defensively but never checked against live responses, per Block 2's own
risk note); (5) Grafana dashboard visual QA once the image can actually be
pulled and run.

---

**Block 3 — and with it, the full three-block plan — is complete and
verified within the limits of this no-VPS-access working session.** No
live-trading code exists anywhere. The live Claude routine was not
touched. Stopping here, as instructed — no Block 4 exists in the plan.
