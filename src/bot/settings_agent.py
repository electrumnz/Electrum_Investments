"""The settings agent: the only route to changing `config/rules.yaml` from the
interface, and it does not change it.

## What this is

An operator wants a limit moved. Today that means editing a YAML file over SSH,
which is fine when the change is considered and offers nothing at all when it is
not — the file does not know what a number is for, and it will take any value
that parses. What was missing is the argument: somebody who states what the new
number actually costs, makes the operator say what it is for, and then gets out
of the way.

**It pushes back; it does not deny.** That distinction is the whole design.
There was briefly a validator in `config.py` that refused a per-class limit
looser than the portfolio one at config load, and it was removed, because a hard
refusal is the same intent implemented as a wall — at boot, with no explanation
of the trade-off and no way for the operator to say "yes, I mean it". This is
what replaced it. If it ends up refusing a change the operator has heard the
consequence of and still wants, it has become the thing it was built to replace.

## Why it produces a request instead of writing the file

`config/` is owned by root on the droplet (`deploy/bootstrap.sh`) precisely so
the service account cannot edit its own limits. The web process runs as
`mudhorn`. **A bot that can widen its own risk limits is exactly what this
architecture is built to prevent**, so an agent that wrote `rules.yaml` directly
would require removing a deliberate safety property to make a convenience work.

And `CLAUDE.md` already says it plainly: limits change deliberately, in their
own commit, with a reason.

So the split is:

- **Here, unprivileged.** Argue, compute the arithmetic consequence, and record
  a `ChangeRequest` in `data/settings_requests.db` — its own file, never the
  journal, and one the service account already owns. It carries the exact key,
  the old value, the new value, the operator's stated reason, the agent's stated
  objection, the transcript, and a digest of the rules file as it stood.
- **There, privileged.** `electrum-bot settings-apply <id>`, run as root, which
  re-validates the edited file through `Rules.load` before replacing anything
  and refuses if the file has moved since the request was argued.

The transcript is kept because **an argument the operator won is a fact worth
keeping, and so is one they lost.** Six months later the useful question is not
what the limit is — the file says that — but what was said before it moved.

## The asymmetry is in the code, not only in the character

`souls/armorer.md` shapes how this is said. What decides it is here:

- A **tightening** change is acknowledged, its cost is stated, and it is
  recorded with no confirmation step. Somebody choosing to lose less should not
  be interrogated for it.
- A **loosening** change states the arithmetic consequence and requires the
  operator to confirm explicitly *after* reading it. Nothing is recorded on the
  first ask.

## The arithmetic is computed here and handed to the model

Same rule as `indicators.py`, arriving at a different surface. A model asked to
work out what doubling a per-trade cap does will produce a number, state it
confidently, and be believed. So every consequence in `LIMITS` is either static
prose lifted from the file that owns the limit, or arithmetic done in Python
over the rules actually loaded. The model reads figures; it does not derive
them.

**Equity is read, never assumed.** Every money figure here is optional and is
omitted with a stated reason when no broker reading is available, rather than
computed against a plausible $100,000. The percentages still stand without it.

Pure apart from the store and the two file operations, which are named for what
they touch. Nothing in this module is reachable from `RiskGate`, from the
decision loop, or from any order path.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import structlog

from .config import DEFAULT_RULES_PATH, Rules

log = structlog.get_logger(__name__)

#: Its own file, and deliberately not the journal.
#:
#: `data/journal.db` is the only irreplaceable file on the box and is backed up
#: hourly. This is a record of arguments about limits: worth keeping, worth
#: reading, and never something a query about a position should be able to reach
#: by accident. Same reasoning as `data/dreams.db`.
DEFAULT_REQUESTS_PATH = Path("data/settings_requests.db")


# --------------------------------------------------------------- the table


class Looser(StrEnum):
    """Which direction of a change counts as loosening.

    Not derivable from the name. `max_risk_per_trade_pct` loosens upward and
    `min_equity_floor_usd` loosens downward, and getting that backwards would
    make the agent argue hardest against the operator being careful — which is
    the exact inversion the asymmetry exists to avoid.
    """

    HIGHER = "higher"
    LOWER = "lower"


class Unit(StrEnum):
    PCT_OF_EQUITY = "pct_of_equity"
    PCT = "pct"
    USD = "usd"
    COUNT = "count"
    SECONDS = "seconds"
    MINUTES = "minutes"
    DAYS = "days"
    R = "r"
    LIST = "list"


@dataclass(frozen=True)
class LimitFact:
    """One limit, and everything a person needs to argue about it.

    Four fields carry the reasoning, and they answer four different questions
    that are easy to collapse into one:

    - `what` — what the number does.
    - `why` — why it sits at the value it sits at.
    - `goal` — the goal it serves, which usually outlives the number.
    - `cost` — what LOOSENING it actually costs, in the account rather than in
      the abstract.

    The text is lifted from `config/rules.yaml` and `CLAUDE.md` rather than
    reinvented, so there is one story about each limit and not two that drift.
    The Settings page renders `what` from here for the same reason.

    `requestable` is False for a limit this path cannot safely edit — a list
    rather than a scalar. The agent still explains it; it simply cannot produce
    a diff for it, and says which.
    """

    key: str
    label: str
    what: str
    why: str
    goal: str
    cost: str
    looser: Looser
    unit: Unit
    family: str
    integer: bool = False
    requestable: bool = True

    @property
    def owner(self) -> str:
        return "config/rules.yaml, enforced in src/bot/risk.py"


def _account_facts() -> list[LimitFact]:
    return [
        LimitFact(
            key="account.max_risk_per_trade_pct",
            label="Risk per trade",
            what="Most any single position may lose if its stop fills.",
            why="One of the operator's four rules. Measured in risk rather than "
            "notional, which is what makes it mean the same thing on cash "
            "equities, on margin, on options and on futures.",
            goal="No single trade can be the one that matters.",
            cost="Every future trade may lose proportionally more, at the same "
            "win rate, and size is computed from stop distance — so this buys a "
            "bigger position at the same stop, never a safer one.",
            looser=Looser.HIGHER,
            unit=Unit.PCT_OF_EQUITY,
            family="per_trade_risk",
        ),
        LimitFact(
            key="account.max_total_risk_pct",
            label="Total open risk",
            what="Most everything open may lose at once, summed as "
            "|entry - stop| x qty across every position.",
            why="The second of the four rules, and the one that makes the first "
            "mean anything: a 1% cap applied ten times is a 10% account.",
            goal="A bad day cannot be a bad year.",
            cost="If every stop filled at once the account loses more. It is "
            "also the ceiling every per-class limit runs into, so raising it is "
            "what makes a raised class limit reachable.",
            looser=Looser.HIGHER,
            unit=Unit.PCT_OF_EQUITY,
            family="total_risk",
        ),
        LimitFact(
            key="account.max_position_pct",
            label="Concentration",
            what="A backstop on one position's market value as a share of equity.",
            why="Deliberately generous at 50%. A 1%-risk trade with a 2% stop is "
            "already a position worth about half the account, so a tight number "
            "here would silently become the real limit and override the risk "
            "rules it sits beside.",
            goal="Catch an absurd position, not shape an ordinary one.",
            cost="Little on its own, because the risk caps bind first — which "
            "is the point worth knowing: moving this usually changes nothing, "
            "and a change that appears to do nothing is one nobody revisits.",
            looser=Looser.HIGHER,
            unit=Unit.PCT_OF_EQUITY,
            family="position_pct",
        ),
        LimitFact(
            key="account.max_concurrent_positions",
            label="Concurrent positions",
            what="How many positions may be open at once across every class.",
            why="Three, against a 2% total at 1% a trade — so the risk cap and "
            "the count arrive at roughly the same answer from two directions.",
            goal="Keep the book small enough to hold in your head.",
            cost="More positions to watch, and more ways for the total-risk cap "
            "to be the thing that refuses a trade rather than this.",
            looser=Looser.HIGHER,
            unit=Unit.COUNT,
            family="concurrent",
            integer=True,
        ),
        LimitFact(
            key="account.daily_loss_kill_pct",
            label="Daily loss kill",
            what="Once the day is down this much from starting equity, no new "
            "position opens until the next session.",
            why="Sticky for the session on purpose: a recovery within the same "
            "day does not re-enable trading, because the recovery is not "
            "evidence the day got better.",
            goal="Stop a bad day being traded into a worse one.",
            cost="The day may lose more before anything interrupts it, and the "
            "hours after a large loss are the ones this rule exists to remove.",
            looser=Looser.HIGHER,
            unit=Unit.PCT_OF_EQUITY,
            family="daily_loss",
        ),
        LimitFact(
            key="account.min_equity_floor_usd",
            label="Equity floor",
            what="Below this the bot halts entirely.",
            why="Set to a 10% drawdown against Alpaca's default $100,000 paper "
            "balance. It is the stop on the account itself.",
            goal="A drawdown stays a drawdown.",
            cost="The halt moves further away, which is what turns a drawdown "
            "into a wipe-out. Lowering it is the only change here that removes a "
            "floor rather than widening a window.",
            looser=Looser.LOWER,
            unit=Unit.USD,
            family="equity_floor",
            integer=True,
        ),
    ]


def _frequency_facts() -> list[LimitFact]:
    return [
        LimitFact(
            key="frequency.max_trades_per_day",
            label="Trades per day",
            what="How many entries may be opened in one day.",
            why="Frequency is treated as a risk parameter here rather than a "
            "performance one. In the Alpha Arena competition fees dominated "
            "P&L: the model that made 238 trades lost 57% of its stake, and the "
            "one that made 38 lost least of the US models.",
            goal="Stop the account being churned.",
            cost="More entries, more fees, and more chances for a marginal "
            "trade to be proposed to look useful.",
            looser=Looser.HIGHER,
            unit=Unit.COUNT,
            family="trades_per_day",
            integer=True,
        ),
        LimitFact(
            key="frequency.max_trades_per_week",
            label="Trades per week",
            what="How many entries may be opened in one week.",
            why="The weekly figure is what catches a run of ordinary days that "
            "add up to a churn nobody chose.",
            goal="Stop the account being churned across days as well as within one.",
            cost="Same as the daily cap, spread thinner and therefore harder to "
            "notice.",
            looser=Looser.HIGHER,
            unit=Unit.COUNT,
            family="trades_per_week",
            integer=True,
        ),
        LimitFact(
            key="frequency.min_seconds_between_trades_per_symbol",
            label="Per-symbol cooldown",
            what="How long before the same symbol may be traded again.",
            why="Thirty minutes. The failure it prevents is flip-flopping: "
            "closing a name and immediately re-entering it in the other "
            "direction on the same fifteen-minute bar.",
            goal="A change of mind costs a pause.",
            cost="A reversal can be executed as fast as the loop can propose it, "
            "which is the behaviour that pays fees twice for one opinion.",
            looser=Looser.LOWER,
            unit=Unit.SECONDS,
            family="cooldown",
            integer=True,
        ),
    ]


def _stand_down_facts() -> list[LimitFact]:
    return [
        LimitFact(
            key="stand_down.consecutive_losses_trigger",
            label="Stand-down trigger",
            what="How many qualifying losses in a row suspend live execution.",
            why="The third of the operator's four rules. A losing run is when "
            "discipline is weakest and sizing gets worst, so this interrupts the "
            "loop rather than trying to predict the next trade.",
            goal="Interrupt revenge trading before it is expensive.",
            cost="More losses in a row before anything stops. Paper trading "
            "continues during a stand-down either way, so what this actually "
            "buys back is the money.",
            looser=Looser.HIGHER,
            unit=Unit.COUNT,
            family="stand_down_trigger",
            integer=True,
        ),
        LimitFact(
            key="stand_down.loss_threshold_r",
            label="Scratch threshold",
            what="A loss smaller than this many R neither counts toward the "
            "streak nor resets it.",
            why="Without it, cutting a trade for a few dollars would count "
            "against the operator, which pushes the bot toward holding losers.",
            goal="Count real losses, not paper cuts.",
            cost="More real losses are reclassified as scratches, so the streak "
            "counter reaches the trigger later or never.",
            looser=Looser.HIGHER,
            unit=Unit.R,
            family="scratch",
        ),
        LimitFact(
            key="stand_down.stage_one_days",
            label="First trip",
            what="How long live execution is withheld on the first breach.",
            why="Three days: long enough to be a break, short enough that the "
            "operator does not simply wait it out by disabling the rule.",
            goal="Put distance between a losing run and the next live trade.",
            cost="A shorter break, taken at the moment a break is worth most.",
            looser=Looser.LOWER,
            unit=Unit.DAYS,
            family="stand_down_days",
            integer=True,
        ),
        LimitFact(
            key="stand_down.stage_two_days",
            label="Repeat trip",
            what="How long live execution is withheld on a second breach inside "
            "the repeat window.",
            why="Ten days. A second trip inside a month looks like a pattern "
            "rather than variance, so it costs more.",
            goal="Escalation has to escalate.",
            cost="A repeat breach costs less, which makes the escalation less "
            "of one.",
            looser=Looser.LOWER,
            unit=Unit.DAYS,
            family="stand_down_days",
            integer=True,
        ),
        LimitFact(
            key="stand_down.repeat_window_days",
            label="Repeat window",
            what="How recently the last breach must have been for the next one "
            "to escalate.",
            why="Thirty days, which is roughly the horizon over which a run of "
            "losses is still the same run.",
            goal="Recognise a pattern rather than a coincidence.",
            cost="A second breach escalates less often, so a pattern reads as "
            "two unrelated incidents.",
            looser=Looser.LOWER,
            unit=Unit.DAYS,
            family="repeat_window",
            integer=True,
        ),
    ]


def _margin_and_news_facts() -> list[LimitFact]:
    return [
        LimitFact(
            key="margin.max_buying_power_utilisation_pct",
            label="Buying-power use",
            what="Share of available buying power one order may consume.",
            why="Alpaca rejects orders that would create an Intraday Margin "
            "Deficit in real time, and repeated non-compliance inside five "
            "business days costs a 90-day account restriction.",
            goal="Never find the edge of the margin rules by hitting it.",
            cost="Less headroom against a deficit call, and the penalty for "
            "getting that wrong is far worse than a skipped trade.",
            looser=Looser.HIGHER,
            unit=Unit.PCT,
            family="buying_power",
        ),
        LimitFact(
            key="margin.max_gross_notional_pct",
            label="Gross exposure",
            what="Total market exposure as a share of equity.",
            why="Reg T permits 200% overnight. 150% leaves headroom against it "
            "rather than sitting on the line.",
            goal="Leverage stays a choice rather than an accident.",
            cost="Less room between the book and the regulatory ceiling, which "
            "is the distance a gap has to move through before it is a call.",
            looser=Looser.HIGHER,
            unit=Unit.PCT,
            family="gross_notional",
        ),
        LimitFact(
            key="news_blackout_minutes_before",
            label="News blackout, before",
            what="No new position opens this many minutes before a high-impact "
            "announcement.",
            why="An earnings print moves a price further than a stop distance "
            "was sized for, so the position that was 1% of equity is not.",
            goal="Do not be in a name when the number lands.",
            cost="Positions may be opened closer to a print, where the stop "
            "means less than the arithmetic that sized it says.",
            looser=Looser.LOWER,
            unit=Unit.MINUTES,
            family="news_blackout",
            integer=True,
        ),
        LimitFact(
            key="news_blackout_minutes_after",
            label="News blackout, after",
            what="No new position opens this many minutes after a high-impact "
            "announcement.",
            why="The spread after a print is wide and one-sided, which is the "
            "habitat of a limit read off a price that is not there.",
            goal="Let the book come back before trading it.",
            cost="Entries land into the widest spread of the session.",
            looser=Looser.LOWER,
            unit=Unit.MINUTES,
            family="news_blackout",
            integer=True,
        ),
        LimitFact(
            key="options.min_days_to_expiry_for_entry",
            label="Minimum days to expiry",
            what="Refuse to open an option position closer than this to expiry.",
            why="Alpaca auto-exercises anything in the money by $0.01, "
            "auto-assigns short in-the-money positions after the close, and "
            "liquidates an in-the-money position the account cannot fund inside "
            "the final hour.",
            goal="The broker never gets to decide how a position ends.",
            cost='"Do Not Exercise" cannot be filed through the API, so closing '
            "early is the only programmatic way to choose a different outcome — "
            "and this is the margin that leaves room to.",
            looser=Looser.LOWER,
            unit=Unit.DAYS,
            family="expiry_entry",
        ),
        LimitFact(
            key="options.warn_days_before_expiry",
            label="Expiry warning",
            what="How far ahead an approaching expiry starts warning.",
            why="A week gives time to plan an exit rather than react to one.",
            goal="An expiry is never a surprise.",
            cost="Less notice, on the one event in this system that resolves "
            "itself whether anyone is watching or not.",
            looser=Looser.LOWER,
            unit=Unit.DAYS,
            family="expiry_warn",
        ),
    ]


def _class_facts(name: str) -> list[LimitFact]:
    """This class's own limits.

    Derived per configured class rather than written out, so adding a class to
    `config/rules.yaml` gives the agent its limits with no second edit here.

    The shared clause on all four: **a per-class value OVERRIDES the portfolio
    one in either direction.** `account:` is the default, not a ceiling, and
    nothing refuses a looser class limit — the validator that once did was
    removed, and this agent is what replaced it.
    """
    where = f"instruments.{name}"
    return [
        LimitFact(
            key=f"{where}.max_risk_per_trade_pct",
            label=f"{name}: risk per trade",
            what=f"Most a single {name} position may lose if its stop fills.",
            why="A per-class value overrides the portfolio one in either "
            "direction. Crypto's is half the equity book's because it trades "
            "while nobody is awake, moves harder, and has no circuit breaker.",
            goal="A class that behaves differently is sized differently.",
            cost="The same as the portfolio limit, confined to one class — and "
            "raising it above max_total_risk_pct buys nothing at all, because "
            "the portfolio total-risk gate refuses the trade first.",
            looser=Looser.HIGHER,
            unit=Unit.PCT_OF_EQUITY,
            family="per_trade_risk",
        ),
        LimitFact(
            key=f"{where}.max_class_total_risk_pct",
            label=f"{name}: total class risk",
            what=f"Most every open {name} position may lose at once, summed "
            "across the class.",
            why="The per-class counterpart to max_total_risk_pct, and a "
            "different question from the per-trade cap: a class allowed 0.5% a "
            "trade with no total is a class that can quietly hold four of them.",
            goal="One class cannot become the book.",
            cost="Measured on PLANNED risk, so unrealised profit does not "
            "offset it and a winner buys no room for a second position. At the "
            "cap, something in the class has to be closed before another opens.",
            looser=Looser.HIGHER,
            unit=Unit.PCT_OF_EQUITY,
            family="class_total_risk",
        ),
        LimitFact(
            key=f"{where}.max_position_pct",
            label=f"{name}: concentration",
            what=f"A backstop on one {name} position's market value.",
            why="Overrides the portfolio concentration backstop for this class.",
            goal="Catch an absurd position in a class that can produce one.",
            cost="Little on its own, because the risk caps bind first.",
            looser=Looser.HIGHER,
            unit=Unit.PCT_OF_EQUITY,
            family="position_pct",
        ),
        LimitFact(
            key=f"{where}.max_concurrent_positions",
            label=f"{name}: concurrent positions",
            what=f"How many {name} positions may be open at once.",
            why="Counted WITHIN the class, so both caps apply and they measure "
            "different things: a class that gets loud cannot fill every slot "
            "the portfolio has.",
            goal="No class monopolises the book.",
            cost="More slots for one class, against a portfolio count that is "
            "unchanged and still binds.",
            looser=Looser.HIGHER,
            unit=Unit.COUNT,
            family="concurrent",
            integer=True,
        ),
        LimitFact(
            key=f"{where}.allowed_symbols",
            label=f"{name}: allowed symbols",
            what="What the gate will let the bot trade in this class. A "
            "permission, unlike the watchlist, which is display only.",
            why="Liquid, heavily covered names, kept short. Every entry is an "
            "instrument the bot may open a position in.",
            goal="The tradeable set is a decision, not a side effect.",
            cost="A new name has no history until bars accumulate. "
            "`indicators.py` reports sma_200 as unavailable rather than "
            "computing a 40-day average with the wrong label, so a newly added "
            "symbol trades on visibly less than the others for months — and "
            "that is the honest version. The dangerous one is nobody noticing.",
            looser=Looser.HIGHER,
            unit=Unit.LIST,
            family="symbols",
            requestable=False,
        ),
    ]


def limits_for(rules: Rules) -> dict[str, LimitFact]:
    """Every limit this agent knows about, keyed by its dotted path.

    Partly derived: the per-class block is generated from whatever classes the
    config actually declares, enabled or not. A disabled class still has limits
    worth arguing about — that is the entire reason crypto is configured while
    switched off, so the numbers are chosen with the market shut rather than at
    whatever hour enabling it becomes urgent.
    """
    facts = (
        _account_facts()
        + _frequency_facts()
        + _stand_down_facts()
        + _margin_and_news_facts()
    )
    for name in rules.instruments:
        facts.extend(_class_facts(name))
    return {fact.key: fact for fact in facts}


def current_value(rules: Rules, key: str) -> float | int | None:
    """What the loaded rules say this key is. `None` for an absent override.

    `None` is not zero and is not "no limit". On a per-class field it means the
    class has no opinion and inherits the portfolio value, which is why
    `effective_value` exists beside this rather than this returning a default.
    """
    node: Any = rules
    for part in key.split("."):
        if isinstance(node, dict):
            if part not in node:
                return None
            node = node[part]
            continue
        if not hasattr(node, part):
            return None
        node = getattr(node, part)
    if isinstance(node, bool) or not isinstance(node, int | float):
        return None
    return node


#: Which portfolio limit a per-class field falls back to when it says nothing.
_CLASS_FALLBACK: dict[str, str] = {
    "max_risk_per_trade_pct": "account.max_risk_per_trade_pct",
    "max_position_pct": "account.max_position_pct",
    "max_concurrent_positions": "account.max_concurrent_positions",
    # Deliberately absent: `max_class_total_risk_pct`. A class with no class
    # total is bounded by the PORTFOLIO total alone, which is a different
    # statement from inheriting a per-class number, and rendering it as an
    # inherited 2% would claim a per-class cap that does not exist.
}


def effective_value(rules: Rules, key: str) -> tuple[float | int | None, str]:
    """The value actually in force, and where it came from.

    Returns `(value, source)` where source is `"set"`, `"portfolio default"` or
    `"unset"`. The third is a real answer: a class with no `max_class_total_risk_pct`
    is bounded by the portfolio total and by nothing of its own, and reporting
    that as an inherited figure would be a limit nobody could read off the file.
    """
    direct = current_value(rules, key)
    if direct is not None:
        return direct, "set"
    parts = key.split(".")
    if len(parts) == 3 and parts[0] == "instruments":
        fallback = _CLASS_FALLBACK.get(parts[2])
        if fallback:
            return current_value(rules, fallback), "portfolio default"
    return None, "unset"


# ------------------------------------------------------------- the argument


class Stance(StrEnum):
    """What kind of change this is. The asymmetry turns on it."""

    LOOSENING = "loosening"
    TIGHTENING = "tightening"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class Argument:
    """What the agent has to say about one proposed change.

    `consequence` is a list of complete sentences, each carrying its own
    figures, so a renderer or a model can quote them individually without
    re-deriving anything.

    `can_record` and `blocked_reason` are separate from `stance` on purpose. A
    limit this path cannot edit — a list, or an override with no line in the
    file — is still worth explaining; it simply cannot become a diff, and the
    agent says which rather than falling silent.
    """

    key: str
    fact: LimitFact
    current: float | int | None
    current_source: str
    proposed: float | int
    stance: Stance
    consequence: list[str]
    objection: str
    requires_confirmation: bool
    can_record: bool = True
    blocked_reason: str = ""
    equity_usd: float | None = None

    @property
    def ok(self) -> bool:
        return self.stance is not Stance.UNCHANGED


class UnknownLimit(ValueError):
    """A key this agent has no entry for.

    Raised rather than guessed at. An agent that accepted an arbitrary dotted
    path would be an agent that could be asked to edit any line in the file,
    including one nobody wrote a consequence for.
    """


def parse_value(fact: LimitFact, raw: str) -> float | int:
    """The operator's typed value, as the number the field holds.

    Strict, and that is the one place in this module where refusing is right:
    `CLAUDE.md`'s rule is that prose truncates and numbers reject. A value that
    is nearly a number is not a number, and a plausible wrong figure that passes
    validation is the exact failure this repository exists to prevent.
    """
    text = raw.strip().replace(",", "").rstrip("%").strip()
    if not text:
        raise ValueError("no value given")
    try:
        number = float(text)
    except ValueError as exc:
        raise ValueError(f"{raw!r} is not a number") from exc
    if fact.integer:
        if number != int(number):
            raise ValueError(f"{fact.label} is a whole number; {raw!r} is not")
        return int(number)
    return number


def classify(fact: LimitFact, current: float | int | None, proposed: float | int) -> Stance:
    """Loosening, tightening, or neither.

    An absent current value compares against nothing, so it is treated as a
    loosening: setting an override where there was none is a change to what the
    class may do, and the safe reading of an unknown baseline is the one that
    gets argued rather than waved through.
    """
    if current is None:
        return Stance.LOOSENING
    if proposed == current:
        return Stance.UNCHANGED
    if fact.looser is Looser.HIGHER:
        return Stance.LOOSENING if proposed > current else Stance.TIGHTENING
    return Stance.LOOSENING if proposed < current else Stance.TIGHTENING


@dataclass(frozen=True)
class _Ctx:
    rules: Rules
    fact: LimitFact
    current: float | int | None
    proposed: float | int
    stance: Stance
    equity_usd: float | None
    class_name: str = ""


def _usd(value: float) -> str:
    return f"${value:,.2f}"


def _num(value: float | int | None) -> str:
    if value is None:
        return "unset"
    if isinstance(value, int):
        return str(value)
    return f"{value:.1f}" if value == int(value) else f"{value:g}"


def _was(value: float | int | None, suffix: str = "", absent: str = "nothing at all") -> str:
    """The previous value, or a phrase rather than `unset%`.

    An absent override is not a number with a hole in it, and rendering it as
    one produces sentences like "instead of unset%", which reads as a bug in the
    agent at the moment the operator most needs to trust it.
    """
    return absent if value is None else f"{_num(value)}{suffix}"


def _trades(count: int) -> str:
    return f"{count} full-size trade" + ("" if count == 1 else "s")


def _money_line(ctx: _Ctx) -> list[str]:
    """Percentages turned into dollars, or a stated reason they were not.

    Never against an assumed $100,000. A figure that looks like a measurement
    and is not is the thing this repository refuses everywhere else.
    """
    if ctx.fact.unit is not Unit.PCT_OF_EQUITY:
        return []
    if ctx.equity_usd is None:
        return [
            "Equity has not been read on this deck yet, so the dollar figures "
            "are unknown rather than estimated. The percentages above stand "
            "without them."
        ]
    was = _usd(ctx.equity_usd * float(ctx.current) / 100) if ctx.current is not None else "unknown"
    now = _usd(ctx.equity_usd * float(ctx.proposed) / 100)
    return [
        f"At the last equity reading of {_usd(ctx.equity_usd)} that is {now} "
        f"instead of {was}."
    ]


def _fits(total: float, per_trade: float) -> int:
    return int(total // per_trade) if per_trade > 0 else 0


def _per_trade_risk(ctx: _Ctx) -> list[str]:
    total = ctx.rules.account.max_total_risk_pct
    out: list[str] = []
    if ctx.current:
        ratio = float(ctx.proposed) / float(ctx.current)
        out.append(
            f"Every future trade may lose {_num(ctx.proposed)}% of equity instead "
            f"of {_num(ctx.current)}% — {ratio:.2f}x the loss on each one, at the "
            "same win rate."
        )
    out.extend(_money_line(ctx))
    fits_now = _fits(total, float(ctx.proposed))
    out.append(
        f"The portfolio total-risk cap is {total:.2f}%, so {_trades(fits_now)} "
        f"{'fits' if fits_now == 1 else 'fit'} at the new size. The next one is "
        "refused however good it looks."
    )
    if float(ctx.proposed) > total:
        out.append(
            f"It is above max_total_risk_pct ({total:.2f}%), so no trade sized to "
            "it could ever fill — the portfolio total-risk gate refuses that "
            "trade first. Raising this alone does nothing until the total moves too."
        )
    if ctx.stance is Stance.LOOSENING:
        out.append(
            "Size is computed from stop distance, so this does not buy a wider "
            "stop. It buys a bigger position at the same stop."
        )
    if ctx.class_name:
        out.append(
            f"This is {ctx.class_name}'s own limit and it OVERRIDES the portfolio "
            f"value of {ctx.rules.account.max_risk_per_trade_pct:.2f}% for that "
            "class, in either direction. Nothing refuses it."
        )
    return out


def _total_risk(ctx: _Ctx) -> list[str]:
    per_trade = ctx.rules.account.max_risk_per_trade_pct
    out = [
        f"If every open stop filled at once the account loses "
        f"{_num(ctx.proposed)}% instead of {_num(ctx.current)}%."
    ]
    out.extend(_money_line(ctx))
    out.append(
        f"At the {per_trade:.2f}% per-trade cap that is "
        f"{_trades(_fits(float(ctx.proposed), per_trade))} open at once instead "
        f"of {_fits(float(ctx.current or 0), per_trade)}."
    )
    out.append(
        "This is the portfolio ceiling every per-class limit runs into, so it is "
        "also what makes a raised class limit reachable."
    )
    return out


def _class_total_risk(ctx: _Ctx) -> list[str]:
    portfolio = ctx.rules.account.max_total_risk_pct
    out = [
        f"Every open {ctx.class_name} position together may risk "
        f"{_num(ctx.proposed)}% of equity instead of "
        + _was(ctx.current, "%", "having no class total at all — only the "
               "portfolio one")
        + "."
    ]
    out.extend(_money_line(ctx))
    out.append(
        "Measured on planned risk out of the journal, so unrealised profit does "
        "not offset it: a position being up buys no room for a second one, and "
        "at the cap something in the class must be closed before another opens."
    )
    if float(ctx.proposed) > portfolio:
        out.append(
            f"It is above the portfolio max_total_risk_pct ({portfolio:.2f}%), "
            "which still binds first, so the class can never actually reach it."
        )
    return out


def _position_pct(ctx: _Ctx) -> list[str]:
    out = [
        f"One position's market value may reach {_num(ctx.proposed)}% of equity "
        f"instead of {_was(ctx.current, '%')}."
    ]
    out.extend(_money_line(ctx))
    out.append(
        "This is a backstop rather than the binding constraint: a 1%-risk trade "
        "with a 2% stop is already a position worth about 48% of equity, so the "
        "risk caps usually refuse first and moving this changes nothing."
    )
    return out


def _concurrent(ctx: _Ctx) -> list[str]:
    per_trade = ctx.rules.account.max_risk_per_trade_pct
    total = ctx.rules.account.max_total_risk_pct
    n = int(ctx.proposed)
    out = [
        f"Up to {n} position(s) open at once instead of {_num(ctx.current)}.",
        f"At the {per_trade:.2f}% per-trade cap, {n} full-size positions is "
        f"{n * per_trade:.2f}% of equity at risk against a {total:.2f}% total — "
        f"so the total-risk cap binds first at {_fits(total, per_trade)} of them.",
    ]
    if ctx.class_name:
        out.append(
            f"Counted within {ctx.class_name} only. The portfolio cap of "
            f"{ctx.rules.account.max_concurrent_positions} still applies across "
            "every class, and both are checked."
        )
    return out


def _daily_loss(ctx: _Ctx) -> list[str]:
    out = [
        f"The day may be down {_num(ctx.proposed)}% from starting equity instead "
        f"of {_num(ctx.current)}% before new positions stop."
    ]
    out.extend(_money_line(ctx))
    out.append(
        "It is sticky for the session: a recovery within the same day does not "
        "re-enable trading."
    )
    return out


def _equity_floor(ctx: _Ctx) -> list[str]:
    out = [
        f"The bot halts at {_usd(float(ctx.proposed))} instead of "
        f"{_usd(float(ctx.current))}."
        if ctx.current is not None
        else f"The bot halts at {_usd(float(ctx.proposed))}."
    ]
    if ctx.equity_usd is not None and ctx.equity_usd > 0:
        was = (
            (1 - float(ctx.current) / ctx.equity_usd) * 100
            if ctx.current is not None
            else None
        )
        now = (1 - float(ctx.proposed) / ctx.equity_usd) * 100
        out.append(
            f"Against the last equity reading of {_usd(ctx.equity_usd)} that is a "
            f"{now:.1f}% drawdown permitted"
            + (f" instead of {was:.1f}%." if was is not None else ".")
        )
    else:
        out.append(
            "Equity has not been read on this deck yet, so the drawdown that "
            "implies is unknown rather than estimated."
        )
    if ctx.stance is Stance.LOOSENING:
        out.append(
            "This is the stop on the account itself. Lowering it is what turns a "
            "drawdown into a wipe-out, and it is the one limit here that removes "
            "a floor rather than widening a window."
        )
    return out


def _trades_per_day(ctx: _Ctx) -> list[str]:
    week = ctx.rules.frequency.max_trades_per_week
    n = int(ctx.proposed)
    return [
        f"Up to {n} entries a day instead of {_num(ctx.current)}.",
        f"Five trading days at {n} is {n * 5} against a weekly cap of {week}, so "
        + (
            "the weekly cap binds first."
            if n * 5 > week
            else "the weekly cap is not what stops it."
        ),
        "Frequency is a risk parameter here, not a performance one: in Alpha "
        "Arena the model that made 238 trades lost 57% of its stake and the one "
        "that made 38 lost least of the US models, with fees explicitly "
        "dominating P&L.",
    ]


def _trades_per_week(ctx: _Ctx) -> list[str]:
    day = ctx.rules.frequency.max_trades_per_day
    n = int(ctx.proposed)
    return [
        f"Up to {n} entries a week instead of {_num(ctx.current)}.",
        f"At {day} a day that is {n / day:.1f} full days' worth of trading before "
        "the weekly cap is what refuses.",
        "Fees compound quietly. A weekly figure is what catches a run of "
        "ordinary days that add up to a churn nobody chose.",
    ]


def _cooldown(ctx: _Ctx) -> list[str]:
    n = int(ctx.proposed)
    return [
        f"The same symbol may be re-traded after {n}s "
        f"({n / 60:.0f} minutes) instead of {_num(ctx.current)}s.",
        "The cooldown is what stops a flip-flop being executed: closing a name "
        "and re-entering it in the other direction pays fees twice for one "
        "opinion.",
    ]


def _stand_down_trigger(ctx: _Ctx) -> list[str]:
    per_trade = ctx.rules.account.max_risk_per_trade_pct
    n = int(ctx.proposed)
    out = [
        f"{n} qualifying losses in a row before live execution is withheld, "
        f"instead of {_num(ctx.current)}.",
        f"At the {per_trade:.2f}% per-trade cap that is up to {n * per_trade:.2f}% "
        "of equity lost before the breaker trips.",
    ]
    if ctx.equity_usd is not None:
        out.append(
            f"At the last equity reading of {_usd(ctx.equity_usd)} that is "
            f"{_usd(ctx.equity_usd * n * per_trade / 100)}."
        )
    out.append(
        "Paper trading continues during a stand-down and closing a position is "
        "never gated, so what this actually buys back is the money."
    )
    return out


def _scratch(ctx: _Ctx) -> list[str]:
    per_trade = ctx.rules.account.max_risk_per_trade_pct
    out = [
        f"A loss up to {_num(ctx.proposed)}R is treated as a scratch instead of "
        f"{_num(ctx.current)}R, so it neither counts toward the streak nor "
        "resets it.",
        f"One R is the per-trade cap of {per_trade:.2f}% of equity, so that is "
        f"{float(ctx.proposed) * per_trade:.2f}% of equity that does not count "
        "as a loss.",
    ]
    if ctx.equity_usd is not None:
        out.append(
            f"At the last equity reading of {_usd(ctx.equity_usd)} that is "
            f"{_usd(ctx.equity_usd * float(ctx.proposed) * per_trade / 100)} a "
            "trade the streak counter ignores."
        )
    return out


def _stand_down_days(ctx: _Ctx) -> list[str]:
    return [
        f"Live execution is withheld for {int(ctx.proposed)} days instead of "
        f"{_num(ctx.current)}.",
        "Paper trading continues throughout. Only the money stops.",
    ]


def _repeat_window(ctx: _Ctx) -> list[str]:
    return [
        f"A second breach escalates only if it lands within {int(ctx.proposed)} "
        f"days of the last one, instead of {_num(ctx.current)}.",
        "Shortening it makes a pattern read as two unrelated incidents.",
    ]


def _buying_power(ctx: _Ctx) -> list[str]:
    return [
        f"One order may consume {_num(ctx.proposed)}% of available buying power "
        f"instead of {_num(ctx.current)}%.",
        "Alpaca rejects an order that would create an Intraday Margin Deficit in "
        "real time, and repeated non-compliance inside five business days costs "
        "a 90-day account restriction. Finding that edge is far more expensive "
        "than skipping a trade.",
    ]


def _gross_notional(ctx: _Ctx) -> list[str]:
    return [
        f"Gross exposure may reach {_num(ctx.proposed)}% of equity instead of "
        f"{_was(ctx.current, '%')}.",
        f"Reg T permits 200% overnight, so this leaves "
        f"{200 - float(ctx.proposed):.0f} points of headroom instead of "
        f"{200 - float(ctx.current or 0):.0f}.",
    ]


def _news_blackout(ctx: _Ctx) -> list[str]:
    out = [
        f"{int(ctx.proposed)} minutes instead of {_num(ctx.current)} around a "
        "high-impact announcement.",
        "An earnings print moves a price further than the stop distance that "
        "sized the position, so the trade that was 1% of equity is not.",
    ]
    if int(ctx.proposed) == 0:
        out.append(
            "Zero switches the rule off entirely. Finnhub still fetches the "
            "calendar every cycle and nothing would use it, which reads on the "
            "Settings page exactly like a feed that is working."
        )
    return out


def _expiry_entry(ctx: _Ctx) -> list[str]:
    return [
        f"An option position may be opened {_num(ctx.proposed)} days from expiry "
        f"instead of {_num(ctx.current)}.",
        "Alpaca auto-exercises anything in the money by $0.01 at 6pm ET on "
        "expiry day and liquidates an in-the-money position the account cannot "
        'fund inside the final hour. "Do Not Exercise" cannot be filed through '
        "the API, so closing early is the only programmatic way to choose a "
        "different outcome — and this margin is the room to do it in.",
        "The bot does not propose option trades today, so this is protective "
        "rather than live.",
    ]


def _expiry_warn(ctx: _Ctx) -> list[str]:
    return [
        f"Expiry warnings start {_num(ctx.proposed)} days out instead of "
        f"{_num(ctx.current)}.",
        "An expiry resolves itself whether anyone is watching or not, so the "
        "notice period is the whole mechanism.",
    ]


def _symbols(ctx: _Ctx) -> list[str]:
    return [
        "Every symbol here is a permission: the gate refuses anything not on "
        "the list, so each name added is a new instrument the bot may open a "
        "position in. The watchlist is the display-only list; this is not it.",
        "A newly added name has no history until bars accumulate. "
        "indicators.py reports sma_200 as unavailable rather than computing a "
        "40-day average with the wrong label, so the symbol trades on visibly "
        "less than the others for months.",
    ]


_CONSEQUENCES: dict[str, Callable[[_Ctx], list[str]]] = {
    "per_trade_risk": _per_trade_risk,
    "total_risk": _total_risk,
    "class_total_risk": _class_total_risk,
    "position_pct": _position_pct,
    "concurrent": _concurrent,
    "daily_loss": _daily_loss,
    "equity_floor": _equity_floor,
    "trades_per_day": _trades_per_day,
    "trades_per_week": _trades_per_week,
    "cooldown": _cooldown,
    "stand_down_trigger": _stand_down_trigger,
    "scratch": _scratch,
    "stand_down_days": _stand_down_days,
    "repeat_window": _repeat_window,
    "buying_power": _buying_power,
    "gross_notional": _gross_notional,
    "news_blackout": _news_blackout,
    "expiry_entry": _expiry_entry,
    "expiry_warn": _expiry_warn,
    "symbols": _symbols,
}

#: Said on every loosening, whatever the limit.
#:
#: Two claims, and both are true of every change here. The gate applies the file
#: exactly and has no way to tell a considered change from one made on a bad
#: afternoon; and this path records rather than applies, so nothing has moved
#: yet. Stated in the consequence list rather than left to the character file,
#: because the soul is read from disk at call time and could have been edited.
LOOSENING_TAIL = (
    "The risk gate is deterministic and will apply this exactly. It cannot tell "
    "a considered change from one made during a losing run — that judgement is "
    "yours, which is why you are being asked for it before anything is recorded."
)

#: Said on every tightening. Short on purpose: choosing to lose less should not
#: cost the operator a lecture.
TIGHTENING_TAIL = (
    "Nothing is being argued here. This is recorded as asked, and still takes a "
    "person running the apply command as root before the file moves."
)


def argue(
    rules: Rules,
    key: str,
    proposed_raw: str | float | int,
    *,
    equity_usd: float | None = None,
    rules_path: Path | None = None,
) -> Argument:
    """The whole argument for one proposed change, computed and never guessed.

    Raises `UnknownLimit` for a key with no entry, and `ValueError` for a value
    that is not the number the field holds. Everything else is an `Argument`,
    including the cases this path cannot record: those carry `can_record=False`
    and a reason, because an operator told "no" with no explanation learns
    nothing.
    """
    facts = limits_for(rules)
    fact = facts.get(key)
    if fact is None:
        raise UnknownLimit(
            f"{key} is not a limit this agent knows about. It argues only about "
            "the entries in `limits_for`, each of which has a stated "
            "consequence; an arbitrary dotted path would be an arbitrary line "
            "in the file with nothing to say about it."
        )
    if fact.unit is Unit.LIST:
        # A list has no "new value" to argue about, and coercing one to a number
        # would produce an argument about a figure nobody meant. The reasoning
        # for the limit still reaches the operator — it is on the Settings page
        # and in `render_briefing` — which is the part worth having here.
        raise ValueError(
            f"{fact.label} is a list rather than a single value, so there is no "
            "number to argue about. What it costs to widen it is on the "
            "Settings page beside the limit; changing it is a commit."
        )

    proposed = (
        parse_value(fact, proposed_raw)
        if isinstance(proposed_raw, str)
        else (int(proposed_raw) if fact.integer else float(proposed_raw))
    )
    current, source = effective_value(rules, key)
    stance = classify(fact, current, proposed)

    parts = key.split(".")
    class_name = parts[1] if len(parts) == 3 and parts[0] == "instruments" else ""
    ctx = _Ctx(
        rules=rules,
        fact=fact,
        current=current,
        proposed=proposed,
        stance=stance,
        equity_usd=equity_usd,
        class_name=class_name,
    )

    # The headline quotes the value in the notation the FILE uses, not a
    # prettier one, so what is read here is what the diff will show.
    was = format_value(fact, current) if current is not None else "nothing"
    origin = {
        "set": ".",
        "portfolio default": (
            f" — {was} is the portfolio default and this class sets nothing today."
        ),
        "unset": " — nothing is set for this key today.",
    }[source]

    lines: list[str] = []
    if stance is Stance.UNCHANGED:
        lines.append(
            f"{fact.label} is already {was}. Nothing to argue about and nothing "
            "to record."
        )
    else:
        lines.append(
            f"{fact.label} moves from {was} to {format_value(fact, proposed)}{origin}"
        )
        lines.extend(_CONSEQUENCES[fact.family](ctx))
        lines.extend(_stale_comment_note(fact, rules_path))
        lines.append(LOOSENING_TAIL if stance is Stance.LOOSENING else TIGHTENING_TAIL)

    can_record, blocked = _recordability(rules, fact, key, rules_path)

    objection = (
        f"{fact.cost} What the current value serves: {fact.goal.rstrip('.')}."
        if stance is Stance.LOOSENING
        else ""
    )

    return Argument(
        key=key,
        fact=fact,
        current=current,
        current_source=source,
        proposed=proposed,
        stance=stance,
        consequence=lines,
        objection=objection,
        requires_confirmation=stance is Stance.LOOSENING,
        can_record=can_record and stance is not Stance.UNCHANGED,
        blocked_reason=blocked,
        equity_usd=equity_usd,
    )


def _stale_comment_note(fact: LimitFact, rules_path: Path | None) -> list[str]:
    """Say when the diff will leave a comment behind that no longer describes
    the value beside it.

    `min_seconds_between_trades_per_symbol: 1800   # 30 minutes` is the case
    that made this necessary. A one-line diff cannot know what the prose meant,
    and rewriting it would be guessing; leaving it silently is worse, because a
    comment that contradicts its own value is read as the truth by the next
    person along. `config/rules.yaml` is mostly reasoning — that is exactly why
    a stale line in it is expensive.
    """
    path = rules_path or DEFAULT_RULES_PATH
    try:
        located = locate(path.read_text(encoding="utf-8"), fact.key)
    except OSError:
        return []
    if located is None or not located.comment.strip():
        return []
    return [
        f"The line carries a trailing comment — `{located.comment.strip()}` — "
        "which the diff does not touch and which will describe the old value "
        "afterwards. Fix it in the same commit."
    ]


def _recordability(
    rules: Rules, fact: LimitFact, key: str, rules_path: Path | None
) -> tuple[bool, str]:
    """Can this change become a diff at all, and if not, exactly why.

    Three answers, and they are different problems wearing the same "no":
    a list is not a scalar this path can rewrite; an override with no line in
    the file would be an addition rather than an edit; and an unreadable file is
    an operational fault rather than a decision.
    """
    if not fact.requestable:
        return False, (
            f"{fact.label} is a list, not a single value, so this path cannot "
            "produce a safe one-line diff for it. Edit it in a commit — the "
            "consequence above is the part worth reading first."
        )
    path = rules_path or DEFAULT_RULES_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"{path} could not be read: {exc}"
    if locate(text, key) is None:
        if current_value(rules, key) is None:
            return False, (
                f"{key} has no line in {path.name} — the class inherits this "
                "rather than setting it. Adding a setting is a different act "
                "from changing one, and it belongs in a commit where the new "
                "line can carry its own comment."
            )
        return False, (
            f"{key} could not be located unambiguously in {path.name}, so no "
            "diff can be produced without guessing which line to edit."
        )
    return True, ""


# ---------------------------------------------------------- the file itself


@dataclass(frozen=True)
class Located:
    """One scalar assignment in the YAML, found by path rather than by regex
    over the whole file."""

    line_no: int
    indent: str
    key: str
    value_text: str
    comment: str

    def rewritten(self, value_text: str) -> str:
        return f"{self.indent}{self.key}: {value_text}{self.comment}"


_ASSIGNMENT = re.compile(
    r"^(?P<indent>[ ]*)(?P<key>[A-Za-z_][A-Za-z0-9_]*):(?P<rest>.*)$"
)
_VALUE = re.compile(r"^[ ]*(?P<value>[^#]*?)[ ]*(?P<comment>#.*)?$")


def locate(text: str, key: str) -> Located | None:
    """Find the one line holding `key`, or `None`.

    Indentation-tracked rather than pattern-matched, because
    `max_risk_per_trade_pct` appears four times in `config/rules.yaml` — once
    under `account:` and once under each instrument class — and a regex for the
    name alone would edit whichever came first.

    **Ambiguity is `None`, never a best guess.** Two lines resolving to the same
    path means the file is not what this thinks it is, and the honest answer to
    "which line did you mean" is that it does not know. Same rule as
    `Rules.true_class_key` returning `""` when two blocks claim one symbol.

    Comment lines cannot match: the pattern requires a letter or underscore
    immediately after the indent, and every commented-out template line in
    `rules.yaml` starts with `#`. That is what keeps the worked Globex block
    from being mistaken for configuration.
    """
    stack: list[tuple[int, str]] = []
    found: list[Located] = []
    for index, line in enumerate(text.splitlines()):
        match = _ASSIGNMENT.match(line)
        if match is None:
            continue
        indent = len(match.group("indent"))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        rest = _VALUE.match(match.group("rest"))
        value = (rest.group("value") if rest else "").strip()
        name = match.group("key")
        path = ".".join([k for _, k in stack] + [name])
        if not value:
            stack.append((indent, name))
            continue
        if path == key:
            comment_text = (rest.group("comment") if rest else None) or ""
            raw_rest = match.group("rest")
            spacing = (
                raw_rest[len(raw_rest.rstrip()) :] if not comment_text else ""
            )
            gap = ""
            if comment_text:
                before = raw_rest.split(comment_text, 1)[0]
                gap = before[len(before.rstrip()) :]
            found.append(
                Located(
                    line_no=index,
                    indent=match.group("indent"),
                    key=name,
                    value_text=value,
                    comment=f"{gap}{comment_text}" if comment_text else spacing,
                )
            )
    return found[0] if len(found) == 1 else None


def format_value(fact: LimitFact, value: float | int) -> str:
    """The new scalar as YAML, shaped like the file it is going into.

    `1.0` rather than `1` for a float field: the surrounding values are written
    that way, and a diff that also changed the notation would be a second change
    riding along with the first.
    """
    if fact.integer:
        return str(int(value))
    number = float(value)
    return f"{number:.1f}" if number == int(number) else f"{number:g}"


def render_diff(text: str, fact: LimitFact, value: float | int, *, name: str) -> str:
    """A unified diff of exactly one line, ready to read and to apply.

    Produced from the file on disk rather than from a re-serialised `Rules`.
    Dumping the parsed model back to YAML would round-trip away every comment in
    `config/rules.yaml`, and that file is mostly reasoning — the numbers are the
    small part.
    """
    located = locate(text, fact.key)
    if located is None:
        return ""
    lines = text.splitlines(keepends=True)
    ending = "\n" if lines[located.line_no].endswith("\n") else ""
    updated = list(lines)
    updated[located.line_no] = located.rewritten(format_value(fact, value)) + ending
    return "".join(
        difflib.unified_diff(lines, updated, fromfile=f"a/{name}", tofile=f"b/{name}")
    )


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------- the request


@dataclass
class ChangeRequest:
    """One argued change, recorded whether the agent won it or lost it.

    Everything needed to apply it later and everything needed to understand it
    years later, in one row. The two that are easy to think optional and are
    not:

    - `objection` — the agent's stated case against, preserved even when the
      operator overruled it. An argument the operator won is a fact worth
      keeping.
    - `rules_digest` — what `config/rules.yaml` hashed to when this was argued.
      An argument made against one version of the file is not an argument about
      a different one, so applying against a moved file is refused rather than
      merged.
    """

    key: str
    old_value: str
    new_value: str
    stance: str
    reason: str
    objection: str
    consequence: list[str] = field(default_factory=list)
    transcript: list[dict[str, str]] = field(default_factory=list)
    rules_digest: str = ""
    rules_path: str = ""
    diff: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    status: str = "pending"
    applied_at: datetime | None = None
    id: int | None = None

    @property
    def apply_command(self) -> str:
        return f"sudo electrum-bot settings-apply {self.id if self.id else '<id>'}"


def commit_message(request: ChangeRequest) -> str:
    """The commit this change wants, in the repository's own voice.

    `CLAUDE.md`: limits change deliberately, in their own commit, with a reason.
    So the message carries the reason the operator gave AND the objection the
    agent made, because a commit that recorded only the winning side would make
    the argument look like it never happened.
    """
    verb = "loosen" if request.stance == Stance.LOOSENING else (
        "tighten" if request.stance == Stance.TIGHTENING else "set"
    )
    lines = [
        f"config: {verb} {request.key} from {request.old_value} to {request.new_value}",
        "",
        request.reason.strip() or "No reason was recorded with the request.",
        "",
    ]
    if request.consequence:
        lines.append("Stated at the time:")
        lines.extend(f"  - {line}" for line in request.consequence)
        lines.append("")
    if request.objection.strip():
        lines.append("The Armorer objected, and the operator overruled it:")
        lines.append(f"  {request.objection.strip()}")
        lines.append("")
    stamp = request.created_at.strftime("%Y-%m-%d %H:%M UTC")
    lines.append(
        f"Argued and recorded {stamp} as settings request "
        f"{request.id if request.id else '<id>'}."
    )
    return "\n".join(lines)


SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  key           TEXT NOT NULL,
  old_value     TEXT NOT NULL,
  new_value     TEXT NOT NULL,
  stance        TEXT NOT NULL,
  reason        TEXT NOT NULL,
  objection     TEXT NOT NULL,
  consequence   TEXT NOT NULL,
  transcript    TEXT NOT NULL,
  rules_digest  TEXT NOT NULL,
  rules_path    TEXT NOT NULL,
  diff          TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  status        TEXT NOT NULL,
  applied_at    TEXT
);
"""


class SettingsRequestStore:
    """SQLite store for argued changes. Its own file, never the journal.

    **Any future change to `SCHEMA` needs a migration beside it and a test that
    starts from the old shape.** `CREATE TABLE IF NOT EXISTS` is a no-op on a
    table that already exists, so editing the block above changes what a fresh
    database gets and nothing whatever about the one on the droplet. Every test
    here builds its store in a `tmp_path`, which makes the suite structurally
    blind to it — that is how a live order once reached Alpaca with a journal
    that could not store the row. The suite will not warn you.
    """

    def __init__(self, path: Path = DEFAULT_REQUESTS_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def record(self, request: ChangeRequest) -> ChangeRequest:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO requests (key, old_value, new_value, stance, reason,"
                " objection, consequence, transcript, rules_digest, rules_path,"
                " diff, created_at, status, applied_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    request.key,
                    request.old_value,
                    request.new_value,
                    str(request.stance),
                    request.reason,
                    request.objection,
                    json.dumps(request.consequence),
                    json.dumps(request.transcript),
                    request.rules_digest,
                    request.rules_path,
                    request.diff,
                    request.created_at.isoformat(),
                    request.status,
                    request.applied_at.isoformat() if request.applied_at else None,
                ),
            )
            request.id = int(cursor.lastrowid or 0)
        log.info(
            "settings_change_requested",
            request_id=request.id,
            key=request.key,
            old=request.old_value,
            new=request.new_value,
            stance=request.stance,
            objected=bool(request.objection.strip()),
        )
        return request

    def get(self, request_id: int) -> ChangeRequest | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE id=?", (request_id,)
            ).fetchone()
        return self._to_request(row) if row else None

    def recent(self, limit: int = 25, *, status: str = "") -> list[ChangeRequest]:
        sql = "SELECT * FROM requests"
        params: list[object] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._to_request(row) for row in rows]

    def mark(self, request_id: int, status: str, *, at: datetime | None = None) -> None:
        stamp = (at or datetime.now(UTC)).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE requests SET status=?, applied_at=? WHERE id=?",
                (status, stamp if status == "applied" else None, request_id),
            )

    @staticmethod
    def _to_request(row: sqlite3.Row) -> ChangeRequest:
        return ChangeRequest(
            id=int(row["id"]),
            key=str(row["key"]),
            old_value=str(row["old_value"]),
            new_value=str(row["new_value"]),
            stance=str(row["stance"]),
            reason=str(row["reason"]),
            objection=str(row["objection"]),
            consequence=list(json.loads(row["consequence"] or "[]")),
            transcript=list(json.loads(row["transcript"] or "[]")),
            rules_digest=str(row["rules_digest"]),
            rules_path=str(row["rules_path"]),
            diff=str(row["diff"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            status=str(row["status"]),
            applied_at=(
                datetime.fromisoformat(str(row["applied_at"]))
                if row["applied_at"]
                else None
            ),
        )


def build_request(
    argument: Argument,
    *,
    reason: str,
    transcript: list[dict[str, str]] | None = None,
    rules_path: Path | None = None,
) -> ChangeRequest:
    """Turn a settled argument into the row that will be stored.

    The diff and the digest are both taken from the file as it is RIGHT NOW,
    which is what makes the staleness check at apply time meaningful.
    """
    path = rules_path or DEFAULT_RULES_PATH
    text = path.read_text(encoding="utf-8")
    # The old value is the EXACT text on the line, not a re-rendering of the
    # loaded number. `min_equity_floor_usd: 90000` parses to 90000.0, and a
    # request recorded as "90000.0" would never match the file again — so
    # `apply_request`'s "is this still the value that was argued about" check
    # would refuse every request it was built to let through.
    located = locate(text, argument.key)
    return ChangeRequest(
        key=argument.key,
        old_value=(
            located.value_text
            if located is not None
            else (
                format_value(argument.fact, argument.current)
                if argument.current is not None
                else ""
            )
        ),
        new_value=format_value(argument.fact, argument.proposed),
        stance=str(argument.stance),
        reason=reason.strip(),
        objection=argument.objection,
        consequence=list(argument.consequence),
        transcript=list(transcript or []),
        rules_digest=digest(text),
        rules_path=str(path),
        diff=render_diff(text, argument.fact, argument.proposed, name=path.name),
    )


# --------------------------------------------------------------- applying it


@dataclass(frozen=True)
class ApplyResult:
    ok: bool
    detail: str
    diff: str = ""


def apply_request(
    store: SettingsRequestStore,
    request_id: int,
    *,
    rules_path: Path | None = None,
    dry_run: bool = False,
) -> ApplyResult:
    """The privileged half. Run as root, by a person, from a shell.

    **Nothing enforces "root" in here, and that is deliberate.** `config/` is
    owned by root on the droplet, so an unprivileged run fails on the write with
    a permission error from the operating system — which is a guarantee, where a
    `os.geteuid()` check in Python would be a comment with an if-statement round
    it. Unix permissions do not fail quietly; a check somebody can delete does.

    Four refusals, each for a different way this could be wrong:

    - the request is not pending, so it has already been decided
    - the file has changed since the argument was made, so the argument was
      about a different file
    - the line no longer holds the value the request recorded, so the edit would
      be overwriting something nobody argued about
    - the result does not load through `Rules`, so the edit would leave the bot
      unable to start
    """
    request = store.get(request_id)
    if request is None:
        return ApplyResult(False, f"No settings request {request_id}.")
    if request.status != "pending":
        return ApplyResult(
            False,
            f"Request {request_id} is {request.status}, not pending. "
            "A decided request is history rather than a queue entry.",
        )

    path = rules_path or Path(request.rules_path or DEFAULT_RULES_PATH)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return ApplyResult(False, f"{path} could not be read: {exc}")

    if request.rules_digest and digest(text) != request.rules_digest:
        return ApplyResult(
            False,
            f"{path} has changed since request {request_id} was argued. The "
            "consequence stated at the time was computed against a different "
            "file, so it is not evidence about this one. Argue it again.",
        )

    rules = Rules.load(path)
    fact = limits_for(rules).get(request.key)
    if fact is None:
        return ApplyResult(False, f"{request.key} is no longer a limit this agent knows.")

    located = locate(text, request.key)
    if located is None:
        return ApplyResult(
            False,
            f"{request.key} could not be located unambiguously in {path.name}.",
        )
    if located.value_text != request.old_value:
        return ApplyResult(
            False,
            f"{request.key} is {located.value_text} in the file and the request "
            f"was argued against {request.old_value}. Refusing to overwrite a "
            "value nobody discussed.",
        )

    lines = text.splitlines(keepends=True)
    ending = "\n" if lines[located.line_no].endswith("\n") else ""
    lines[located.line_no] = located.rewritten(request.new_value) + ending
    updated = "".join(lines)
    diff = "".join(
        difflib.unified_diff(
            text.splitlines(keepends=True),
            lines,
            fromfile=f"a/{path.name}",
            tofile=f"b/{path.name}",
        )
    )

    # Validated through the real loader on a real file, not by re-parsing the
    # string. `Rules.load` is what the bot itself runs at startup, so a config
    # that passes here is one the bot can start on — anything less is a second
    # implementation of the same check waiting to disagree with the first.
    staging = path.with_name(path.name + ".settings-apply.tmp")
    try:
        staging.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return ApplyResult(
            False,
            f"Could not write beside {path}: {exc}. config/ is root-owned on the "
            "box on purpose — run this as root.",
            diff,
        )
    try:
        Rules.load(staging)
    # Broad on purpose: pydantic raises `ValidationError`, PyYAML raises its own
    # errors, and a file that will not load is the same answer whichever it was.
    # A narrow catch here would propagate an unanticipated one out of a command
    # that has already staged a temporary file beside `rules.yaml`.
    except Exception as exc:
        staging.unlink(missing_ok=True)
        return ApplyResult(
            False,
            f"The edited file does not load: {exc}. Nothing was written.",
            diff,
        )

    if dry_run:
        staging.unlink(missing_ok=True)
        return ApplyResult(
            True,
            f"Dry run: request {request_id} would apply cleanly and the result "
            "loads. Nothing was written.",
            diff,
        )

    # `os.replace` rather than a write in place: either the new file is there
    # whole or the old one is untouched. A half-written rules file is a bot that
    # will not start, and it would be discovered at the next restart rather than
    # now.
    os.replace(staging, path)
    store.mark(request_id, "applied")
    log.info(
        "settings_change_applied",
        request_id=request_id,
        key=request.key,
        old=request.old_value,
        new=request.new_value,
        path=str(path),
    )
    return ApplyResult(
        True,
        f"Applied request {request_id}: {request.key} {request.old_value} -> "
        f"{request.new_value}. Commit it with the message the request carries.",
        diff,
    )


# ---------------------------------------------------- what the model is told


def render_briefing(rules: Rules, *, equity_usd: float | None = None) -> str:
    """The limit table, as text handed to the agent before it speaks.

    The model must not derive any of this. Every current value is read off the
    loaded rules and every consequence sentence is computed here, so the agent's
    job is to argue with figures it was given rather than to work any out. Same
    rule as `indicators.py`: the model reads figures, it does not compute them.

    Equity is stated or its absence is stated. There is no third option and no
    assumed $100,000.
    """
    lines = [
        "THE LIMITS YOU MAY BE ASKED ABOUT.",
        "",
        "Every figure below is read from config/rules.yaml as currently loaded. "
        "Quote them exactly. Do not compute a consequence yourself — ask for the "
        "arithmetic, which is produced by src/bot/settings_agent.py and shown to "
        "the operator beside your reply.",
        "",
    ]
    lines.append(
        f"Last equity reading: {_usd(equity_usd)}."
        if equity_usd is not None
        else "Equity has not been read on this deck. Dollar figures are unknown, "
        "not estimated."
    )
    lines.append("")
    for key, fact in limits_for(rules).items():
        value, source = effective_value(rules, key)
        shown = _num(value) if fact.unit is not Unit.LIST else "(a list)"
        note = "" if source == "set" else f" [{source}]"
        lines.append(f"- {key} = {shown}{note}")
        lines.append(f"    what: {fact.what}")
        lines.append(f"    why: {fact.why}")
        lines.append(f"    goal: {fact.goal}")
        lines.append(f"    loosening it costs: {fact.cost}")
        lines.append(
            f"    looser means {fact.looser.value}"
            + ("" if fact.requestable else "; not editable through this path")
        )
    lines.extend(
        [
            "",
            "YOU CANNOT CHANGE ANY OF THESE. config/ is root-owned so the service "
            "account cannot edit its own limits. What the surface produces is a "
            "change request the operator applies as root with "
            "`electrum-bot settings-apply <id>`. Say recorded, never changed.",
        ]
    )
    return "\n".join(lines)
