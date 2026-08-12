"""Block 2: Feature Engine, Market Regime Classifier, Strategy Engine,
Scoring Engine, hard gates, and Risk Engine.

Everything here is deterministic and independent of any LLM/AI — entry,
stop-loss, take-profit, and score are always computed by this code, never
guessed at runtime by a language model. `core/services/strategy_engine_service.py`
is the only orchestration seam Block 3's MCP tools will call into.
"""
