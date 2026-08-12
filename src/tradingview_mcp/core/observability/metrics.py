"""Prometheus metrics registry for the trading-system extension.

Metric names follow the exact list required by the plan. Importing this
module never touches the network or requires a running Prometheus — it
only registers metric objects in the default (or an injectable) registry,
so it's safe to import from unit tests.
"""
from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge

# A dedicated registry (rather than the global default) keeps repeated test
# imports from raising "Duplicated timeseries" during pytest collection.
REGISTRY = CollectorRegistry(auto_describe=True)

bybit_ws_connected = Gauge(
    "bybit_ws_connected",
    "1 if the Bybit public WebSocket is currently connected, else 0",
    ["symbol"],
    registry=REGISTRY,
)

bybit_last_message_age_seconds = Gauge(
    "bybit_last_message_age_seconds",
    "Seconds since the last message was received on the Bybit WebSocket",
    ["symbol", "topic"],
    registry=REGISTRY,
)

bybit_messages_total = Counter(
    "bybit_messages_total",
    "Total Bybit WebSocket messages received",
    ["symbol", "topic"],
    registry=REGISTRY,
)

bybit_reconnects_total = Counter(
    "bybit_reconnects_total",
    "Total Bybit WebSocket reconnect attempts",
    ["symbol", "reason"],
    registry=REGISTRY,
)

orderbook_consistency_errors_total = Counter(
    "orderbook_consistency_errors_total",
    "Total order book sequence-gap / consistency errors detected",
    ["symbol"],
    registry=REGISTRY,
)

collector_errors_total = Counter(
    "collector_errors_total",
    "Total unhandled errors in the Bybit collector",
    ["component"],
    registry=REGISTRY,
)

database_write_errors_total = Counter(
    "database_write_errors_total",
    "Total failed database write attempts",
    ["table"],
    registry=REGISTRY,
)

rest_poll_errors_total = Counter(
    "rest_poll_errors_total",
    "Total failed REST poller requests",
    ["endpoint"],
    registry=REGISTRY,
)

# ── Block 3: Feature/Strategy Engine + paper trading metrics ────────────────

feature_calculations_total = Counter(
    "feature_calculations_total",
    "Total feature snapshots computed",
    ["symbol"],
    registry=REGISTRY,
)

strategy_evaluations_total = Counter(
    "strategy_evaluations_total",
    "Total strategy evaluation cycles run",
    ["symbol", "setup_name"],
    registry=REGISTRY,
)

signals_generated_total = Counter(
    "signals_generated_total",
    "Total CANDIDATE signals generated",
    ["symbol", "setup_name", "direction"],
    registry=REGISTRY,
)

signals_rejected_total = Counter(
    "signals_rejected_total",
    "Total signals rejected by hard gates or the Risk Engine",
    ["symbol", "setup_name"],
    registry=REGISTRY,
)

paper_orders_total = Counter(
    "paper_orders_total",
    "Total paper orders created",
    ["symbol", "direction", "order_type"],
    registry=REGISTRY,
)

paper_pnl_total = Counter(
    "paper_pnl_total",
    "Cumulative realized paper PnL magnitude processed (see paper_equity for signed current value)",
    ["symbol"],
    registry=REGISTRY,
)

paper_equity = Gauge(
    "paper_equity",
    "Current paper-trading account equity",
    registry=REGISTRY,
)

daily_drawdown_pct = Gauge(
    "daily_drawdown_pct",
    "Current day's drawdown from the day's starting equity, in percent",
    registry=REGISTRY,
)
