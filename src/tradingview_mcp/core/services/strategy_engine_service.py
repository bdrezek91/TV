"""Strategy Engine orchestrator (Block 2).

Wires together, in order: Feature Engine snapshot -> TradingView context
fusion -> Market Regime Classifier -> each active strategy -> Scoring
Engine -> hard gates -> Risk Engine -> a persisted `StrategySignal` (either
CANDIDATE or REJECTED, always with full context/reasons).

This is a pure orchestration layer over the modules in `core/analysis/*`
and `core/trading/*` — no business logic lives here that isn't already in
those modules, and `server.py` does not import this module in Block 2 (no
new MCP tools yet; that's Block 3). `workers/analysis_worker_main.py` is
the intended caller once wired to real DB-backed inputs.

Nothing here calls this project's own MCP server over HTTP, and nothing
here computes entry/stop/target/score at "LLM discretion" — every number
comes from the deterministic modules this function calls.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, List, Optional

from tradingview_mcp.core.analysis import hard_gates as gates
from tradingview_mcp.core.analysis import regime_classifier
from tradingview_mcp.core.analysis import scoring as scoring_mod
from tradingview_mcp.core.analysis.risk_engine import RiskEngineInput, evaluate_risk
from tradingview_mcp.core.analysis.strategies import breakout_retest, liquidation_reversal, trend_pullback
from tradingview_mcp.core.analysis.strategies.base import StrategySetup
from tradingview_mcp.core.analysis.tradingview_context import (
    TradingViewContext,
    resolve_context_conflict,
)
from tradingview_mcp.core.database.models.signals import SignalStatus


@dataclass
class DailyState:
    daily_trades_count: int = 0
    max_trades_per_day: int = 5
    consecutive_losses: int = 0
    max_consecutive_losses: int = 3
    daily_loss_limit_hit: bool = False


@dataclass
class DuplicateChecker:
    """Injectable so the orchestrator stays DB-free/pure for unit tests;
    a DB-backed implementation is supplied in production via
    `signals_repository.has_active_duplicate`."""

    has_duplicate: Callable[[str, str, str], bool] = lambda symbol, setup, direction: False
    has_conflicting_position: Callable[[str], bool] = lambda symbol: False


@dataclass
class EvaluatedSetup:
    setup: StrategySetup
    status: SignalStatus
    score: Optional[int]
    risk_reward_1: Optional[Decimal]
    risk_reward_2: Optional[Decimal]
    reasons: List[str]
    warnings: List[str]
    components: List[dict]
    risk_result: Optional[dict]
    regime: str
    rejection_reasons: List[str]


def _strategy_registry(enable_liquidation_reversal: bool):
    registry = [
        ("trend_pullback", trend_pullback.evaluate, False),
        ("breakout_retest", breakout_retest.evaluate, False),
    ]
    if enable_liquidation_reversal:
        registry.append(
            (
                "liquidation_exhaustion_reversal",
                lambda f, tv, r: liquidation_reversal.evaluate(f, tv, r, enabled=True),
                True,
            )
        )
    return registry


def evaluate_symbol(
    symbol: str,
    features: dict,
    tv_context: TradingViewContext,
    *,
    account_equity: Decimal,
    risk_per_trade_pct: Decimal,
    tick_size: Decimal,
    qty_step: Decimal,
    fee_rate: Decimal,
    slippage: Decimal,
    max_leverage: Decimal,
    max_spread_bps: Decimal = Decimal("8"),
    min_risk_reward: Decimal = Decimal("2.0"),
    signal_expiry_minutes: int = 60,
    enable_liquidation_reversal: bool = False,
    daily_state: Optional[DailyState] = None,
    duplicate_checker: Optional[DuplicateChecker] = None,
    open_positions_count: int = 0,
    max_open_positions: int = 1,
    config_path: Optional[str] = None,
    now: Optional[dt.datetime] = None,
) -> dict:
    """Returns:
        {
          "status": "OPPORTUNITIES_FOUND" | "NO_TRADE",
          "regime": {...},
          "signals": [EvaluatedSetup for approved candidates],
          "rejected": [EvaluatedSetup for rejected setups],
          "reasons": [str, ...]   # only set when status == NO_TRADE
        }
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    daily_state = daily_state or DailyState()
    duplicate_checker = duplicate_checker or DuplicateChecker()
    config = scoring_mod.load_strategy_config(config_path)
    allowed_by_strategy = config.get("regimes", {}).get("allowed_by_strategy", {})

    regime_result = regime_classifier.classify_regime(features)
    regime = regime_result["regime"]

    orderbook = features.get("orderbook") or {}
    spread_bps = orderbook.get("spread_bps")
    spread_bps_dec = Decimal(str(spread_bps)) if spread_bps is not None else None

    signals: List[EvaluatedSetup] = []
    rejected: List[EvaluatedSetup] = []
    no_setup_reasons: List[str] = []

    for setup_name, evaluator, is_experimental_strategy in _strategy_registry(enable_liquidation_reversal):
        setup = evaluator(features, tv_context, regime)
        if setup is None:
            no_setup_reasons.append(f"{setup_name}: no setup found this cycle")
            continue

        bybit_bias = "BULLISH" if setup.direction == "LONG" else "BEARISH"
        conflict = resolve_context_conflict(tv_context, bybit_bias)

        score_result = scoring_mod.score_setup(setup.component_fractions, config_path=config_path)
        total_score = max(0, score_result.total_score - conflict.score_penalty)

        rr1 = setup.risk_reward(setup.take_profit_1)
        rr2 = setup.risk_reward(setup.take_profit_2)

        allowed_regimes = allowed_by_strategy.get(setup.setup_name, [])
        ctx = gates.GateContext(
            is_tradeable=bool(features.get("is_tradeable", False)),
            orderbook_consistent=bool(orderbook.get("is_consistent", False)),
            spread_bps=spread_bps_dec,
            max_spread_bps=max_spread_bps,
            entry_price=setup.entry_reference,
            stop_loss=setup.stop_loss,
            risk_reward=rr1,
            min_risk_reward=min_risk_reward,
            expires_at=now + dt.timedelta(minutes=signal_expiry_minutes),
            now=now,
            regime=regime,
            regime_allowed_for_strategy=regime in allowed_regimes,
            duplicate_active_exists=duplicate_checker.has_duplicate(symbol, setup.setup_name, setup.direction),
            daily_trades_count=daily_state.daily_trades_count,
            max_trades_per_day=daily_state.max_trades_per_day,
            consecutive_losses=daily_state.consecutive_losses,
            max_consecutive_losses=daily_state.max_consecutive_losses,
            conflicting_position_exists=duplicate_checker.has_conflicting_position(symbol),
            daily_loss_limit_hit=daily_state.daily_loss_limit_hit,
        )
        gate_result = gates.evaluate_hard_gates(ctx)

        reasons = list(setup.reasons)
        warnings = list(setup.warnings) + conflict.notes
        components = [
            {
                "name": c.name, "weight": c.weight, "fraction": c.fraction, "contribution": c.contribution,
            }
            for c in score_result.components
        ]

        risk_result_dict: Optional[dict] = None
        status = SignalStatus.CANDIDATE
        final_reasons = list(gate_result.reasons)

        if setup.experimental:
            # Never routed to risk sizing or paper orders, regardless of
            # whether hard gates would have passed.
            status = SignalStatus.REJECTED
            final_reasons = final_reasons or []
            final_reasons.append("experimental strategy — never produces a paper order")
        elif not gate_result.passed:
            status = SignalStatus.REJECTED
        else:
            risk_input = RiskEngineInput(
                account_equity=account_equity,
                entry_price=setup.entry_reference,
                stop_loss=setup.stop_loss,
                risk_per_trade_pct=risk_per_trade_pct,
                tick_size=tick_size,
                qty_step=qty_step,
                fee_rate=fee_rate,
                slippage=slippage,
                max_leverage=max_leverage,
                min_risk_reward=min_risk_reward,
                risk_reward=rr1,
                daily_loss_limit_hit=daily_state.daily_loss_limit_hit,
                consecutive_losses=daily_state.consecutive_losses,
                max_consecutive_losses=daily_state.max_consecutive_losses,
                daily_trades_count=daily_state.daily_trades_count,
                max_trades_per_day=daily_state.max_trades_per_day,
                open_positions_count=open_positions_count,
                max_open_positions=max_open_positions,
            )
            risk_result = evaluate_risk(risk_input)
            risk_result_dict = risk_result.to_dict()
            if not risk_result.approved:
                status = SignalStatus.REJECTED
                final_reasons = final_reasons + risk_result.rejection_reasons

        evaluated = EvaluatedSetup(
            setup=setup,
            status=status,
            score=total_score,
            risk_reward_1=rr1,
            risk_reward_2=rr2,
            reasons=reasons,
            warnings=warnings,
            components=components,
            risk_result=risk_result_dict,
            regime=regime,
            rejection_reasons=final_reasons,
        )
        if status == SignalStatus.CANDIDATE:
            signals.append(evaluated)
        else:
            rejected.append(evaluated)
            no_setup_reasons.extend(final_reasons)

    overall_status = "OPPORTUNITIES_FOUND" if signals else "NO_TRADE"
    result = {
        "status": overall_status,
        "regime": regime_result,
        "signals": signals,
        "rejected": rejected,
    }
    if overall_status == "NO_TRADE":
        result["reasons"] = no_setup_reasons or ["no strategy produced a qualifying setup this cycle"]
    return result
