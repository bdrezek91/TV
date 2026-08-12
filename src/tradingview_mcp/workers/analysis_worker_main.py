"""`analysis-worker` service entry point.

Runs the full Block 2 pipeline on a timer, per symbol:

    DB reads (candles/trade-aggregates/orderbook/derivatives/liquidations)
    -> Feature Engine snapshot -> TradingView context fetch
    -> strategy_engine_service.evaluate_symbol (regime -> strategies ->
       scoring -> hard gates -> risk sizing)
    -> persist feature snapshot + market regime + every signal
       (CANDIDATE or REJECTED — nothing is silently dropped)

Never places a paper or real order itself — `run_futures_opportunity_scan`
(Block 3) only ever *reads* the CANDIDATE signals this worker writes.
Nothing here computes entry/SL/TP/score at "LLM discretion"; every number
comes from the deterministic `core/analysis/*` modules this loop calls.
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import logging
import signal
import sys
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from prometheus_client import exposition

from tradingview_mcp.core.analysis.feature_engine import build_feature_snapshot
from tradingview_mcp.core.analysis.tradingview_context import fetch_tradingview_context
from tradingview_mcp.core.config.trading_settings import get_trading_settings
from tradingview_mcp.core.database.repositories import query_repository as q
from tradingview_mcp.core.database.repositories import signals_repository as sig_repo
from tradingview_mcp.core.database.session import session_scope
from tradingview_mcp.core.observability import metrics as m
from tradingview_mcp.core.services.strategy_engine_service import (
    DailyState,
    DuplicateChecker,
    evaluate_symbol,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tradingview_mcp.analysis_worker_main")

# BTCUSDT linear-perpetual instrument constants. The `instruments` table
# (Block 1 schema) exists for this but nothing seeds it yet; hardcoding a
# reasonable default here is a deliberately small stopgap rather than
# building a full instrument-metadata service for one symbol.
_DEFAULT_TICK_SIZE = Decimal("0.1")
_DEFAULT_QTY_STEP = Decimal("0.001")
_DEFAULT_FEE_RATE = Decimal("0.00055")  # Bybit linear-perp taker fee
_DEFAULT_SLIPPAGE = Decimal("10")  # absolute price units, not a fraction


def _row_dict(row, rename: Optional[dict] = None) -> dict:
    """Strips SQLAlchemy instance-state internals; keeps column names as
    dict keys, which is exactly what `feature_engine.compute_*` expects
    (it reads these dicts by the same names as the ORM columns)."""
    d = {k: v for k, v in vars(row).items() if not k.startswith("_")}
    if rename:
        for old, new in rename.items():
            if old in d:
                d[new] = d.pop(old)
    return d


async def _load_inputs(symbol: str, max_ages: dict):
    async with session_scope() as session:
        candles_rows = await q.get_recent_candles(session, symbol, "15", limit=250)
        trade_rows = await q.get_recent_trade_aggregates(session, symbol, 60, limit=60)
        liq_1m_rows = await q.get_recent_liquidation_aggregates(session, symbol, 60, limit=60)
        liq_5m_rows = await q.get_recent_liquidation_aggregates(session, symbol, 300, limit=60)
        liq_15m_rows = await q.get_recent_liquidation_aggregates(session, symbol, 900, limit=60)
        ob_recent_rows = await q.get_recent_orderbook_snapshots(session, symbol, limit=20)
        deriv_history_rows = await q.get_recent_derivatives_snapshots(session, symbol, limit=20)
        active_keys = await sig_repo.list_active_signal_keys(session, symbol)
        open_positions = await q.get_open_positions(session)
        equity_snapshot = await q.get_latest_equity_snapshot(session)
        today = dt.datetime.now(dt.timezone.utc).date()
        daily_state_row = await q.get_daily_risk_state_for(session, today)

    candles = [_row_dict(c) for c in candles_rows]
    trades = [_row_dict(t) for t in trade_rows]
    liq_1m = [_row_dict(l) for l in liq_1m_rows]
    liq_5m = [_row_dict(l) for l in liq_5m_rows]
    liq_15m = [_row_dict(l) for l in liq_15m_rows]
    ob_dicts = [_row_dict(o, rename={"depth_bands_json": "depth_bands"}) for o in ob_recent_rows]
    ob_latest = ob_dicts[-1] if ob_dicts else {}
    ob_previous = ob_dicts[-2] if len(ob_dicts) >= 2 else None
    deriv_dicts = [_row_dict(d) for d in deriv_history_rows]
    deriv_latest = deriv_dicts[-1] if deriv_dicts else {}

    open_symbols = {p.symbol for p in open_positions}
    open_positions_count = len(open_positions)
    account_equity = Decimal(equity_snapshot.equity) if equity_snapshot else None

    return {
        "candles": candles,
        "trades": trades,
        "liq_1m": liq_1m,
        "liq_5m": liq_5m,
        "liq_15m": liq_15m,
        "orderbook_latest": ob_latest,
        "orderbook_previous": ob_previous,
        "orderbook_recent": ob_dicts,
        "derivatives_latest": deriv_latest,
        "derivatives_history": deriv_dicts,
        "active_keys": active_keys,
        "open_symbols": open_symbols,
        "open_positions_count": open_positions_count,
        "account_equity": account_equity,
        "daily_state_row": daily_state_row,
    }


async def _persist_results(
    symbol: str, snapshot: dict, result: dict, tv_context, config_version: str, signal_expiry_minutes: int
) -> None:
    tv_context_dict = dataclasses.asdict(tv_context)
    bybit_context_dict = {
        "orderbook": snapshot.get("orderbook", {}),
        "futures": snapshot.get("futures", {}),
        "liquidations": snapshot.get("liquidations", {}),
        "volume": snapshot.get("volume", {}),
    }

    async with session_scope() as session:
        snapshot_id = await sig_repo.save_feature_snapshot(session, symbol, snapshot)
        await sig_repo.save_market_regime(session, symbol, snapshot["as_of"], result["regime"])

        for evaluated in list(result["signals"]) + list(result["rejected"]):
            setup = evaluated.setup
            await sig_repo.save_signal(
                session,
                symbol=symbol,
                setup_name=setup.setup_name,
                direction=setup.direction,
                market_regime=evaluated.regime,
                status=evaluated.status,
                score=evaluated.score,
                entry_min=setup.entry_min,
                entry_max=setup.entry_max,
                stop_loss=setup.stop_loss,
                take_profit_1=setup.take_profit_1,
                take_profit_2=setup.take_profit_2,
                risk_reward_1=evaluated.risk_reward_1,
                risk_reward_2=evaluated.risk_reward_2,
                expires_at=snapshot["as_of"] + dt.timedelta(minutes=signal_expiry_minutes),
                invalidation_reason=setup.invalidation_reason,
                feature_snapshot_id=snapshot_id,
                tradingview_context=tv_context_dict,
                bybit_context=bybit_context_dict,
                risk_context=evaluated.risk_result or {},
                rejection_reasons=evaluated.rejection_reasons,
                strategy_config_version=config_version,
                components=evaluated.components,
            )


async def _evaluate_symbol_cycle(settings, symbol: str) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    max_ages = {
        "candles": settings.max_data_age_candles_seconds,
        "trades": settings.max_data_age_trades_seconds,
        "orderbook": settings.max_data_age_orderbook_seconds,
        "derivatives": settings.max_data_age_oi_seconds,
    }

    try:
        inputs = await _load_inputs(symbol, max_ages)

        snapshot = build_feature_snapshot(
            symbol,
            candles_15m=inputs["candles"],
            trade_aggregates_1m=inputs["trades"],
            orderbook_latest=inputs["orderbook_latest"],
            orderbook_previous=inputs["orderbook_previous"],
            orderbook_recent=inputs["orderbook_recent"],
            derivatives_latest=inputs["derivatives_latest"],
            derivatives_history=inputs["derivatives_history"],
            liq_1m=inputs["liq_1m"],
            liq_5m=inputs["liq_5m"],
            liq_15m=inputs["liq_15m"],
            max_ages=max_ages,
            now=now,
        )

        tv_context = await fetch_tradingview_context(symbol, "BYBIT")

        active_keys = inputs["active_keys"]
        open_symbols = inputs["open_symbols"]
        duplicate_checker = DuplicateChecker(
            has_duplicate=lambda sym, setup_name, direction: (setup_name, direction) in active_keys,
            has_conflicting_position=lambda sym: sym in open_symbols,
        )

        daily_row = inputs["daily_state_row"]
        daily_state = DailyState(
            daily_trades_count=daily_row.trades_count if daily_row else 0,
            max_trades_per_day=settings.max_trades_per_day,
            consecutive_losses=daily_row.consecutive_losses if daily_row else 0,
            max_consecutive_losses=settings.max_consecutive_losses,
            daily_loss_limit_hit=bool(daily_row.is_locked) if daily_row else False,
        )

        account_equity = inputs["account_equity"] or settings.paper_starting_balance

        result = evaluate_symbol(
            symbol,
            snapshot,
            tv_context,
            account_equity=account_equity,
            risk_per_trade_pct=settings.risk_per_trade_pct,
            tick_size=_DEFAULT_TICK_SIZE,
            qty_step=_DEFAULT_QTY_STEP,
            fee_rate=_DEFAULT_FEE_RATE,
            slippage=_DEFAULT_SLIPPAGE,
            max_leverage=settings.max_leverage,
            min_risk_reward=settings.min_risk_reward,
            signal_expiry_minutes=settings.signal_expiry_minutes,
            enable_liquidation_reversal=settings.enable_liquidation_reversal,
            daily_state=daily_state,
            duplicate_checker=duplicate_checker,
            open_positions_count=inputs["open_positions_count"],
            max_open_positions=settings.max_open_positions,
            config_path=None,  # resolved from STRATEGY_CONFIG_PATH env var by scoring.load_strategy_config
            now=now,
        )

        await _persist_results(
            symbol, snapshot, result, tv_context,
            config_version="v1", signal_expiry_minutes=settings.signal_expiry_minutes,
        )

        m.feature_calculations_total.labels(symbol=symbol).inc()
        for evaluated in result["signals"]:
            m.strategy_evaluations_total.labels(symbol=symbol, setup_name=evaluated.setup.setup_name).inc()
            m.signals_generated_total.labels(
                symbol=symbol, setup_name=evaluated.setup.setup_name, direction=evaluated.setup.direction
            ).inc()
        for evaluated in result["rejected"]:
            m.strategy_evaluations_total.labels(symbol=symbol, setup_name=evaluated.setup.setup_name).inc()
            m.signals_rejected_total.labels(symbol=symbol, setup_name=evaluated.setup.setup_name).inc()
        logger.info(
            "evaluated %s: status=%s regime=%s signals=%d rejected=%d",
            symbol, result["status"], result["regime"]["regime"],
            len(result["signals"]), len(result["rejected"]),
        )
    except Exception:
        m.collector_errors_total.labels(component="analysis_worker").inc()
        logger.exception("evaluation cycle failed for %s", symbol)


def _make_health_server(port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/metrics":
                data = exposition.generate_latest(m.REGISTRY)
                self.send_response(200)
                self.send_header("Content-Type", exposition.CONTENT_TYPE_LATEST)
                self.end_headers()
                self.wfile.write(data)
            elif self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"healthy": true}')
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

    return ThreadingHTTPServer(("0.0.0.0", port), Handler)


async def main_async() -> None:
    try:
        settings = get_trading_settings()
    except Exception as exc:
        logger.critical("refusing to start: invalid trading configuration: %s", exc)
        sys.exit(1)

    port = settings.metrics_port + 1  # avoid clashing with bybit-collector's port
    server = _make_health_server(port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("analysis-worker healthy on :%d, symbols=%s", port, settings.symbols)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    while not stop_event.is_set():
        for symbol in settings.symbols:
            await _evaluate_symbol_cycle(settings, symbol)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.strategy_interval_seconds)
        except asyncio.TimeoutError:
            pass

    server.shutdown()


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
