"""MCP-facing query service for the Warstwa 1-5 research pipeline (regime
-> setup -> order-flow confirmation -> risk decision -> paper execution).

Coexists with (does not replace) `trading_query_service.py`, which backs
the OLDER, separate Block 1/2/3 production system (its own regime
classifier, Strategy Engine, and paper broker). Both are exposed as MCP
tools; callers choose which pipeline they want.

Every function here either reads what's already computed/persisted, or
(only `advance_paper_trading`) runs the SAME deterministic, pure pipeline
`scripts/research/audit_paper_trading.py`'s live run already uses,
writing only to this project's own local JSON state under
`artifacts/research/` -- never a real order, never a call to any
exchange's order-placement endpoint. All DB reads go through
`session_scope()` against data the `bybit-collector`/`analysis-worker`
containers already persisted -- nothing here calls a live TradingView/
Bybit fetch inline.
"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import Any, Dict, List, Optional

from tradingview_mcp.core.analysis import paper_execution_v2 as pe
from tradingview_mcp.core.analysis import risk_manager_v2 as rm
from tradingview_mcp.core.config.trading_settings import get_trading_settings
from tradingview_mcp.core.database.session import session_scope
from tradingview_mcp.core.services import paper_broker_service_v2 as pb
from tradingview_mcp.core.services import regime_service_v2 as rs
from tradingview_mcp.core.services import signal_pipeline_v2 as pipeline
from tradingview_mcp.core.services import signal_snapshot_service_v2 as snap

_EMPTY_PORTFOLIO = {"open_positions": [], "daily_realized_pnl_pct": Decimal("0"),
                     "weekly_realized_pnl_pct": Decimal("0"), "btc_regime_flip_detected": False}


def _jsonify(obj: Any) -> Any:
    """Recursively converts Decimal/datetime -> JSON-safe str, matching
    trading_query_service.py's `_dec_str` convention."""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, dt.datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _jsonify(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonify(v) for v in obj]
    return obj


def _tracked_symbols(requested: Optional[List[str]]) -> List[str]:
    settings = get_trading_settings()
    all_symbols = requested or list(settings.symbols)
    return (["BTCUSDT"] if "BTCUSDT" in all_symbols else []) + [s for s in all_symbols if s != "BTCUSDT"]


async def regime_snapshot(symbol: str) -> Dict[str, Any]:
    """Live-computed Warstwa 1 regime classification for one symbol."""
    symbol = symbol.upper().strip()
    async with session_scope() as session:
        result = await rs.classify_symbol(session, symbol)
    result.pop("_state", None)
    return _jsonify(result)


async def trading_chain_snapshot(symbol: str, btc_regime: Optional[str] = None) -> Dict[str, Any]:
    """Full live regime -> setup -> order-flow confirmation -> risk
    decision chain for one symbol. Read-only: evaluates risk against an
    EMPTY portfolio (no persisted paper state consulted or mutated) --
    for the real, stateful picture use `paper_account_status` /
    `advance_paper_trading`."""
    symbol = symbol.upper().strip()
    async with session_scope() as session:
        r = await pipeline.run_pipeline_for_symbol(session, symbol, btc_regime=btc_regime)
    r.pop("_state", None)
    risk_config = rm.DEFAULT_RISK_CONFIG
    risk_decisions = {}
    for setup_type, s in r["setups"].items():
        confirmation = r["confirmations"][setup_type]
        risk_decisions[setup_type] = rm.evaluate_risk(
            symbol, r["as_of"], r["regime"], s, confirmation, r["orderbook"], r["data_quality"],
            _EMPTY_PORTFOLIO, risk_config,
        )
    r["risk_decisions"] = risk_decisions
    return _jsonify(r)


async def market_scan(symbols: Optional[List[str]] = None) -> Dict[str, Any]:
    """Chain across all tracked symbols (or a caller-provided subset),
    compact per-symbol summary. Read-only, same empty-portfolio caveat as
    `trading_chain_snapshot`."""
    ordered = _tracked_symbols(symbols)
    risk_config = rm.DEFAULT_RISK_CONFIG

    results: Dict[str, Any] = {}
    btc_regime = None
    async with session_scope() as session:
        for sym in ordered:
            try:
                r = await pipeline.run_pipeline_for_symbol(session, sym, btc_regime=btc_regime)
                r.pop("_state", None)
                if sym == "BTCUSDT":
                    btc_regime = r["regime"]["primary_regime"]
                chain = {}
                for setup_type, s in r["setups"].items():
                    if s["direction"] == "NO_SETUP":
                        continue
                    confirmation = r["confirmations"][setup_type]
                    risk = rm.evaluate_risk(sym, r["as_of"], r["regime"], s, confirmation, r["orderbook"],
                                             r["data_quality"], _EMPTY_PORTFOLIO, risk_config)
                    chain[setup_type] = {"direction": s["direction"], "setup_confidence": s["confidence"],
                                          "confirmation": confirmation["status"], "risk_decision": risk["decision"],
                                          "reward_to_risk": risk["reward_to_risk"]}
                results[sym] = {"regime": r["regime"]["primary_regime"], "regime_confidence": r["regime"]["confidence"],
                                 "data_quality_score": r["data_quality"].get("data_quality_score"), "chain": chain}
            except Exception as e:  # noqa: BLE001
                results[sym] = {"error": str(e)}
    return _jsonify({"timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
                      "status": "IMPLEMENTATION_COMPLETE_VALIDATION_PENDING", "symbols": results})


async def paper_account_status() -> Dict[str, Any]:
    """Current PERSISTED Warstwa 5 paper-trading account: balance, equity,
    margin, open positions, active (PENDING_ENTRY) orders, daily/weekly
    PnL, drawdown. Reads live prices for unrealized-PnL context but never
    creates or advances an order -- use `advance_paper_trading` for that."""
    config = pe.DEFAULT_PAPER_CONFIG
    state = pb.load_state(config)
    current_prices: Dict[str, Decimal] = {}
    async with session_scope() as session:
        for order in state["orders"].values():
            sym = order["symbol"]
            if sym in current_prices:
                continue
            built = await rs.fetch_and_build_live_features(session, sym, as_of=dt.datetime.now(dt.timezone.utc))
            pa = built["tf"].get("1h") or {}
            if pa.get("available"):
                current_prices[sym] = pa.get("last_close")
    summary = pb.compute_account_summary(state, current_prices, config)
    pb.save_state(state)  # persists the refreshed peak_equity/max_drawdown watermark
    return _jsonify(summary)


async def advance_paper_trading() -> Dict[str, Any]:
    """Runs the FULL live chain (regime -> setup -> order-flow -> risk ->
    paper execution) for every tracked symbol -- identical logic to
    scripts/research/audit_paper_trading.py's live run. Creates new
    simulated orders for APPROVED/APPROVED_REDUCED_SIZE risk decisions
    (deduplicated by signal_id, never twice for the same signal) and
    advances existing PENDING/OPEN orders using already-persisted 1m
    candles. NEVER sends a real order to any exchange. Mutates this
    project's own local JSON paper-trading state and signal log --
    nothing else."""
    ordered = _tracked_symbols(None)
    paper_config = pe.DEFAULT_PAPER_CONFIG
    risk_config = rm.DEFAULT_RISK_CONFIG
    now = dt.datetime.now(dt.timezone.utc)

    state = pb.load_state(paper_config)
    state = pb.apply_daily_weekly_rollover(state, now)
    state.setdefault("last_seen_regime", {})

    all_snapshots: List[dict] = []
    outcome_updates: Dict[str, dict] = {}
    results: Dict[str, Any] = {}
    btc_regime = None
    async with session_scope() as session:
        for sym in ordered:
            try:
                r = await pipeline.run_pipeline_for_symbol(session, sym, btc_regime=btc_regime)
                r.pop("_state", None)
                if sym == "BTCUSDT":
                    btc_regime = r["regime"]["primary_regime"]

                last_regime = state["last_seen_regime"].get(sym)
                regime_changed = last_regime is not None and last_regime != r["regime"]["primary_regime"]
                state["last_seen_regime"][sym] = r["regime"]["primary_regime"]

                risk_decisions = {}
                orders_this_symbol = {}
                for setup_type, s in r["setups"].items():
                    confirmation = r["confirmations"][setup_type]
                    portfolio_state_for_risk = pb.build_portfolio_state_for_risk(state)
                    decision = rm.evaluate_risk(sym, r["as_of"], r["regime"], s, confirmation, r["orderbook"],
                                                 r["data_quality"], portfolio_state_for_risk, risk_config)
                    risk_decisions[setup_type] = decision
                    if s["direction"] not in ("LONG", "SHORT"):
                        continue
                    regime_ts = (r["data_quality"].get("source_timestamps") or {}).get("candles")
                    signal_id = snap.make_signal_id(sym, setup_type, s["direction"], r["as_of"], regime_ts)
                    order = await pb.process_signal(
                        session, sym, signal_id, s, decision, r["as_of"], state, paper_config,
                        regime_changed=regime_changed,
                        regime_change_detail=f"{last_regime} -> {r['regime']['primary_regime']}" if regime_changed else "",
                    )
                    if order is not None:
                        orders_this_symbol[setup_type] = order["status"]
                        outcome_updates[signal_id] = pb.order_to_outcome_update(order, decision.get("risk_amount"))

                results[sym] = {
                    "regime": r["regime"]["primary_regime"],
                    "chain": {t: {"direction": s["direction"], "risk_decision": risk_decisions[t]["decision"],
                                   "paper_status": orders_this_symbol.get(t)}
                              for t, s in r["setups"].items() if s["direction"] != "NO_SETUP"},
                }
                records = snap.build_snapshots(sym, r["as_of"], r["regime"], r["setups"], r["confirmations"],
                                                risk_decisions, r["data_quality"], r["price_at_decision"])
                all_snapshots.extend(records)
            except Exception as e:  # noqa: BLE001
                results[sym] = {"error": str(e)}

    write_result = snap.append_snapshots(all_snapshots)
    update_result = snap.update_snapshot_outcomes(outcome_updates)
    account = pb.compute_account_summary(state, {}, paper_config)
    pb.save_state(state)

    return _jsonify({
        "timestamp": now.isoformat(), "symbols": results, "account_summary": account,
        "snapshots_written": write_result["written"], "outcomes_updated": update_result["updated"],
    })


async def recent_signals(symbol: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
    """Most recent persisted signal snapshots (newest first), optionally
    filtered by symbol -- each includes the full regime/setup/confirmation/
    risk_decision chain and, once available, the outcome Warstwa 5 filled
    in (never fabricated here)."""
    path = snap.SNAPSHOT_PATH
    if not path.exists():
        return {"signals": []}
    symbol = symbol.upper().strip() if symbol else None
    records: List[dict] = []
    for line in reversed(path.read_text().splitlines()):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if symbol and rec.get("symbol") != symbol:
            continue
        records.append(rec)
        if len(records) >= limit:
            break
    return {"signals": records}
