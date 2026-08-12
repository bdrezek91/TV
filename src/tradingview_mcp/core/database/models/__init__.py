"""ORM models for the trading-system extension.

Tables are grouped by concern into separate modules but all share the same
:class:`~tradingview_mcp.core.database.base.Base` metadata, so Alembic
autogeneration sees the full schema regardless of which submodule is
imported first. Import this package (not an individual submodule) to make
sure every model is registered before calling ``Base.metadata.create_all``
or running Alembic autogenerate.
"""
from __future__ import annotations

from tradingview_mcp.core.database.models.market_data import (  # noqa: F401
    Candle,
    DerivativesSnapshot,
    Instrument,
    LiquidationAggregate,
    OrderbookFeatureSnapshot,
    TradeAggregate,
)
from tradingview_mcp.core.database.models.analysis import (  # noqa: F401
    FeatureSnapshot,
    MarketRegime,
)
from tradingview_mcp.core.database.models.signals import (  # noqa: F401
    SignalComponent,
    SignalStatus,
    StrategySignal,
)
from tradingview_mcp.core.database.models.paper_trading import (  # noqa: F401
    DailyRiskState,
    PaperEquitySnapshot,
    PaperFill,
    PaperOrder,
    PaperPosition,
    StrategyMetrics,
)
from tradingview_mcp.core.database.models.system import SystemEvent  # noqa: F401

__all__ = [
    "Instrument",
    "Candle",
    "TradeAggregate",
    "OrderbookFeatureSnapshot",
    "LiquidationAggregate",
    "DerivativesSnapshot",
    "FeatureSnapshot",
    "MarketRegime",
    "StrategySignal",
    "SignalComponent",
    "SignalStatus",
    "PaperOrder",
    "PaperFill",
    "PaperPosition",
    "PaperEquitySnapshot",
    "DailyRiskState",
    "StrategyMetrics",
    "SystemEvent",
]
