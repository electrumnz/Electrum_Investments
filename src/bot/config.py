"""Typed configuration loaded from environment variables and config/rules.yaml."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import ExecutionMode

# Indexed by Python weekday, so Monday is 0. Written out rather than taken from
# `calendar.day_abbr`, which is locale-dependent and would render the operator's
# limits in whatever language the droplet happens to be configured for.
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


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

    # Set this and every dashboard route requires a login. Unset, the dashboard
    # behaves as it always did: no login, because it binds to 127.0.0.1 and
    # nothing is published.
    #
    # It lives in the environment and NEVER in the repository. `brand/` is a
    # public GitHub repo served as static files, so a password committed there
    # would be readable in the page source by anyone who opened it — which is
    # not a gate at all.
    dashboard_password: str = Field(default="", alias="DASHBOARD_PASSWORD")

    finnhub_api_key: str = Field(default="", alias="FINNHUB_API_KEY")
    marketaux_api_key: str = Field(default="", alias="MARKETAUX_API_KEY")
    # Reading timelines needs a paid X tier. Absent, the social feed is simply
    # off and the loop says so once at startup.
    x_bearer_token: str = Field(default="", alias="X_BEARER_TOKEN")

    claude_tier: ClaudeTier = Field(default=ClaudeTier.HAIKU, alias="CLAUDE_TIER")

    # The dreamer's tier, separately settable. Unset, it follows CLAUDE_TIER.
    #
    # They are different problems bought at different rates. The decision loop
    # runs 96 times a day reading precise figures for six symbols, so the tier
    # is a real running cost and Haiku is the sensible default. The dreamer runs
    # once a day doing the one thing in this system that genuinely rewards a
    # bigger model — following a causal chain two hops out and then attacking
    # it — and at that cadence the whole year costs single-digit dollars on any
    # tier. Measured: $2.60/yr on Haiku against $13.00/yr on Opus.
    #
    # So this exists to let the cheap thing stay cheap without making the
    # thoughtful thing stupid.
    #
    # It deliberately DOES NOT fall back to CLAUDE_TIER. The default there is
    # Haiku, which has no extended thinking at all, and thinking is the entire
    # mechanism by which a dream gets past its first hop. A dreamer on Haiku
    # produces "AI is big so buy chips": one hop, already priced. Sonnet is the
    # floor rather than the ceiling here — the work wants a model that can think
    # for a while, not the largest one available.
    dream_claude_tier: ClaudeTier = Field(
        default=ClaudeTier.SONNET, alias="DREAM_CLAUDE_TIER"
    )

    @property
    def dream_tier(self) -> ClaudeTier:
        return self.dream_claude_tier

    # Who the operator is, for the interface and the two agents to address.
    #
    # In the ENVIRONMENT rather than the repository, and empty by default, for
    # two reasons. A person's name is theirs and does not belong committed to a
    # public GitHub repo; and every surface that uses it has to work without it,
    # because a deployment that has not set it is a supported configuration
    # rather than a broken one.
    #
    # **It is only ever rendered behind the login.** The sign-in page reveals
    # nothing about the account by design — see `render.login_page` — and a name
    # on it would tell an unauthenticated visitor whose account this is. That is
    # a small disclosure for paper money and it is still a change to a property
    # the page is built around, so it does not happen.
    # One name, used everywhere. There were briefly two — a plain one and a
    # formal one — and the split earned nothing: every surface wanted the same
    # form, so the second field was a choice nobody had to make being offered
    # anyway.
    operator_name: str = Field(default="", alias="OPERATOR_NAME")

    @property
    def greeting_name(self) -> str:
        """What to call the operator, or empty when nobody said."""
        return self.operator_name.strip()

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

    # Two accepted shapes, and the second exists so a second broker is an
    # adapter rather than a redesign.
    #
    #   sessions_utc: [[14, 21]]            same window on every trading day
    #   sessions_utc: {0: [[0, 21], [22, 24]], 6: [[22, 24]]}   per weekday
    #
    # The flat form covers a fixed-window market and a 24/7 one, which is
    # everything Alpaca offers. The mapping form covers CME Globex — futures,
    # crude, gold — which runs Sunday 17:00 CT to Friday 16:00 CT with a
    # 60-minute maintenance break each day. That schedule cannot be written in
    # the flat form at all: Sunday opens only in the evening, so giving Sunday
    # Monday's hours would declare the market open all Sunday morning, and the
    # gate would approve into a shut market for the broker to queue.
    #
    # Multiple windows per day are what express the daily break. Hours only, no
    # minutes: every Globex boundary is a whole hour in Central Time, and the US
    # equity window already rounds 13:30 up to 14:00 on purpose to skip the
    # noisiest half-hour of the open.
    #
    # **A Globex config is wrong for half the year unless it is revisited.**
    # These are UTC and Globex is defined in Central Time, so every boundary
    # moves an hour when US daylight saving starts and ends. Nothing here can
    # detect that; it is a diary entry, not a code problem.
    sessions_utc: list[tuple[int, int]] | dict[int, list[tuple[int, int]]] = Field(
        default_factory=list
    )

    # Which days those hours apply to, as Python weekdays: Monday 0, Sunday 6.
    #
    # Required with the flat form rather than defaulted, because both plausible
    # defaults are wrong for one of the two classes already configured. Mon-Fri
    # would silently forbid crypto at weekends, which is the exact failure the
    # per-instrument split was introduced to fix. All seven would leave equities
    # tradeable on a Saturday, and Alpaca queues an out-of-hours equity order to
    # the next session rather than refusing it, so the fill lands at Monday's
    # open — the half-hour `sessions_utc` deliberately skips. A missing value
    # fails loudly at startup instead.
    #
    # With the mapping form it is DERIVED from the keys, because two places
    # naming the trading days is two places to disagree. Supplying both is
    # allowed only when they match exactly.
    session_days_utc: list[int] = Field(default_factory=list)

    # Operator's rule, stated directly: **this bot does not trade pre-market.**
    # After hours is fine — the stretch from the close onward — and so is the
    # regular session. Only 04:00-09:30 New York is refused.
    #
    # It needs its own field because `sessions_utc` cannot express it, and the
    # reason is daylight saving. That window is fixed UTC hours; the US session
    # is defined in New York time and moves an hour twice a year. So
    # `[[14, 21]]` is the WINTER window applied all year, and in winter it
    # opens at 14:00 UTC — which is 09:00 New York, half an hour of pre-market
    # permitted every day, silently, with nothing in a fixed-UTC window able to
    # detect it.
    #
    # That gap used to be nearly free, because an out-of-hours equity order was
    # simply queued to the next open. It is not free now: Alpaca runs a
    # pre-market session from 04:00 ET, so an order placed into it TRADES, in a
    # thinner book than the operator chose.
    #
    # `market_clock` computes the phase in New York time, so this follows
    # daylight saving without anybody keeping a diary entry, and it is
    # deterministic and offline as everything the gate reads must be. It can
    # only ever NARROW what the UTC window allows — an additional reason to
    # refuse, never a reason to permit.
    #
    # Off by default, and it must stay off for crypto: a 24/7 market has no
    # pre-market, and the phases this reads are the US equity ones.
    refuse_premarket: bool = False

    # Optional ceiling on this class's share of equity, as a fraction of the
    # portfolio. Used to keep a volatile class from quietly dominating.
    capital_cap_pct: float | None = Field(default=None, ge=0, le=100)

    # ------------------------------------------------------ per-class limits
    #
    # Each instrument class carries its own risk limits. Crypto moves harder
    # than the equity book and trades while nobody is awake, so it has no
    # business borrowing the same per-trade budget as the equity side.
    #
    # **A value here OVERRIDES the portfolio limit for this class, in either
    # direction.** `account:` is the default, not a ceiling. Set a class to 3%
    # and that class gets 3%.
    #
    # An override is not silently floored back to the portfolio value, and that
    # is the part worth stating: a config saying 3% while the gate quietly used
    # 1% would be a limit nobody could read off the file, which is worse than
    # either number on its own. The Settings page shows which value is in force
    # per class for the same reason.
    #
    # `None` means "no opinion, use the portfolio limit". Absent is not zero.
    #
    # Worth knowing rather than guarded against: `max_total_risk_pct` is still
    # portfolio-wide, so a per-trade override above it can never actually fill —
    # the total-risk gate refuses the trade that would breach it. If a class
    # limit is raised past the total, raise the total too or the setting does
    # nothing.
    max_risk_per_trade_pct: float | None = Field(default=None, gt=0, le=10)
    max_position_pct: float | None = Field(default=None, gt=0, le=200)

    # Counted WITHIN this class rather than across the account. Two open crypto
    # positions plus three equity ones is five against the global cap and two
    # against this one, which is the point: a class that gets loud should not
    # be able to fill every slot the portfolio has.
    max_concurrent_positions: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _enabled_classes_must_be_usable(self) -> Self:
        if isinstance(self.sessions_utc, dict):
            self._reconcile_days_with_the_mapping()

        for day, windows in self.windows_by_day.items():
            for start, end in windows:
                if not 0 <= start < end <= 24:
                    raise ValueError(
                        f"session window [{start}, {end}] on weekday {day} is not a "
                        "valid range; needs 0 <= start < end <= 24"
                    )

        if not self.enabled:
            return self
        if not self.allowed_symbols:
            raise ValueError("instrument is enabled but allowed_symbols is empty")
        if not self.sessions_utc:
            raise ValueError(
                "instrument is enabled but sessions_utc is empty, so nothing could "
                "ever trade. Use [[0, 24]] for a 24/7 market."
            )
        if not self.session_days_utc:
            raise ValueError(
                "instrument is enabled but session_days_utc is empty, so nothing "
                "could ever trade. Monday is 0 and Sunday is 6: use [0, 1, 2, 3, 4] "
                "for a weekday market and [0, 1, 2, 3, 4, 5, 6] for a 24/7 one."
            )
        if any(day < 0 or day > 6 for day in self.session_days_utc):
            raise ValueError(
                f"session_days_utc must be weekdays 0-6, got {self.session_days_utc}"
            )
        return self

    def _reconcile_days_with_the_mapping(self) -> None:
        """Derive the trading days from the mapping, or prove they agree.

        A day listed in `session_days_utc` with no window in the mapping would
        be a trading day on which nothing can trade, and the reverse would be a
        window on a day the gate calls shut. Both read as a bug in the bot
        rather than a typo in the config, so neither is allowed to exist.
        """
        assert isinstance(self.sessions_utc, dict)
        keys = sorted(self.sessions_utc)
        if any(day < 0 or day > 6 for day in keys):
            raise ValueError(f"sessions_utc keys must be weekdays 0-6, got {keys}")
        if any(not windows for windows in self.sessions_utc.values()):
            raise ValueError(
                "a weekday in sessions_utc has no windows. Leave the day out "
                "entirely rather than listing it as closed."
            )
        if not self.session_days_utc:
            self.session_days_utc = keys
        elif sorted(self.session_days_utc) != keys:
            raise ValueError(
                f"session_days_utc {sorted(self.session_days_utc)} does not match "
                f"the days in sessions_utc {keys}. With per-day windows the "
                "mapping is the authority; omit session_days_utc."
            )

    @property
    def windows_by_day(self) -> dict[int, list[tuple[int, int]]]:
        """Both shapes, normalised to one. Everything downstream reads this."""
        if isinstance(self.sessions_utc, dict):
            return {day: list(w) for day, w in sorted(self.sessions_utc.items())}
        return {day: list(self.sessions_utc) for day in sorted(self.session_days_utc)}

    # These three live here rather than in `risk.py` so that the gate and the
    # loop cannot drift apart on what "open" means. The gate uses the two halves
    # separately, because a rejection has to say whether it was the day or the
    # hour; the loop only asks the combined question. A second implementation of
    # either would be a silent disagreement waiting to happen — the loop skipping
    # a session the gate would have allowed, and nothing to say why.

    def is_trading_day(self, moment: datetime) -> bool:
        return moment.weekday() in self.windows_by_day

    def is_within_hours(self, moment: datetime) -> bool:
        """Within today's windows, which differ by day under the mapping form."""
        windows = self.windows_by_day.get(moment.weekday(), [])
        return any(start <= moment.hour < end for start, end in windows)

    def is_in_session(self, moment: datetime) -> bool:
        return self.is_trading_day(moment) and self.is_within_hours(moment)

    def render_sessions(self) -> str:
        """One human-readable line. Collapses the flat form back to a summary."""
        by_day = self.windows_by_day
        if not by_day:
            return "none"
        hours = {tuple(w) for w in by_day.values()}
        if len(hours) == 1:
            windows = next(iter(by_day.values()))
            return ", ".join(f"{s:02d}:00-{e:02d}:00" for s, e in windows)
        return "; ".join(
            f"{DAY_NAMES[day]} "
            + ", ".join(f"{s:02d}:00-{e:02d}:00" for s, e in windows)
            for day, windows in by_day.items()
        )


class SocialRules(BaseModel):
    """Accounts whose posts are worth reading before trading.

    Context only. Nothing here reaches `RiskGate`, and adding a blackout window
    after a high-impact post would be a change to what the gate refuses, which
    belongs in its own commit with a test that proves it rejects.
    """

    enabled: bool = False
    # Handles, without the @. Kept short on purpose: this is a feed of accounts
    # that move prices, not a timeline.
    accounts: list[str] = Field(default_factory=list)
    lookback_minutes: int = Field(default=90, gt=0, le=1440)
    max_posts: int = Field(default=12, gt=0, le=100)

    @model_validator(mode="after")
    def _enabled_needs_accounts(self) -> SocialRules:
        if self.enabled and not self.accounts:
            raise ValueError(
                "social.enabled is true but accounts is empty, so the feed would "
                "fetch nothing every cycle and report itself healthy."
            )
        return self


class WatchlistRules(BaseModel):
    """Symbols the ticker tape shows. **Display only, never a permission.**

    Deliberately NOT `instruments.*.allowed_symbols`, and the distinction is
    the whole reason this block exists. That list is what `RiskGate` will let
    the bot trade; this one is what an operator wants to see scrolling past.
    Growing the tape to fifteen names by extending `allowed_symbols` would
    quietly grant the bot nine new instruments to open positions in, which is
    a change to what may be traded wearing the costume of a display tweak.

    So: nothing here reaches the gate, and a symbol on the tape is not
    tradeable unless it is also in an enabled instrument class. The renderer
    marks the ones that are.
    """

    enabled: bool = True
    symbols: list[str] = Field(default_factory=list)
    # Its own cadence, slower than the account poll. Fifteen quotes every five
    # seconds is 180 requests a minute against a per-minute limit; once a
    # minute is nowhere near it, and a tape is not a figure anyone trades off.
    refresh_seconds: float = Field(default=60.0, gt=0)

    @model_validator(mode="after")
    def _enabled_needs_symbols(self) -> WatchlistRules:
        if self.enabled and not self.symbols:
            raise ValueError(
                "watchlist.enabled is true but symbols is empty, so the tape "
                "would render nothing and look broken rather than switched off."
            )
        return self


class LoopRules(BaseModel):
    """How the decision loop spends its time and money.

    **Not risk rules.** Nothing here reaches `RiskGate`, and nothing here can
    permit a trade the gate would refuse — the worst a wrong value does is stop
    the model being asked, never widen what it is allowed to answer. Kept in a
    separate block so that stays obvious.
    """

    skip_model_call_when_all_markets_closed: bool = Field(
        default=True,
        description=(
            "Skip the model call on a cycle where no enabled instrument class "
            "is in session. Reconcile, stand-down state and expiry alerts still "
            "run every cycle."
        ),
    )


class Rules(BaseModel):
    account: AccountRules
    frequency: FrequencyRules
    margin: MarginRules = Field(default_factory=MarginRules)
    stand_down: StandDownRules = Field(default_factory=StandDownRules)
    options: OptionRules = Field(default_factory=OptionRules)
    news_blackout_minutes_before: int = Field(ge=0)
    news_blackout_minutes_after: int = Field(ge=0)

    social: SocialRules = Field(default_factory=SocialRules)
    loop: LoopRules = Field(default_factory=LoopRules)
    watchlist: WatchlistRules = Field(default_factory=WatchlistRules)

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

    def classes_in_session(self, moment: datetime) -> list[str]:
        """Enabled classes whose window is open right now.

        Derived from the instrument blocks, exactly like `allowed_symbols`, so
        enabling crypto makes the whole account a seven-day operation without
        anything else being edited. Nothing here holds a second copy of the
        equity hours.
        """
        return sorted(
            name
            for name, instrument in self.enabled_instruments.items()
            if instrument.is_in_session(moment)
        )

    def any_class_in_session(self, moment: datetime) -> bool:
        return bool(self.classes_in_session(moment))

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
