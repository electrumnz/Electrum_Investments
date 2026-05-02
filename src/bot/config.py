"""Typed configuration loaded from environment variables and config/rules.yaml."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BotMode(StrEnum):
    DEMO = "demo"
    LIVE = "live"


class ClaudeTier(StrEnum):
    HAIKU = "haiku"
    SONNET = "sonnet"
    OPUS = "opus"


CLAUDE_MODEL_IDS: dict[ClaudeTier, str] = {
    ClaudeTier.HAIKU: "claude-haiku-4-5-20251001",
    ClaudeTier.SONNET: "claude-sonnet-4-6",
    ClaudeTier.OPUS: "claude-opus-4-7",
}


class Env(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")

    mt5_login: int = Field(default=0, alias="MT5_LOGIN")
    mt5_password: str = Field(default="", alias="MT5_PASSWORD")
    mt5_server: str = Field(default="BlackBull-Demo", alias="MT5_SERVER")

    finnhub_api_key: str = Field(default="", alias="FINNHUB_API_KEY")
    marketaux_api_key: str = Field(default="", alias="MARKETAUX_API_KEY")

    bot_mode: BotMode = Field(default=BotMode.DEMO, alias="BOT_MODE")
    claude_tier: ClaudeTier = Field(default=ClaudeTier.HAIKU, alias="CLAUDE_TIER")
    decision_interval_seconds: int = Field(default=900, alias="DECISION_INTERVAL_SECONDS")


class AccountRules(BaseModel):
    min_equity_floor_usd: float
    max_risk_per_trade_pct: float = Field(gt=0, le=10)
    max_concurrent_positions: int = Field(gt=0)
    max_gross_leverage: float = Field(gt=0)
    daily_loss_kill_pct: float = Field(gt=0, le=100)


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
    sessions_utc: list[tuple[int, int]]
    news_blackout_minutes_before: int = Field(ge=0)
    news_blackout_minutes_after: int = Field(ge=0)
    allowed_symbols: list[str]
    crypto_sleeve: CryptoSleeve
    min_hold_seconds: int = Field(ge=0)
    required_order_fields: list[str]

    @classmethod
    def load(cls, path: Path) -> Rules:
        with path.open() as f:
            return cls.model_validate(yaml.safe_load(f))

    def is_symbol_allowed(self, symbol: str) -> bool:
        if symbol in self.allowed_symbols:
            return True
        if self.crypto_sleeve.enabled and symbol in self.crypto_sleeve.allowed_symbols:
            return True
        return False
