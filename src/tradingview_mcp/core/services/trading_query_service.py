"""Query/orchestration service backing the Block 3 MCP tools.

Every function here is what a `server.py` handler delegates to — thin
parameter validation lives in `server.py`, everything else (DB reads,
assembling the response shape, computing aggregate stats) lives here, per
the plan's "server.py stays a thin layer" rule.

Nothing in this module ever places an order, calls this project's own MCP
over HTTP, or lets an LLM compute a number that should come from the
deterministic engine — it only *reads* what Block 1/2's collector,
Feature Engine, and Strategy Engine already computed and persisted.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence
from uuid import UUID

from tradingview_mcp.core.config.trading_settings import get_trading_settings
from tradingview_mcp.core.database.repositories import query_repository as q
from tradingview_mcp.core.database.repositories.signal_components_repository import get_components_for_signal
from tradingview_mcp.core.database.session import session_scope
from tradingview_mcp.core.errors import ErrorCode, make_error
from tradingview_mcp.core.market_data.bybit.reconnect import data_age_seconds, is_stale
from tradingview_mcp.core.services.system_health_service import build_health_report

MAX_SCAN_RESULTS = 2


def _dec_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    return str(v)


async def bybit_market_state(symbol: str) -> Dict[str, Any]:
    """Snapshot of everything Bybit-side known about `symbol` right now."""
    symbol = symbol.upper().strip()
    async with session_scope() as session:
        orderbook = await q.get_latest_orderbook_snapshot(session, symbol)
        derivatives = await q.get_latest_derivatives_snapshot(session, symbol)
        regime = await q.get_latest_market_regime(session, symbol)
        liq_1m = await q.get_recent_liquidation_aggregates(session, symbol, 60, limit=15)
        trades_1m = await q.get_recent_trade_aggregates(session, symbol, 60, limit=15)

    now = dt.datetime.now(dt.timezone.utc)
    settings = None
    try:
        settings = get_trading_settings()
    except Exception:  # noqa: BLE001
        pass

    orderbook_age = data_age_seconds(orderbook.source_timestamp if orderbook else None, now=now)
    derivatives_age = data_age_seconds(derivatives.source_timestamp if derivatives else None, now=now)
    trades_age = data_age_seconds(trades_1m[-1].bucket_start if trades_1m else None, now=now)

    max_ob_age = settings.max_data_age_orderbook_seconds if settings else 5
    max_oi_age = settings.max_data_age_oi_seconds if settings else 600

    cvd = sum((Decimal(t.delta) for t in trades_1m), Decimal("0")) if trades_1m else None
    delta_last = Decimal(trades_1m[-1].delta) if trades_1m else None

    long_liq = sum((Decimal(l.long_liq_value) for l in liq_1m), Decimal("0")) if liq_1m else Decimal("0")
    short_liq = sum((Decimal(l.short_liq_value) for l in liq_1m), Decimal("0")) if liq_1m else Decimal("0")

    data_ok = bool(orderbook) and not is_stale(orderbook.source_timestamp if orderbook else None, max_ob_age, now=now)
    data_ok = data_ok and bool(derivatives) and not is_stale(derivatives.source_timestamp if derivatives else None, max_oi_age, now=now)

    return {
        "symbol": symbol,
        "generated_at": now.isoformat(),
        "price": {
            "mark_price": _dec_str(derivatives.mark_price) if derivatives else None,
            "index_price": _dec_str(derivatives.index_price) if derivatives else None,
        },
        "spread": {
            "best_bid": _dec_str(orderbook.best_bid) if orderbook else None,
            "best_ask": _dec_str(orderbook.best_ask) if orderbook else None,
            "spread_bps": _dec_str(orderbook.spread_bps) if orderbook else None,
            "microprice": _dec_str(orderbook.microprice) if orderbook else None,
            "imbalance": _dec_str(orderbook.imbalance) if orderbook else None,
            "is_consistent": orderbook.is_consistent if orderbook else False,
        },
        "futures": {
            "open_interest": _dec_str(derivatives.open_interest) if derivatives else None,
            "funding_rate": _dec_str(derivatives.funding_rate) if derivatives else None,
            "basis": _dec_str(derivatives.basis) if derivatives else None,
        },
        "order_flow": {"cvd_recent": _dec_str(cvd), "delta_last_bucket": _dec_str(delta_last)},
        "liquidations": {"long_value_recent": _dec_str(long_liq), "short_value_recent": _dec_str(short_liq)},
        "regime": {
            "regime": regime.regime if regime else "NO_DATA",
            "confidence": _dec_str(regime.confidence) if regime else None,
        } if regime else {"regime": "NO_DATA", "confidence": None},
        "data_freshness": {
            "orderbook_age_seconds": orderbook_age,
            "derivatives_age_seconds": derivatives_age,
            "trades_age_seconds": trades_age,
        },
        "data_quality": "GOOD" if data_ok else "STALE_OR_MISSING",
    }


def _signal_to_dict(signal) -> Dict[str, Any]:
    return {
        "signal_id": str(signal.id),
        "symbol": signal.symbol,
        "setup": signal.setup_name,
        "direction": signal.direction,
        "regime": signal.market_regime,
        "score": signal.score,
        "status": signal.status.value if hasattr(signal.status, "value") else str(signal.status),
        "entry_zone": [_dec_str(signal.entry_min), _dec_str(signal.entry_max)],
        "stop_loss": _dec_str(signal.stop_loss),
        "take_profit_1": _dec_str(signal.take_profit_1),
        "take_profit_2": _dec_str(signal.take_profit_2),
        "risk_reward_1": _dec_str(signal.risk_reward_1),
        "risk_reward_2": _dec_str(signal.risk_reward_2),
        "expires_at": signal.expires_at.isoformat() if signal.expires_at else None,
        "invalidation": signal.invalidation_reason,
        "created_at": signal.created_at.isoformat() if signal.created_at else None,
    }


async def rank_futures_opportunities(
    symbols: Sequence[str],
    *,
    minimum_score: int = 75,
    maximum_results: int = 10,
    include_rejected: bool = False,
) -> Dict[str, Any]:
    symbols = [s.upper().strip() for s in symbols]
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(hours=6)  # only consider recent signals as "current"

    async with session_scope() as session:
        candidates = await q.list_ranked_candidate_signals(
            session, symbols, minimum_score=minimum_score, maximum_results=maximum_results, since=since
        )
        rejected = (
            await q.list_rejected_signals(session, symbols, since=since, limit=20)
            if include_rejected
            else []
        )

    opportunities = [_signal_to_dict(s) for s in candidates]
    result: Dict[str, Any] = {
        "generated_at": now.isoformat(),
        "market": "BYBIT_LINEAR",
        "status": "OPPORTUNITIES_FOUND" if opportunities else "NO_TRADE",
        "opportunities": opportunities,
    }
    if include_rejected:
        result["rejected"] = [
            {
                "symbol": s.symbol, "setup": s.setup_name, "direction": s.direction,
                "reasons": s.rejection_reasons_json,
            }
            for s in rejected
        ]
    if not opportunities:
        result["reasons"] = [
            "No signal reached the minimum score in the lookback window",
            f"minimum_score={minimum_score}, symbols={symbols}",
        ]
    return result


async def get_trade_setup(signal_id: str) -> Dict[str, Any]:
    try:
        sid = UUID(str(signal_id))
    except (ValueError, AttributeError, TypeError):
        return make_error(ErrorCode.INVALID_PARAMETER, f"signal_id is not a valid UUID: {signal_id!r}")

    async with session_scope() as session:
        signal = await q.get_signal_by_id(session, sid)
        if signal is None:
            return make_error(ErrorCode.NO_DATA, f"no signal found with id {signal_id}")
        components = await get_components_for_signal(session, sid)

    payload = _signal_to_dict(signal)
    payload["score_components"] = [
        {
            "name": c.component_name, "weight": _dec_str(c.weight),
            "raw_value": _dec_str(c.raw_value), "contribution": _dec_str(c.contribution),
        }
        for c in components
    ]
    payload["strategy_config_version"] = signal.strategy_config_version
    payload["tradingview_context"] = signal.tradingview_context_json
    payload["bybit_context"] = signal.bybit_context_json
    payload["risk_context"] = signal.risk_context_json
    payload["rejection_reasons"] = signal.rejection_reasons_json
    return payload


async def paper_portfolio_status() -> Dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc)
    async with session_scope() as session:
        equity_snap = await q.get_latest_equity_snapshot(session)
        open_positions = await q.get_open_positions(session)
        daily_state = await q.get_daily_risk_state_for(session, now.date())

    settings = None
    try:
        settings = get_trading_settings()
    except Exception:  # noqa: BLE001
        pass
    starting_balance = settings.paper_starting_balance if settings else Decimal("10000")

    balance = Decimal(equity_snap.balance) if equity_snap else starting_balance
    equity = Decimal(equity_snap.equity) if equity_snap else starting_balance
    daily_pnl = Decimal(daily_state.realized_pnl) if daily_state else Decimal("0")
    max_daily_loss_pct = settings.max_daily_loss_pct if settings else Decimal("1.00")
    risk_per_trade_pct = settings.risk_per_trade_pct if settings else Decimal("0.25")

    daily_loss_limit_amount = equity * max_daily_loss_pct / Decimal("100")
    remaining_risk_budget = max(Decimal("0"), daily_loss_limit_amount + daily_pnl) if daily_pnl < 0 else daily_loss_limit_amount

    return {
        "generated_at": now.isoformat(),
        "balance": _dec_str(balance),
        "equity": _dec_str(equity),
        "open_positions": [
            {
                "position_id": str(p.id), "symbol": p.symbol, "direction": p.direction,
                "entry_price": _dec_str(p.entry_price), "quantity": _dec_str(p.quantity),
                "stop_loss": _dec_str(p.stop_loss), "take_profit_1": _dec_str(p.take_profit_1),
                "take_profit_2": _dec_str(p.take_profit_2),
            }
            for p in open_positions
        ],
        "open_positions_count": len(open_positions),
        "daily_pnl": _dec_str(daily_pnl),
        "daily_loss_limit_pct": _dec_str(max_daily_loss_pct),
        "daily_loss_limit_amount": _dec_str(daily_loss_limit_amount),
        "is_locked": bool(daily_state.is_locked) if daily_state else False,
        "lock_reason": daily_state.lock_reason if daily_state else None,
        "available_risk_budget": _dec_str(remaining_risk_budget),
        "risk_per_trade_pct": _dec_str(risk_per_trade_pct),
    }


async def strategy_performance(
    *,
    days: int = 30,
    setup_name: Optional[str] = None,
    symbol: Optional[str] = None,
    market_regime: Optional[str] = None,
) -> Dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=days)

    async with session_scope() as session:
        closed = await q.get_closed_positions_since(session, since)
        signal_ids = {p.signal_id for p in closed if p.signal_id is not None}
        signal_lookup = {}
        for sid in signal_ids:
            sig = await q.get_signal_by_id(session, sid)
            if sig is not None:
                signal_lookup[sid] = sig

    filtered = []
    for p in closed:
        sig = signal_lookup.get(p.signal_id)
        if symbol and p.symbol != symbol.upper():
            continue
        if setup_name and (sig is None or sig.setup_name != setup_name):
            continue
        if market_regime and (sig is None or sig.market_regime != market_regime):
            continue
        filtered.append((p, sig))

    trades = len(filtered)
    if trades == 0:
        return {
            "generated_at": now.isoformat(), "days": days, "trades": 0,
            "win_rate": None, "avg_r": None, "expectancy": None, "profit_factor": None,
            "max_drawdown": None, "long_vs_short": {}, "by_regime": {},
            "note": "no closed paper trades in this window",
        }

    pnls = [Decimal(p.realized_pnl) for p, _ in filtered]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    win_rate = (Decimal(len(wins)) / Decimal(trades)).quantize(Decimal("0.0001"))
    gross_profit = sum(wins, Decimal("0"))
    gross_loss = -sum(losses, Decimal("0"))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None
    expectancy = (sum(pnls, Decimal("0")) / Decimal(trades)).quantize(Decimal("0.0001"))

    # Max drawdown across the equity path implied by these trades' order.
    running = Decimal("0")
    peak = Decimal("0")
    max_dd = Decimal("0")
    for pnl in pnls:
        running += pnl
        peak = max(peak, running)
        max_dd = min(max_dd, running - peak)

    long_pnls = [Decimal(p.realized_pnl) for p, _ in filtered if p.direction == "LONG"]
    short_pnls = [Decimal(p.realized_pnl) for p, _ in filtered if p.direction == "SHORT"]

    by_regime: Dict[str, Decimal] = {}
    for p, sig in filtered:
        key = sig.market_regime if sig and sig.market_regime else "UNKNOWN"
        by_regime[key] = by_regime.get(key, Decimal("0")) + Decimal(p.realized_pnl)

    return {
        "generated_at": now.isoformat(),
        "days": days,
        "filters": {"setup_name": setup_name, "symbol": symbol, "market_regime": market_regime},
        "trades": trades,
        "win_rate": _dec_str(win_rate),
        "avg_r": None,  # requires per-trade R-multiples, not tracked at the position level in Block 1/2 schema
        "expectancy": _dec_str(expectancy),
        "profit_factor": _dec_str(profit_factor) if profit_factor is not None else None,
        "max_drawdown": _dec_str(max_dd),
        "total_pnl_after_fees": _dec_str(sum(pnls, Decimal("0"))),
        "long_vs_short": {
            "long_trades": len(long_pnls), "long_pnl": _dec_str(sum(long_pnls, Decimal("0"))),
            "short_trades": len(short_pnls), "short_pnl": _dec_str(sum(short_pnls, Decimal("0"))),
        },
        "by_regime": {k: _dec_str(v) for k, v in by_regime.items()},
    }


async def trading_system_health() -> Dict[str, Any]:
    """Extends `system_health_service.build_health_report` with strategy/
    paper-broker specific state, all read from the DB (no live collector
    process handle exists at MCP-tool-call time — that's a separate
    process; this reports the *data* it has left behind)."""
    base = await build_health_report()
    now = dt.datetime.now(dt.timezone.utc)
    settings = None
    try:
        settings = get_trading_settings()
    except Exception:  # noqa: BLE001
        pass

    symbols = settings.symbols if settings else []
    per_symbol = {}
    async with session_scope() as session:
        for sym in symbols:
            ob = await q.get_latest_orderbook_snapshot(session, sym)
            deriv = await q.get_latest_derivatives_snapshot(session, sym)
            per_symbol[sym] = {
                "orderbook_consistent": ob.is_consistent if ob else False,
                "orderbook_age_seconds": data_age_seconds(ob.source_timestamp if ob else None, now=now),
                "derivatives_age_seconds": data_age_seconds(deriv.source_timestamp if deriv else None, now=now),
            }
        equity_snap = await q.get_latest_equity_snapshot(session)
        open_positions = await q.get_open_positions(session)

    base["symbols"] = per_symbol
    base["paper_broker"] = {
        "has_equity_data": equity_snap is not None,
        "open_positions_count": len(open_positions),
    }
    base["strategy_engine"] = {"config_version": "v1"}
    return base


async def run_futures_opportunity_scan(
    *, minimum_score: int = 75, maximum_results: int = MAX_SCAN_RESULTS
) -> Dict[str, Any]:
    """The routine's single entry point: pulls the already-computed
    ranking (never runs live analysis inline), returns at most
    `maximum_results` (capped at 2, per the plan) or NO_TRADE. Places no
    trades."""
    maximum_results = min(maximum_results, MAX_SCAN_RESULTS)
    settings = None
    try:
        settings = get_trading_settings()
    except Exception:  # noqa: BLE001
        pass
    symbols = settings.symbols if settings else ["BTCUSDT"]

    now = dt.datetime.now(dt.timezone.utc)
    health = await trading_system_health()
    system_warnings: List[str] = []
    if health["overall_status"] != "HEALTHY":
        system_warnings.append(f"system health is {health['overall_status']}, not HEALTHY")

    ranking = await rank_futures_opportunities(
        symbols, minimum_score=minimum_score, maximum_results=maximum_results, include_rejected=True
    )

    data_quality = "GOOD" if not system_warnings else "DEGRADED"

    if ranking["status"] == "NO_TRADE":
        return {
            "generated_at": now.isoformat(),
            "market": "BYBIT_LINEAR",
            "status": "NO_TRADE",
            "data_quality": data_quality,
            "opportunities": [],
            "reasons": ranking.get("reasons", ["no qualifying setup found"]),
            "system_warnings": system_warnings,
        }

    return {
        "generated_at": now.isoformat(),
        "market": "BYBIT_LINEAR",
        "status": "OPPORTUNITIES_FOUND",
        "data_quality": data_quality,
        "opportunities": ranking["opportunities"][:MAX_SCAN_RESULTS],
        "rejected_summary": {"count": len(ranking.get("rejected", []))},
        "system_warnings": system_warnings,
    }
