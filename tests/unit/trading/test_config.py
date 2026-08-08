from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from tradingview_mcp.core.config.trading_settings import TradingSettings


def _env(**overrides):
    base = dict(
        TRADING_MODE="paper",
        TRADING_SYMBOLS="BTCUSDT",
        DATABASE_URL="postgresql+asyncpg://trading:change_me@postgres:5432/trading",
        REDIS_URL="redis://redis:6379/0",
    )
    base.update(overrides)
    return base


def test_valid_config_constructs():
    settings = TradingSettings(_env_file=None, **_env())
    assert settings.trading_mode == "paper"
    assert settings.symbols == ["BTCUSDT"]


def test_rejects_non_paper_trading_mode():
    with pytest.raises(ValidationError):
        TradingSettings(_env_file=None, **_env(TRADING_MODE="live"))


def test_rejects_empty_symbol_list():
    with pytest.raises(ValidationError):
        TradingSettings(_env_file=None, **_env(TRADING_SYMBOLS=""))


def test_rejects_malformed_database_url():
    with pytest.raises(ValidationError):
        TradingSettings(_env_file=None, **_env(DATABASE_URL="not-a-url"))


def test_rejects_non_asyncpg_database_scheme():
    with pytest.raises(ValidationError):
        TradingSettings(_env_file=None, **_env(DATABASE_URL="mysql://trading:x@host/db"))


def test_rejects_database_url_missing_db_name():
    with pytest.raises(ValidationError):
        TradingSettings(_env_file=None, **_env(DATABASE_URL="postgresql+asyncpg://trading:x@postgres:5432/"))


def test_rejects_bad_redis_url():
    with pytest.raises(ValidationError):
        TradingSettings(_env_file=None, **_env(REDIS_URL="http://redis:6379/0"))


def test_rejects_zero_or_negative_risk_pct():
    with pytest.raises(ValidationError):
        TradingSettings(_env_file=None, **_env(RISK_PER_TRADE_PCT="0"))
    with pytest.raises(ValidationError):
        TradingSettings(_env_file=None, **_env(MAX_DAILY_LOSS_PCT="-1"))


def test_rejects_excessive_leverage():
    with pytest.raises(ValidationError):
        TradingSettings(_env_file=None, **_env(MAX_LEVERAGE="50"))


def test_rejects_invalid_orderbook_depth():
    with pytest.raises(ValidationError):
        TradingSettings(_env_file=None, **_env(ORDERBOOK_DEPTH="17"))


def test_rejects_invalid_market():
    with pytest.raises(ValidationError):
        TradingSettings(_env_file=None, **_env(BYBIT_MARKET="spot"))


def test_symbols_property_parses_and_uppercases_csv():
    settings = TradingSettings(_env_file=None, **_env(TRADING_SYMBOLS="btcusdt, ethusdt"))
    assert settings.symbols == ["BTCUSDT", "ETHUSDT"]
