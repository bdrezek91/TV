from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradingview_mcp.core.backtest.comparison_breakout_v14 import (
    V14_NAME,
    detect_v14_setup,
    validate_v14_development_cache_root,
)


def _chain(*, regime: str = "TREND_UP", trigger_close: str = "105") -> dict:
    history = [
        {"open": Decimal("100"), "high": Decimal("101"), "low": Decimal("99"), "close": Decimal("100")}
        for _ in range(16)
    ]
    trigger = {
        "open": Decimal("100"),
        "high": Decimal(trigger_close),
        "low": Decimal("99.5"),
        "close": Decimal(trigger_close),
    }
    return {
        "regime": {"primary_regime": regime},
        "features": {"tf": {"1h": {"atr": Decimal("10")}}},
        "_windows": SimpleNamespace(candles_15m=history + [trigger]),
    }


def test_detects_fixed_channel_breakout_in_regime_direction() -> None:
    setup = detect_v14_setup(_chain())
    assert setup["direction"] == "LONG"
    assert setup["detection_version"] == V14_NAME
    assert setup["regime_compatible"] is True


def test_rejects_breakout_against_directional_regime() -> None:
    setup = detect_v14_setup(_chain(regime="TREND_DOWN", trigger_close="105"))
    assert setup["direction"] == "NO_SETUP"


def test_rejects_non_directional_regime() -> None:
    setup = detect_v14_setup(_chain(regime="RANGE"))
    assert setup["direction"] == "NO_SETUP"


def test_development_runner_rejects_reserved_holdout_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="reserved 120d holdout"):
        validate_v14_development_cache_root(tmp_path / "holdout_mrv2_120d" / "cache")


def test_development_runner_accepts_revealed_data_path(tmp_path: Path) -> None:
    path = tmp_path / "revealed_90d" / "cache"
    assert validate_v14_development_cache_root(path) == path.resolve()
