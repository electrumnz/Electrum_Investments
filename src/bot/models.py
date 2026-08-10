"""Domain models — orders, positions, market snapshots, Claude decisions.

Terminology note: these models describe an **Alpaca** account (US equities and
crypto), not an FX/CFD account. Quantities are shares or coin units, never
"lots"; order identifiers are Alpaca UUID strings, never integer tickets; and
Alpaca aggregates all exposure to one symbol into a single position, so
positions are keyed by symbol rather than by individual fill.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator

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
    # `claude_client` asks for a `position_plan` on every open position with an
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
    """What Claude proposes — must pass the risk gate before execution."""

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
    rationale: str = Field(min_length=10, max_length=RATIONALE_MAX_CHARS)

    # Truncates rather than rejects, and only because nothing reads this field.
    # See RATIONALE_MAX_CHARS.
    _trim_rationale = field_validator("rationale", mode="before")(truncate_free_text)

    @property
    def notional_usd(self) -> float:
        return self.qty * self.limit_price

    @property
    def risk_usd(self) -> float:
        """What this trade loses if the stop fills as planned.

        The unit the risk caps are measured in, and the reason they are
        leverage-neutral: this is the same number whether the position was paid
        for in cash, on margin, or as a futures contract.
        """
        return abs(self.limit_price - self.stop_loss_price) * self.qty


class OrderResult(BaseModel):
    accepted: bool
    order_id: str | None = None
    error: str | None = None
    filled_price: float | None = None
    filled_qty: float | None = None


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
    stop_price: float | None = None

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
        """
        return "stop" in self.order_type.lower()

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
    """One symbol the model considered, whether or not it proposed anything."""

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
    HOLD = "hold"
    CLOSE = "close"
    TIGHTEN_STOP = "tighten_stop"


class PositionPlan(BaseModel):
    """The model's read on a position already held.

    **Advisory only.** The loop does not act on these: closing a position and
    moving a stop are deliberately outside the proposal path, so nothing here
    reaches the broker. It exists because "why am I still in this, and what
    would get me out" is the question an open position raises and nothing else
    here answers.
    """

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

    exit_time: datetime | None = None
    exit_price: float | None = None

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
        """What the trade was designed to lose if the stop filled as planned."""
        return abs(self.entry_price - self.planned_stop) * self.qty

    @property
    def is_open(self) -> bool:
        return self.exit_time is None

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
    """One full pass of the loop — what we asked, what Claude said, what we did.

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
