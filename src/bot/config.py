"""Typed configuration loaded from environment variables and config/rules.yaml."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import ExecutionMode


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

    # Chat is off unless this is set. Fail-closed: a dashboard that gains the
    # ability to drive an agent should do so because someone decided to, not
    # because they deployed a new version.
    dashboard_chat_token: str = Field(default="", alias="DASHBOARD_CHAT_TOKEN")

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

    @property
    def execution_mode(self) -> ExecutionMode:
        """Where orders would actually go, before any stand-down is applied.

        Derived from ALPACA_PAPER_TRADE rather than being its own env var, so
        there is exactly one switch and it is the one Alpaca itself uses.
        """
        return ExecutionMode.PAPER if self.alpaca_paper_trade else ExecutionMode.LIVE

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
    """Portfolio-level limits.

    The two that matter are risk-based: `max_risk_per_trade_pct` and
    `max_total_risk_pct`. Both measure what would be lost if stops fill, which
    is leverage-neutral — the same rule means the same thing on cash equities,
    on margin, on options or on futures. Notional-based caps do not have that
    property, which is why they are relegated to sanity checks here.
    """

    min_equity_floor_usd: float = Field(ge=0)

    # The load-bearing pair.
    max_risk_per_trade_pct: float = Field(gt=0, le=10)
    max_total_risk_pct: float = Field(gt=0, le=20)

    # Concentration sanity check, not the binding constraint. A 1%-risk trade
    # with a 2% stop implies a position worth 50% of equity, so this has to be
    # generous or it would silently become the real limit.
    max_position_pct: float = Field(gt=0, le=200)

    max_concurrent_positions: int = Field(gt=0)
    daily_loss_kill_pct: float = Field(gt=0, le=100)

    @model_validator(mode="after")
    def _total_risk_covers_one_trade(self) -> Self:
        if self.max_total_risk_pct < self.max_risk_per_trade_pct:
            raise ValueError(
                f"max_total_risk_pct ({self.max_total_risk_pct}) is below "
                f"max_risk_per_trade_pct ({self.max_risk_per_trade_pct}), so no "
                "trade could ever be opened"
            )
        return self


class MarginRules(BaseModel):
    """Guards against Intraday Margin Deficit calls.

    Replaces the Pattern Day Trader gate, which FINRA retired on 2026-06-04
    (Regulatory Notice 26-10). Alpaca now rejects orders that would create a
    margin deficit in real time, and repeated non-compliance inside five
    business days triggers a 90-day account restriction — a far worse outcome
    than a skipped trade, which is the same reasoning the PDT gate used.
    """

    max_buying_power_utilisation_pct: float = Field(default=50.0, gt=0, le=100)
    max_gross_notional_pct: float = Field(
        default=150.0,
        gt=0,
        description="Reg T permits 200% overnight; the default leaves headroom",
    )


class StandDownRules(BaseModel):
    """Consecutive-loss circuit breaker.

    Exists to interrupt revenge trading: a run of losses is when discipline is
    weakest and position sizing gets worst. Breaching it demotes live trading to
    paper for a fixed period — trading continues, the money stops.
    """

    consecutive_losses_trigger: int = Field(default=3, gt=0)
    loss_threshold_r: float = Field(
        default=0.25,
        ge=0,
        description="Losses smaller than this many R are scratches, not losses",
    )
    stage_one_days: int = Field(default=3, gt=0)
    stage_two_days: int = Field(default=10, gt=0)
    repeat_window_days: int = Field(
        default=30,
        gt=0,
        description="A second trigger inside this window escalates to stage two",
    )

    @model_validator(mode="after")
    def _stage_two_is_longer(self) -> Self:
        if self.stage_two_days <= self.stage_one_days:
            raise ValueError(
                f"stage_two_days ({self.stage_two_days}) must exceed "
                f"stage_one_days ({self.stage_one_days}) — escalation has to escalate"
            )
        return self


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


class OptionRules(BaseModel):
    """Expiry safety for option positions.

    Alpaca auto-exercises anything $0.01 in the money, liquidates un-fundable
    in-the-money positions inside the final hour, and does not accept "Do Not
    Exercise" through the API. So an expiry that arrives unnoticed is not a
    missed opportunity — it is the broker making the decision instead.
    """

    # How far ahead to start warning. A week gives time to plan an exit rather
    # than react to one.
    warn_days_before_expiry: float = Field(default=7.0, gt=0)

    # Refuse to open anything new in a contract closer than this to expiry.
    # Only relevant once the bot can trade options; harmless before then.
    min_days_to_expiry_for_entry: float = Field(default=3.0, ge=0)

    # Treat an approaching expiry as requiring action, not merely noting it.
    escalate_to_action_days: float = Field(default=1.0, gt=0)


class InstrumentRules(BaseModel):
    """Rules that differ by asset class.

    Session windows are the reason this exists. Equities trade a fixed window;
    crypto trades continuously. Holding both under one top-level `sessions_utc`
    meant enabling crypto silently forbade trading it for three quarters of the
    day, which is not a limit anyone would choose on purpose.
    """

    enabled: bool = False
    strategy: str = Field(
        default="unspecified",
        description="Label recorded on every trade so metrics can separate them",
    )
    allowed_symbols: list[str] = Field(default_factory=list)
    sessions_utc: list[tuple[int, int]] = Field(default_factory=list)

    # Optional ceiling on this class's share of equity, as a fraction of the
    # portfolio. Used to keep a volatile class from quietly dominating.
    capital_cap_pct: float | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def _enabled_classes_must_be_usable(self) -> Self:
        if not self.enabled:
            return self
        if not self.allowed_symbols:
            raise ValueError("instrument is enabled but allowed_symbols is empty")
        if not self.sessions_utc:
            raise ValueError(
                "instrument is enabled but sessions_utc is empty, so nothing could "
                "ever trade. Use [[0, 24]] for a 24/7 market."
            )
        return self


class Rules(BaseModel):
    account: AccountRules
    frequency: FrequencyRules
    margin: MarginRules = Field(default_factory=MarginRules)
    stand_down: StandDownRules = Field(default_factory=StandDownRules)
    options: OptionRules = Field(default_factory=OptionRules)
    news_blackout_minutes_before: int = Field(ge=0)
    news_blackout_minutes_after: int = Field(ge=0)

    # Keyed by AssetClass value: "us_equity", "crypto".
    instruments: dict[str, InstrumentRules] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Rules:
        with path.open() as f:
            return cls.model_validate(yaml.safe_load(f))

    @property
    def enabled_instruments(self) -> dict[str, InstrumentRules]:
        return {name: i for name, i in self.instruments.items() if i.enabled}

    @property
    def allowed_symbols(self) -> list[str]:
        """Every tradeable symbol across enabled classes.

        Derived rather than configured, so call sites that just want a watchlist
        (`fetch_market_ticks`, the system prompt) keep working unchanged.
        """
        symbols: list[str] = []
        for instrument in self.enabled_instruments.values():
            symbols.extend(instrument.allowed_symbols)
        return sorted(set(symbols))

    def for_symbol(self, symbol: str) -> InstrumentRules | None:
        """Which instrument class a symbol belongs to, if any enabled one claims it."""
        for instrument in self.enabled_instruments.values():
            if symbol in instrument.allowed_symbols:
                return instrument
        return None

    def class_name_for(self, symbol: str) -> str | None:
        for name, instrument in self.enabled_instruments.items():
            if symbol in instrument.allowed_symbols:
                return name
        return None

    def is_symbol_allowed(self, symbol: str) -> bool:
        return self.for_symbol(symbol) is not None

    def strategy_for(self, symbol: str) -> str:
        instrument = self.for_symbol(symbol)
        return instrument.strategy if instrument else "unspecified"


DEFAULT_RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "rules.yaml"


def load_rules(path: Path | None = None) -> Rules:
    return Rules.load(path or DEFAULT_RULES_PATH)
