# TradingView MCP — Trading System Extension: Implementation Plan

Status: **Block 1 complete** (audit, infrastructure, data). Blocks 2 and 3
are planned but not started — see the end of this document.

## 1. Current architecture (as audited)

- `tradingview-mcp-server` is a single Python package (`src/tradingview_mcp/`)
  exposing ~35 MCP tools through `server.py`, a thin FastMCP routing layer
  (1122 lines, no business logic — every `@mcp.tool()` handler validates
  parameters and delegates to a function in `core/services/*`).
- Business logic lives in `core/services/` (screener, backtest, futures,
  options, egx, sentiment, indicators, etc. — ~9700 lines across ~19
  modules) plus `core/errors.py` (structured `{"error": {"code", "message",
  ...}}` envelopes via `make_error`/`exception_to_envelope`) and
  `core/utils/validators.py`.
- Transport: `mcp[cli]` (FastMCP) served over `streamable-http` on port
  **8000** inside the container, published as **8080:8000** by
  `docker-compose.yml`. `main()` in `server.py` also supports `stdio` for
  local/dev use. **Neither the port nor the transport were changed.**
- Docker: a two-stage `Dockerfile` (`python:3.11-slim` builder + runtime),
  `uv pip install --system .` (ignores `uv.lock` — pins in `pyproject.toml`
  are load-bearing), non-root `mcpuser`, a `HEALTHCHECK` hitting
  `http://localhost:8000/health` (provided by FastMCP's streamable-http
  app). **Dockerfile is unchanged** — `git diff Dockerfile` is empty.
- `docker-compose.yml` previously defined exactly one service
  (`tradingview-mcp`). Block 1 adds new services alongside it without
  touching its `build`, `image`, `ports`, or `environment`.
- Tests: `tests/unit/` (227 tests before Block 1) + `tests/stress/`
  (real-upstream, excluded by default via `-m "not stress"` in
  `pyproject.toml`). Run with `uv run pytest`.
- No VPS SSH access exists in this working session (see the operating
  constraint below) — everything in this document was validated **locally**
  in the worktree, not on the user's real deployment.

### Reusable existing functions identified for later blocks

| Concern | Existing function(s) |
|---|---|
| Multi-timeframe TA (15m/1h/4h context) | `core/services/screener_service.py::run_multi_timeframe_analysis`, `fetch_multi_timeframe_patterns` |
| Single-instrument analysis | `core/services/screener_service.py::analyze_coin` |
| Volume scanners | `core/services/scanner_service.py::volume_breakout_scan`, `smart_volume_scan`, `volume_confirmation_analyze` |
| Bollinger | `core/services/screener_service.py::fetch_bollinger_analysis` |
| Futures tools | `core/services/futures_service.py` (overview/top-movers/category/watchlist) |
| Backtesting | `core/services/backtest_service.py` (783 lines: strategy backtest, comparison) |
| Walk-forward | `core/services/backtest_service.py` (walk-forward variant used by `walk_forward_backtest_strategy`) |
| Indicator math | `core/services/indicators.py`, `core/services/indicators_calc.py` |

Block 2's Feature Engine / Strategy Engine will call these **directly**
(same-process function calls), never over HTTP against the MCP server
itself, per the plan's rule.

## 2. VPS deployment status

**This working session/worktree has no SSH or other access to the user's
real VPS.** The user explicitly chose (in this same planning thread) to
have Block 1 developed and validated locally, stopping at "ready to
deploy," rather than granting VPS access. Consequently:

- Nothing in Block 1 was deployed, restarted, or otherwise touched on the
  real VPS.
- The existing "Claude routine" (the ~2h market-scan routine) is a
  scheduling artifact outside this git repository and was not located,
  inspected, or modified — per the operating constraints for this session.
- All verification (tests, Alembic migrations, `docker compose config`)
  ran against local substitutes: a locally-installed PostgreSQL 16 server
  (Docker image pulls are blocked in this sandbox — see §7) and the
  existing local test suite.
- Deploying to the real VPS is the user's action item after reviewing this
  report; §9 below is the run/rollback instruction set for that step.

## 3. Compatibility with the connector

- MCP port (8000 in-container / 8080 published), transport
  (`streamable-http`), and `server.py`'s tool surface are byte-for-byte
  unchanged (`git diff -- src/tradingview_mcp/server.py` is empty).
- No new MCP tools were added in Block 1 (per the plan — Block 3 adds
  `bybit_market_state`, `run_futures_opportunity_scan`, etc.). The new
  `core/services/bybit_market_service.py` and
  `core/services/system_health_service.py` exist as the seam those future
  tools will call into, but nothing in `server.py` imports them yet.
- The new services (`bybit-collector`, `analysis-worker`, `paper-broker`,
  `postgres`, `redis`, `prometheus`, `grafana`) run as separate Docker
  Compose services/containers on a private `trading-net` bridge network —
  they do not share a process, port, or Python import graph with
  `tradingview-mcp` beyond the `core/` package they both ship inside the
  same image.

## 4. Module plan (delivered)

```text
src/tradingview_mcp/
├── server.py                              # UNCHANGED
├── core/
│   ├── config/
│   │   └── trading_settings.py            # pydantic-settings, fail-fast validation
│   ├── database/
│   │   ├── base.py                        # Declarative base + naming convention
│   │   ├── session.py                     # lazy async engine/session factory
│   │   ├── models/                        # market_data / analysis / signals / paper_trading / system
│   │   └── repositories/
│   │       └── market_data_repository.py  # idempotent, "never overwrite newer with older" writes
│   ├── market_data/bybit/
│   │   ├── schemas.py                     # typed parsing of Bybit v5 WS payloads
│   │   ├── orderbook.py                   # local book reconstruction, gap/consistency detection
│   │   ├── aggregation.py                 # trade/liquidation 1s/1m/5m aggregation + dedup
│   │   ├── reconnect.py                   # backoff+jitter policy, staleness helpers
│   │   ├── archive.py                     # optional raw JSONL archival (off by default)
│   │   ├── client.py                      # WSTransport (real+fake) / pybit REST wrapper
│   │   ├── websocket_collector.py         # orchestrates all of the above
│   │   └── rest_poller.py                 # OI/funding/mark/index/basis + candle backfill
│   ├── observability/
│   │   └── metrics.py                     # the exact Prometheus metric names from the plan
│   └── services/
│       ├── bybit_market_service.py        # data-readiness (WARMING_UP/READY) — Block 3 tool seam
│       └── system_health_service.py       # aggregated health report — Block 3 tool seam
└── workers/
    ├── bybit_collector_main.py            # `bybit-collector` service entry point
    ├── analysis_worker_main.py            # `analysis-worker` infra-only stub (Block 2 fills logic)
    └── paper_broker_main.py               # `paper-broker` infra-only stub (Block 2 fills logic)
```

No existing file was deleted or renamed to fit this structure — it was
grafted onto the existing `core/services/` convention rather than
replacing it.

## 5. Database plan

SQLAlchemy 2.0 async ORM + `asyncpg` + Alembic. All money/quantity columns
are `Numeric` (mapped to Python `Decimal`); all timestamps are
`DateTime(timezone=True)` storing UTC.

18 tables created by migration `0001_initial_schema`:
`instruments`, `candles`, `trade_aggregates`, `orderbook_feature_snapshots`,
`liquidation_aggregates`, `derivatives_snapshots`, `feature_snapshots`,
`market_regimes`, `strategy_signals`, `signal_components`, `paper_orders`,
`paper_fills`, `paper_positions`, `paper_equity_snapshots`,
`daily_risk_state`, `strategy_metrics`, `system_events`, plus Alembic's own
`alembic_version`.

`strategy_signals` implements the plan's exact field list (`entry_min/max`,
`stop_loss`, `take_profit_1/2`, `risk_reward_1/2`, `expires_at`,
`invalidation_reason`, `feature_snapshot_id`, the three `*_context_json`
columns, `rejection_reasons_json`, `strategy_config_version`) and the
10-value `signal_status` enum (`CANDIDATE`…`CLOSED`), with indexes on
`symbol`, `created_at`, `setup_name`, `status`, `expires_at`.

Only `candles`, `trade_aggregates`, `orderbook_feature_snapshots`,
`liquidation_aggregates`, and `derivatives_snapshots` are **populated** in
Block 1 (by the collector/poller). The rest exist as schema only, ready for
Block 2/3.

## 6. Docker Compose plan

- `tradingview-mcp`: **untouched** (build, image, ports, environment,
  `restart: unless-stopped` all identical to before).
- `postgres` (16-alpine), `redis` (7-alpine): healthchecked, on the private
  `trading-net` bridge network, **not published to the host** by default
  (commented-out `ports:` blocks documented for local debugging only).
  Postgres has a named, persistent volume; Redis is run with persistence
  disabled (`--save "" --appendonly no`) since it's a cache/coordination
  layer in this design, not a source of truth.
- `bybit-collector`: builds from the same `Dockerfile`/image as
  `tradingview-mcp`, overrides `entrypoint` to
  `python -m tradingview_mcp.workers.bybit_collector_main`, depends on
  healthy `postgres`+`redis`, has its own healthcheck (hits its `/health`
  endpoint on :9100), `restart: unless-stopped`, `stop_grace_period: 15s`
  for graceful shutdown.
- `analysis-worker` / `paper-broker`: same shared-image pattern, infra-only
  stubs in Block 1 (start, healthcheck green, expose a metrics port; the
  Block 2 logic will replace the stub loop body without touching Compose).
- `prometheus` (v2.55.1): scrapes the three worker metrics ports, has a
  persistent volume, not published by default.
- `grafana` (11.2.0): persistent volume, published only on
  `127.0.0.1:3000` (not to `0.0.0.0`).
- All services: `restart: unless-stopped`, JSON-file logging capped at
  10MB × 3 files (log rotation), on the private `trading-net` network.
- Secrets (`POSTGRES_PASSWORD`, `GRAFANA_ADMIN_PASSWORD`,
  `TELEGRAM_BOT_TOKEN`) are environment-variable driven with safe defaults
  for local dev only — the user must set real values via `.env` (already
  gitignored) or the VPS's own secret store before production use.
- Rollback: `docker compose stop bybit-collector analysis-worker
  paper-broker postgres redis prometheus grafana` (or `docker compose down`
  for the new services only, listing them explicitly) leaves
  `tradingview-mcp` running untouched throughout, since it has no
  `depends_on` relationship to anything new.

## 7. Dependencies added

`pybit`, `websockets`, `sortedcontainers`, `sqlalchemy[asyncio]`,
`asyncpg`, `alembic`, `pydantic-settings`, `redis`, `prometheus-client` —
all added to `pyproject.toml`'s main `dependencies` list (the Docker image
is shared across all four Python services, so they can't be dev-only).
`uv sync` resolved them cleanly against the existing dependency set — no
version conflicts, `uv.lock` updated accordingly. No ML/TensorFlow/PyTorch,
no paid services, no additional AI models were added.

**Sandbox limitation (not a code defect):** this working environment
cannot pull any image from Docker Hub — `docker pull postgres:16-alpine`,
`redis:7-alpine`, and even `python:3.11-slim` (the existing Dockerfile's
own base image) all fail with `403 Forbidden` from Docker Hub's CDN
(`production.cloudfront.docker.com`), regardless of the configured HTTPS
proxy, and no images are pre-cached locally. This blocks `docker build`
and `docker compose up -d` here. It does **not** indicate a problem with
the Dockerfile or docker-compose.yml themselves — `docker compose config`
validates the full merged configuration with zero errors/warnings, and the
Dockerfile is byte-for-byte unchanged. This must be re-verified with real
`docker build` / `docker compose up -d` on the VPS (which has normal
internet/registry access) before relying on it in production — see §9's
run instructions.

## 8. Migrations

Alembic, async (`async_engine_from_config` in `alembic/env.py`), naming
convention fixed via `Base.metadata` so future autogenerated migrations get
stable constraint names. Migration `0001_initial_schema`:

- Validated **against a real local PostgreSQL 16 server** (installed
  directly in this sandbox, since Docker's Postgres image couldn't be
  pulled — see §7): `alembic upgrade head` succeeded, created all 18
  tables + the `signal_status` enum; `alembic downgrade base` cleanly
  dropped everything back to empty; `alembic upgrade head` re-applied
  cleanly a second time.
- Also validated in fully offline mode (`alembic upgrade head --sql` /
  `alembic downgrade 0001:base --sql`, which compiles DDL against the
  PostgreSQL dialect without any connection) to confirm the SQL itself is
  sound independent of any specific database instance.
- Non-destructive (CREATE-only in `upgrade()`); reversible (`downgrade()`
  drops tables in reverse FK-dependency order, drops the enum type last).

## 9. Run / rollback instructions (for the VPS, once the user deploys this)

**Run:**
```bash
cd /path/to/tradingview-mcp
cp .env.example .env   # if not already present; fill in real secrets
docker compose build
docker compose up -d postgres redis
docker compose exec bybit-collector alembic upgrade head   # or run alembic from the host with DATABASE_URL set to the published/host-reachable Postgres
docker compose up -d
docker compose ps
docker compose logs -f bybit-collector
```
`tradingview-mcp` itself does not need to be restarted for any of this —
it has no dependency on the new services and was never touched.

**Rollback:**
```bash
docker compose stop bybit-collector analysis-worker paper-broker prometheus grafana
# Data-preserving: postgres/redis stay up so no history is lost.
# Full rollback of the data layer too (destructive):
#   docker compose down postgres redis   (volumes persist unless -v is added)
#   alembic downgrade base               (drops all Block 1 tables)
git status   # everything in this document is uncommitted in this worktree;
             # `git checkout -- .` / discard the branch to fully revert code.
```

## 10. Risks

- **No VPS validation yet.** Everything above was proven locally; the VPS's
  actual resource headroom, real Bybit connectivity, and real Docker Hub
  access must be checked before trusting Block 1 in production (see the
  Docker Hub note in §7 — the *code* is validated, the *container build*
  is not, in this sandbox).
- **WebSocket reconnect/backoff logic is fixture-driven, not
  live-network-tested** — real Bybit disconnect/gap patterns may differ
  from the synthetic fixtures in `tests/fixtures/bybit/`. Recommend a
  supervised first run watching `bybit_reconnects_total` and
  `orderbook_consistency_errors_total`.
- **`redis` is currently unused by the collector itself** (only declared in
  Compose/config per the plan) — Block 2 is expected to use it for
  cross-worker coordination (e.g. sharing the in-memory order book state
  or signal cooldowns) but nothing writes to it yet.
- **Rate limits**: the REST poller's backoff is conservative but has not
  been tuned against Bybit's actual rate-limit response codes/headers —
  worth revisiting once live traffic is observed.
- **Shared image, four services**: `docker compose build` will rebuild the
  same image four times (once per service that declares `build:`) since
  they intentionally share one Dockerfile/image tag — functionally correct
  but slower than necessary; an easy Block 2/3 optimization if it matters.

## 11. Block 1 completion criteria — status

| Criterion | Status |
|---|---|
| Audit complete, existing tools/tests untouched | ✅ 227/227 pre-existing tests still pass |
| `docs/trading-system/IMPLEMENTATION_PLAN.md` created | ✅ this document |
| New module tree under `core/`+`workers/` | ✅ |
| Dependencies added (no ML/paid) | ✅ |
| `.env.example` extended, fail-fast config validation | ✅ |
| Bybit WS collector: reconnect/backoff/jitter/heartbeat/resubscribe/dedup/staleness/metrics/graceful shutdown | ✅ (fixture-tested) |
| Local order book reconstruction + gap detection + consistency flag | ✅ |
| Trade/liquidation aggregation (1s/1m/5m) | ✅ |
| Optional raw archival, off by default | ✅ |
| REST poller: OI/funding/mark/index/basis + backfill, idempotent | ✅ |
| PostgreSQL + Alembic, all 17 plan tables + `strategy_signals` exact schema | ✅ (proven on a real local Postgres) |
| Health checks + the 8 named Prometheus metrics | ✅ |
| Docker Compose: existing service untouched, 6 new services added | ✅ config-validated; not build-validated in this sandbox |
| ≥20 test scenarios from the plan | ✅ 57 new tests covering all 20 scenarios |
| Full regression run | ✅ 284/284 passing |

## 12. Planned scope for Block 2 (not started)

Feature Engine (price action, volume/CVD, order book, futures,
liquidations, data-quality scoring), TradingView↔Bybit context fusion,
Market Regime Classifier, Trend Pullback + Breakout/Retest strategies
(Liquidation Reversal experimental/disabled), versioned Scoring Engine,
hard gates, Risk Engine (independent of any AI), local event-driven paper
broker (market/limit fills, SL/TP1/TP2, fees/spread/slippage/funding). Will
populate the schema-only tables from Block 1 and fill in the
`analysis-worker`/`paper-broker` stub loops.

## Block 3 (only after explicit approval, and only after Block 2)

New MCP tools (`bybit_market_state`, `rank_futures_opportunities`,
`get_trade_setup`, `paper_portfolio_status`, `strategy_performance`,
`trading_system_health`, `run_futures_opportunity_scan`), a careful,
diffed update to the existing ~2h Claude routine (never destroyed, always
backed up first), Telegram alerts, Prometheus/Grafana dashboards, a Replay
Engine, and `docs/trading-system/LIVE_TRADING_GATE.md` (a gate document
only — no live-trading code, ever).
