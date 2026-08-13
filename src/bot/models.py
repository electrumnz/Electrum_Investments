"""Domain models — orders, positions, market snapshots, model decisions.

Terminology note: these models describe an **Alpaca** account (US equities and
crypto), not an FX/CFD account. Quantities are shares or coin units, never
"lots"; order identifiers are Alpaca UUID strings, never integer tickets; and
Alpaca aggregates all exposure to one symbol into a single position, so
positions are keyed by symbol rather than by individual fill.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Free text the model writes and nothing downstream parses. Generous, because
# the prompt now asks for a stance on every symbol and a plan for every
# position, so the answers are longer than they were when 500 was chosen.
#
# **Over-length truncates; it does not reject.** That split is deliberate and
# should not be extended to anything else. A rationale is prose read by a human
# on the Decisions page: losing its last clause costs a little context. A
# rejected response costs the whole cycle, and — until this was caught — the
# whole loop, because a `ValidationError` out of the SDK is not something the
# decision loop was catching. Trading stopped because a sentence was too long.
#
# Numbers keep rejecting. A price or a quantity that fails validation must
# never be quietly coerced into something plausible: that is the failure this
# repository exists to prevent, and a truncated number is a different number.
RATIONALE_MAX_CHARS = 2000


def truncate_free_text(value: object) -> object:
    """Trim over-long free text to the cap, marking that it was cut."""
    if isinstance(value, str) and len(value) > RATIONALE_MAX_CHARS:
        return value[: RATIONALE_MAX_CHARS - 1].rstrip() + "…"
    return value


def _require_every_property(schema: dict[str, Any]) -> None:
    """Declare every property of this model REQUIRED in its JSON schema.

    Python keeps its defaults — this touches the schema only — so every caller
    that constructs one of these models by hand is unaffected. What changes is
    the contract sent to the API: the model must emit every field, using the
    empty value where it has nothing to say, instead of omitting it.
    """
    schema["required"] = list(schema.get("properties", ()))


# Attach to any model handed to `messages.parse` as an `output_format`.
#
# **A null is free; an ABSENCE is what costs.** A property the schema does not
# require may be present or absent, so the grammar the API compiles has to
# accept every subset of the optional set in any order, and each optional field
# doubles that space. `model_client.EVERY_FIELD_REQUIRED` re-exports this and
# carries the measurements — read them there before adding a field with a
# default to anything the model returns.
#
# It lives HERE, in the leaf module, rather than beside those measurements,
# because `model_client` imports this file and the models that need it are
# defined in it. One definition, imported both ways round, rather than a second
# copy that can drift.
EVERY_FIELD_REQUIRED = ConfigDict(json_schema_extra=_require_every_property)


def served_matches_requested(*, requested: str, served: str) -> bool | None:
    """Did the endpoint answer with the model that was asked for?

    **Three-valued, and the third value is the point.** An empty string on
    either side means the question could not be settled — the reply named no
    model, or none was recorded to compare against — and that must never
    collapse into agreement, nor into a substitution nobody performed.

    **A dated snapshot of the requested alias counts as the same model.**
    Anthropic resolves `claude-sonnet-5` to `claude-sonnet-5-20260401`, so
    exact equality would report a mismatch on every ordinary Anthropic call —
    and a warning that fires on every call is a warning nobody reads, which
    would leave the real substitution as invisible as it was before anyone
    looked. The exemption is deliberately NARROW: a hyphen and eight digits.
    `nemotron-3-ultra` answered by `nemotron-3-ultra-550b` is a different model
    with a plausible name, and it reads as False.

    It lives here, in the leaf module, so `CallUsage` and `Decision` share ONE
    implementation. Two copies of this comparison would be two answers, and the
    one rendered on the Decisions page is the one nobody re-checks — the same
    reasoning that gives `stop_width` a single `CarriesATR` protocol instead of
    two functions.
    """
    if not requested or not served:
        return None
    if served == requested:
        return True
    suffix = served.removeprefix(requested)
    return suffix.startswith("-") and len(suffix) == 9 and suffix[1:].isdigit()


class Direction(StrEnum):
    BUY = "buy"
    SELL = "sell"


class AssetClass(StrEnum):
    """Alpaca asset classes we support.

    The distinction still matters for sessions — crypto trades 24/7 while
    equities respect the market calendar — and for the crypto capital sleeve.

    ETFs are `us_equity` at Alpaca, so they need no separate class. CFDs are
    absent deliberately — they are barred for US residents under CFTC rules,
    which is why this project moved off BlackBull.
    """

    EQUITY = "us_equity"
    CRYPTO = "crypto"
    # The bot does not propose option trades — that is deferred work. This class
    # exists so positions opened elsewhere are recognised, tracked, and warned
    # about before Alpaca exercises or liquidates them automatically.
    OPTION = "us_option"


# Alpaca writes a crypto pair with a slash — `BTC/USD` — and writes nothing else
# that way.
_CRYPTO_PAIR_MARK = "/"


def is_crypto_symbol(symbol: str) -> bool:
    """Alpaca writes crypto pairs with a slash (BTC/USD); equities never have one.

    **This is the ROUTER's rule and the FENCE's rule, and they have to be the
    same one.** `AlpacaBroker.place_order` branches on it to decide whether an
    order goes out as an unbracketed crypto order — no broker-side stop, because
    Alpaca accepts no bracket on crypto — and `grants.py` and `RiskGate` branch
    on it to decide which class's limits a symbol faces. An adversarial audit
    found the two disagreeing: an adopted dream claiming `BTC/USD` under
    `us_equity` was permitted under the equity book's limits and then routed to
    Alpaca as a crypto order, so the operator's third rule was gone. One
    definition, re-exported by `broker.is_crypto_symbol`, is what stops that
    happening again.
    """
    return _CRYPTO_PAIR_MARK in symbol


def class_key_for_symbol(symbol: str) -> str:
    """Which `instruments:` key a symbol would belong to, from its shape alone.

    Three answers, and they are the three asset classes Alpaca actually offers:
    a slash is a crypto pair, an OCC contract is an option, and everything else
    is an equity — which is what a bare ticker IS at Alpaca, ETFs included.
    Empty for a symbol that is not a symbol, which callers read as "cannot be
    established" and never as a default.

    `parse_occ_symbol` is reused rather than a second regex written here, so
    there is one definition of an OCC symbol in the repository. The import is
    local because `options` imports nothing from this module and this module is
    imported by everything: keeping it inside the function keeps that true
    whichever way a future edit runs.

    **It answers a shape, not an existence.** `ZZZZ` comes back `us_equity`
    because that is what a four-letter ticker is, and whether Alpaca lists it is
    a question for the broker at order time. Guessing wrong in that direction
    costs nothing: the symbol still faces every gate under that class's limits.

    **A second broker would need this revisited.** `ES` is a Globex future and
    reads as an equity here. Harmless while Alpaca is the only adapter — nothing
    on Globex is reachable — and it is the same seam the session-shape template
    in `config/rules.yaml` is waiting on.
    """
    from .options import parse_occ_symbol

    clean = symbol.strip().upper()
    if not clean:
        return ""
    if is_crypto_symbol(clean):
        return str(AssetClass.CRYPTO)
    if parse_occ_symbol(clean) is not None:
        return str(AssetClass.OPTION)
    return str(AssetClass.EQUITY)


class ExecutionMode(StrEnum):
    """Where an order actually goes.

    A stand-down forces `PAPER` for its duration: the rule is "can't trade
    money, only paper", not "stop trading". Today the whole build is locked to
    PAPER regardless, so this is inert — but the machinery is exercised and
    tested now rather than written in a hurry the day real money is involved.
    """

    PAPER = "paper"
    LIVE = "live"


class TradeOutcome(StrEnum):
    """How a closed trade is classified for the consecutive-loss counter.

    SCRATCH exists so that cutting a trade for a few dollars does not count as
    a loss. A counter that punished scratches would push the bot toward holding
    losers to avoid tripping its own rule, which is exactly backwards.
    """

    WIN = "win"
    LOSS = "loss"
    SCRATCH = "scratch"


class FillState(StrEnum):
    """How much of a submitted entry is KNOWN to have filled, and how well known.

    `Trade` carries one `qty` and one `entry_price`, so it cannot express "3
    filled now and 18 later" as two rows. What it can express — and what this
    is — is **how much confidence the single row deserves**, which is the fact a
    reader actually needs: a quantity nobody has read back from the broker and a
    quantity confirmed against a held position are different claims and must not
    share a representation.

    The four states are ordered by evidence, not by time:

    - `UNCONFIRMED` — written at submission from the PROPOSAL. Nothing has been
      read back. This is the honest state of every row the moment it is created,
      because `record_fill` runs immediately after `broker.place_order` and the
      broker's own submission response reports `filled_qty=0` on an order that
      goes on to fill completely.
    - `RESTING` — the entry order was seen working at the broker with nothing
      filled. Positive evidence that no position exists yet, which is what an
      out-of-hours entry looks like: it rests and becomes eligible at the next
      regular open.
    - `PARTIAL` — the entry order has reached a terminal state and the position
      behind it is SMALLER than what was submitted. Settled: the rest will not
      arrive.
    - `COMPLETE` — confirmed filled for the submitted quantity.

    **A mid-fill reading is not `PARTIAL`.** That distinction is the whole
    reason this enum has four members rather than three. A poll during the first
    real order returned `FILLED 3.0` of 21 and was briefly written down as a
    partial fill; the order completed moments later. An order still working at
    the broker is a snapshot, so it stays `UNCONFIRMED` (or `RESTING` when
    nothing has filled at all) until the order is terminal and there is an
    outcome to record.
    """

    UNCONFIRMED = "unconfirmed"
    RESTING = "resting"
    PARTIAL = "partial"
    COMPLETE = "complete"


class ExitReason(StrEnum):
    """Why a position closed. **The plan, never the profit.**

    `record_exit` takes a price, a time and a realised figure, so stop-hit,
    target-hit, closed-by-hand and expiry were indistinguishable afterwards.
    These are the six answers, and they grade whether the trade ended the way it
    was designed to — which is true regardless of what it made, and therefore
    has no outcome sample to overfit to. It belongs beside `triggers.py` and
    `DreamLedger`, not beside `metrics.py`. See `exit_review.py`.

    **`CLOSED_EARLY` is the one that matters.** A stop that fires and a target
    that fills are the plan working; a hand close with the price at neither
    level is the plan being ABANDONED, which is discipline rather than luck and
    is the only bucket here that says something about the operator or the agent
    rather than about the market.

    `UNKNOWN` is a real answer and must never be filled in with a guess. It
    covers an exit price that had to be estimated — which is the entry price,
    and would read as "at neither level" while being no evidence at all — and a
    close that reached neither level with nothing on file to say who closed it.
    """

    STOP_HIT = "stop_hit"
    TARGET_HIT = "target_hit"
    # Closed by hand, with the exit at NEITHER level. The plan abandoned.
    CLOSED_EARLY = "closed_early"
    # Closed by hand, but a level had already been reached. The plan followed,
    # with somebody pressing the button rather than the broker's leg firing.
    CLOSED_AT_LEVEL = "closed_at_level"
    # An option the broker resolved itself: auto-exercise, assignment, or the
    # liquidation of a position the account could not fund.
    EXPIRY = "expiry"
    UNKNOWN = "unknown"


class Tick(BaseModel):
    """A two-sided quote. **Both sides must be real prices.**

    That constraint is the whole point of this class, and it was learned the
    expensive way. `mid` is `(bid + ask) / 2`, so a quote with a missing side
    does not fail — it returns HALF THE REAL PRICE, and half a price looks
    exactly like a price.

    Observed live: Alpaca's free tier is IEX-only, IEX often has no bid in the
    pre-market, and SPY came back as `bid=0, ask=771.64`. `mid` was 385.82
    against a true 773.26. Nothing raised. That figure then fed position
    sizing, the limit price, the gate's own sanity check, the ticker tape and
    unrealised P&L — every one of them agreeing with itself while being
    twice out.

    802 tests were green over it, because `MockBroker` always seeds both sides.
    It took a real quote and somebody checking the price against a second
    source. So the guard lives HERE, at the point of construction, rather than
    in whichever caller happens to remember: a `Tick` that exists is a `Tick`
    whose `mid` means something.
    """

    symbol: str
    bid: float
    ask: float
    timestamp: datetime

    @model_validator(mode="after")
    def _both_sides_are_real(self) -> Tick:
        # Zero is the value a missing side actually arrives as — not None, not
        # an error. Negative is nonsense from any feed. Either way the quote is
        # one-sided and a midpoint cannot be computed from it.
        if self.bid <= 0 or self.ask <= 0:
            raise ValueError(
                f"one-sided quote for {self.symbol}: bid={self.bid}, "
                f"ask={self.ask}. A midpoint from this would be half the real "
                "price, so the quote is refused rather than halved. Thin or "
                "pre-market books do this; the symbol is reported as having no "
                "quote."
            )
        return self

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid


class Bar(BaseModel):
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class Position(BaseModel):
    """An open position. Alpaca reports one aggregated position per symbol."""

    symbol: str
    asset_class: AssetClass = AssetClass.EQUITY
    direction: Direction
    qty: float = Field(gt=0, description="Shares or coin units; always positive")
    entry_price: float
    opened_at: datetime
    current_price: float | None = None
    unrealised_pnl_usd: float = 0.0

    @property
    def notional_usd(self) -> float:
        """Current market value of the position, falling back to entry price."""
        return self.qty * (self.current_price or self.entry_price)

    @property
    def cost_basis_usd(self) -> float:
        """What the position cost to open, ignoring what it is worth now.

        The total-invested cap is measured on this rather than on market value,
        so a winner drifting upward never retroactively breaches the cap or
        forces a close.
        """
        return self.qty * self.entry_price


class AccountSnapshot(BaseModel):
    """Account state as reported by Alpaca, plus open risk from the journal.

    Carries no day-trade fields: FINRA retired the Pattern Day Trader rule on
    2026-06-04, and Alpaca removed `daytrade_count` and `pattern_day_trader`
    from its API on 2026-07-06.
    """

    equity_usd: float
    cash_usd: float
    buying_power_usd: float
    open_positions: list[Position] = Field(default_factory=list)
    realised_pnl_today_usd: float = 0.0

    # Combined planned risk across open positions. Alpaca keeps stop-losses as
    # separate orders rather than attributes of a position, so this cannot be
    # derived from broker state — it comes from the journal, which records the
    # planned stop for every trade. Callers populate it via
    # `Journal.open_risk_usd()`; left at zero the total-risk cap silently has
    # nothing to count, so keep them wired together.
    open_risk_usd: float = Field(default=0.0, ge=0)

    # The same figure broken out per symbol, from the same journal read. A
    # per-class risk cap has to know which part of the open risk belongs to
    # which class, and one portfolio total cannot answer that.
    #
    # Planned risk, so a position sitting in unrealised PROFIT still contributes
    # the full `|entry - stop| x qty`. Being up today does not change what the
    # stop costs if it fills, and the cap this feeds is a statement about that
    # cost rather than about the mark.
    open_risk_by_symbol: dict[str, float] = Field(default_factory=dict)

    # The journalled stop LEVEL per symbol, from that same read.
    #
    # This exists because the model is asked to manage stops it could not see.
    # `model_client` asks for a `position_plan` on every open position with an
    # action of hold, close or **tighten_stop**, while the context block
    # rendered direction, quantity, entry, current price and P&L — and no stop
    # at all. So the agent was being asked whether to tighten a level that was
    # never put in front of it, and whether a thesis still held without the
    # number that defines what being wrong costs.
    #
    # It is the PLANNED stop from the journal, which is not necessarily what
    # rests at the broker; `WorkingOrder.stop_price` is the other half of that
    # pair and the two are deliberately reported separately, because the
    # interesting case is when they disagree.
    #
    # A symbol absent here has no journalled stop, which is the same fact as
    # its presence in `symbols_with_unknown_risk` — never a stop of zero.
    planned_stop_by_symbol: dict[str, float] = Field(default_factory=dict)

    # Held positions the journal has never seen. Their planned stop is
    # unknowable, so their risk is MISSING rather than zero — an empty entry in
    # `open_risk_by_symbol` would read as "risks nothing", which is the
    # confident wrong figure this repository exists to refuse.
    #
    # Named the same way `reconcile.ReconcileResult` names it, because it is the
    # same fact travelling further: `untracked_positions` there, and
    # `risk_is_understated` below is deliberately the same property under the
    # same name rather than a second vocabulary for one idea.
    symbols_with_unknown_risk: list[str] = Field(default_factory=list)

    @property
    def risk_is_understated(self) -> bool:
        """True when positions exist whose planned risk cannot be known.

        Identical in meaning to `ReconcileResult.risk_is_understated`: any
        untracked position means every risk figure here counts less than is
        actually at risk. `RiskGate._class_total_risk` refuses rather than
        approving against an understated total.
        """
        return bool(self.symbols_with_unknown_risk)

    @property
    def gross_exposure_usd(self) -> float:
        """Current market value of all open positions."""
        return sum(p.notional_usd for p in self.open_positions)

    @property
    def total_invested_usd(self) -> float:
        """What all open positions cost to open. Basis for the total-invested cap."""
        return sum(p.cost_basis_usd for p in self.open_positions)

    @property
    def cash_pct(self) -> float:
        if self.equity_usd <= 0:
            return 0.0
        return self.cash_usd / self.equity_usd * 100

    def position_for(self, symbol: str) -> Position | None:
        return next((p for p in self.open_positions if p.symbol == symbol), None)


class TradingActivity(BaseModel):
    """Recent trading history, used by the frequency and cooldown gates.

    Overtrading — not stock picking — was the dominant loss driver in the
    Alpha Arena LLM trading competition, where fees consumed P&L. These counts
    are what let the risk gate enforce a hard ceiling on churn.
    """

    trades_today: int = Field(default=0, ge=0)
    trades_this_week: int = Field(default=0, ge=0)
    last_trade_at_by_symbol: dict[str, datetime] = Field(default_factory=dict)

    def seconds_since_last_trade(self, symbol: str, now: datetime) -> float | None:
        last = self.last_trade_at_by_symbol.get(symbol)
        if last is None:
            return None
        return (now - last).total_seconds()


class OrderProposal(BaseModel):
    """What the model proposes — must pass the risk gate before execution.

    **Every property is REQUIRED on the wire** and none is required in Python.
    See `EVERY_FIELD_REQUIRED`: this is the schema the decision loop sends 96
    times a day, and an optional property is what makes one expensive to
    compile. `asset_class` is therefore stated by the model on every proposal
    now rather than defaulted — which is harmless, because no gate reads it.
    `RiskGate` resolves a symbol's class from the SYMBOL, by the same rule
    `AlpacaBroker` routes on, so a mislabelled proposal cannot choose which caps
    it faces.
    """

    model_config = EVERY_FIELD_REQUIRED

    symbol: str
    asset_class: AssetClass = AssetClass.EQUITY
    direction: Direction
    qty: float = Field(gt=0, description="Shares or coin units")
    limit_price: float = Field(
        gt=0,
        description="Limit orders only. Market orders are refused — they were a "
        "documented source of slippage loss in LLM trading experiments.",
    )
    stop_loss_price: float = Field(gt=0)

    # **Optional, and the stop is not.** That asymmetry is the operator's third
    # rule stated in the type: every trade has a hard stop, and no trade is
    # obliged to name a target.
    #
    # It used to be required, which forced whoever built a proposal to invent a
    # level to satisfy the validator. That was survivable while the field was
    # only journalled. It stopped being survivable when entries became GTC
    # brackets: an invented target is now a live OCO leg resting at the broker,
    # so a number picked to get past validation is an exit nobody chose.
    #
    # `None` means "no target" and the order goes out as an OTO — entry plus a
    # stop — rather than a bracket. It does NOT mean "decide one for me".
    take_profit_price: float | None = Field(default=None, gt=0)

    # A TRAILING exit, as a percentage of the best price seen. `None` is a fixed
    # stop and is what every trade so far has been.
    #
    # **One field, not an enum and a number.** "Which exit style" and "how far
    # does it trail" are one decision, and carrying them as two facts is a
    # third fact about the same thing that can disagree with the other two —
    # the same reason `Adoption.is_live` is computed rather than stored. A
    # figure here IS the trail; its absence IS a fixed stop.
    #
    # **Percent rather than an absolute distance**, for two reasons. It means
    # the same thing on SPY at 773 and on BTC/USD at 65,000, like every other
    # limit in this repository; and Alpaca's own trailing order takes
    # `trail_percent` unchanged, so nothing has to convert a unit at the
    # boundary. Offering both — as Alpaca does — would need an "exactly one of"
    # rule the model can get wrong, on a schema whose whole cost model is about
    # not making the model choose.
    #
    # ## What it does NOT buy, and this is the part to read
    #
    # **Alpaca accepts no trailing leg on an entry.** The only stop a bracket or
    # an OTO can carry is `StopLossRequest`, which takes a trigger and an
    # optional limit and nothing else; a trailing stop is a standalone order
    # type that can only be submitted against a position that already exists.
    # So this is the same shape as "a broker-side stop OR an out-of-hours fill,
    # never both": the leg resting behind the entry is FIXED, at
    # `stop_loss_price`, and the trail is a plan the broker is not holding.
    #
    # That ordering is deliberate rather than a compromise. The fixed leg is
    # what makes the operator's third rule true from the first instant, and it
    # sits at exactly the level the position was sized against. A trail is only
    # ever an improvement on it — see `trailing_stop_level`, which cannot widen
    # a stop — so nothing is at risk that was not already.
    #
    # **Nothing moves the stop on this alone yet.** The journal has no column
    # for a trail, so a trailing proposal is recorded in the audit log and in
    # the order that goes out, and the level in force stays the initial stop
    # until something moves it through `position_actions.tighten_stop` — which
    # refuses any move away from entry, which is a trail's own invariant.
    #
    # Upper bound rather than an opinion on placement: at 100% a long's stop is
    # zero and a short's is twice the price, which is not a stop.
    trail_percent: float | None = Field(
        default=None,
        gt=0,
        lt=100,
        description=(
            "For a TRAILING exit: how far behind the best price the stop "
            "follows, as a percentage. Omit (null) for a fixed stop. The stop "
            "never widens — `stop_loss_price` is still the initial level and "
            "still what the position is sized from."
        ),
    )
    rationale: str = Field(min_length=10, max_length=RATIONALE_MAX_CHARS)

    # Truncates rather than rejects, and only because nothing reads this field.
    # See RATIONALE_MAX_CHARS.
    _trim_rationale = field_validator("rationale", mode="before")(truncate_free_text)

    @property
    def notional_usd(self) -> float:
        return self.qty * self.limit_price

    @property
    def exit_is_trailing(self) -> bool:
        """Did the agent ask for a trail rather than a fixed stop?

        The one reading of `trail_percent`, so no caller re-derives it and gets
        `> 0` or `is not None` subtly differently.
        """
        return self.trail_percent is not None

    @property
    def risk_usd(self) -> float:
        """What this trade loses if the stop fills as planned.

        The unit the risk caps are measured in, and the reason they are
        leverage-neutral: this is the same number whether the position was paid
        for in cash, on margin, or as a futures contract.

        **A trail does not change it**, and that is why the risk gate needed no
        edit for one. The trail is measured from the best price SEEN, which at
        the moment of sizing is the entry, so the worst a trailing exit can do
        is exactly what the initial stop does — `trailing_stop_level` cannot
        return anything looser. A trail that widened the number here would be a
        bigger loss at the same 1%, which is the thing the prompt tells the
        model not to buy.
        """
        return abs(self.limit_price - self.stop_loss_price) * self.qty


def trailing_stop_level(
    *,
    direction: Direction,
    high_water_mark: float,
    trail_percent: float,
    stop_in_force: float,
) -> float:
    """Where a trail of this size puts the stop, given the best price seen.

    Pure arithmetic, here rather than in whichever caller needs it, because the
    alternative is every caller inventing the number — which is the failure
    `indicators.py` exists to prevent, arriving on the exit side.

    `high_water_mark` is the best price the trade has seen: the HIGHEST on a
    long, the LOWEST on a short. Alpaca names its field the same way and
    documents it the same way, so a level computed here and one read back off a
    resting trailing leg mean the same thing.

    **It can only ever TIGHTEN, and that is the whole guarantee.** The result is
    floored (a long) or capped (a short) by the stop already in force, so a
    trail wider than the initial stop distance simply does nothing until price
    has run far enough to earn it, and a high-water mark that goes backwards —
    which it must not, but which a caller could pass — cannot give room back.
    Without that floor a trail would be the one thing this repository refuses:
    a stop moving AWAY from entry on a live position, increasing the loss at
    unchanged size, with no gate anywhere that sees it.

    A non-positive high-water mark is refused rather than used. On a long it
    would be harmless — the floor absorbs it — and on a SHORT it would compute a
    stop of zero and cap the level down to it, which is a stop that triggers
    immediately. Two sides of one function must not fail differently, so the
    nonsense input is rejected on both.
    """
    if high_water_mark <= 0:
        raise ValueError(
            f"high_water_mark must be a real price, got {high_water_mark}. A "
            "trail computed from it would put a short's stop at zero."
        )
    if direction == Direction.BUY:
        return max(stop_in_force, high_water_mark * (1 - trail_percent / 100))
    return min(stop_in_force, high_water_mark * (1 + trail_percent / 100))


class StopAtBroker(StrEnum):
    """What is actually resting at the broker behind an entry, if anything.

    Not what was intended, and not what the journal says — what the adapter
    submitted. The two differ by design in three places already, and until this
    existed the difference was documented in comments and visible nowhere:

    - **Crypto carries no stop at all.** Alpaca accepts no bracket on it, so the
      stop has always been a journal figure watched by `stop_watch`. That is
      correct and long-standing, and an `OrderResult` that looked identical to a
      bracketed equity entry's was the `FinnhubCalendar.is_degraded` problem
      again: a caller cannot tell "there is a leg" from "there is nothing" by
      reading a success.
    - **A trailing exit gets a FIXED leg.** Alpaca accepts no trailing leg on an
      entry, so a proposal carrying `trail_percent` still rests a fixed stop at
      the initial level — the safe half — and the trail is not at the broker.
    - **Everything that is not an entry says nothing.** `UNSTATED` is the
      default, so a close, a stop replacement or a refusal cannot be misread as
      a claim about protection. Same rule as `has_cycles` and
      `can_grade_anything`: not asked and answered-no are different findings.

    `TRAILING` is unreachable from `place_order` today, deliberately kept as a
    member because it is what a leg read back by `get_open_orders` can be, and
    because naming it is what makes the absence above a statement rather than an
    oversight.
    """

    UNSTATED = "unstated"
    FIXED = "fixed"
    TRAILING = "trailing"
    ABSENT = "absent"


class OrderResult(BaseModel):
    accepted: bool
    order_id: str | None = None
    error: str | None = None
    filled_price: float | None = None
    filled_qty: float | None = None

    # What the broker is holding behind this entry. See `StopAtBroker`.
    #
    # Defaults to UNSTATED, which keeps every existing construction truthful:
    # a close, a replaced stop and a refusal are not claims about what is
    # protecting a position, and a boolean here would have made all three say
    # "no stop" in a field somebody would later read.
    stop_at_broker: StopAtBroker = StopAtBroker.UNSTATED


class OrderStatus(StrEnum):
    """Where a submitted order has got to.

    Distinct from `OrderResult`, which records what happened when the order was
    *submitted*. An order can be accepted and then sit unfilled for the rest of
    the session, which is invisible from the result alone and is exactly the
    state an operator needs to see.
    """

    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    OTHER = "other"


class WorkingOrder(BaseModel):
    """An order sitting at the broker, not yet resolved.

    The bot submits limit orders only, so an order that does not fill simply
    rests. Without this the dashboard shows a position that does not exist yet
    and nothing to say an order is waiting on a price.
    """

    order_id: str
    symbol: str
    direction: Direction
    qty: float = Field(gt=0)
    limit_price: float | None = None

    # The level a stop or stop-limit leg TRIGGERS at, read back from the
    # broker.
    #
    # Its absence was most of the way to having no stop at all. Every entry now
    # goes out as a bracket or an OTO, so the stop leg resting at Alpaca is the
    # thing the operator's third rule actually depends on — and with only
    # `limit_price` on this model, a stop leg rendered as `limit_price=None` on
    # every surface that shows working orders. An operator could see that a leg
    # existed while nothing in this repository could state what price it fires
    # at. The journal's `planned_stop` and the broker's real trigger are two
    # different facts, and only one of them was visible.
    #
    # **On a TRAILING leg this is a reading, not a level somebody chose.**
    # Alpaca recomputes it as the high-water mark moves, so it is true at the
    # moment it was fetched and will be a different number later. That is why
    # `trail_percent`, `trail_price` and `high_water_mark` are carried beside
    # it: without them a trailing leg is indistinguishable from a fixed stop
    # that keeps moving on its own, which reads as an unexplained move rather
    # than as the exit that was chosen.
    stop_price: float | None = None

    # How far behind the high-water mark a trailing leg follows, and where that
    # mark currently is, exactly as the broker reports them. Alpaca supplies
    # `trail_percent` OR `trail_price` — never both — and `hwm` alongside.
    #
    # All three are `None` on every non-trailing order, which is correct and
    # dull there, and `trail_is_unreadable` is what tells that apart from a
    # trailing leg the broker described to nobody. Same arrangement as
    # `order_type` beside `stop_price`.
    trail_percent: float | None = None
    trail_price: float | None = None
    high_water_mark: float | None = None

    # What the broker calls this order: "limit", "stop", "stop_limit",
    # "trailing_stop", "market". Lowercased; empty means the broker did not say.
    #
    # Carried because without it `stop_price is None` cannot be read. On a plain
    # limit order that None is correct and uninteresting; on a stop leg it means
    # nobody can say where the stop is. Same rule as everywhere else here —
    # absent and unknown are different facts and must not share a
    # representation. Kept as a raw string rather than an enum on purpose: an
    # order type this code has never heard of should travel through and be
    # displayed, not be coerced into the nearest known member.
    order_type: str = ""

    status: OrderStatus = OrderStatus.NEW

    # What the BROKER called it, lowercased and untranslated. Empty means the
    # broker did not say.
    #
    # `status` above answers "which of our seven buckets is this", and OTHER is
    # an honest answer to that question and a useless one to an operator: the
    # live stop leg protecting a short renders as OTHER, and so does a status
    # this build has simply never seen. Alpaca's own word for the first is
    # `held` — a bracket child waiting on its parent — which is actionable, and
    # for the second it is whatever it is, which is at least reportable.
    #
    # Same rule and same reason as `order_type` immediately above: a raw string
    # rather than a wider enum, so a status this code has never heard of
    # travels through and is displayed instead of being coerced into the
    # nearest known member.
    broker_status: str = ""

    submitted_at: datetime | None = None
    filled_qty: float = 0.0

    @property
    def is_stop(self) -> bool:
        """Does this order TRIGGER at a price, rather than rest at one?

        Substring rather than equality, because "stop", "stop_limit" and
        "trailing_stop" are all stops and the set grows with the broker.

        A trailing leg is therefore a stop here, which is what puts it in the
        protective group on the Board rather than among pending entries. It is
        the right answer: a trail reduces a position, it never opens one.
        """
        return "stop" in self.order_type.lower()

    @property
    def is_trailing(self) -> bool:
        """Does this leg move its own trigger as price runs in its favour?

        Worth asking apart from `is_stop` because the two disagree about what
        `stop_price` MEANS. On a fixed stop it is a level somebody chose and it
        will read the same tomorrow; on a trailing stop it is where the broker
        has trailed to so far, and quoting it without saying so states a
        decision that was never made.

        **This is the predicate `position_actions.detect_unexplained_moves`
        will need, and does not yet use.** That comparison holds a resting leg's
        `stop_price` against the journal's `effective_stop` and reports any
        difference as an unexplained move — which is right for a fixed leg and
        wrong for a trailing one, whose whole job is to move without anyone
        recording a reason. Measured: a trailing leg at 809 against a journalled
        820 is reported as an unexplained stop move, and would be again on every
        cycle it trailed. Nothing this bot PLACES can reach that state — Alpaca
        accepts no trailing leg on an entry, so `place_order` never creates one
        — but a leg placed by hand does, and so would any later path that
        converts a stop after the fill.
        """
        return "trailing" in self.order_type.lower()

    @property
    def trail_is_unreadable(self) -> bool:
        """A trailing leg whose trail size the broker did not report.

        The `trigger_price_unknown` problem one level up. A trailing stop with
        no trail on it can have its current trigger read and nothing whatever
        said about where that trigger goes next — so the level on screen looks
        like a stop that is quietly moving for reasons nobody can state.
        """
        return (
            self.is_trailing
            and self.trail_percent is None
            and self.trail_price is None
        )

    @property
    def trigger_price_unknown(self) -> bool:
        """A stop leg whose trigger price the broker did not report.

        This is the state worth showing loudly, and it is the reason
        `order_type` is carried alongside `stop_price`. A renderer that only
        checks `stop_price is None` cannot tell "this is a limit order and has
        no stop" from "this is the stop leg protecting a live position and its
        level is unreadable" — and it would print the same blank for both.
        """
        return self.is_stop and self.stop_price is None

    @property
    def remaining_qty(self) -> float:
        return max(self.qty - self.filled_qty, 0.0)

    def distance_to_fill(self, current_price: float) -> float | None:
        """How far the market is from the limit, as a percentage.

        Positive means the price still has to move in the order's favour. This
        is the difference between "waiting patiently" and "never going to fill",
        and neither is visible from the order alone.
        """
        if self.limit_price is None or current_price <= 0:
            return None
        if self.direction == Direction.BUY:
            return (current_price - self.limit_price) / current_price * 100
        return (self.limit_price - current_price) / current_price * 100


class Stance(StrEnum):
    """What the model made of a symbol this cycle.

    The point of recording anything other than TAKE: a cycle that proposes
    nothing currently leaves no trace of what was looked at. "Nothing worth
    taking" and "I never examined QQQ" are indistinguishable afterwards, and
    only one of them is a working bot.
    """

    TAKE = "take"        # proposed this cycle
    WATCH = "watch"      # setup forming; `waiting_for` says what would trigger it
    PASS = "pass"        # examined and declined
    BLOCKED = "blocked"  # would take it, but a rule or missing data forbids


class TriggerField(StrEnum):
    """What a trigger is measured against.

    Deliberately short, and every entry is a figure `indicators.py` already
    computes in Python. A field the loop cannot produce would be a trigger
    nobody can check, which is the failure this whole type exists to close.
    """

    CLOSE = "close"
    SMA_20 = "sma_20"
    SMA_200 = "sma_200"
    ATR_14 = "atr_14"
    VOLUME_RATIO = "volume_ratio"
    DISTANCE_FROM_SMA_20_ATR = "distance_from_sma_20_atr"
    SWING_HIGH = "swing_high"
    SWING_LOW = "swing_low"
    HIGHEST_CLOSE = "highest_close"
    LOWEST_CLOSE = "lowest_close"


class TriggerOp(StrEnum):
    ABOVE = "above"
    BELOW = "below"
    AT_OR_ABOVE = "at_or_above"
    AT_OR_BELOW = "at_or_below"


class AssessmentTrigger(BaseModel):
    """The machine-checkable half of a watch.

    `waiting_for` is prose for a human, and prose cannot be graded. Without
    this, "SPY closing below 641.20, roughly 1 ATR under the 20-day" is a
    sentence nobody can later score, so a watch is an opinion with no
    consequence and the stance means nothing.

    **The threshold is a number, never another field, and that is the design
    rather than a shortcut.** "Above the 20-day" re-checked next week tests a
    different level from the one the model was looking at, because the average
    moved — so it is not the claim that was made. A number pins the claim to
    the moment it was written, which is the entire point of pre-registering it.

    Evaluated in `triggers.py` by comparison against figures computed in
    Python. Nothing here is parsed out of prose.
    """

    field: TriggerField
    op: TriggerOp
    value: float = Field(
        description="The level, as a number read off the indicators supplied."
    )

    def holds(self, reading: float | None) -> bool | None:
        """Whether the condition is met. `None` when the figure is unavailable.

        Unknown is not False. A symbol whose bars could not supply the field
        has not failed the trigger, it simply cannot be scored, and reporting
        that as "did not fire" would quietly turn missing data into evidence.
        """
        if reading is None:
            return None
        if self.op is TriggerOp.ABOVE:
            return reading > self.value
        if self.op is TriggerOp.BELOW:
            return reading < self.value
        if self.op is TriggerOp.AT_OR_ABOVE:
            return reading >= self.value
        return reading <= self.value

    def render(self) -> str:
        return f"{self.field.value} {self.op.value.replace('_', ' ')} {self.value:,.4g}"


class SymbolAssessment(BaseModel):
    """One symbol the model considered, whether or not it proposed anything.

    Every property is REQUIRED on the wire and none in Python. See
    `EVERY_FIELD_REQUIRED`: there is one of these per symbol per cycle, so it is
    the object most likely to grow a field with a default.
    """

    model_config = EVERY_FIELD_REQUIRED

    symbol: str
    stance: Stance
    reasoning: str = Field(
        min_length=10,
        max_length=RATIONALE_MAX_CHARS,
        description="Why this stance, referring to the computed indicators supplied.",
    )
    waiting_for: str = Field(
        default="",
        max_length=RATIONALE_MAX_CHARS,
        description=(
            "For WATCH: the specific, observable condition that would turn this "
            "into a proposal. Name a level or a figure, not a feeling."
        ),
    )

    # Optional with a default, like every field added after the fact: the audit
    # log is append-only and never migrated, so a record written before this
    # existed must still parse.
    trigger: AssessmentTrigger | None = Field(
        default=None,
        description=(
            "For WATCH: the same condition as `waiting_for`, expressed as a "
            "field, a comparison and a number, so it can be checked later "
            "without anyone re-reading the sentence."
        ),
    )

    _trim = field_validator("reasoning", "waiting_for", mode="before")(truncate_free_text)


class PositionAction(StrEnum):
    """What may be done to a position that is already open.

    **There is deliberately no WIDEN_STOP.** Every member here either leaves
    exposure alone or reduces it, which is the whole reason this vocabulary can
    exist beside an order path that `RiskGate.evaluate` never sees: the gate
    vets proposals that OPEN exposure, and a stand-down that froze position
    management would strand open trades with no way out. That exemption is safe
    only for moves that cannot increase what is at risk.

    Moving a stop AWAY from entry is the one position move that increases the
    loss at unchanged size, on a live position, with no gate anywhere in the
    system to catch it. It is refused in `position_actions.classify_stop_move`
    rather than merely absent here — an enum with no member for it is a
    convention, and this repository uses structure where it can — but the
    absence is the readable half of the same guarantee.
    """

    HOLD = "hold"
    CLOSE = "close"
    TIGHTEN_STOP = "tighten_stop"


class PositionPlan(BaseModel):
    """The model's read on a position already held.

    **Advisory by default, and executable only behind a switch that is off.**
    Nothing here reaches the broker on the loop's own initiative:
    `position_actions.execute_position_plan` refuses unless
    `position_actions.enabled` is true in `config/rules.yaml`, and it ships
    false. The attended path — an operator in chat, or an agent calling
    `tighten_stop` / `close_position_with_reason` over MCP — is what the
    operator asked for; a loop that closes a position at 3am unattended is a
    different proposition and is the operator's own switch to throw.

    It exists because "why am I still in this, and what would get me out" is the
    question an open position raises and nothing else here answers.

    Every property is REQUIRED on the wire and none in Python. This object was
    the measured worst in the repository — five optional properties, which made
    `ModelDecision` the slowest schema this build sends — so a `null` for a
    plan that has nothing to say is cheap and its absence was not. See
    `EVERY_FIELD_REQUIRED`.
    """

    model_config = EVERY_FIELD_REQUIRED

    symbol: str
    action: PositionAction = PositionAction.HOLD
    thesis_intact: bool = True
    reasoning: str = Field(
        min_length=10,
        max_length=RATIONALE_MAX_CHARS,
        description="Why the position is still held, or why it should not be.",
    )
    waiting_for: str = Field(
        default="",
        max_length=RATIONALE_MAX_CHARS,
        description="The observable event that would close this: a level, a target, a date.",
    )

    _trim = field_validator("reasoning", "waiting_for", mode="before")(truncate_free_text)
    invalidation: str = Field(
        default="", description="What would prove the original thesis wrong."
    )

    # WHERE to move the stop to, for `action = tighten_stop`. Optional with a
    # default, like every field added after the fact: the audit log is
    # append-only and never migrated, so a plan written before this existed must
    # still parse.
    #
    # Without it a `tighten_stop` plan names an intention and no level, so it is
    # not executable at all — `execute_position_plan` refuses one and says why,
    # rather than picking a number, which would be the invented-target failure
    # arriving on the exit side.
    #
    # The direction is not the model's to choose. A level that widens the stop
    # is refused by `position_actions.classify_stop_move` whatever is written
    # here, on either side of the market.
    new_stop_price: float | None = Field(
        default=None,
        gt=0,
        description=(
            "For tighten_stop: the new stop level, as a number. It must be "
            "CLOSER to entry than the stop currently in force — a level further "
            "away is refused, because widening a stop on an open position "
            "increases the loss at unchanged size."
        ),
    )


def _required_prose(value: object) -> object:
    """Trim, then cap. A blank string survives as blank so `min_length` refuses it.

    Order matters: stripping before the length check is what makes a reason of
    three spaces a refusal rather than a stored reason nobody can read.
    """
    if isinstance(value, str):
        return truncate_free_text(value.strip())
    return value


class PositionActionRecord(BaseModel):
    """One intentional move on an OPEN position, and the reason it was made.

    `record_exit` takes a price, a time and a realised figure, so stop-hit,
    target-hit, closed-by-hand and expiry are indistinguishable afterwards. This
    is the front half of closing that gap: the reason is captured at the moment
    of the move rather than reconstructed from the numbers later, which is the
    only moment anybody actually knows it.

    **The reason is required and a blank one is refused**, here and again in
    `Journal.record_position_action`. Two guards rather than one because the
    absence of a reason is exactly what the Board's `unexplained-move` tag
    exists to make visible, and a row with an empty reason would be an
    explained move that explains nothing — worse than an unrecorded one,
    because it would silence the tag.

    Nothing here judges the reason. It does not have to be a good reason; it has
    to exist.

    `before_*` and `after_*` are the values on either side of the move, so the
    record is readable without joining it against whatever the trade row says
    today. `None` on both sides of a pair means that dimension did not change —
    a close moves quantity and not the stop.
    """

    id: int | None = None

    # The journal row this acted on, or `None` for a move on a position the
    # journal has never seen. Absent rather than zero, and the same fact as a
    # symbol appearing in `AccountSnapshot.symbols_with_unknown_risk`.
    trade_id: int | None = None

    symbol: str
    action: PositionAction

    # Who moved it. Free text and recorded verbatim — a claim about who acted
    # rather than a verified identity, the same as `Message.speaker` on a dream.
    # Conventionally "operator", "trader" (the chat agent) or "loop".
    actor: str = Field(min_length=1, max_length=64)

    at: datetime
    reason: str = Field(min_length=1, max_length=RATIONALE_MAX_CHARS)

    before_stop: float | None = None
    after_stop: float | None = None
    before_qty: float | None = None
    after_qty: float | None = None

    # The order the broker is now holding for this stop, if one is. Alpaca
    # REPLACES a leg rather than editing it, so this is a NEW id and not the one
    # that was resting before the move.
    broker_order_id: str | None = None

    # Whether the move actually reached the broker, as opposed to being recorded
    # against a journal figure alone. False is an ordinary state — crypto
    # carries no bracket at Alpaca, so its stop has always been a journal figure
    # watched by `stop_watch` — and it is carried explicitly because "the stop
    # moved" and "the stop moved at the broker" are different claims, and only
    # one of them puts something between the position and a loss.
    reached_broker: bool = False

    _trim = field_validator("reason", "actor", mode="before")(_required_prose)

    @property
    def stop_changed(self) -> bool:
        return self.before_stop is not None and self.after_stop is not None

    @property
    def qty_changed(self) -> bool:
        return self.before_qty is not None and self.after_qty is not None

    def describe(self) -> str:
        """One line, for a log or a cycle summary."""
        if self.action is PositionAction.TIGHTEN_STOP and self.stop_changed:
            where = "at the broker" if self.reached_broker else "in the journal only"
            what = (
                f"stop {self.before_stop:,.4f} -> {self.after_stop:,.4f} {where}"
            )
        elif self.action is PositionAction.CLOSE:
            qty = f"{self.before_qty:,.4f}" if self.before_qty is not None else "unknown"
            what = f"closed {qty}"
        else:
            what = str(self.action)
        return f"{self.symbol}: {what} — {self.actor}: {self.reason}"


class IndicatorSnapshot(BaseModel):
    """The daily figures for one symbol on one cycle, as numbers.

    Every field is optional because every one of them can genuinely be
    unavailable — `sma_200` needs 200 bars, `volume_ratio` needs an average to
    divide by — and `None` has to survive the round trip. A stored zero would
    be read back as a real figure by whoever looks at this next, which is the
    same reason `indicators.summarise` prints "unavailable" rather than a
    number it does not have.

    Field names match `TriggerField` so evaluation is a lookup, not a mapping
    table somebody has to keep in step.
    """

    close: float | None = None
    sma_20: float | None = None
    sma_200: float | None = None
    atr_14: float | None = None
    volume_ratio: float | None = None
    distance_from_sma_20_atr: float | None = None
    swing_high: float | None = None
    swing_low: float | None = None
    highest_close: float | None = None
    lowest_close: float | None = None

    def get(self, field: TriggerField) -> float | None:
        value = getattr(self, field.value, None)
        return value if isinstance(value, (int, float)) else None


class MarketInputs(BaseModel):
    """What the model was actually shown, recorded alongside what it decided.

    Without this a past decision cannot be re-read. "Why did it pass on SPY on
    Tuesday" is unanswerable if the headlines, the calendar and the indicators
    it saw are gone, and reconstructing them from a later snapshot answers a
    different question.
    """

    headlines: list[str] = Field(default_factory=list)
    social_posts: list[str] = Field(default_factory=list)
    news_windows: list[str] = Field(default_factory=list)
    indicators: dict[str, str] = Field(default_factory=dict)
    symbols_without_history: list[str] = Field(default_factory=list)

    # Both optional with a default, like everything else here: the audit log is
    # append-only and never migrated, so a reader that rejected a line written
    # before these existed would throw away the history it exists to preserve.
    intraday: dict[str, str] = Field(default_factory=dict)
    symbols_without_intraday: list[str] = Field(default_factory=list)

    # The same daily figures as `indicators`, as numbers rather than as the
    # rendered line. Both are kept on purpose and neither replaces the other.
    #
    # The rendered line is what a person reads when re-opening an old cycle.
    # These are what a trigger is checked against and what a chart is drawn
    # from, and they exist because the alternative — parsing the figures back
    # out of the prose — produces a value nobody can check. That is the failure
    # `indicators.py` exists to prevent, arriving from the other direction.
    #
    # This cannot be backfilled. A cycle recorded before it shipped has no
    # numbers, and no amount of later cleverness puts them there.
    readings: dict[str, IndicatorSnapshot] = Field(default_factory=dict)

    calendar_degraded: bool = False
    social_degraded: bool = False


class RiskVerdict(BaseModel):
    approved: bool
    reasons: list[str] = Field(default_factory=list)

    # Set when the ONLY thing that got this symbol past `_symbol_allowed` was a
    # live grant from an adopted dream, and carrying the instrument-class key
    # that grant resolved to — "us_equity", "crypto". `None` means the symbol
    # was already in `config/rules.yaml`, which is the ordinary case.
    #
    # **It names the class and not the dream, and the name says so**, because
    # the gate is handed a symbol -> class mapping and nothing else. That is
    # deliberate: the class is what the gate needs (it is how a granted symbol
    # gets limits at all), and a database read to fetch a dream id is exactly
    # what `RiskGate` must not do. Provenance — which dream — is resolved
    # outside the gate by `grants.resolve_grant_dream_ids` and stored on
    # `Trade.dream_id`. A field named for the dream while holding a class key
    # would be a plausible wrong label, which is the failure this repository
    # exists to refuse.
    #
    # Recorded on a rejection as well as an approval. A refused trade in a
    # granted symbol is a fact about the grant, and the Decisions page is the
    # only surface a rejected proposal exists on.
    #
    # Optional with a default, like every field added after the fact: the audit
    # log is append-only and never migrated, so a verdict written before this
    # existed must still parse.
    granted_by_dream_class: str | None = None

    @property
    def permitted_by_dream_grant(self) -> bool:
        """Whether a dream is the reason this symbol could be considered."""
        return self.granted_by_dream_class is not None

    @classmethod
    def approve(cls, *, granted_by_dream_class: str | None = None) -> RiskVerdict:
        return cls(approved=True, granted_by_dream_class=granted_by_dream_class)

    @classmethod
    def reject(
        cls, *reasons: str, granted_by_dream_class: str | None = None
    ) -> RiskVerdict:
        return cls(
            approved=False,
            reasons=list(reasons),
            granted_by_dream_class=granted_by_dream_class,
        )


class Trade(BaseModel):
    """One trade's full lifecycle, from proposal through to close.

    This is the journal's unit of record. Everything the metrics engine reports
    — R-multiple, expectancy, profit factor, MAE/MFE — is derived from these.
    """

    id: int | None = None

    symbol: str
    asset_class: AssetClass = AssetClass.EQUITY
    strategy: str = "unspecified"
    direction: Direction
    qty: float = Field(gt=0)

    entry_time: datetime
    entry_price: float = Field(gt=0)
    planned_stop: float = Field(gt=0)
    # `None` where the trade was opened with no target. See
    # `OrderProposal.take_profit_price`: the stop is required, the exit
    # never was.
    planned_target: float | None = Field(default=None, gt=0)

    # The stop actually in force now, when it is no longer the one the position
    # was sized against. `None` means it has never moved, which is the truth
    # about every trade this bot has placed so far.
    #
    # **Two columns rather than one, and the reason is that they answer
    # different questions.** `planned_stop` is the sizing figure: the position's
    # quantity was computed from `|entry - planned_stop|`, so it is what 1R
    # means and what `metrics.py` grades stop placement against. Overwriting it
    # on a tighten would silently redefine R after the fact and make a trade
    # that lost half what it risked read as a full stop-out.
    #
    # `effective_stop` is what would actually fill, so it is what the risk caps
    # count, what the model is shown, and what a breach is measured against.
    #
    # Only ever moves TOWARD entry. `position_actions.classify_stop_move`
    # refuses the other direction, on either side of the market, because
    # widening a stop on an open position is the one position move that
    # increases the loss at unchanged size and no gate in this system sees it.
    current_stop: float | None = Field(default=None, gt=0)

    # What was ORDERED, kept beside what was filled.
    #
    # `qty` and `entry_price` are written from the proposal at submission and
    # corrected by `reconcile` once the entry order is terminal, so without
    # these two the correction would erase the only record of what was actually
    # asked for. Measured on the first real order: the limit was 772.84 and the
    # fill averaged 773.324285, so `open_risk_usd` read $990.36 against a real
    # $980.19 — overstated, which is the safe direction, and still a figure that
    # does not describe the account.
    #
    # `None` on a row written before this existed. Absent, never zero: a
    # submitted quantity of zero is not a thing anybody ordered, and
    # `fill_shortfall_qty` answers `None` rather than inventing a difference.
    submitted_qty: float | None = Field(default=None, gt=0)
    submitted_price: float | None = Field(default=None, gt=0)

    # How much of the above is known to have happened. See `FillState`.
    #
    # `UNCONFIRMED` is the default and is the truthful state of a row the
    # instant it is written, as well as of every row written before this field
    # existed — in both cases nothing was read back from the broker. It is not a
    # migration artefact wearing an optimistic label.
    fill_state: FillState = FillState.UNCONFIRMED

    exit_time: datetime | None = None
    exit_price: float | None = None

    # Why it closed. `None` means nothing was recorded — a row that predates the
    # exit review — which is deliberately NOT the same as `ExitReason.UNKNOWN`,
    # where the question was asked and could not be answered. Same rule as
    # `has_cycles` in `news_history` and `can_grade_anything` in `triggers`: not
    # graded and graded-as-unknowable are opposite findings.
    exit_reason: ExitReason | None = None

    # Whether `exit_price` is a real reading or a fallback. `reconcile` uses the
    # entry price when the broker cannot supply a mark, which makes the trade
    # look flat rather than inventing a plausible result — and it also puts the
    # exit at neither level, so an exit review that did not know would report a
    # confident `CLOSED_EARLY` off no evidence at all.
    #
    # `None` means it was not recorded, which is every row written before this
    # existed. Not `False`: the good outcome must not be what an absence looks
    # like.
    exit_price_estimated: bool | None = None

    realised_pnl_usd: float | None = None
    fees_usd: float = 0.0

    # Worst and best unrealised P&L seen while the trade was open, in USD.
    # Sampled at the decision interval rather than from ticks, so both
    # understate the true excursion — see docs.
    mae_usd: float = 0.0
    mfe_usd: float = 0.0

    execution_mode: ExecutionMode = ExecutionMode.PAPER
    rationale: str = ""
    entry_order_id: str | None = None
    exit_order_id: str | None = None

    # The adopted dream whose grant permitted this symbol, or `None` for a trade
    # in a symbol `config/rules.yaml` already allowed — which is every trade the
    # bot has ever placed.
    #
    # **Provenance, never endorsement.** It says where the permission to hold
    # this came from. It must not be rendered as "a prophecy backs this trade":
    # the chain that produced it is speculative by construction, and a badge
    # implying otherwise would put the dreamer's confidence next to a real
    # figure. It is also the only thing that makes the Board's `dream` and
    # `dream-expired-holding` tags derivable from STORED state rather than
    # recomputed by a model, which is what stops a tag being argued into
    # existence.
    #
    # `journal.SCHEMA` carries the column and `_add_dream_id_column` adds it to
    # a database that predates it. `CREATE TABLE IF NOT EXISTS` does nothing to
    # a table that already exists, and the suite is structurally blind to that
    # because every test builds a fresh journal in a `tmp_path`.
    dream_id: int | None = None

    @property
    def planned_risk_usd(self) -> float:
        """What the trade was designed to lose if the stop filled as planned.

        The SIZING figure, and deliberately fixed at entry: this is the
        denominator of `r_multiple`, so it has to keep meaning "what one R was
        when this position was sized". A stop moved afterwards changes what the
        trade now stands to lose — that is `current_risk_usd` — and does not
        change what it was built to lose.
        """
        return abs(self.entry_price - self.planned_stop) * self.qty

    @property
    def effective_stop(self) -> float:
        """The stop in force. The moved one if it has been moved, else the plan.

        Every consumer that asks "what would this fill at" wants this rather
        than `planned_stop`: the risk caps, the level the model is shown on the
        position it manages, and the breach check.
        """
        return self.current_stop if self.current_stop is not None else self.planned_stop

    @property
    def stop_has_moved(self) -> bool:
        return self.current_stop is not None

    @property
    def current_risk_usd(self) -> float:
        """What the trade loses NOW if its stop fills. What the caps count.

        Floored at zero rather than left as `abs(...)`, and that floor is the
        point at which the arithmetic would otherwise start lying. A stop moved
        past entry — breakeven-plus, the end state of tightening — is a
        GUARANTEED PROFIT, not a large loss, and `abs()` would report it as
        risk growing again the further into profit it went. Zero is the honest
        answer: there is nothing left to lose on this position.
        """
        if self.direction == Direction.BUY:
            return max(0.0, self.entry_price - self.effective_stop) * self.qty
        return max(0.0, self.effective_stop - self.entry_price) * self.qty

    @property
    def is_open(self) -> bool:
        return self.exit_time is None

    @property
    def fill_is_confirmed(self) -> bool:
        """Has anything been read back from the broker about this entry?

        False for `UNCONFIRMED` and for `RESTING`, and those are different
        facts: the first is "nobody has looked", the second is "we looked and
        the order is still sitting there". Both mean the quantity and price on
        this row came from the proposal rather than from a fill.
        """
        return self.fill_state in (FillState.PARTIAL, FillState.COMPLETE)

    @property
    def fill_shortfall_qty(self) -> float | None:
        """How much of the order never arrived. `None` when it cannot be said.

        Answers `None` rather than zero whenever the submitted quantity was
        never recorded or the fill has not been confirmed — an unconfirmed row
        whose `qty` still equals the proposal's would otherwise report a
        shortfall of nothing, which is a claim about a fill nobody has read.
        """
        if self.submitted_qty is None or not self.fill_is_confirmed:
            return None
        return max(0.0, self.submitted_qty - self.qty)

    @property
    def entry_price_is_the_proposal(self) -> bool:
        """True while `entry_price` is still the limit price that was asked for.

        The reason `open_risk_usd` can be wrong in the safe direction: risk is
        `|entry - stop| x qty`, and a limit price is not a fill.
        """
        return not self.fill_is_confirmed

    @property
    def net_pnl_usd(self) -> float | None:
        if self.realised_pnl_usd is None:
            return None
        return self.realised_pnl_usd - self.fees_usd

    @property
    def r_multiple(self) -> float | None:
        """Net result as a multiple of the risk actually planned.

        The unit that makes trades of different sizes comparable: +2R is the
        same quality of outcome whether the position was $500 or $5,000.
        """
        net = self.net_pnl_usd
        risk = self.planned_risk_usd
        if net is None or risk <= 0:
            return None
        return net / risk

    def outcome(self, scratch_threshold_r: float) -> TradeOutcome | None:
        """Classify for the consecutive-loss counter. None while still open."""
        r = self.r_multiple
        if r is None:
            return None
        if r > scratch_threshold_r:
            return TradeOutcome.WIN
        if r < -scratch_threshold_r:
            return TradeOutcome.LOSS
        return TradeOutcome.SCRATCH


class StandDownState(BaseModel):
    """Persisted stand-down state.

    Lives in SQLite rather than in memory: a stand-down that vanished when the
    process restarted would be trivially defeated by restarting the process,
    which is precisely what someone tilting would do.
    """

    stage: int = Field(default=0, ge=0, le=2, description="0 = not standing down")
    started_at: datetime | None = None
    ends_at: datetime | None = None
    consecutive_losses: int = Field(default=0, ge=0)
    last_triggered_at: datetime | None = None

    def is_active(self, now: datetime) -> bool:
        return self.ends_at is not None and now < self.ends_at

    def days_remaining(self, now: datetime) -> float:
        if not self.is_active(now) or self.ends_at is None:
            return 0.0
        return (self.ends_at - now).total_seconds() / 86_400


class Decision(BaseModel):
    """One full pass of the loop — what we asked, what the model said, what we did.

    Every field added after `notes` is optional with a default, so a record
    written by an older build still parses. The audit log is append-only and
    never migrated; a reader that rejected yesterday's format would throw away
    the history it exists to preserve.
    """

    timestamp: datetime
    proposals: list[OrderProposal] = Field(default_factory=list)
    verdicts: list[RiskVerdict] = Field(default_factory=list)
    executed: list[OrderResult] = Field(default_factory=list)

    # What was considered, not merely what was proposed.
    assessments: list[SymbolAssessment] = Field(default_factory=list)
    position_plans: list[PositionPlan] = Field(default_factory=list)
    # What the model was shown when it decided.
    inputs: MarketInputs | None = None
    claude_input_tokens: int = 0
    claude_output_tokens: int = 0
    claude_cached_tokens: int = 0
    estimated_cost_usd: float = 0.0
    notes: str = ""

    # **Which model actually produced this decision, as the endpoint named it.**
    # The token fields above are called `claude_*` because they are written into
    # `audit/*.jsonl`, which is append-only and never migrated — renaming them
    # would read every historical cycle back as `0 in / 0 out`. This one is new,
    # so it gets the honest name.
    #
    # `None` means the response carried no model id, or the record predates this
    # field. It is not "the requested one": a cycle whose served model is
    # unknown and a cycle served by the model that was asked for are different
    # findings, and only one of them is reassuring.
    # `""` rather than `None`, matching `CallUsage`, so one convention answers
    # "which model" everywhere. On this record the empty string covers both
    # ways the answer can be missing — the reply named no model, and the record
    # predates the field — and they are the same finding to a reader: nobody
    # can say what produced this cycle. What it must never read as is
    # agreement.
    served_model_id: str = ""

    # And what was ASKED for, so the two can be compared by anything reading
    # this record back. Storing only the served id would leave the Decisions
    # page comparing against whatever the process is configured for TODAY,
    # which is a different fact about a different moment.
    requested_model_id: str = ""

    @property
    def served_as_requested(self) -> bool | None:
        """Did the endpoint answer with the model this cycle asked for?

        One implementation, shared with `CallUsage.served_as_requested` through
        `served_matches_requested` — two copies of this comparison would be two
        answers, and the one on the page is the one nobody re-checks.

        It matters on the Decisions page in particular because that page prints
        a COST, and the cost was computed from the price sheet of the model
        that was asked for. A substitution makes the figure beside it wrong.
        """
        return served_matches_requested(
            requested=self.requested_model_id, served=self.served_model_id
        )
