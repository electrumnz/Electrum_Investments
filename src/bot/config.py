"""Typed configuration loaded from environment variables and config/rules.yaml."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LiveTradingRefused(RuntimeError):
    """Raised when something tries to start the bot against a live-money account.

    This build is paper-only by design. Going live is a deliberate, separate
    decision that requires the account holder's own KYC — see docs/HANDOFF.md.
    """


class ClaudeTier(StrEnum):
    HAIKU = "haiku"
    SONNET = "sonnet"
    OPUS = "opus"


CLAUDE_MODEL_IDS: dict[ClaudeTier, str] = {
    ClaudeTier.HAIKU: "claude-haiku-4-5-20251001",
    ClaudeTier.SONNET: "claude-sonnet-5",
    ClaudeTier.OPUS: "claude-opus-5",
}

# USD per million tokens, for the running-cost tracker. See docs/COSTS.md.
CLAUDE_PRICING_USD_PER_MTOK: dict[ClaudeTier, tuple[float, float, float]] = {
    # (base input, output, cache read)
    ClaudeTier.HAIKU: (1.0, 5.0, 0.10),
    ClaudeTier.SONNET: (2.0, 10.0, 0.20),
    ClaudeTier.OPUS: (5.0, 25.0, 0.50),
}


class Env(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")

    alpaca_api_key: str = Field(default="", alias="ALPACA_API_KEY")
    alpaca_secret_key: str = Field(default="", alias="ALPACA_SECRET_KEY")
    alpaca_paper_trade: bool = Field(default=True, alias="ALPACA_PAPER_TRADE")

    finnhub_api_key: str = Field(default="", alias="FINNHUB_API_KEY")
    marketaux_api_key: str = Field(default="", alias="MARKETAUX_API_KEY")

    claude_tier: ClaudeTier = Field(default=ClaudeTier.HAIKU, alias="CLAUDE_TIER")
    decision_interval_seconds: int = Field(default=900, alias="DECISION_INTERVAL_SECONDS")

    @property
    def alpaca_base_url(self) -> str:
        return (
            "https://paper-api.alpaca.markets"
            if self.alpaca_paper_trade
            else "https://api.alpaca.markets"
        )

    def assert_paper_only(self) -> None:
        """Refuse to run against real money.

        Called at startup and again inside AlpacaBroker, so flipping the env var
        alone is not enough to reach a live account.
        """
        if not self.alpaca_paper_trade:
            raise LiveTradingRefused(
                "ALPACA_PAPER_TRADE is false, but this build is paper-only.\n"
                "Live trading is deliberately out of scope: it needs the account "
                "holder's own KYC and a reviewed strategy with a demonstrated "
                "track record. See docs/HANDOFF.md before changing this."
            )


class AccountRules(BaseModel):
    min_equity_floor_usd: float = Field(ge=0)
    max_risk_per_trade_pct: float = Field(gt=0, le=10)
    max_position_pct: float = Field(gt=0, le=100)
    min_cash_reserve_pct: float = Field(ge=0, le=100)
    max_concurrent_positions: int = Field(gt=0)
    max_gross_exposure_pct: float = Field(gt=0)
    daily_loss_kill_pct: float = Field(gt=0, le=100)


class FrequencyRules(BaseModel):
    """Anti-churn limits.

    In the Alpha Arena competition the heaviest trader (238 trades) lost 57% of
    its stake while the lightest (38 trades) lost the least of the US models,
    with fees explicitly cited as dominating P&L. Frequency is a risk parameter,
    not a performance one.
    """

    max_trades_per_day: int = Field(gt=0)
    max_trades_per_week: int = Field(gt=0)
    min_seconds_between_trades_per_symbol: int = Field(ge=0)

    @model_validator(mode="after")
    def _weekly_at_least_daily(self) -> Self:
        if self.max_trades_per_week < self.max_trades_per_day:
            raise ValueError(
                f"max_trades_per_week ({self.max_trades_per_week}) is below "
                f"max_trades_per_day ({self.max_trades_per_day})"
            )
        return self


class PdtRules(BaseModel):
    """US Pattern Day Trader rule.

    Under FINRA rules a margin account below the equity threshold may not make
    more than three day trades in any rolling five business days. Breaching it
    triggers a 90-day restriction, so the gate blocks the fourth attempt.
    Crypto is exempt — it is not a security.
    """

    enforce: bool = True
    equity_threshold_usd: float = Field(default=25_000.0, ge=0)
    max_day_trades_per_5_days: int = Field(default=3, ge=0)


class CryptoSleeve(BaseModel):
    enabled: bool
    capital_cap_pct: float = Field(ge=0, le=100)
    allowed_symbols: list[str]

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        if self.enabled and self.capital_cap_pct == 0:
            raise ValueError("crypto_sleeve.enabled=true but capital_cap_pct is 0")
        if self.enabled and not self.allowed_symbols:
            raise ValueError("crypto_sleeve.enabled=true but allowed_symbols is empty")
        return self


class Rules(BaseModel):
    account: AccountRules
    frequency: FrequencyRules
    pdt: PdtRules
    sessions_utc: list[tuple[int, int]]
    news_blackout_minutes_before: int = Field(ge=0)
    news_blackout_minutes_after: int = Field(ge=0)
    allowed_symbols: list[str]
    crypto_sleeve: CryptoSleeve

    @classmethod
    def load(cls, path: Path) -> Rules:
        with path.open() as f:
            return cls.model_validate(yaml.safe_load(f))

    def is_symbol_allowed(self, symbol: str) -> bool:
        if symbol in self.allowed_symbols:
            return True
        return self.crypto_sleeve.enabled and symbol in self.crypto_sleeve.allowed_symbols

    def is_crypto(self, symbol: str) -> bool:
        return symbol in self.crypto_sleeve.allowed_symbols


DEFAULT_RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "rules.yaml"


def load_rules(path: Path | None = None) -> Rules:
    return Rules.load(path or DEFAULT_RULES_PATH)
