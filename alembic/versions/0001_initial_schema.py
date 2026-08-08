"""Block 1 initial schema: all trading-system tables.

Non-destructive by construction (only CREATE TABLE / CREATE INDEX); the
downgrade drops the same tables in reverse dependency order, so this
migration is fully reversible in an empty/dev database. It intentionally
creates the full Block 2/3 schema now (feature_snapshots, market_regimes,
strategy_signals, paper_* tables, strategy_metrics) even though most of it
is unused until later blocks, per the plan.

Revision ID: 0001
Revises:
Create Date: 2026-08-08

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PRICE = sa.Numeric(20, 8)
QTY = sa.Numeric(24, 10)
MONEY = sa.Numeric(20, 8)


def upgrade() -> None:
    op.create_table(
        "instruments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("exchange", sa.String(16), nullable=False, server_default="bybit"),
        sa.Column("market", sa.String(16), nullable=False, server_default="linear"),
        sa.Column("base_asset", sa.String(16), nullable=False),
        sa.Column("quote_asset", sa.String(16), nullable=False),
        sa.Column("tick_size", PRICE, nullable=False, server_default="0.1"),
        sa.Column("qty_step", QTY, nullable=False, server_default="0.001"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("symbol", name="uq_instruments_symbol"),
    )

    op.create_table(
        "candles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("interval", sa.String(8), nullable=False),
        sa.Column("open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", PRICE, nullable=False),
        sa.Column("high", PRICE, nullable=False),
        sa.Column("low", PRICE, nullable=False),
        sa.Column("close", PRICE, nullable=False),
        sa.Column("volume", QTY, nullable=False),
        sa.Column("turnover", QTY, nullable=False, server_default="0"),
        sa.Column("is_closed", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("source", sa.String(16), nullable=False, server_default="rest_backfill"),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "symbol", "interval", "open_time", name="uq_candles_symbol_interval_open_time"
        ),
    )
    op.create_index(
        "ix_candles_symbol_interval_open_time", "candles", ["symbol", "interval", "open_time"]
    )

    op.create_table(
        "trade_aggregates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("bucket_seconds", sa.Integer(), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("buy_taker_volume", QTY, nullable=False, server_default="0"),
        sa.Column("sell_taker_volume", QTY, nullable=False, server_default="0"),
        sa.Column("delta", QTY, nullable=False, server_default="0"),
        sa.Column("trade_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("avg_trade_size", QTY, nullable=False, server_default="0"),
        sa.Column("largest_trade_size", QTY, nullable=False, server_default="0"),
        sa.Column("large_trade_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "symbol", "bucket_seconds", "bucket_start", name="uq_trade_aggregates_symbol_bucket"
        ),
    )
    op.create_index(
        "ix_trade_aggregates_symbol_bucket_start",
        "trade_aggregates",
        ["symbol", "bucket_seconds", "bucket_start"],
    )

    op.create_table(
        "orderbook_feature_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("best_bid", PRICE, nullable=True),
        sa.Column("best_ask", PRICE, nullable=True),
        sa.Column("spread", PRICE, nullable=True),
        sa.Column("spread_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("mid_price", PRICE, nullable=True),
        sa.Column("microprice", PRICE, nullable=True),
        sa.Column("bid_depth", QTY, nullable=True),
        sa.Column("ask_depth", QTY, nullable=True),
        sa.Column("imbalance", sa.Numeric(8, 6), nullable=True),
        sa.Column("depth_bands_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("is_consistent", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_orderbook_feature_snapshots_symbol_ts",
        "orderbook_feature_snapshots",
        ["symbol", "source_timestamp"],
    )

    op.create_table(
        "liquidation_aggregates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("bucket_seconds", sa.Integer(), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("long_liq_value", QTY, nullable=False, server_default="0"),
        sa.Column("short_liq_value", QTY, nullable=False, server_default="0"),
        sa.Column("long_liq_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("short_liq_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("largest_liq_value", QTY, nullable=False, server_default="0"),
        sa.Column("imbalance", sa.Numeric(8, 6), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "symbol", "bucket_seconds", "bucket_start", name="uq_liquidation_aggregates_symbol_bucket"
        ),
    )
    op.create_index(
        "ix_liquidation_aggregates_symbol_bucket_start",
        "liquidation_aggregates",
        ["symbol", "bucket_seconds", "bucket_start"],
    )

    op.create_table(
        "derivatives_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("open_interest", QTY, nullable=True),
        sa.Column("open_interest_value", QTY, nullable=True),
        sa.Column("funding_rate", sa.Numeric(14, 10), nullable=True),
        sa.Column("next_funding_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mark_price", PRICE, nullable=True),
        sa.Column("index_price", PRICE, nullable=True),
        sa.Column("basis", PRICE, nullable=True),
        sa.Column("basis_pct", sa.Numeric(12, 6), nullable=True),
        sa.Column("long_short_ratio", sa.Numeric(12, 6), nullable=True),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_derivatives_snapshots_symbol_ts", "derivatives_snapshots", ["symbol", "source_timestamp"]
    )

    op.create_table(
        "feature_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("price_action_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("volume_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("orderbook_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("futures_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("liquidations_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("data_quality_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("missing_fields_json", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("stale_fields_json", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("source_timestamps_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("is_tradeable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_feature_snapshots_symbol_ts", "feature_snapshots", ["symbol", "as_of"])

    op.create_table(
        "market_regimes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("regime", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False, server_default="0"),
        sa.Column("reasons_json", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("warnings_json", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_market_regimes_symbol_ts", "market_regimes", ["symbol", "as_of"])

    signal_status = postgresql.ENUM(
        "CANDIDATE", "REJECTED", "ACTIVE", "EXPIRED", "CANCELLED",
        "FILLED", "TP1", "TP2", "STOPPED", "CLOSED",
        name="signal_status",
    )
    signal_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "strategy_signals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("setup_name", sa.String(64), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("market_regime", sa.String(32), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "CANDIDATE", "REJECTED", "ACTIVE", "EXPIRED", "CANCELLED",
                "FILLED", "TP1", "TP2", "STOPPED", "CLOSED",
                name="signal_status", create_type=False,
            ),
            nullable=False,
            server_default="CANDIDATE",
        ),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("entry_min", PRICE, nullable=True),
        sa.Column("entry_max", PRICE, nullable=True),
        sa.Column("stop_loss", PRICE, nullable=True),
        sa.Column("take_profit_1", PRICE, nullable=True),
        sa.Column("take_profit_2", PRICE, nullable=True),
        sa.Column("risk_reward_1", sa.Numeric(8, 4), nullable=True),
        sa.Column("risk_reward_2", sa.Numeric(8, 4), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidation_reason", sa.String(256), nullable=True),
        sa.Column("feature_snapshot_id", sa.Integer(), sa.ForeignKey("feature_snapshots.id"), nullable=True),
        sa.Column("tradingview_context_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("bybit_context_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("risk_context_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("rejection_reasons_json", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("strategy_config_version", sa.String(32), nullable=True),
    )
    op.create_index("ix_strategy_signals_symbol", "strategy_signals", ["symbol"])
    op.create_index("ix_strategy_signals_created_at", "strategy_signals", ["created_at"])
    op.create_index("ix_strategy_signals_setup_name", "strategy_signals", ["setup_name"])
    op.create_index("ix_strategy_signals_status", "strategy_signals", ["status"])
    op.create_index("ix_strategy_signals_expires_at", "strategy_signals", ["expires_at"])

    op.create_table(
        "signal_components",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("signal_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("strategy_signals.id"), nullable=False),
        sa.Column("component_name", sa.String(64), nullable=False),
        sa.Column("weight", sa.Numeric(6, 2), nullable=False),
        sa.Column("raw_value", sa.Numeric(12, 6), nullable=True),
        sa.Column("contribution", sa.Numeric(6, 2), nullable=False, server_default="0"),
        sa.Column("notes", sa.String(256), nullable=True),
    )
    op.create_index("ix_signal_components_signal_id", "signal_components", ["signal_id"])

    op.create_table(
        "paper_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("signal_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("strategy_signals.id"), nullable=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("order_type", sa.String(16), nullable=False),
        sa.Column("price", PRICE, nullable=True),
        sa.Column("quantity", QTY, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="NEW"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_paper_orders_signal_id", "paper_orders", ["signal_id"])

    op.create_table(
        "paper_fills",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("paper_orders.id"), nullable=False),
        sa.Column("price", PRICE, nullable=False),
        sa.Column("quantity", QTY, nullable=False),
        sa.Column("fee", MONEY, nullable=False, server_default="0"),
        sa.Column("slippage", PRICE, nullable=False, server_default="0"),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_paper_fills_order_id", "paper_fills", ["order_id"])

    op.create_table(
        "paper_positions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("signal_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("strategy_signals.id"), nullable=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("entry_price", PRICE, nullable=True),
        sa.Column("quantity", QTY, nullable=False, server_default="0"),
        sa.Column("stop_loss", PRICE, nullable=True),
        sa.Column("take_profit_1", PRICE, nullable=True),
        sa.Column("take_profit_2", PRICE, nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="OPEN"),
        sa.Column("realized_pnl", MONEY, nullable=False, server_default="0"),
        sa.Column("fees_paid", MONEY, nullable=False, server_default="0"),
        sa.Column("funding_paid", MONEY, nullable=False, server_default="0"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_paper_positions_symbol_status", "paper_positions", ["symbol", "status"])

    op.create_table(
        "paper_equity_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("balance", MONEY, nullable=False),
        sa.Column("equity", MONEY, nullable=False),
        sa.Column("open_positions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("daily_pnl", MONEY, nullable=False, server_default="0"),
    )
    op.create_index("ix_paper_equity_snapshots_ts", "paper_equity_snapshots", ["as_of"])

    op.create_table(
        "daily_risk_state",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("starting_equity", MONEY, nullable=False),
        sa.Column("realized_pnl", MONEY, nullable=False, server_default="0"),
        sa.Column("trades_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consecutive_losses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("lock_reason", sa.String(128), nullable=True),
    )
    op.create_index("ix_daily_risk_state_date", "daily_risk_state", ["trading_date"], unique=True)

    op.create_table(
        "strategy_metrics",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("setup_name", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("market_regime", sa.String(32), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trades", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("win_rate", sa.Numeric(6, 4), nullable=True),
        sa.Column("avg_r", sa.Numeric(8, 4), nullable=True),
        sa.Column("expectancy", sa.Numeric(10, 4), nullable=True),
        sa.Column("profit_factor", sa.Numeric(10, 4), nullable=True),
        sa.Column("max_drawdown", sa.Numeric(10, 4), nullable=True),
        sa.Column("extra_json", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_strategy_metrics_setup_regime", "strategy_metrics", ["setup_name", "market_regime"])

    op.create_table(
        "system_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False, server_default="INFO"),
        sa.Column("component", sa.String(64), nullable=False),
        sa.Column("message", sa.String(512), nullable=False),
        sa.Column("context_json", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_system_events_created_at", "system_events", ["created_at"])
    op.create_index("ix_system_events_event_type", "system_events", ["event_type"])


def downgrade() -> None:
    op.drop_table("system_events")
    op.drop_table("strategy_metrics")
    op.drop_table("daily_risk_state")
    op.drop_table("paper_equity_snapshots")
    op.drop_table("paper_positions")
    op.drop_table("paper_fills")
    op.drop_table("paper_orders")
    op.drop_table("signal_components")
    op.drop_table("strategy_signals")
    postgresql.ENUM(name="signal_status").drop(op.get_bind(), checkfirst=True)
    op.drop_table("market_regimes")
    op.drop_table("feature_snapshots")
    op.drop_table("derivatives_snapshots")
    op.drop_table("liquidation_aggregates")
    op.drop_table("orderbook_feature_snapshots")
    op.drop_table("trade_aggregates")
    op.drop_table("candles")
    op.drop_table("instruments")
