from __future__ import annotations

import pytest

from tradingview_mcp.core.database.orderbook_retention import OrderbookRetentionPolicy
from tradingview_mcp.workers.orderbook_maintenance_main import policy_from_env


def test_default_policy_is_safe() -> None:
    policy = OrderbookRetentionPolicy()
    policy.validate()
    assert policy.high_res_days == 30
    assert policy.archive_days == 180
    assert policy.downsample_seconds == 60
    assert policy.batch_size == 5000


@pytest.mark.parametrize(
    "kwargs",
    [
        {"high_res_days": 0},
        {"high_res_days": 30, "archive_days": 30},
        {"high_res_days": 31, "archive_days": 30},
        {"downsample_seconds": 1},
        {"batch_size": 10},
    ],
)
def test_invalid_policy_is_rejected(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        OrderbookRetentionPolicy(**kwargs).validate()


def test_policy_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORDERBOOK_RETENTION_ENABLED", "true")
    monkeypatch.setenv("ORDERBOOK_HIGH_RES_RETENTION_DAYS", "21")
    monkeypatch.setenv("ORDERBOOK_ARCHIVE_RETENTION_DAYS", "120")
    monkeypatch.setenv("ORDERBOOK_DOWNSAMPLE_SECONDS", "30")
    monkeypatch.setenv("ORDERBOOK_RETENTION_BATCH_SIZE", "2500")

    policy = policy_from_env()
    assert policy.enabled is True
    assert policy.high_res_days == 21
    assert policy.archive_days == 120
    assert policy.downsample_seconds == 30
    assert policy.batch_size == 2500
