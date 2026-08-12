"""Strategy implementations. Every strategy returns a `StrategySetup` or
`None` (meaning: this strategy found nothing to propose this cycle — which
is different from a setup that was proposed and then hard-gated/rejected).
Entry/stop/target numbers are always computed here deterministically —
never guessed by an LLM at runtime.
"""
from tradingview_mcp.core.analysis.strategies.base import StrategySetup  # noqa: F401
