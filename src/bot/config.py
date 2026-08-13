"""Typed configuration loaded from environment variables and config/rules.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Self

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import ExecutionMode

if TYPE_CHECKING:  # pragma: no cover - types only, never imported at runtime
    # `DreamingRules` returns these two value objects and nothing else here
    # touches the dream store. Kept behind TYPE_CHECKING so reading a limit
    # never drags a SQLite store into the process — see `vault_caps`.
    from .dreaming import VaultCaps, VaultTTLs

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


@dataclass(frozen=True)
class ModelPricing:
    """USD per million tokens, for the running-cost tracker. See docs/COSTS.md.

    A model has one of these or it has NOTHING — there is deliberately no
    default, no fallback tier and no "close enough" table. An invented price is
    the same class of error as an invented indicator: it produces a figure
    nobody measured, on a page whose whole argument is that its numbers are
    read rather than plausible. `ModelSpec.pricing` is therefore
    `ModelPricing | None`, and `None` travels all the way to the surface as
    "unknown".
    """

    input_usd_per_mtok: float
    output_usd_per_mtok: float
    cache_read_usd_per_mtok: float

    def as_tuple(self) -> tuple[float, float, float]:
        """(base input, output, cache read), the shape the old table used."""
        return (
            self.input_usd_per_mtok,
            self.output_usd_per_mtok,
            self.cache_read_usd_per_mtok,
        )


@dataclass(frozen=True)
class ModelSpec:
    """Which model a call goes to, what a token costs there, and which optional
    request fields that endpoint will actually accept.

    **This replaces "which of three Claude tiers" as the answer to "which
    model".** `CLAUDE_MODEL_IDS[tier]` could only ever name one of three
    Anthropic strings, so a DigitalOcean model could not be named at all. The
    three tiers are now three instances of this rather than the only
    possibility, and `resolve_model_spec` will build one for any id.

    The two booleans are named for the ANTHROPIC fields they control, and that
    is deliberate rather than verbose. `thinking` and `output_config.effort`
    are Anthropic request fields with an Anthropic shape; DigitalOcean's schema
    lists a flat `reasoning_effort` and carries no `output_config` at all, so a
    spec that set a generically-named `takes_effort=True` for a DO model would
    send the wrong field in the wrong shape and get a silently unreasoned
    answer. A vendor in the field name is what makes that mistake visible at
    the point somebody types `True`.

    So a model this repository has never seen gets both False, which sends
    NEITHER field: correct-and-plain rather than wrong. Emitting
    `reasoning_effort` for a provider that wants it is a separate change with
    its own evidence, and it belongs beside the forced-tool-call substitute for
    structured output rather than here.
    """

    model_id: str
    pricing: ModelPricing | None = None
    sends_anthropic_thinking: bool = False
    sends_anthropic_effort: bool = False

    @property
    def price_is_known(self) -> bool:
        """Whether a cost can be computed at all for this model.

        False means nobody has put this model's prices on file — never that it
        is free. Every consumer of a cost has to keep those apart, which is why
        `CallUsage.estimated_cost_usd` is `float | None` rather than a float
        that quietly reads 0.00.
        """
        return self.pricing is not None


# The three Claude tiers, as specs. Prices are USD per million tokens.
#
# Haiku sends neither `thinking` nor `output_config.effort` because it has no
# extended thinking at all; Sonnet and Opus send both. That is exactly the
# `if self._tier in (SONNET, OPUS)` test the transport used to carry inline,
# moved to where the model is described rather than where the request is built.
CLAUDE_MODEL_SPECS: dict[ClaudeTier, ModelSpec] = {
    ClaudeTier.HAIKU: ModelSpec(
        model_id="claude-haiku-4-5-20251001",
        pricing=ModelPricing(1.0, 5.0, 0.10),
        sends_anthropic_thinking=False,
        sends_anthropic_effort=False,
    ),
    ClaudeTier.SONNET: ModelSpec(
        model_id="claude-sonnet-5",
        pricing=ModelPricing(2.0, 10.0, 0.20),
        sends_anthropic_thinking=True,
        sends_anthropic_effort=True,
    ),
    ClaudeTier.OPUS: ModelSpec(
        model_id="claude-opus-5",
        pricing=ModelPricing(5.0, 25.0, 0.50),
        sends_anthropic_thinking=True,
        sends_anthropic_effort=True,
    ),
}


# Every model this repository knows the shape and the price of, keyed by the id
# that goes on the wire.
#
# **A model reaches this table in a commit, with a reason, exactly as a limit
# reaches `config/rules.yaml`.** There is deliberately no environment variable
# carrying a price: a cost typed into `.env` is a figure with no review and no
# history, and the surfaces that render it cannot tell it apart from a measured
# one. Naming a model that is not in here is fully supported — it simply costs
# an unknown amount until somebody adds its prices here.
MODEL_SPECS: dict[str, ModelSpec] = {
    spec.model_id: spec for spec in CLAUDE_MODEL_SPECS.values()
}


def resolve_model_spec(model_id: str, tier: ClaudeTier) -> ModelSpec:
    """The spec for an explicitly named model, or for the Claude tier otherwise.

    Empty `model_id` is today's behaviour exactly — the tier picks one of three
    Claude specs — so an existing deployment on `CLAUDE_TIER=sonnet` builds a
    byte-identical request and computes an identical cost. The same shape as
    `DO_INFERENCE_KEY`: the empty value is the supported default and rollback is
    unsetting the variable rather than editing code.

    An id nobody has priced comes back as a spec with `pricing=None` and both
    Anthropic-only flags False. That is the whole point and it is not a
    degraded state: the model is nameable, the request is plain, and the cost
    is reported as unknown rather than as zero.
    """
    named = model_id.strip()
    if not named:
        return CLAUDE_MODEL_SPECS[tier]
    known = MODEL_SPECS.get(named)
    if known is not None:
        return known
    return ModelSpec(model_id=named)


def _claude_model_ids() -> dict[ClaudeTier, str]:
    return {tier: spec.model_id for tier, spec in CLAUDE_MODEL_SPECS.items()}


def _claude_prices() -> dict[ClaudeTier, tuple[float, float, float]]:
    prices: dict[ClaudeTier, tuple[float, float, float]] = {}
    for tier, spec in CLAUDE_MODEL_SPECS.items():
        if spec.pricing is not None:
            prices[tier] = spec.pricing.as_tuple()
    return prices


# DERIVED from `CLAUDE_MODEL_SPECS`, and kept because `scripts/` reads them to
# label a live run. Two dicts naming the same three models is two places to
# disagree, so neither is written out by hand any more. New code should take a
# `ModelSpec`; these exist so the spec change did not have to reach into files
# it has no business editing.
CLAUDE_MODEL_IDS: dict[ClaudeTier, str] = _claude_model_ids()
CLAUDE_PRICING_USD_PER_MTOK: dict[ClaudeTier, tuple[float, float, float]] = _claude_prices()


# ----------------------------------------------------------------- inference
#
# DigitalOcean Gradient serverless inference, as an alternative endpoint for
# the Anthropic Messages API. The research and the reasoning are in
# `docs/DROPLET_AI.md`; what is wired here is that document's **phase 1 and
# only phase 1**.
#
# The switch is one variable whose EMPTY value is today's behaviour, so
# rollback is unsetting it rather than editing code — the same shape as
# `DASHBOARD_CHAT_TOKEN` and `X_BEARER_TOKEN`, and for the same reason: a
# deployment that has not set it is a supported configuration.

# The documented serverless endpoint. Held here so an operator sets ONE
# variable rather than two, and so the string exists in exactly one place.
DO_INFERENCE_DEFAULT_BASE_URL = "https://inference.do-ai.run"

ANTHROPIC = "anthropic"
DIGITALOCEAN = "digitalocean"

# Whether any PYTHON model call actually goes through DigitalOcean.
#
# **It is False, and this exists so that stays checkable rather than
# remembered.** Phase 1 moves the three souls, which run on Hermes and read
# their own environment — no Python is involved in that path at all. So a key
# in `.env` today is a DECLARATION and not yet a route, and
# `Env.inference_provider` says so in the sentence it hands a startup banner.
#
# Phase 2 (`claude.dream`, then `claude.confer`) is what flips this, in its own
# commit, once the forced tool-call substitute for server-enforced structured
# output has been proven on the one structured call that cannot lose money.
#
# **`claude.propose` was once written here as "never in scope", and that word
# was withdrawn.** The argument behind it was purely economic — DigitalOcean
# charges Anthropic's exact list price for ANTHROPIC models, so proxying Claude
# through it buys nothing — and it silently assumed the destination was the same
# model at the other end of a longer wire. The operator's instruction is the
# other thing entirely: *"there's a full base of models there including better
# ones for the task. We need to move all AI requirements to that."* Open models
# in that catalogue run $0.18-$0.99 per million against Sonnet's $2/$10, so the
# cost half of the argument inverts.
#
# What does NOT change with the destination, because it is a property of the
# path rather than of the vendor:
#
# - **The schema must be ENFORCED, not requested.** `propose` produces order
#   quantities and stop prices. A model asked nicely for a shape is a model that
#   can return a plausible wrong one.
# - **The model is PINNED per path, and nothing may re-route on failure.**
#   Automatic fallback to a second model is a silent downgrade that still emits
#   `cycle_complete`.
#
# Note which way the evidence actually points. Alpha Arena is this repository's
# founding lesson and it is not a Claude endorsement: Claude Sonnet finished
# -$3,081, second-worst of six flagships. The rule was never "use this vendor",
# it is "the gate holds whoever is talking".
PYTHON_MODEL_PATH_USES_DO = False


@dataclass(frozen=True)
class InferenceProvider:
    """Which endpoint model calls are CONFIGURED for, and whether that
    configuration is coherent.

    **Three states rather than two, and the third is the whole point.** A key
    that is present but unusable must not read as "Anthropic, all fine": that
    is the silent fallback this arrangement exists to refuse. `usable` is False
    and `detail` names what is wrong, so a caller can say so plainly instead of
    proceeding on a provider the operator did not choose.

    `detail` is a complete sentence and is always safe to print — it never
    contains the key, in the same way the Settings page reports a credential as
    configured or not configured and never renders one.

    Nothing here raises. A misconfigured endpoint for a path that has not moved
    yet must not be able to stop the trading loop booting: refusing at startup
    is a denial at the least useful moment, and this repository puts that
    friction where the setting is actually USED. For phase 1 that is
    `deploy/run-chat.sh` and `deploy/run-dream.sh`, which refuse the turn —
    costing a chat message rather than a session's trading.
    """

    name: str
    base_url: str
    usable: bool
    detail: str

    @property
    def is_digitalocean(self) -> bool:
        return self.name == DIGITALOCEAN


class Env(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")

    # DigitalOcean Gradient serverless inference. Both optional, both empty by
    # default, and **empty means Anthropic** — see the block above this class.
    #
    # A **model access key**, never a DigitalOcean personal access token. A PAT
    # controls the whole account — droplets, DNS, billing — and this box also
    # runs an agent with a shell; a model access key is scopable to specific
    # models. Creating one through the API is retired (`{"id": "gone"}`), so it
    # is made by hand in the control panel and pasted in. Nothing here may
    # assume a key can be minted programmatically.
    do_inference_key: str = Field(default="", alias="DO_INFERENCE_KEY")

    # Optional even when the key is set: blank means the documented endpoint,
    # so the operator sets one variable rather than two. It is not the switch —
    # a base URL with no key moves nothing, and `inference_provider` says so
    # rather than letting half a configuration read as a working one.
    do_inference_base_url: str = Field(default="", alias="DO_INFERENCE_BASE_URL")

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

    # Naming a model OUTRIGHT, which the two tier settings above cannot do.
    #
    # Both are empty by default and **empty means the tier**, so a deployment
    # that has not set them behaves exactly as it always did — the same shape as
    # `DO_INFERENCE_KEY` and `DASHBOARD_CHAT_TOKEN`, and for the same reason:
    # rollback is unsetting a variable rather than editing code.
    #
    # Set one to any string and that string goes on the wire. If it is a model
    # `MODEL_SPECS` has prices for, the prices come with it; if it is not, the
    # call is made and its cost is reported as UNKNOWN rather than as zero. That
    # is the honest half of making an arbitrary model nameable, and it is why
    # `CallUsage.estimated_cost_usd` can be None.
    #
    # They are separate for the reason the two tiers are separate: the decision
    # loop wakes 96 times a day and the dreamer once, so the model that is right
    # for one is not automatically right for the other.
    #
    # Named `decision_`/`dream_` rather than `model_id`: `model_` is a RESERVED
    # namespace in Pydantic v2, so a field starting with it is unavailable here.
    decision_model_id: str = Field(default="", alias="DECISION_MODEL_ID")
    dream_model_id: str = Field(default="", alias="DREAM_MODEL_ID")

    @property
    def decision_spec(self) -> ModelSpec:
        """The model `claude.propose` runs on: the loop and the smoketest."""
        return resolve_model_spec(self.decision_model_id, self.claude_tier)

    @property
    def dream_spec(self) -> ModelSpec:
        """The model `claude.dream` and `claude.confer` run on.

        Follows `dream_tier` rather than `claude_tier` when no id is named, for
        the reason `dream_tier` exists at all: the loop's default is a tier with
        no extended thinking, and thinking is how a dream gets past its first
        hop.
        """
        return resolve_model_spec(self.dream_model_id, self.dream_claude_tier)

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
    def inference_provider(self) -> InferenceProvider:
        """Which endpoint this process's model calls are configured for.

        Pure, offline and never raising. `detail` is the line a startup banner
        prints, for the same reason `electrum-bot-web` announces its auth mode
        out loud: a provider swap is invisible afterwards, so the moment the
        process starts is the only moment anyone can be told.

        The sentence is written to be true rather than encouraging. While
        `PYTHON_MODEL_PATH_USES_DO` is False it says the switch is NOT IN FORCE
        for any Python model call, because it is not — phase 1 moves Hermes,
        and a banner claiming otherwise would be exactly the confident partial
        answer this repository exists to prevent.
        """
        key = self.do_inference_key.strip()
        declared = self.do_inference_base_url.strip()

        if not key:
            if declared:
                # Half a configuration. Reported rather than ignored: an
                # operator who set the endpoint and not the key would otherwise
                # believe the swap had happened.
                return InferenceProvider(
                    name=ANTHROPIC,
                    base_url="",
                    usable=False,
                    detail=(
                        f"DO_INFERENCE_BASE_URL is set ({declared}) but DO_INFERENCE_KEY "
                        "is not, so nothing has moved. The key is the switch; a base URL "
                        "on its own does nothing. Model calls go to Anthropic directly."
                    ),
                )
            return InferenceProvider(
                name=ANTHROPIC,
                base_url="",
                usable=True,
                detail=(
                    "Inference provider: Anthropic direct (DO_INFERENCE_KEY is not set). "
                    "This is the default and a supported configuration."
                ),
            )

        base_url = declared or DO_INFERENCE_DEFAULT_BASE_URL
        if not base_url.startswith("https://"):
            return InferenceProvider(
                name=DIGITALOCEAN,
                base_url=base_url,
                usable=False,
                detail=(
                    f"DO_INFERENCE_KEY is set but DO_INFERENCE_BASE_URL ({base_url}) is "
                    "not an https:// URL, so the DigitalOcean endpoint is unusable. "
                    "This is NOT a fall back to Anthropic — fix the value or unset "
                    "DO_INFERENCE_KEY."
                ),
            )

        # The clause that stops this claiming a switch that has not been
        # thrown. It comes off when phase 2 lands, in the same commit that
        # flips `PYTHON_MODEL_PATH_USES_DO`, and `tests/test_config.py` fails
        # until one moves with the other.
        not_yet = (
            ""
            if PYTHON_MODEL_PATH_USES_DO
            else (
                " It is NOT IN FORCE for any Python model call: phase 1 moves the three "
                "souls on Hermes, whose key lives in the hermes user's own environment "
                "and never in this file. See deploy/README.md."
            )
        )
        return InferenceProvider(
            name=DIGITALOCEAN,
            base_url=base_url,
            usable=True,
            detail=(
                f"Inference provider: DigitalOcean serverless at {base_url} "
                f"(DO_INFERENCE_KEY is set).{not_yet}"
            ),
        )

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

    # ------------------------------------------- the out-of-hours fill switch
    #
    # **This is the only setting in this file that trades away one of the
    # operator's four rules, so it is the most deliberate switch in the repo.**
    # Off in the model, off in `config/rules.yaml`, and refusing to engage
    # unless BOTH are true — see `broker.plan_extended_hours_fill`, which is
    # where the arithmetic lives and which fails closed on every unknown.
    #
    # WHAT IT BUYS. An entry submitted during the pre-market (04:00-09:30 New
    # York) or after hours (16:00-20:00) that actually FILLS THERE, instead of
    # resting until the next regular open.
    #
    # WHAT IT COSTS, stated as a consequence rather than as a mechanism: the
    # position stands at the broker with **nothing resting behind it**. The
    # stop is a journal figure and `stop_watch` reports a breach on the loop's
    # fifteen-minute pulse. Rule 3 — hard stops on every trade — is still true
    # at sizing time and is no longer true at the broker.
    #
    # WHY THERE IS NO THIRD OPTION. `AlpacaBroker.place_order` attaches the
    # stop, which makes every entry a bracket or an OTO, and Alpaca accepts
    # `extended_hours` on neither. An entry that carries a stop cannot fill out
    # of hours; an entry that can fill out of hours carries no stop. "Place the
    # trade now" and "get a position on now" are therefore different
    # instructions and the code can satisfy exactly one of them.
    #
    # WHAT IS ACTUALLY SURRENDERED IS NARROWER THAN IT LOOKS. A resting stop
    # could not have fired out of hours anyway: a stop becomes a MARKET order
    # and extended-hours venues take limit orders only. What is given up is the
    # leg being THERE when the regular session reopens, and the window between
    # the fill and that reopening in which nothing is resting at the broker.
    #
    # It must stay off for a continuous market. Crypto is already unbracketed —
    # Alpaca accepts no bracket there either — so it has nothing to surrender,
    # and it has no pre-market to surrender it for. `plan_extended_hours_fill`
    # refuses a crypto symbol outright rather than a validator refusing the
    # config, for the same reason `refuse_premarket` is handled by a comment
    # there: refusing to start denies at the least useful moment.
    allow_extended_hours_fills: bool = False

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

    # This class's TOTAL open risk, summed across every position in it, as a
    # percentage of equity. The per-class counterpart to `max_total_risk_pct`,
    # and a different question from the two limits above: `max_risk_per_trade_pct`
    # bounds one trade, this bounds the class. A class allowed 0.5% per trade
    # with no total is a class that can quietly hold four of them.
    #
    # Same doctrine as the fields above. **A value here OVERRIDES the portfolio
    # limit for this class, in either direction**, and is deliberately not
    # floored back with a `min`: a file saying 3% while the gate quietly applied
    # 2% would be a limit nobody could read off the config, which is worse than
    # either number on its own. Arguing the case for a looser one is the
    # settings agent's job, not a validator's.
    #
    # Measured on PLANNED risk — `|entry - stop| x qty`, taken from the journal
    # — so **unrealised profit does not offset it**. A position being up today
    # does not change what its stop costs if it fills, and netting a paper gain
    # against a real stop distance would make the cap loosest exactly when the
    # class had run furthest. The consequence is deliberate: at the cap, an
    # existing position in the class has to be closed before another opens.
    #
    # Worth knowing rather than guarded against, exactly as with the per-trade
    # override: `max_total_risk_pct` is still portfolio-wide, so a class total
    # above it can never fill — the portfolio total-risk gate refuses the trade
    # that would breach it first. Raising a class total past the portfolio one
    # does nothing until the portfolio one moves too.
    #
    # `None` means "no opinion": the class is bounded by the portfolio-wide
    # total alone. Absent is not zero.
    max_class_total_risk_pct: float | None = Field(default=None, gt=0, le=10)

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

    # Symbols grouped by KIND, and the grouping is the point rather than
    # decoration. Sixteen large-cap US equities scroll past as sixteen of the
    # same thing; an index beside a metal beside a crypto pair beside the long
    # bond is a picture of what is moving where. The kind is declared here
    # because it cannot be derived — `GLD` is an ETF by structure and gold by
    # meaning, and only the second is worth showing.
    #
    # Order within a kind is preserved; order ACROSS kinds is not what reaches
    # the tape. See `symbols`.
    kinds: dict[str, list[str]] = Field(default_factory=dict)

    # Its own cadence, slower than the account poll. Fifteen quotes every five
    # seconds is 180 requests a minute against a per-minute limit; once a
    # minute is nowhere near it, and a tape is not a figure anyone trades off.
    refresh_seconds: float = Field(default=60.0, gt=0)

    @property
    def symbols(self) -> list[str]:
        """Every watched symbol, INTERLEAVED across kinds rather than grouped.

        Round-robin, so consecutive cells are of different kinds wherever there
        are kinds left to draw from. Rendering the groups in order would put
        five equities in a row and then three metals, which is the monotony the
        kinds were introduced to break — the mix has to be visible in a glance
        at any part of the strip, not only in a full scroll of it.

        Duplicates are dropped, keeping the first appearance: a symbol listed
        under two kinds is a config mistake, and drawing it twice on a
        sixteen-cell strip would look like a rendering fault.
        """
        out: list[str] = []
        seen: set[str] = set()
        columns = list(self.kinds.values())
        for row in range(max((len(c) for c in columns), default=0)):
            for column in columns:
                if row < len(column) and column[row] not in seen:
                    seen.add(column[row])
                    out.append(column[row])
        return out

    def kind_of(self, symbol: str) -> str:
        """The kind a symbol was declared under, or `unclassified`.

        Never guessed from the ticker. A symbol nobody classified is shown as
        unclassified rather than assigned to whichever kind looks likeliest —
        same rule as an indicator with too few bars reporting `unavailable`
        instead of a shorter average wearing the wrong label.
        """
        for kind, symbols in self.kinds.items():
            if symbol in symbols:
                return kind
        return "unclassified"

    @model_validator(mode="after")
    def _enabled_needs_symbols(self) -> WatchlistRules:
        if self.enabled and not self.symbols:
            raise ValueError(
                "watchlist.enabled is true but no symbols are listed under "
                "watchlist.kinds, so the tape would render nothing and look "
                "broken rather than switched off."
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


class PositionActionRules(BaseModel):
    """Whether the LOOP may act on its own position plans, unattended.

    **Off by default in the model and off in `config/rules.yaml`**, and unlike
    `dreaming.allow_symbol_grants` the two agree, because nobody has asked for
    this one to be on. A config that predates this block permits nothing, which
    is the direction every switch here fails in.

    What it does NOT control is the attended path. `tighten_stop` and
    `close_position_with_reason` on the MCP server work regardless: the operator
    asked for the trading agent to be able to action its own position, and that
    is satisfied by an agent in a conversation with a person present. This flag
    is about the other thing — the fifteen-minute loop closing a position or
    moving a stop at 3am with nobody watching, which is a new unattended
    execution path and the operator's deliberate switch to throw.

    **Nothing here can widen a stop**, whichever way it is set.
    `position_actions.classify_stop_move` refuses a move away from entry before
    any of this is consulted, because that is the one position move that
    increases the loss at unchanged size and `RiskGate` never sees a position
    move at all.
    """

    enabled: bool = False


class VaultCapRules(BaseModel):
    """How many dreams each vault will hold.

    The numbers `dreaming.VaultCaps` is built from, carried here so the file an
    operator edits is the file that decides. `DreamingRules.vault_caps()`
    constructs the value object; the dataclass keeps its own defaults for the
    callers that pass nothing, and `tests/test_config.py` asserts the two agree
    so they cannot drift into two different answers.

    A cap here is a working constraint rather than a risk rule — nothing in the
    dream store can lose money — and it exists because a vault with two hundred
    entries is a vault nobody reads.

    **`adopted` is 3 to match `max_concurrent_positions`.** An adopted dream is
    a promise the trading agent may act on it, and a promise the account has no
    slot to keep reads on the page as permission granted while the position it
    implies can never be opened. The two are related by intent rather than by
    code; changing either wants a look at the other.

    **The archive is uncapped on purpose.** `None` means no limit. The only
    alternative to archiving a dream when the archive is full is deleting it, so
    a cap there would quietly turn "retire this" into "destroy the record of it".
    """

    workbench: int = Field(default=24, gt=0)
    prophecy: int = Field(default=12, gt=0)
    vault: int = Field(default=12, gt=0)
    adopted: int = Field(default=3, gt=0)
    archive: int | None = Field(default=None, gt=0)


class VaultTTLRules(BaseModel):
    """How long a dream may sit in a vault before it is reported as expired.

    Days, measured from `vault_entered_at` and never from `created_at`: a dream
    pulled back out for another pass gets a fresh clock, because the alternative
    punishes exactly the behaviour the arrangement wants.

    The operator was explicitly unsure between ninety days and a year, which is
    why these are configuration rather than constants. `prophecy` gets the long
    one because a prophecy is a long-horizon claim by nature — a supply chain
    does not reprice in a quarter. `archive` is `None` because the archive IS
    the retirement state, so a TTL on it would be an expiry on an expiry.
    """

    workbench: int = Field(default=90, gt=0)
    prophecy: int = Field(default=365, gt=0)
    vault: int = Field(default=90, gt=0)
    adopted: int = Field(default=90, gt=0)
    archive: int | None = Field(default=None, gt=0)


class DreamingRules(BaseModel):
    """The dreamer's shelves, and the one setting here that widens a permission.

    **`allow_symbol_grants` defaults to False in the model and is `true` in
    `config/rules.yaml`, and that asymmetry is deliberate.** A config that does
    not mention this feature grants nothing, so every deployment that predates
    it — and every test that builds `Rules` without a `dreaming:` block — fails
    closed. The shipped file turns it on because the operator asked for the
    behaviour, in a file that can be read and reverted in one line. The default
    is what protects a caller who never heard of the feature; the file is what
    records a decision somebody made.

    What the switch buys, stated plainly because it is a widening: an adopted
    dream's symbols join `allowed_symbols` for as long as the adoption is live,
    **under the existing limits of an already-enabled instrument class**. It
    cannot enable a class, cannot raise a cap, and cannot skip a gate. A
    permission is not an order.

    Nothing in this block reaches `RiskGate` by itself. The grant is resolved in
    `grants.resolve_granted_symbols` and handed to the gate as a mapping, in the
    same shape as `news_windows`, because the gate has to stay deterministic:
    no SQLite read, no network, and it must not fail open.
    """

    allow_symbol_grants: bool = False

    # A ceiling on how many symbols may be granted at once. Over it, NOTHING is
    # granted and the resolver says so loudly — see `grants.py`. Taking an
    # arbitrary subset would be a permission set nobody could predict, which is
    # worse than none at all.
    #
    # Six is twice `caps.adopted`, so three live adoptions naming two symbols
    # each fit and a runaway does not. It is a blast-radius bound rather than a
    # target: the binding constraint on how much a grant can cost is still the
    # class's own risk limits, which every granted symbol faces in full.
    max_granted_symbols: int = Field(default=6, gt=0)

    caps: VaultCapRules = Field(default_factory=VaultCapRules)
    ttl_days: VaultTTLRules = Field(default_factory=VaultTTLRules)

    def vault_caps(self) -> VaultCaps:
        """This block's caps as the value object `dreaming.py` takes.

        Imported inside the method on purpose. `config` is imported by nearly
        everything here and `dreaming` is a store; the dependency runs one way,
        from the seam towards the store, and a module-level import would drag
        the dream store into every process that reads a limit.
        """
        from .dreaming import VaultCaps

        return VaultCaps(
            workbench=self.caps.workbench,
            prophecy=self.caps.prophecy,
            vault=self.caps.vault,
            adopted=self.caps.adopted,
            archive=self.caps.archive,
        )

    def vault_ttls(self) -> VaultTTLs:
        """This block's TTLs as the value object `dreaming.py` takes."""
        from .dreaming import VaultTTLs

        return VaultTTLs(
            workbench=self.ttl_days.workbench,
            prophecy=self.ttl_days.prophecy,
            vault=self.ttl_days.vault,
            adopted=self.ttl_days.adopted,
            archive=self.ttl_days.archive,
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

    # The dreamer's shelves, and the only setting in this file that can widen
    # what may be traded without an edit to `instruments:`. Defaulted, so a
    # config with no `dreaming:` block grants nothing at all.
    dreaming: DreamingRules = Field(default_factory=DreamingRules)

    # Whether the loop may act on its own position plans unattended. Defaulted
    # to off, so a config with no `position_actions:` block executes none of
    # them. Read by `position_actions.execute_position_plan`, which refuses
    # before doing anything else.
    position_actions: PositionActionRules = Field(default_factory=PositionActionRules)

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
    def extended_hours_fill_classes(self) -> list[str]:
        """Enabled classes that have given up the broker-side stop out of hours.

        Empty is the shipped state and the answer this should keep giving. It
        exists so the surrender is a fact something can READ rather than a flag
        buried in one block of one file — a startup banner, a Settings card, an
        operator asking. The same reason `electrum-bot-web` announces its auth
        mode out loud: a switch like this is invisible afterwards.

        Enabled classes only. A class the operator has switched off cannot
        trade at all, so listing it here would name a surrender that is not
        being made.
        """
        return sorted(
            name
            for name, instrument in self.enabled_instruments.items()
            if instrument.allow_extended_hours_fills
        )

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

    def true_class_key(self, symbol: str) -> str:
        """Which instrument class a symbol REALLY belongs to. `""` when unknowable.

        **The question `for_symbol` cannot answer, and the difference is a
        permission.** `for_symbol` scans ENABLED classes only, so a symbol listed
        under a class the operator has switched off is invisible to it — which
        made "is this symbol from a blocked class?" unanswerable at exactly the
        moment an adopted dream claimed one under a different class's key. This
        scans every block, enabled or not, and falls back to the symbol's shape.

        Two sources, and they must AGREE:

        - **What the file lists.** A symbol in an `allowed_symbols` list belongs
          to that block, whether or not it is enabled.
        - **What the broker routes on.** `models.class_key_for_symbol` is the
          same rule `AlpacaBroker.place_order` branches on, so a `BTC/USD` order
          goes out unbracketed with no broker-side stop no matter which class a
          grant claimed for it.

        **Where they cannot both answer, the answer is `""`.** Two blocks
        listing one symbol, or a listing that disagrees with the routing rule,
        leaves the class genuinely undetermined — and a symbol whose class is
        unknown is a symbol whose limits are unknown. Callers read `""` as "drop
        this", never as a default, which is the same rule as an absent
        indicator being `None` rather than zero.

        Consequence worth knowing rather than guarding against: a hypothetical
        `futures:` block listing `ES` would resolve to `""`, because the shape
        rule reads a bare ticker as an equity and nothing on Globex is reachable
        through Alpaca anyway. That is the seam the session-shape template in
        `config/rules.yaml` is already waiting on; when a second broker arrives,
        the routing rule is what has to learn about it, not this method.

        Pure, and cheap enough to call per symbol per gate: it walks a handful
        of lists held in memory.
        """
        from .models import class_key_for_symbol

        clean = symbol.strip().upper()
        shape = class_key_for_symbol(clean)
        listed = sorted(
            name
            for name, instrument in self.instruments.items()
            if clean in instrument.allowed_symbols
        )
        if len(listed) > 1:
            return ""
        if listed:
            return listed[0] if listed[0] == shape else ""
        return shape

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
