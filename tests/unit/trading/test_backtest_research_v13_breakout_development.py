from pathlib import Path

import pytest

from tradingview_mcp.core.backtest.comparison_breakout_v13 import (
    validate_v13_development_cache_root,
)


def test_development_runner_rejects_reserved_holdout_path(tmp_path: Path) -> None:
    reserved = tmp_path / "holdout_mrv2_120d" / "cache"
    with pytest.raises(ValueError, match="reserved 120d holdout"):
        validate_v13_development_cache_root(reserved)


def test_development_runner_accepts_normal_development_path(tmp_path: Path) -> None:
    development = tmp_path / "development" / "cache"
    assert validate_v13_development_cache_root(development) == development.resolve()
