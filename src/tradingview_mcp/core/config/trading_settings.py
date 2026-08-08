"""Centralised, environment-driven configuration for the trading-system extension.

This module is entirely separate from the existing TradingView MCP tool
configuration (`.env` keys such as ``MARKETAUX_API_TOKEN`` / proxy settings
are untouched). It only governs the new Bybit collector / paper-trading
stack introduced in Block 1 onward.

Design goals (see plan "Zasady bezwzględne" / "Konfiguracja"):

- Everything comes from environment variables — never hardcode secrets.
- The system must refuse to start if the configuration is unsafe:
  * ``TRADING_MODE`` must be exactly ``"paper"`` (live trading is out of
    scope for this project entirely; there is no code path that could ever
    place a real order).
  * ``TRADING_SYMBOLS`` must be non-empty.
  * Risk parameters must be within sane bounds.
  * ``DATABASE_URL`` must at least look like a valid asyncpg DSN.

Import-time construction is deliberately avoided: call
``get_trading_settings()`` (cached) or ``TradingSettings()`` directly so unit
tests can construct arbitrary variations without import-order surprises.
"""
from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import List

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingSettingsError(ValueError):
    """Raised when the trading-system configuration is invalid or unsafe."""


class TradingSettings(BaseSettings):
    """Environment-backed configuration for the Bybit collector & friends.

    Every field maps 1:1 to an entry in ``.env.example``. Values are parsed
    and range-checked eagerly (at construction time), so a broken
    configuration fails fast at process startup instead of surfacing as a
    confusing runtime error hours later.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Mode / safety ──────────────────────────────────────────────────
    trading_mode: str = Field(default="paper", alias="TRADING_MODE")

    # ── Bybit ──────────────────────────────────────────────────────────
    bybit_testnet: bool = Field(default=False, alias="BYBIT_TESTNET")
    bybit_market: str = Field(default="linear", alias="BYBIT_MARKET")
    trading_symbols: str = Field(default="BTCUSDT", alias="TRADING_SYMBOLS")

    # ── Storage ────────────────────────────────────────────────────────
    database_url: str = Field(
        default="postgresql+asyncpg://trading:change_me@postgres:5432/trading",
        alias="DATABASE_URL",
    )
    redis_url: str = Field(default="redis://redis:6379/0", alias="REDIS_URL")

    # ── Collector feature flags ───────────────────────────────────────
    collect_trades: bool = Field(default=True, alias="COLLECT_TRADES")
    collect_orderbook: bool = Field(default=True, alias="COLLECT_ORDERBOOK")
    orderbook_depth: int = Field(default=50, alias="ORDERBOOK_DEPTH")
    collect_liquidations: bool = Field(default=True, alias="COLLECT_LIQUIDATIONS")
    collect_tickers: bool = Field(default=True, alias="COLLECT_TICKERS")
    collect_klines: bool = Field(default=True, alias="COLLECT_KLINES")

    # ── Intervals (seconds) ───────────────────────────────────────────
    feature_interval_seconds: int = Field(default=60, alias="FEATURE_INTERVAL_SECONDS")
    strategy_interval_seconds: int = Field(default=300, alias="STRATEGY_INTERVAL_SECONDS")
    oi_poll_interval_seconds: int = Field(default=300, alias="OI_POLL_INTERVAL_SECONDS")
    funding_poll_interval_seconds: int = Field(
        default=300, alias="FUNDING_POLL_INTERVAL_SECONDS"
    )
    orderbook_snapshot_interval_seconds: int = Field(
        default=5, alias="ORDERBOOK_SNAPSHOT_INTERVAL_SECONDS"
    )

    # ── Staleness thresholds (seconds) ────────────────────────────────
    max_data_age_trades_seconds: int = Field(default=10, alias="MAX_DATA_AGE_TRADES_SECONDS")
    max_data_age_orderbook_seconds: int = Field(
        default=5, alias="MAX_DATA_AGE_ORDERBOOK_SECONDS"
    )
    max_data_age_oi_seconds: int = Field(default=600, alias="MAX_DATA_AGE_OI_SECONDS")
    max_data_age_candles_seconds: int = Field(
        # This is checked against the *open_time* of the most recently
        # closed 15m candle (see feature_engine.build_feature_snapshot's
        # candles_15m usage) — that open_time is, by construction, always
        # 15-30 minutes in the past (a 15m bar isn't "closed" until 15
        # minutes after it opened, and the next one only closes 15 minutes
        # after that). A threshold below ~20 minutes can never be
        # satisfied regardless of how promptly the collector refreshes,
        # which silently pins is_tradeable=False forever. 1200s (20min)
        # covers one full bar plus slack for collector poll latency.
        default=1200, alias="MAX_DATA_AGE_CANDLES_SECONDS"
    )

    # ── Paper trading risk params (used from Block 2 onward, validated now) ──
    paper_starting_balance: Decimal = Field(
        default=Decimal("10000"), alias="PAPER_STARTING_BALANCE"
    )
    risk_per_trade_pct: Decimal = Field(default=Decimal("0.25"), alias="RISK_PER_TRADE_PCT")
    max_daily_loss_pct: Decimal = Field(default=Decimal("1.00"), alias="MAX_DAILY_LOSS_PCT")
    max_open_positions: int = Field(default=1, alias="MAX_OPEN_POSITIONS")
    max_trades_per_day: int = Field(default=5, alias="MAX_TRADES_PER_DAY")
    max_consecutive_losses: int = Field(default=3, alias="MAX_CONSECUTIVE_LOSSES")
    min_risk_reward: Decimal = Field(default=Decimal("2.0"), alias="MIN_RISK_REWARD")
    max_leverage: Decimal = Field(default=Decimal("3"), alias="MAX_LEVERAGE")

    # ── Signal params (used from Block 2 onward) ──────────────────────
    signal_min_score: int = Field(default=75, alias="SIGNAL_MIN_SCORE")
    signal_cooldown_minutes: int = Field(default=45, alias="SIGNAL_COOLDOWN_MINUTES")
    signal_expiry_minutes: int = Field(default=60, alias="SIGNAL_EXPIRY_MINUTES")

    enable_liquidation_reversal: bool = Field(
        default=False, alias="ENABLE_LIQUIDATION_REVERSAL"
    )

    # ── Raw data archival ──────────────────────────────────────────────
    raw_data_archive_enabled: bool = Field(default=False, alias="RAW_DATA_ARCHIVE_ENABLED")
    raw_data_archive_path: str = Field(default="/data/archive", alias="RAW_DATA_ARCHIVE_PATH")

    # ── Telegram (used from Block 3 onward) ───────────────────────────
    telegram_enabled: bool = Field(default=False, alias="TELEGRAM_ENABLED")
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")

    # ── Metrics / health server ───────────────────────────────────────
    metrics_port: int = Field(default=9100, alias="TRADING_METRICS_PORT")

    # -- Derived helpers -------------------------------------------------

    @property
    def symbols(self) -> List[str]:
        """Parsed, upper-cased, de-duplicated symbol list."""
        return [s.strip().upper() for s in self.trading_symbols.split(",") if s.strip()]

    # -- Validators --------------------------------------------------------

    @field_validator("trading_mode")
    @classmethod
    def _validate_trading_mode(cls, v: str) -> str:
        v_norm = (v or "").strip().lower()
        if v_norm != "paper":
            raise TradingSettingsError(
                "TRADING_MODE must be 'paper'. Live trading is not implemented and "
                "this project must never place real orders. "
                f"Got: {v!r}"
            )
        return v_norm

    @field_validator("bybit_market")
    @classmethod
    def _validate_market(cls, v: str) -> str:
        if v.lower() not in {"linear", "inverse"}:
            raise TradingSettingsError(
                f"BYBIT_MARKET must be 'linear' or 'inverse', got {v!r}"
            )
        return v.lower()

    @field_validator("trading_symbols")
    @classmethod
    def _validate_symbols_nonempty(cls, v: str) -> str:
        parsed = [s.strip() for s in (v or "").split(",") if s.strip()]
        if not parsed:
            raise TradingSettingsError("TRADING_SYMBOLS must not be empty")
        return v

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, v: str) -> str:
        if not v or "://" not in v:
            raise TradingSettingsError(f"DATABASE_URL is malformed: {v!r}")
        if not v.startswith("postgresql+asyncpg://") and not v.startswith("postgresql://"):
            raise TradingSettingsError(
                "DATABASE_URL must use the postgresql(+asyncpg):// scheme, "
                f"got {v!r}"
            )
        # Must have a host and a database name component.
        tail = v.split("://", 1)[1]
        if "@" not in tail:
            raise TradingSettingsError(f"DATABASE_URL is missing credentials/host: {v!r}")
        host_and_db = tail.split("@", 1)[1]
        if "/" not in host_and_db or host_and_db.split("/", 1)[1] == "":
            raise TradingSettingsError(f"DATABASE_URL is missing a database name: {v!r}")
        return v

    @field_validator("redis_url")
    @classmethod
    def _validate_redis_url(cls, v: str) -> str:
        if not v.startswith("redis://") and not v.startswith("rediss://"):
            raise TradingSettingsError(f"REDIS_URL must start with redis:// or rediss://, got {v!r}")
        return v

    @field_validator(
        "risk_per_trade_pct",
        "max_daily_loss_pct",
    )
    @classmethod
    def _validate_positive_pct(cls, v: Decimal, info) -> Decimal:  # noqa: ANN001
        if v <= 0:
            raise TradingSettingsError(f"{info.field_name} must be > 0, got {v}")
        if v > Decimal("100"):
            raise TradingSettingsError(f"{info.field_name} must be <= 100, got {v}")
        return v

    @field_validator("max_open_positions", "max_trades_per_day", "max_consecutive_losses")
    @classmethod
    def _validate_positive_int(cls, v: int, info) -> int:  # noqa: ANN001
        if v <= 0:
            raise TradingSettingsError(f"{info.field_name} must be > 0, got {v}")
        return v

    @field_validator("min_risk_reward")
    @classmethod
    def _validate_min_rr(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise TradingSettingsError(f"MIN_RISK_REWARD must be > 0, got {v}")
        return v

    @field_validator("max_leverage")
    @classmethod
    def _validate_max_leverage(cls, v: Decimal) -> Decimal:
        if v <= 0 or v > Decimal("20"):
            raise TradingSettingsError(
                f"MAX_LEVERAGE must be in (0, 20] for a paper/intraday system, got {v}"
            )
        return v

    @field_validator("orderbook_depth")
    @classmethod
    def _validate_depth(cls, v: int) -> int:
        if v not in {1, 50, 200, 500}:
            raise TradingSettingsError(
                f"ORDERBOOK_DEPTH must be one of Bybit's supported depths (1, 50, 200, 500), got {v}"
            )
        return v

    @model_validator(mode="after")
    def _cross_field_checks(self) -> "TradingSettings":
        if not self.symbols:
            raise TradingSettingsError("TRADING_SYMBOLS resolved to an empty symbol list")
        if self.paper_starting_balance <= 0:
            raise TradingSettingsError("PAPER_STARTING_BALANCE must be > 0")
        return self


@lru_cache(maxsize=1)
def get_trading_settings() -> TradingSettings:
    """Return the process-wide cached :class:`TradingSettings` instance.

    Raises:
        TradingSettingsError (via pydantic ValidationError wrapping it): if
        the environment is unsafe or malformed. Callers (workers, health
        checks) should let this propagate at startup rather than swallow it.
    """
    return TradingSettings()  # type: ignore[call-arg]


def reset_trading_settings_cache() -> None:
    """Clear the cached settings instance (test helper)."""
    get_trading_settings.cache_clear()
