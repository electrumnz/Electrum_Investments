"""The dreamer: second-order investment hypotheses, thought about in public.

The decision loop watches six symbols and four strategies and is deliberately
narrow. This is the other thing: an agent that looks two hops away from what
anyone asked about, records the chain link by link, attacks it, and reaches a
verdict that is never a trade.

The worked example the module is shaped around, and the shape every good dream
has:

    Cicadas emerge on fixed multi-year broods. In a year when two of the three
    largest sesame-producing countries fall inside overlapping brood ranges,
    their harvests are damaged in the same season. Indonesia has no periodical
    cicadas, so Indonesian sesame becomes the marginal supplier into a shortage
    it did not experience.

What makes that worth recording is not the conclusion. It is that every link is
a separate physical claim which can be checked on its own, and any one of them
breaking kills the whole chain. That is why `Hop` is a first-class type here and
why `checked` and `source` sit on it: a chain whose hops cannot be attacked
individually is a story rather than a hypothesis.

## Why this cannot become an order path

`RiskGate.evaluate` vets proposals. The dreamer never makes one, and the
guarantee is structural rather than a matter of discipline:

**`Dream` does not carry the fields an order needs.** No quantity, no entry
price, no stop-loss price, no side. `OrderProposal` requires all of them and
`stop_loss_price` is mandatory and validated, so there is no code path that
turns one of these into the other without a person writing new fields and new
validation. `tests/test_dreaming.py` asserts that overlap stays empty.

That is on purpose and it is the whole safety argument. A speculative-idea
generator wired into an execution path is the Alpha Arena failure with extra
steps: six frontier models traded real money on confident prose and every US
flagship finished underwater. Confidence is exactly what this module produces
most of, so it is kept the furthest from the account of anything in the repo.

`instruments` names what a dream is *about* — a commodity, a region, a listed
proxy. It is a subject, not an instruction, and it is deliberately free text so
that it cannot be mistaken for a symbol the bot trades.

`symbols` is the other half of that pair and the two must never be collapsed
into one field. `instruments` says "Indonesian sesame"; `symbols` says
`["SPY"]`. The first is prose and can never be read as a ticker, which is what
makes it safe to let a model write anything it likes there. The second is the
structured claim that a finished dream is *about something the bot can trade*,
and it is the only thing in this module that can become a permission. Merging
them would mean either the subject line starts being parsed as tickers, or the
permission starts being free text — both of which are the same failure from
opposite ends.

**A permission is not an order, and that is why `symbols` is safe to add.**
A granted symbol widens what the trading agent may *consider*. Everything that
decides whether a considered trade actually happens is untouched:
`RiskGate.evaluate` still runs on every order path, the four operator rules
still hold, `stop_loss_price` is still required and still validated, and the
size still follows from the stop. `Dream` gains no quantity, no entry, no stop
and no side, so there is still no function that turns one into an
`OrderProposal` without somebody writing new fields and new validation by hand.
`tests/test_dreaming.py` pins that.

## The vaults, and why a dream moves rather than gets a flag

A dream has a life beyond "what stage is the thinking at". `DreamStage` answers
how far the reasoning has got; `Vault` answers who is holding it and who may
see it, and those are different questions that would fight if they shared a
field.

    WORKBENCH   being dreamt about now. The default, and where everything starts.
    PROPHECY    fleshed out, with conditions attached, being tracked for whether
                the world does what it was said to do.
    VAULT       the dream vault. Conditions met, or offered straight there by the
                dreamer. **The only vault the trading agent can see**, and where
                the two agents talk.
    ADOPTED     the trading agent has taken it. The dreamer keeps a WISP.
    ARCHIVE     retired, kept for the record.

**Who may move what is enforced in `DreamStore.move`, not by convention.** The
dreamer moves freely between WORKBENCH, PROPHECY, VAULT and ARCHIVE, and may
delete from those. The trading agent may do exactly two things: adopt out of
VAULT, and hand an adopted dream back to VAULT **with a stated reason**. It may
never delete and it may never move a dream anywhere else.

That asymmetry is the same shape as every other boundary here. Prose asking an
agent to stay in its lane is what `souls/grogu.md` already does and is worth
having; a store that refuses the write is what makes it true. The trading agent
is the one with a route to the broker, so it is the one that gets the smaller
set of verbs.

**A refusal returns a result; it never raises.** A full vault, a wrong actor or
a missing reason are ordinary answers to an ordinary question, and an exception
out of here would take down the page an operator opened to find out what is in
the vaults. `MoveResult` collects every reason at once rather than
short-circuiting, exactly as `RiskGate` does, so a caller is told everything
wrong with a move instead of the first thing.

## The wisp, and what it is not

Adoption leaves the dreamer a `wisp`: a trace, in the Pensieve sense — the
memory has been taken out and what is left is the shape of it.

**Nothing is deleted to make that true.** The chain, the thoughts, the
conditions and the messages all stay on the row, because destroying the
reasoning behind a live trading permission to honour a metaphor would be the
worst trade in this file. The wisp is what the *dreamer* is handed back when it
asks what it is working on, so an adopted dream stops occupying a slot in its
head without the record going anywhere. Same distinction as `metrics.py`: the
figures exist, they just do not reach the agent that must not have them.

## Expiry is measured from `vault_entered_at`, never from `created_at`

A dream pulled back out of the vault for another pass would otherwise inherit a
clock that is nearly dead and expire in the middle of the rework — punishing
exactly the behaviour the whole arrangement is meant to encourage. Entering a
vault restarts that vault's clock, and moving is the only thing that sets it.

A no-op move is refused rather than treated as a success for the same reason
from the other side: `move(d, VAULT)` on a dream already in VAULT must not
quietly reset its expiry, or a caller with a refresh loop would keep everything
alive forever without ever saying so.

## This is not Anthropic's "Dreaming", and the difference matters

Anthropic shipped a feature called Dreaming as a research preview in May 2026:
a memory *consolidation* pass that reads an agent's own session transcripts and
folds corrections, preferences and recurring patterns back into its memory files
between sessions. Same word, different machine. That one is about an agent
learning from its own mistakes; this one is about generating second-order
hypotheses.

The overlap is real and it is the dangerous part, so it is worth being explicit.
"Learn from what happened last time" applied to a trading account means learning
from profit and loss, and this repository forbids that deliberately: forty
trades is noise, a model shown three losses will confidently change approach,
and that is the Alpha Arena failure exactly. `souls/grogu.md` says it in the
character file and `DreamLedger` is the shape learning is allowed to take here.

**Consolidation over reasoning quality, never over returns.** `DreamLedger`
counts how often the dreamer sources a hop, how often it drops a chain, how
often it names a trigger. Those are facts about the thinking, they are true
regardless of how any trade went, and there is no sample of outcomes to overfit
to. They reach the operator on the Dreaming page and stop there, exactly as
`metrics.py` reaches the Analytics page and stops there. The loop that closes is
journal to figures to a human to a commit, at human speed, with an audit trail.

## Why the store is its own file

`data/dreams.db`, not `data/journal.db`. The journal is the only irreplaceable
file on the box and `deploy/backup-journal.sh` snapshots it hourly; losing every
dream ever recorded costs some speculative notes and nothing else. Keeping them
apart also means no query can accidentally read a hypothesis as a position.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import structlog

from .models import AssessmentTrigger, TriggerField, TriggerOp

log = structlog.get_logger(__name__)

DEFAULT_DREAMS_PATH = Path("data/dreams.db")

# Free-form prose is trimmed rather than rejected, exactly as `claude.propose`
# treats a rationale: nothing downstream parses it, so losing the tail of a
# sentence costs a reader some context where a rejected response would cost the
# whole dream. The same leniency is NOT extended to anything structural.
TEXT_MAX_CHARS = 900


class DreamStage(StrEnum):
    """Where a mini-project has got to.

    The order matters: `iterate` can loop back to `explore`, and only `verdict`
    is terminal. A dream that never leaves `explore` is a dream that is still
    running, which is a normal state and is rendered as one.
    """

    SEED = "seed"
    EXPLORE = "explore"
    ITERATE = "iterate"
    VERDICT = "verdict"


class DreamVerdict(StrEnum):
    """How a mini-project ended. None of these is an instruction.

    `DROP` is the one worth defending. A dream that fell apart on inspection is
    a good outcome and recording *which hop broke* is what stops the same idea
    arriving again next month wearing a different headline.
    """

    KEEP = "keep"
    PARK = "park"
    DROP = "drop"


class Verification(StrEnum):
    """How much of a chain rests on something somebody actually looked at.

    Deliberately not a percentage and not a five-point scale. Confidence in a
    causal chain is the minimum across its links, and a number invites an
    average. This is a statement about evidence, which can be audited, rather
    than about belief, which cannot.
    """

    UNVERIFIED = "unverified"  # at least one hop is an assumption
    PARTIAL = "partial"  # some hops sourced, the gaps are named
    SOURCED = "sourced"  # every hop names where it came from


class Vault(StrEnum):
    """Where a dream is being held, and therefore who can see it.

    Deliberately separate from `DreamStage`. The stage says how far the
    *thinking* has got — a chain can be at `iterate` in any vault — and this
    says who is holding it. Folding the two into one field would mean either a
    stage nobody could reach without moving vaults or a vault that moved every
    time a thought was added, and the second is worse: `vault_entered_at` is the
    expiry clock, so a vault that changed on every thought would be a dream that
    never aged.
    """

    WORKBENCH = "workbench"
    PROPHECY = "prophecy"
    VAULT = "vault"
    ADOPTED = "adopted"
    ARCHIVE = "archive"


# Who is asking. Plain strings rather than an enum because `DreamMessage.speaker`
# has to stay open — the intended direction is several dreamers arguing a topic
# out, and a closed enum would refuse the transcript of the thing it exists to
# record. The MOVE rules below are closed, because a mover this store does not
# recognise must not be given the benefit of the doubt.
DREAMER = "dreamer"
TRADER = "trader"
OPERATOR = "operator"

# The dreamer's own shelves. It may move a dream between any of these, in any
# direction, and may delete from them.
#
# ADOPTED is absent on purpose and it is the whole rule: once the trading agent
# has taken a dream, the dreamer holds a wisp and nothing else. It cannot pull
# it back, cannot archive it out from under a live permission, and cannot delete
# it. Handing it back is the trader's move and needs a stated reason.
DREAMER_VAULTS: frozenset[Vault] = frozenset(
    {Vault.WORKBENCH, Vault.PROPHECY, Vault.VAULT, Vault.ARCHIVE}
)


@dataclass(frozen=True)
class Hop:
    """One link in a causal chain, checkable on its own.

    `source` is empty when `checked` is False, and the pair is what lets the
    Dreaming page mark a chain honestly rather than presenting six confident
    sentences of which two were invented.
    """

    claim: str
    checked: bool = False
    source: str = ""

    def to_row(self) -> dict[str, Any]:
        return {"claim": self.claim, "checked": self.checked, "source": self.source}

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Hop:
        return cls(
            claim=str(row.get("claim", "")),
            checked=bool(row.get("checked", False)),
            source=str(row.get("source", "")),
        )


@dataclass(frozen=True)
class DreamCondition:
    """What has to happen before a prophecy is a dream worth offering.

    Same split as `SymbolAssessment` and `AssessmentTrigger`, and it is here for
    the same reason it is there: `text` is the sentence a person reads, and
    `field`/`op`/`value` are the checkable form, because prose cannot be graded.
    Without the structured half a prophecy is an opinion with no consequence and
    "conditions met" means whatever the reader wants it to mean.

    Both halves are kept rather than one derived from the other. The sentence
    carries the reasoning — "the brood map is published for the season, which is
    the only public confirmation before harvest" — and no comparison operator
    holds that. The triple carries the claim, and no sentence can be checked by
    code.

    **The threshold is a number and never the name of another figure.** "Above
    the 20-day" re-checked next month tests a level nobody ever saw, because the
    average moved in the meantime, so it is not the claim that was made. A
    number pins the claim to the moment somebody wrote it down, which is the
    entire point of pre-registering one. `TriggerField` and `TriggerOp` are
    reused from `models.py` rather than redefined here, so a condition can only
    name a figure `indicators.py` actually computes — a field the loop cannot
    produce would be a condition nobody can ever check.

    A condition with prose and no triple is legal and is counted as such rather
    than being rejected. Refusing it would push a dreamer towards inventing a
    number to satisfy a validator, and an invented threshold is worse than an
    honest sentence that says it is not yet checkable.
    """

    text: str
    # The checkable half. All three or none of the three: two of them describe
    # nothing, so `is_checkable` requires the set.
    field: TriggerField | None = None
    op: TriggerOp | None = None
    value: float | None = None

    fulfilled: bool = False
    fulfilled_at: datetime | None = None
    # Why it was marked, or what was seen. Prose, trimmed like all prose here.
    note: str = ""

    @property
    def is_checkable(self) -> bool:
        """Whether code could ever settle this, as opposed to a person."""
        return self.field is not None and self.op is not None and self.value is not None

    def as_trigger(self) -> AssessmentTrigger | None:
        """The structured half as the type `triggers.py` already grades.

        Returned rather than reimplemented so there is one comparison in the
        repository instead of two that can drift. `None` when the condition is
        prose only, which is "cannot be scored" and never "did not hold" — the
        same distinction `AssessmentTrigger.holds` makes for a missing reading.
        """
        if self.field is None or self.op is None or self.value is None:
            return None
        return AssessmentTrigger(field=self.field, op=self.op, value=self.value)

    def to_row(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "field": str(self.field) if self.field else None,
            "op": str(self.op) if self.op else None,
            "value": self.value,
            "fulfilled": self.fulfilled,
            "fulfilled_at": self.fulfilled_at.isoformat() if self.fulfilled_at else None,
            "note": self.note,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> DreamCondition:
        """Forgiving, like every read here. An unreadable half is dropped.

        A condition whose field name came from a newer version comes back as
        prose rather than taking the row down with it. That loses the ability to
        grade it and keeps the sentence, which is the right way round: the
        operator can still read what was claimed.
        """
        field_raw, op_raw, value_raw = row.get("field"), row.get("op"), row.get("value")
        trigger_field: TriggerField | None = None
        trigger_op: TriggerOp | None = None
        value: float | None = None
        try:
            if field_raw is not None and op_raw is not None and value_raw is not None:
                trigger_field = TriggerField(str(field_raw))
                trigger_op = TriggerOp(str(op_raw))
                value = float(value_raw)
        except (ValueError, TypeError):
            trigger_field, trigger_op, value = None, None, None

        fulfilled_at = row.get("fulfilled_at")
        return cls(
            text=str(row.get("text", "")),
            field=trigger_field,
            op=trigger_op,
            value=value,
            fulfilled=bool(row.get("fulfilled", False)),
            fulfilled_at=_dt(fulfilled_at) if fulfilled_at else None,
            note=str(row.get("note", "")),
        )


@dataclass(frozen=True)
class DreamMessage:
    """One turn of the conversation between the two agents, on one dream.

    Stored rather than held in a session, and append-only, for the reason the
    audit log is append-only: the interesting part of a negotiation is the point
    where somebody changed their mind, and a store that kept only the current
    position would throw exactly that away. An operator reading the Dreaming
    page should be able to see the dreamer offer something, the trader ask what
    would kill it, and the answer that followed.

    `speaker` and `kind` are plain strings on purpose. A closed enum would
    refuse the transcript of a second dreamer the moment one exists — which is
    the intended direction, and `Thought.by` is here for the same reason — and
    an unfamiliar `kind` costs a page a badge rather than a row.

    Conventionally `speaker` is one of `DREAMER`, `TRADER` or `OPERATOR`, and
    `kind` is one of `MESSAGE_KINDS`.
    """

    dream_id: int
    at: datetime
    speaker: str
    text: str
    kind: str = "note"
    id: int | None = None


# What a message is doing, for a reader and for a badge. Not enforced: see the
# class docstring. `offer`, `accept` and `return` are written by the store
# itself when an adoption starts or ends, so the transcript is complete even if
# neither agent thought to narrate it.
MESSAGE_KINDS = ("question", "answer", "offer", "accept", "return", "note")


@dataclass(frozen=True)
class Adoption:
    """The record that a symbol permission exists, and for how long.

    One row per adoption, and the row rather than the dream is the authority on
    what was granted. That matters: a dream can be edited after it is taken —
    the dreamer no longer owns it, but an operator or a later migration can
    still touch the row — and a permission that was re-read off a mutable field
    at query time would silently change scope. So `symbols_granted` and
    `asset_class` are copied here at adoption and never read back from the
    dream. Same reasoning as the journal recording the proposal rather than
    re-deriving it later.

    **Live is computed, never stored.** There is no `active` boolean to get out
    of step with `returned_at` and `expires_at`; `is_live` is arithmetic over
    the two, given a `now` the caller supplies. A flag would be a third fact
    about the same thing and would eventually disagree with the other two.
    """

    dream_id: int
    adopted_at: datetime
    symbols_granted: list[str]
    asset_class: str
    returned_at: datetime | None = None
    return_reason: str = ""
    # Every grant made through `DreamStore.adopt` carries one. A permission with
    # no end is a permission nobody ever revisits, and the dreamer that argued
    # for it will not be in the room when it matters.
    expires_at: datetime | None = None
    id: int | None = None

    def is_live(self, now: datetime) -> bool:
        """Whether this grant is in force at `now`. Pure."""
        if self.returned_at is not None:
            return False
        return self.expires_at is None or self.expires_at > now


@dataclass(frozen=True)
class Thought:
    """One entry in the thoughts stream, at a point in time.

    Append-only, like the audit log and for the same reason: the interesting
    thing about a dream is usually the step where it changed its mind, and a
    store that overwrote its working would throw exactly that away.
    """

    stage: DreamStage
    text: str
    at: datetime
    # WHO thought it. Empty today, because there is one dreamer.
    #
    # It exists now rather than later because the intended direction is several
    # dreamers working a topic independently and then arguing it out, and a
    # debate whose transcript cannot say who said what is not a transcript. The
    # field is optional with a default for the same reason every field on
    # `Decision` is: this store is append-only and never migrated, so a reader
    # that rejected yesterday's rows would throw away the history it exists to
    # keep. Adding it now costs one line; adding it after a year of dreams costs
    # a migration and the attribution is unrecoverable anyway.
    by: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "stage": str(self.stage),
            "text": self.text,
            "at": self.at.isoformat(),
            "by": self.by,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Thought:
        return cls(
            stage=_stage(row.get("stage")),
            text=str(row.get("text", "")),
            at=_dt(row.get("at")),
            by=str(row.get("by", "")),
        )


@dataclass
class Dream:
    """One mini-project.

    Carries no quantity, no entry, no stop and no side, and must not gain any.
    See the module docstring: that absence is the reason this module is allowed
    to exist alongside a live order path.
    """

    title: str
    seed: str
    stage: DreamStage = DreamStage.SEED
    chain: list[Hop] = field(default_factory=list)
    thoughts: list[Thought] = field(default_factory=list)
    verdict: DreamVerdict | None = None
    # The hop the whole thing rests on. Named explicitly because a reader who
    # only has time for one sentence should be given the one that could kill it.
    weakest_hop: str = ""
    # What has to happen for a `keep` to become interesting, or a `park` to wake
    # up. A watch with no named trigger is not a plan, which is the same rule
    # the Decisions page applies to the decision loop's own watches.
    trigger: str = ""
    instruments: list[str] = field(default_factory=list)
    origin: str = ""
    id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    # ---------------------------------------------------------------- vaults

    vault: Vault = Vault.WORKBENCH

    # When it entered the vault it is in NOW, which is what expiry is measured
    # from. Never `created_at`.
    #
    # A dream pulled back out of the vault for another pass would otherwise
    # inherit a nearly-dead clock and expire in the middle of the rework, which
    # punishes precisely the behaviour this arrangement wants: taking a stale
    # chain apart again rather than leaving it to rot at the back of a shelf.
    # Entering a vault restarts that vault's clock and nothing else sets it —
    # not saving, not adding a thought, not marking a condition.
    vault_entered_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    # What has to happen before this prophecy is worth offering. See
    # `DreamCondition`: prose for a person, a triple for code, and the threshold
    # is a number rather than the name of another figure.
    conditions: list[DreamCondition] = field(default_factory=list)

    # The tradeable symbols this dream claims, and the ONLY field here that can
    # become a permission.
    #
    # Deliberately separate from `instruments`, which is free text naming what
    # the dream is ABOUT precisely so it can never be read as a ticker. Both
    # exist on purpose and nothing may collapse them: merging would either start
    # parsing the subject line for tickers or turn the permission into prose.
    # See the module docstring — a permission is not an order, and every gate in
    # `RiskGate` still runs on anything traded under one.
    symbols: list[str] = field(default_factory=list)

    # Which `config/rules.yaml` `instruments:` key those symbols belong to —
    # "us_equity", "crypto". Empty means unresolved, and unresolved must GRANT
    # NOTHING: `DreamStore.granted_symbols` drops any adoption without one,
    # because a symbol whose class is unknown is a symbol whose limits are
    # unknown, and guessing a class would be guessing which risk cap applies.
    #
    # **Named `asset_class_key` rather than `asset_class`** because
    # `OrderProposal` carries an `asset_class` and `tests/test_dreaming.py`
    # asserts that `Dream` shares no field name with it at all. That assertion
    # is a blunt net over a sharp rule and it is worth keeping blunt. The two
    # are not the same thing anyway: `OrderProposal.asset_class` is the
    # `AssetClass` enum the broker adapter switches on, and this is a key into
    # the instrument-class block of the rules file.
    asset_class_key: str = ""

    # What the dreamer is left with once the trading agent has taken this.
    #
    # A trace, in the Pensieve sense. Nothing is deleted to produce it — the
    # chain, the thoughts and the conditions all stay on the row, because
    # destroying the reasoning behind a live trading permission to honour a
    # metaphor would be the worst trade in this file. The wisp is what the
    # DREAMER is handed back when it asks what it is working on, so an adopted
    # dream stops occupying a slot in its head while the record stays whole.
    wisp: str = ""

    @property
    def verification(self) -> Verification:
        """Derived from the chain, never set by the agent.

        A model asked to rate its own sourcing will rate it generously. This
        counts `checked` flags instead, so the badge on the page is arithmetic
        over what the dream actually recorded.
        """
        if not self.chain:
            return Verification.UNVERIFIED
        checked = sum(1 for hop in self.chain if hop.checked)
        if checked == len(self.chain):
            return Verification.SOURCED
        if checked == 0:
            return Verification.UNVERIFIED
        return Verification.PARTIAL

    @property
    def unverified_hops(self) -> list[Hop]:
        return [hop for hop in self.chain if not hop.checked]

    @property
    def is_open(self) -> bool:
        return self.stage is not DreamStage.VERDICT

    # ------------------------------------------------------------ conditions

    @property
    def has_conditions(self) -> bool:
        """Whether anything was ever pre-registered on this dream.

        A separate question from whether the conditions hold, and it has to stay
        separate. Same rule as `WatchReport.can_grade_anything` and
        `news_history.has_cycles`: "nothing was recorded" and "everything
        recorded came back clean" are opposite findings that produce identical
        empty lists, and only one of them says anything about the dream.
        """
        return bool(self.conditions)

    @property
    def conditions_met(self) -> int:
        return sum(1 for c in self.conditions if c.fulfilled)

    @property
    def unmet_conditions(self) -> list[DreamCondition]:
        return [c for c in self.conditions if not c.fulfilled]

    @property
    def all_conditions_met(self) -> bool:
        """True only when there was something to meet and all of it was met.

        **A dream with no conditions is False here, not True.** `all([])` is
        True, which is exactly the trap: an empty condition list would otherwise
        report a prophecy as fulfilled the moment it was written, and the vault
        it unlocks is the one the trading agent can see. The good outcome must
        not be what an absence of evidence looks like — the same rule as an
        empty chain reading `UNVERIFIED` rather than `SOURCED`, and as the
        tailnet status reporting `unknown` rather than healthy.

        Read this with `has_conditions` when the distinction matters to a
        reader: False here means either "not yet" or "nothing was ever claimed",
        and those deserve different sentences on a page.
        """
        return bool(self.conditions) and all(c.fulfilled for c in self.conditions)

    @property
    def is_offerable(self) -> bool:
        """Whether the trading agent could see this at all.

        Membership of one vault, stated as a property so that callers ask the
        store's question rather than each writing their own comparison. It is
        deliberately NOT "is this a good idea": the vault says who is holding
        it, and nothing here has a view on whether it should be traded.
        """
        return self.vault is Vault.VAULT

    def add_thought(
        self,
        stage: DreamStage,
        text: str,
        *,
        at: datetime | None = None,
        by: str = "",
    ) -> None:
        self.thoughts.append(
            Thought(stage=stage, text=_trim(text), at=at or datetime.now(UTC), by=by)
        )
        self.stage = stage
        self.updated_at = at or datetime.now(UTC)


def _trim(text: str) -> str:
    """Prose truncates. Nothing structural goes through here.

    Same split as the decision loop: a rationale that overran a cap once killed
    a live cycle and restarted straight into the same failure, so free text is
    trimmed rather than rejected. A number is never trimmed, because a truncated
    number is a different number.
    """
    clean = text.strip()
    if len(clean) <= TEXT_MAX_CHARS:
        return clean
    return clean[: TEXT_MAX_CHARS - 1].rstrip() + "…"


def _stage(value: object) -> DreamStage:
    try:
        return DreamStage(str(value))
    except ValueError:
        # An unreadable stage is a display problem, not a reason to lose the
        # row. Same tolerance as the audit reader, which counts what it could
        # not parse rather than dropping it silently.
        return DreamStage.SEED


def _dt(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.now(UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _vault(value: object) -> Vault:
    try:
        return Vault(str(value))
    except ValueError:
        # A vault name from a newer version must not lose the row. It comes back
        # on the workbench, which is the safe direction in the one way that
        # matters: the workbench is not the vault the trading agent can see, so
        # an unreadable value can never accidentally publish a dream to it.
        return Vault.WORKBENCH


def _symbols(values: object) -> list[str]:
    """Normalise a symbol list. Structural, so it is cleaned and never trimmed.

    Upper-cased, stripped, blanks dropped, order preserved, duplicates removed.
    `_trim` is for prose; a truncated symbol is a different symbol, in exactly
    the way a truncated price is a different price.
    """
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        symbol = str(value).strip().upper()
        if symbol and symbol not in out:
            out.append(symbol)
    return out


@dataclass(frozen=True)
class VaultCaps:
    """How many dreams each vault will hold.

    A cap here is a working constraint rather than a risk rule — nothing in this
    file can lose money — and it exists because a vault with two hundred entries
    is a vault nobody reads, which is the same as an empty one with extra steps.

    **`adopted` is 3 to match `max_concurrent_positions`.** An adopted dream is
    a promise that the trading agent may act on it, and a promise the account
    has no slot to keep is worse than a refusal: it reads on the page as
    permission granted while the position it implies can never be opened. The
    two numbers are related by intent and not by code — `config/rules.yaml` owns
    the real one, this file must not import it, and a change to either wants a
    look at the other.

    **The archive is uncapped, and that is deliberate.** `None` means no limit.
    The only alternative to archiving a dream when the archive is full is
    deleting it, so a cap there would quietly turn "retire this" into "destroy
    the record of it" — the same reasoning that keeps the audit snapshots
    unpruned in `deploy/backup-journal.sh`.
    """

    workbench: int = 24
    prophecy: int = 12
    vault: int = 12
    adopted: int = 3
    archive: int | None = None

    def limit_for(self, vault: Vault) -> int | None:
        return {
            Vault.WORKBENCH: self.workbench,
            Vault.PROPHECY: self.prophecy,
            Vault.VAULT: self.vault,
            Vault.ADOPTED: self.adopted,
            Vault.ARCHIVE: self.archive,
        }[vault]


@dataclass(frozen=True)
class VaultTTLs:
    """How long a dream may sit in a vault before it is reported as expired.

    In days, measured from `vault_entered_at` and never from `created_at`. See
    the module docstring: a dream pulled back for another pass gets a fresh
    clock, because the alternative punishes reworking a chain.

    **The operator was explicitly unsure between ninety days and a year**, which
    is why these are a value object passed in rather than constants read off the
    module. The defaults are a starting point and are meant to be argued with,
    not a limit anything depends on.

    `prophecy` gets the long one because a prophecy is a long-horizon claim by
    nature — a supply chain does not reprice in a quarter, and expiring one at
    ninety days would throw away the only kind of idea this whole module exists
    to produce. A workbench chain that has not been touched in three months, by
    contrast, has been abandoned rather than left to mature.

    `archive` is `None`: the archive IS the retirement state, so a TTL on it
    would be an expiry on an expiry.
    """

    workbench: int = 90
    prophecy: int = 365
    vault: int = 90
    adopted: int = 90
    archive: int | None = None

    def days_for(self, vault: Vault) -> int | None:
        return {
            Vault.WORKBENCH: self.workbench,
            Vault.PROPHECY: self.prophecy,
            Vault.VAULT: self.vault,
            Vault.ADOPTED: self.adopted,
            Vault.ARCHIVE: self.archive,
        }[vault]


# Module-level singletons so a default argument is a name rather than a call.
# Both types are frozen, so sharing one instance is safe.
DEFAULT_CAPS = VaultCaps()
DEFAULT_TTLS = VaultTTLs()


class MoveRefusal(StrEnum):
    """Why a move did not happen. Machine-readable so a page can style it.

    Every one of these is an ordinary answer rather than an error. A vault
    filling up is a normal Tuesday.
    """

    NOT_FOUND = "not_found"
    # The actor is not allowed to make this move at all. Covers an unknown
    # actor, which is refused rather than waved through: a mover this store does
    # not recognise gets nothing, because failing open on an authorisation
    # question is how the one rule that matters here stops mattering.
    FORBIDDEN_ACTOR = "forbidden_actor"
    ALREADY_THERE = "already_there"
    FULL = "full"
    NEEDS_REASON = "needs_reason"
    NEEDS_SYMBOLS = "needs_symbols"
    NEEDS_ASSET_CLASS = "needs_asset_class"
    # The dream is not in the vault this operation moves out of — adopting
    # something that is not in the vault, returning something that was never
    # adopted.
    WRONG_VAULT = "wrong_vault"


@dataclass(frozen=True)
class MoveResult:
    """What happened, or every reason it did not.

    **Refusals are collected rather than short-circuited**, exactly as
    `RiskGate` collects every failure reason: a caller told only the first thing
    wrong with a move fixes it, tries again and is told the second. The trading
    agent is one of the callers, and an agent handed one reason at a time will
    keep asking.

    **Nothing here raises.** A full vault, a wrong actor or a missing reason
    must not take down the page an operator opened to see what is in the vaults,
    and an agent must not be able to crash the store by asking for something it
    is not allowed to have.
    """

    ok: bool
    dream_id: int
    moved_from: Vault | None = None
    moved_to: Vault | None = None
    refusals: tuple[MoveRefusal, ...] = ()
    # One sentence per refusal, joined. Written for the agent that reads it, so
    # it names the constraint rather than restating the code.
    detail: str = ""

    @property
    def refused(self) -> bool:
        return not self.ok

    def __bool__(self) -> bool:
        return self.ok


def _refused(
    dream_id: int,
    refusals: list[MoveRefusal],
    details: list[str],
    *,
    moved_from: Vault | None = None,
    moved_to: Vault | None = None,
) -> MoveResult:
    return MoveResult(
        ok=False,
        dream_id=dream_id,
        moved_from=moved_from,
        moved_to=moved_to,
        refusals=tuple(refusals),
        detail=" ".join(details),
    )


SCHEMA = """
CREATE TABLE IF NOT EXISTS dreams (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  title        TEXT NOT NULL,
  seed         TEXT NOT NULL,
  stage        TEXT NOT NULL,
  verdict      TEXT,
  weakest_hop  TEXT NOT NULL DEFAULT '',
  trigger_note TEXT NOT NULL DEFAULT '',
  origin       TEXT NOT NULL DEFAULT '',
  chain        TEXT NOT NULL DEFAULT '[]',
  thoughts     TEXT NOT NULL DEFAULT '[]',
  instruments  TEXT NOT NULL DEFAULT '[]',
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dream_messages (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  dream_id INTEGER NOT NULL,
  at       TEXT NOT NULL,
  speaker  TEXT NOT NULL,
  kind     TEXT NOT NULL DEFAULT 'note',
  text     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_dream_messages_dream ON dream_messages (dream_id, at);

CREATE TABLE IF NOT EXISTS adoptions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  dream_id        INTEGER NOT NULL,
  adopted_at      TEXT NOT NULL,
  symbols_granted TEXT NOT NULL DEFAULT '[]',
  asset_class     TEXT NOT NULL DEFAULT '',
  returned_at     TEXT,
  return_reason   TEXT NOT NULL DEFAULT '',
  expires_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_adoptions_dream ON adoptions (dream_id);
-- The index `granted_symbols` runs on. A live grant is one that has not been
-- handed back, so the partial index is the whole working set and stays small
-- however long the history gets.
CREATE INDEX IF NOT EXISTS ix_adoptions_live ON adoptions (returned_at, expires_at);
"""


# The columns added to `dreams` after the table already existed on the box, and
# the SQL to add each one. Order matters only in that `vault_entered_at` is
# backfilled below and must exist first.
_ADDED_DREAM_COLUMNS: tuple[tuple[str, str], ...] = (
    ("vault", "TEXT NOT NULL DEFAULT 'workbench'"),
    ("vault_entered_at", "TEXT NOT NULL DEFAULT ''"),
    ("conditions", "TEXT NOT NULL DEFAULT '[]'"),
    ("symbols", "TEXT NOT NULL DEFAULT '[]'"),
    ("asset_class_key", "TEXT NOT NULL DEFAULT ''"),
    ("wisp", "TEXT NOT NULL DEFAULT ''"),
)


def _add_vault_columns(conn: sqlite3.Connection) -> None:
    """Bring a `dreams` table that predates the vaults up to the new shape.

    **`CREATE TABLE IF NOT EXISTS` is a no-op on a table that already exists.**
    Editing `SCHEMA` therefore changes what a FRESH database gets and nothing
    whatever about `data/dreams.db` on the droplet, which was created months
    ago. This is not a hypothetical: the same mistake in `journal.py` produced a
    live order that the journal had never heard of, because 866 tests were green
    over a database that could not store the row the models had just been
    changed to allow. Every test builds its store from scratch in a `tmp_path`,
    so **the suite is structurally blind to this** and cannot tell you that you
    have forgotten. `tests/test_dreaming.py` builds the OLD schema by hand for
    exactly that reason, which is the only way to exercise a migration at all.

    Three properties, and they are the ones `_drop_planned_target_not_null`
    already established:

    - **Idempotent and cheap.** `PRAGMA table_info` is a lookup, so a database
      that is already correct pays one query per open and stops. It runs on
      every open rather than behind a version flag, because a version flag is
      one more piece of bookkeeping that can be wrong.
    - **Additive only.** `ADD COLUMN` with a non-null default touches no
      existing value, so there is no half-migrated state to be left in. The
      table rebuild `journal.py` needs was only necessary there because SQLite
      cannot drop a NOT NULL with `ALTER`; nothing here drops anything.
    - **`vault_entered_at` is backfilled rather than left empty.** An empty
      stamp parses as "now" on every read, so the expiry clock would restart on
      every process start and nothing would ever age out — a bug that looks like
      nothing at all until somebody asks why the workbench has ninety entries.
      `updated_at` is the honest answer available: the row was last touched
      then, so treating that as when it entered the workbench is the reading
      that makes an abandoned chain expire and a live one not.

    Any future change to `SCHEMA` needs a migration beside it and a test that
    starts from the old shape. The suite will not warn you.
    """
    existing = {str(c["name"]) for c in conn.execute("PRAGMA table_info(dreams)")}
    if not existing:  # pragma: no cover - SCHEMA has just created the table
        return

    added: list[str] = []
    for name, ddl in _ADDED_DREAM_COLUMNS:
        if name in existing:
            continue
        conn.execute(f"ALTER TABLE dreams ADD COLUMN {name} {ddl}")
        added.append(name)

    if not added:
        return

    log.warning(
        "dreams_migrating_vault_columns",
        added=added,
        detail=(
            "This dreams database predates the vaults. Adding the new columns "
            "in place; no existing row is modified except to backfill the vault "
            "clock from updated_at."
        ),
    )
    # Backfilled unconditionally rather than only for the rows just migrated: a
    # row inserted by an older binary against a migrated database would also
    # carry the empty default, and it costs one UPDATE that matches nothing on
    # a healthy file.
    conn.execute(
        "UPDATE dreams SET vault_entered_at = updated_at WHERE vault_entered_at = ''"
    )


class DreamStore:
    """SQLite store for dreams. Its own file, not the journal.

    Deliberately forgiving on read and strict on write, in the same shape as
    `audit.py`: a row that will not parse is skipped and counted rather than
    raising, because the page that renders these is one an operator opens to
    look at everything else too.
    """

    def __init__(self, path: Path = DEFAULT_DREAMS_PATH) -> None:
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

    def save(self, dream: Dream) -> int:
        """Insert or update. Returns the row id."""
        payload = (
            dream.title,
            dream.seed,
            str(dream.stage),
            str(dream.verdict) if dream.verdict else None,
            dream.weakest_hop,
            dream.trigger,
            dream.origin,
            json.dumps([h.to_row() for h in dream.chain]),
            json.dumps([t.to_row() for t in dream.thoughts]),
            json.dumps(dream.instruments),
            dream.created_at.isoformat(),
            dream.updated_at.isoformat(),
        )
        with self._connect() as conn:
            if dream.id is None:
                cursor = conn.execute(
                    "INSERT INTO dreams (title, seed, stage, verdict, weakest_hop,"
                    " trigger_note, origin, chain, thoughts, instruments, created_at,"
                    " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    payload,
                )
                dream.id = int(cursor.lastrowid or 0)
            else:
                conn.execute(
                    "UPDATE dreams SET title=?, seed=?, stage=?, verdict=?,"
                    " weakest_hop=?, trigger_note=?, origin=?, chain=?, thoughts=?,"
                    " instruments=?, created_at=?, updated_at=? WHERE id=?",
                    (*payload, dream.id),
                )
        return dream.id or 0

    def recent(self, limit: int = 30) -> list[Dream]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM dreams ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        out: list[Dream] = []
        for row in rows:
            parsed = self._to_dream(row)
            if parsed is not None:
                out.append(parsed)
        return out

    def get(self, dream_id: int) -> Dream | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM dreams WHERE id=?", (dream_id,)).fetchone()
        return self._to_dream(row) if row else None

    @staticmethod
    def _to_dream(row: sqlite3.Row) -> Dream | None:
        try:
            chain = [Hop.from_row(h) for h in json.loads(row["chain"] or "[]")]
            thoughts = [Thought.from_row(t) for t in json.loads(row["thoughts"] or "[]")]
            instruments = [str(i) for i in json.loads(row["instruments"] or "[]")]
        except (json.JSONDecodeError, TypeError, AttributeError) as exc:
            # Counted rather than raised, for the reason on the class. One bad
            # row must not take the page down.
            log.warning("dream_unreadable", dream_id=row["id"], error=str(exc))
            return None

        verdict_raw = row["verdict"]
        try:
            verdict = DreamVerdict(verdict_raw) if verdict_raw else None
        except ValueError:
            verdict = None

        return Dream(
            id=int(row["id"]),
            title=str(row["title"]),
            seed=str(row["seed"]),
            stage=_stage(row["stage"]),
            verdict=verdict,
            weakest_hop=str(row["weakest_hop"] or ""),
            trigger=str(row["trigger_note"] or ""),
            origin=str(row["origin"] or ""),
            chain=chain,
            thoughts=thoughts,
            instruments=instruments,
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )


# Below this many resolved dreams, every rate here is noise. Same reasoning and
# roughly the same threshold as `PerformanceSummary.sample_is_thin`: a drop rate
# computed over four chains says nothing, and a figure presented without its
# sample count gets believed anyway.
THIN_LEDGER_THRESHOLD = 12


@dataclass(frozen=True)
class DreamLedger:
    """What the dreamer has learned about its own thinking.

    This is the consolidation pass, and it is deliberately narrow. It counts
    properties of the REASONING — was a hop sourced, was a chain attacked, was a
    trigger named — and never touches what any idea would have earned. See the
    module docstring: consolidation over reasoning quality is safe because the
    facts are true regardless of how a trade went, and there is no outcome
    sample to overfit to.

    It reaches the operator through the Dreaming page and stops there. Nothing
    here is fed back to the model, for the same reason `metrics.py` is not.

    Every rate is `None` rather than zero when there is nothing to divide by.
    A sourcing rate of "0%" on an empty store reads as a damning result; "n/a"
    reads as the absence of evidence it actually is.
    """

    dreams: int
    resolved: int
    sourcing_rate: float | None
    drop_rate: float | None
    median_hops: float | None
    untriggered_keeps: int
    unattacked: int

    @property
    def sample_is_thin(self) -> bool:
        return self.resolved < THIN_LEDGER_THRESHOLD

    @classmethod
    def of(cls, dreams: list[Dream]) -> DreamLedger:
        hops = [hop for d in dreams for hop in d.chain]
        resolved = [d for d in dreams if d.verdict is not None]
        lengths = sorted(len(d.chain) for d in dreams if d.chain)

        median: float | None = None
        if lengths:
            middle = len(lengths) // 2
            median = (
                float(lengths[middle])
                if len(lengths) % 2
                else (lengths[middle - 1] + lengths[middle]) / 2
            )

        return cls(
            dreams=len(dreams),
            resolved=len(resolved),
            sourcing_rate=(
                sum(1 for h in hops if h.checked) / len(hops) * 100 if hops else None
            ),
            # A dreamer that never drops anything is not attacking its chains,
            # which is the failure this number exists to make visible. High is
            # healthy here, which is why it is labelled rather than left to be
            # read as a defect rate.
            drop_rate=(
                sum(1 for d in resolved if d.verdict is DreamVerdict.DROP)
                / len(resolved)
                * 100
                if resolved
                else None
            ),
            median_hops=median,
            untriggered_keeps=sum(
                1 for d in dreams if d.verdict is DreamVerdict.KEEP and not d.trigger
            ),
            unattacked=sum(1 for d in dreams if d.chain and not d.weakest_hop),
        )


@dataclass(frozen=True)
class DreamSummary:
    """Counts for the page header. Pure arithmetic over what is stored."""

    total: int
    open_dreams: int
    kept: int
    parked: int
    dropped: int
    unverified: int

    @classmethod
    def of(cls, dreams: list[Dream]) -> DreamSummary:
        return cls(
            total=len(dreams),
            open_dreams=sum(1 for d in dreams if d.is_open),
            kept=sum(1 for d in dreams if d.verdict is DreamVerdict.KEEP),
            parked=sum(1 for d in dreams if d.verdict is DreamVerdict.PARK),
            dropped=sum(1 for d in dreams if d.verdict is DreamVerdict.DROP),
            unverified=sum(
                1 for d in dreams if d.verification is Verification.UNVERIFIED
            ),
        )
