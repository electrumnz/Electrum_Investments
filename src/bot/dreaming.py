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

## Symbiosis: two chains that meet at the same hop

Two second-order chains often share a link. A dream about drought cutting hydro
output in one region and a dream about aluminium smelters chasing cheap power
are separately interesting; the hop they share — *that region's power gets
scarce and dear* — is the mechanism, and a mechanism two independent routes
arrive at is better evidenced than either route alone. `fuse` is how that gets
recorded rather than noticed once and forgotten.

Six properties, and every one of them is a rule already used somewhere else in
this file:

- **A fusion is a NEW dream and the parents survive.** `parents` on the child,
  nothing consumed, nothing deleted. A fused dream that ate its sources would
  destroy the ability to attack either of them, which is the whole activity
  this module exists to support — and it is the same reason adoption leaves the
  chain on the row rather than trading it for a wisp.
- **The shared hops are a first-class field, not something a reader has to
  spot.** `shared_hops` names the overlap, because the overlap is the reason
  the fusion exists. A fusion whose reason has to be inferred from two chains
  side by side is a fusion nobody will check.
- **Verification cannot improve by fusing, and `verification_ceiling` is what
  makes that structural.** Two unverified chains do not make a sourced one. The
  child's badge is capped at the WEAKEST parent's, so a union that happens to
  contain some checked hops cannot read as better evidenced than the worse
  argument it came from. A claim one parent sourced and the other did not comes
  across UNCHECKED, with the source left on the parent that has it: the child
  must not take the more flattering of two readings of the same link.
- **Conditions are the union and `all_conditions_met` needs all of them.** A
  fusion is a strictly stronger claim, so it must be HARDER to promote than
  either parent, never easier. Nothing special is needed for that — the
  property already requires all — but it is the property to check first if
  somebody ever "simplifies" the merge.
- **`symbols` is the union and never wider.** A fusion must not become a route
  around the class hard-block, so the child can only ever name symbols its
  parents already claimed, an override may only NARROW that set (the `adopt`
  rule, in a second place), and the class resolves only when the parents agree
  — `""` otherwise, which grants nothing. `grants.resolve_granted_symbols` is
  still the lock that decides what may actually be traded.
- **Two or three parents.** One is not a fusion, and at four the shared-hop
  argument stops meaning anything: a hop four chains reach is a truism rather
  than a mechanism. Both are typed refusals rather than exceptions, like every
  other refusal here.

**Only the dreamer may fuse, and an ADOPTED dream may not be fused**, for the
same reason it may not be moved or deleted: a live grant points at that row,
and the trading agent is the one holding it. The back-reference from a parent
to its children is DERIVED (`children_of`) rather than stored, because a second
copy of a relationship is a second thing that can disagree with the first —
the reasoning that keeps `Adoption.is_live` computed and that the two answers
to "is this grant live" already cost once.

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

`data/dreams.db`, not `data/journal.db`. Keeping them apart means no query can
accidentally read a hypothesis as a position, and it means a bug in the
speculative half cannot corrupt the trading record.

**What losing this file costs is no longer "some speculative notes".** That was
true when a dream reached nobody. The `adoptions` table is now a live trading
PERMISSION — `granted_symbols` is what widens `allowed_symbols`, and
`Trade.dream_id` on the journal points here for the provenance the Board
renders. Losing it withdraws every grant, which is the safe direction and is
why it is still not in `deploy/backup-journal.sh`: an adoption is recoverable by
adopting again, a trade is not recoverable at all, and restoring a stale
`dreams.db` over a current one would resurrect permissions somebody handed back.
The journal stays the only irreplaceable file on the box. Dangling `dream_id`s
are the visible cost, and a provenance that cannot be looked up is better than a
permission that was reinstated by a restore.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import structlog

from .models import AssessmentTrigger, TriggerField, TriggerOp
from .triggers import CycleReadings

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


# Weakest first. Written down because `Verification` is a StrEnum and `min()`
# over one would compare the WORDS — "partial" < "sourced" < "unverified"
# alphabetically, which puts the strongest badge in the middle and the weakest
# last. A silent alphabetical answer to an evidence question is exactly the
# plausible wrong figure this repository refuses, so the order is a tuple and
# `weaker_of` reads it.
VERIFICATION_ORDER: tuple[Verification, ...] = (
    Verification.UNVERIFIED,
    Verification.PARTIAL,
    Verification.SOURCED,
)


def weaker_of(left: Verification, right: Verification) -> Verification:
    """The less-evidenced of two badges. Pure, total, and never averages.

    Confidence in a chain is the minimum across its links, so combining two
    chains takes the minimum of the two badges rather than anything cleverer.
    An average would let a sourced chain carry an invented one, which is the
    single thing `Hop.checked` exists to prevent.
    """
    return min(left, right, key=VERIFICATION_ORDER.index)


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

# The store narrating a fusion into the parents' transcripts. Deliberately
# neither `DREAMER` nor `TRADER`, for the reason `dreamer.SCOPE` is neither:
# `confer.last_agent_turn_at` would read an agent-named message as a TURN, and
# a marker that swallowed a machine note would silence the operator's own.
#
# It IS a new voice to `confer.has_something_changed`, which is deliberate and
# is the one consequence worth stating out loud: a parent sitting in the vault
# becomes conferrable again when it is fused, because "this argument now exists
# in a stronger form" genuinely changes what adopting the parent means.
FUSION = "fusion"

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

    # WHOSE figure the triple names. A `field`/`op`/`value` says "close below
    # 641.20" and says nothing about what closed, so without this the triple is
    # a comparison with no subject and `grade_conditions` has no reading to
    # fetch. Deliberately its own field rather than assumed to be one of
    # `Dream.symbols`: a dream about aluminium may hinge on a power utility it
    # would never trade, and picking one of the dream's own symbols to check
    # against would be grading a claim nobody made.
    #
    # Empty is legal and means "not gradeable by code", exactly as a missing
    # triple does. It is counted rather than rejected, for the reason the
    # docstring gives about prose-only conditions.
    symbol: str = ""

    # WHICH hop of the dream's chain this condition would settle. **One-based**,
    # matching the `hop 1`, `hop 2` numbering `dreamer.build_prompt` renders, so
    # the number the model writes back is the number it was shown. Zero and
    # negatives are not positions in that numbering and are dropped on read.
    #
    # This is what makes the prophecy shelf a claim rather than a filing
    # decision. A condition is otherwise free to pre-register a number about a
    # link nobody doubted, which grades cleanly, promotes cleanly and settles
    # nothing — see `promotion_for`, which now requires the chain's weakest hop
    # to be covered.
    #
    # **A tuple because one condition may honestly settle several hops.** "The
    # brood map is published for the season" can be the evidence for both the
    # emergence hop and the overlap hop, and forcing a choice between them would
    # push the dreamer into writing the same threshold twice. The reverse is
    # allowed too: several conditions may name the same hop.
    #
    # Empty is legal — a condition that settles nothing in particular is still a
    # condition, and refusing one would push the dreamer towards pinning a
    # number to a hop it does not actually bear on, which is the invented
    # threshold problem wearing a different hat.
    #
    # Persisted inside the `conditions` JSON column, so this needed **no schema
    # migration**: the store keeps the whole list as one TEXT blob and
    # `from_row` reads an absent key as "nothing pinned", exactly as it does for
    # `symbol`. A row written before this field existed genuinely does not say
    # which hop its threshold bears on, and inventing one would be the confident
    # wrong figure this repository refuses.
    settles_hops: tuple[int, ...] = ()

    fulfilled: bool = False
    fulfilled_at: datetime | None = None
    # Why it was marked, or what was seen. Prose, trimmed like all prose here.
    note: str = ""

    @property
    def is_checkable(self) -> bool:
        """Whether code could ever settle this, as opposed to a person."""
        return self.field is not None and self.op is not None and self.value is not None

    @property
    def is_gradeable(self) -> bool:
        """Whether `grade_conditions` can actually settle it against readings.

        Strictly narrower than `is_checkable`, and the two are kept apart on
        purpose. `is_checkable` asks whether a NUMBER was pre-registered, which
        is what the promotion rule turns on — a conclusion with a number in it
        is a different kind of claim from a conclusion without one, whether or
        not this repository happens to be able to check it today. This asks
        whether the figure can be looked up, which additionally needs a symbol.

        Collapsing them would silently change what promotes to the prophecy
        shelf, so a condition naming a real threshold on a symbol the loop does
        not follow would stop counting as a prophecy at all.
        """
        return self.is_checkable and bool(self.symbol.strip())

    def settles(self, hop: int | None) -> bool:
        """Whether this condition claims to settle a given one-based hop.

        `None` — the position of a weakest hop nobody could establish — is False
        rather than "matches anything". A condition cannot be shown to settle a
        hop that could not be identified, and the permissive reading would let
        an unresolvable weakest hop count as covered by every condition on the
        dream, which is the exact inversion `promotion_for` exists to close.
        """
        if hop is None:
            return False
        return hop in self.settles_hops

    @property
    def key(self) -> tuple[str, str, str, float | None, str]:
        """What makes this the SAME claim across two steps of one dream.

        A dreamer restating its conditions on a later step must not wipe the
        grading already done on them — a fulfilled condition that came back
        unfulfilled would leave a dream stuck below the vault forever, checked
        and re-checked against readings that already fired. So a rewritten step
        carries the old verdict forward for any condition that is still the same
        claim.

        The checkable triple plus the symbol IS the claim, and the prose is only
        the claim's description, so a reworded sentence over an unchanged
        threshold keeps its grade. Change the number and it is a NEW claim that
        starts ungraded, which is the honest half: a threshold moved after the
        fact is exactly what pre-registering one exists to prevent.

        **`settles_hops` is deliberately NOT in here.** Which hop a threshold
        bears on is bookkeeping about the chain, not the claim itself: the
        number is what was pre-registered, and re-pinning it to a different hop
        does not make it a different prediction. Including it would reset the
        grading of a fulfilled condition every time a step renumbered the chain,
        which is `carry_forward_grading`'s whole failure mode. The new pin still
        travels — `carry_forward_grading` copies the verdict onto the INCOMING
        condition, so a re-pinned claim keeps its grade and gains its new hop.
        """
        if self.is_checkable:
            return (
                self.symbol.strip().upper(),
                str(self.field),
                str(self.op),
                self.value,
                "",
            )
        # Prose-only conditions have no claim to key on, so they key on the
        # sentence. Two different sentences are two different conditions.
        return ("", "", "", None, self.text.strip())

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
            "symbol": self.symbol,
            "settles_hops": list(self.settles_hops),
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
            # Absent on every condition written before the field existed, which
            # reads as "not gradeable" rather than as an error. The store is
            # JSON in a TEXT column, so this needed no migration — but a row
            # from before it shipped genuinely does not say which symbol its
            # threshold belongs to, and inventing one would be the confident
            # wrong figure this repository refuses.
            symbol=str(row.get("symbol", "") or ""),
            # Absent on every condition written before the pin existed, which
            # reads as "nothing pinned" rather than as an error — and therefore
            # as a dream that cannot leave the workbench until a later step says
            # which hop its threshold bears on. `_ids` drops anything that will
            # not read as a positive integer rather than defaulting it to zero,
            # because hop 0 is not a hop and a pin pointing nowhere is worse
            # than no pin: it would be reported as covered.
            settles_hops=tuple(_ids(row.get("settles_hops"))),
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

    **And it is the ONLY definition of live.** `granted_symbols` used to answer
    the same question in SQL, comparing `expires_at > ?` as TEXT, which is
    string ordering rather than instant ordering. `adopt` wrote whatever offset
    the caller's `at` carried, so a grant stamped `+13:00` — Pacific/Auckland,
    which the dream timer uses by design — read as LIVE from the permission path
    six hours after `is_live` correctly called it expired. Two paths, two
    answers, and the one that failed open was the one that decides what may be
    traded. The SQL now filters only on facts a string can hold and every
    liveness question comes back through here.
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
        """Whether this grant is in force at `now`. Pure, and never raises.

        **No expiry reads as EXPIRED, not as eternal.** `adopt` always writes
        one, so a row without it is hand-edited, migrated from somewhere, or
        written by something that is not this store — and "a permission nobody
        set an end on" is exactly the state that should not quietly outlive
        everyone who remembers it. The safe reading of a missing figure is the
        one that grants nothing, the same rule as an absent indicator being
        `None` rather than zero.

        A naive `now` is read as UTC rather than raising. Everything in this
        repository reasons in UTC deliberately, and a `TypeError` out of here
        lands in the decision cycle — the shape `claude.propose` was wrapped
        for after a `ValidationError` killed a live loop.
        """
        if self.returned_at is not None:
            return False
        if self.expires_at is None:
            return False
        return _as_utc(self.expires_at) > _as_utc(now)


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

    # WHICH hop that sentence names, one-based into `chain`. The same split as
    # `DreamCondition.text` against its triple, and it is here for the same
    # reason: the sentence is what a person reads and the number is what code
    # can act on. `weakest_hop` is a paraphrase in practice — "that the smelter
    # cannot re-contract power elsewhere" against a chain claim reading
    # "Smelters buy power on long interconnect contracts" — so matching prose
    # back to a hop is a similarity judgement, and this file already refuses
    # those in `_claim_key`.
    #
    # `None` means the dreamer did not say. `resolved_weakest_hop` falls back to
    # an exact claim match on the prose, and answers `None` when neither
    # establishes a hop — never a guess and never a clamp.
    weakest_hop_index: int | None = None
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

    # ------------------------------------------------------------- symbiosis

    # The dreams this one was fused FROM. Empty for an ordinary dream, two or
    # three ids for a fusion, and the parents are not consumed to produce it.
    #
    # This is the only stored half of the relationship. A parent's children are
    # derived by `DreamStore.children_of`, because a back-reference written on
    # the parent would be a second copy of one fact and the two would
    # eventually disagree — the reasoning that keeps `Adoption.is_live`
    # computed rather than stored, and that a second SQL answer to "is this
    # grant live" already cost once.
    parents: list[int] = field(default_factory=list)

    # The hop or hops the parents had in common. THE REASON THE FUSION EXISTS,
    # so it is a field rather than something a reader is left to spot by
    # holding two chains side by side. Same argument as `weakest_hop`: a reader
    # who has time for one line should be given the load-bearing one.
    shared_hops: list[str] = field(default_factory=list)

    # The best `verification` may read, inherited from the WEAKEST parent when
    # this dream was fused. `None` on an ordinary dream, which is "no cap".
    #
    # It exists because the union of two chains can contain checked hops from
    # one parent and unchecked hops from the other, which counts as PARTIAL —
    # strictly better than an UNVERIFIED parent. Two unverified chains must not
    # make a sourced one, and a fusion must never render as better evidenced
    # than the worse argument it came from. Recorded at fusion time and never
    # re-derived, the same rule as `Adoption.symbols_granted`: the parents can
    # be edited afterwards, and a badge that moved when a parent was reworked
    # would be a claim about evidence nobody could check.
    verification_ceiling: Verification | None = None

    @property
    def is_fusion(self) -> bool:
        return bool(self.parents)

    @property
    def verification(self) -> Verification:
        """Derived from the chain, never set by the agent, and never improved by fusing.

        A model asked to rate its own sourcing will rate it generously. This
        counts `checked` flags instead, so the badge on the page is arithmetic
        over what the dream actually recorded.

        `verification_ceiling` caps it for a fused dream. See that field: the
        union of two chains can read PARTIAL when one parent was UNVERIFIED,
        and combining two arguments is not evidence for either of them.
        """
        counted = self._counted_verification()
        if self.verification_ceiling is None:
            return counted
        return weaker_of(counted, self.verification_ceiling)

    def _counted_verification(self) -> Verification:
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

    # ----------------------------------------------------- the weakest hop

    @property
    def resolved_weakest_hop(self) -> int | None:
        """Which hop of the chain the weakest-hop sentence names. One-based.

        Two sources, in order, and neither of them guesses:

        - **The number the dreamer wrote**, when it lands inside the chain. An
          index outside it is not honoured and is not clamped — a model that
          answered `4` on a three-hop chain has named no hop, and pinning the
          rule to the last one would be this file choosing which link is weakest.
        - **An exact claim match on the prose**, normalised by `_claim_key`,
          which is the same whitespace-and-case rule fusion uses to decide two
          hops are the same claim. Nothing fuzzier: a similarity score would
          decide that two differently-worded claims are one, which is precisely
          the judgement a reader has to be able to check.

        `None` for a dream with no chain, for a sentence naming nothing, and for
        a sentence that matches no hop. All three are "could not establish it",
        which `promotion_for` treats as a refusal rather than as a pass — the
        `_class_total_risk` rule in a new place: an unknown that would make a
        limit unenforceable fails closed.
        """
        if not self.chain:
            return None
        index = self.weakest_hop_index
        if index is not None and 1 <= index <= len(self.chain):
            return index
        named = _claim_key(self.weakest_hop)
        if not named:
            return None
        for position, hop in enumerate(self.chain, 1):
            if _claim_key(hop.claim) == named:
                return position
        return None

    @property
    def awaits_settlement(self) -> bool:
        """Whether any link in this chain is still an assumption.

        The question `promotion_for`'s weakest-hop clause turns on, and it is
        arithmetic over `checked` rather than a view on the dreamer's judgement.
        A chain every hop of which is sourced has nothing awaiting settlement,
        so there is no link for a prophecy to be parked against; an empty chain
        has no links at all. Both read False here and both are documented on the
        rule itself, because they reach the same answer for opposite reasons.
        """
        return bool(self.unverified_hops)

    @property
    def weakest_hop_is_pinned(self) -> bool:
        """Whether a CHECKABLE condition claims to settle the weakest hop.

        `is_checkable` rather than `is_gradeable`, matching the clause above it
        in `promotion_for`: the promotion rule turns on whether a number was
        pre-registered, not on whether this repository can currently look the
        figure up. Collapsing the two would silently make a threshold on a
        symbol the loop does not follow stop counting as a prophecy at all.

        A prose-only condition pinned to the weakest hop is therefore not
        enough. That is deliberate and is the whole point: the prophecy shelf
        holds claims that can be graded, and "the spread normalises, hop 2" is
        an opinion with a hop number attached.
        """
        hop = self.resolved_weakest_hop
        if hop is None:
            return False
        return any(c.is_checkable and c.settles(hop) for c in self.conditions)

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


# ------------------------------------------------------------------- promotion


# The two shelves a dream can be promoted OFF. Adoption is the trading agent
# taking something and is not a promotion; the vault is where promotion ends;
# and the archive is where retired dreams rest, so pulling one back out on an
# automatic rule would resurrect ideas somebody deliberately put down.
PROMOTABLE_FROM = frozenset({Vault.WORKBENCH, Vault.PROPHECY})


@dataclass(frozen=True)
class Promotion:
    """Where a dream belongs now, and why. Computed, never stored.

    Pure, so the rule can be read and tested on its own, without a database and
    without the caps, the transcript or the clock that `DreamStore.promote`
    brings with it. `to` is `None` for "stays where it is", which is the answer
    for most dreams on most steps and is an ordinary outcome rather than a
    failure — a dream still being worked on belongs on the workbench.
    """

    to: Vault | None = None
    reason: str = ""

    @property
    def moves(self) -> bool:
        return self.to is not None


def _excerpt(text: str, limit: int = 140) -> str:
    """A claim short enough to sit inside a refusal sentence.

    Prose, so it truncates rather than being rejected — the `_trim` rule, with
    a tighter cap because this one is quoted mid-sentence rather than stored.
    """
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def _weakest_hop_refusal(dream: Dream) -> Promotion | None:
    """Why the weakest hop is not covered, or `None` when it is.

    Three refusals rather than one, because they are fixed three different ways
    and a caller told "not promotable" fixes nothing. The same reasoning
    `MoveResult` collects every refusal for, and the same reasoning
    `SYMBOLS_NOT_OFFERED` and `CLASS_NOT_OFFERED` are two refusals rather than
    one: name the hop, number the hop, or pin a threshold to it.

    Every message carries the CONSEQUENCE rather than a code. A dream that
    stalls here has a dreamer reading the reason on the next run, and "not
    promotable" would teach it nothing about what to write differently.
    """
    hops = len(dream.chain)
    assumptions = len(dream.unverified_hops)
    checkable = sum(1 for c in dream.conditions if c.is_checkable)

    hop = dream.resolved_weakest_hop
    if hop is None:
        if not dream.weakest_hop.strip() and dream.weakest_hop_index is None:
            return Promotion(
                reason=(
                    "Stays on the workbench: the chain's weakest link has "
                    "nothing pinned to it, because nobody has said which link "
                    f"it is. {assumptions} of {hops} hop(s) are still "
                    "assumptions and none is named as the one that could kill "
                    "the chain. The prophecy shelf holds a dream parked "
                    "awaiting the link that would settle it, so name the "
                    "weakest hop and pre-register a condition against it."
                )
            )
        return Promotion(
            reason=(
                "Stays on the workbench: the chain's weakest link has nothing "
                f'pinned to it, because "{_excerpt(dream.weakest_hop)}" matches '
                f"no hop in this {hops}-hop chain and no usable hop number was "
                f"given. Set weakest_hop_index to one of hops 1-{hops} so a "
                "condition can be shown to settle it."
            )
        )

    if dream.weakest_hop_is_pinned:
        return None

    claim = _excerpt(dream.chain[hop - 1].claim) or "unstated"
    return Promotion(
        reason=(
            "Stays on the workbench: the chain's weakest link has nothing "
            f'pinned to it. Hop {hop} — "{claim}" — is what this rests on, and '
            f"none of its {checkable} checkable condition(s) claims to settle "
            "it. A prophecy is a dream parked awaiting the link that could kill "
            "it; conditions on links nobody doubted grade cleanly and settle "
            "nothing, which makes the shelf a filing decision rather than a "
            f"claim. Put {hop} in settles_hops on a condition that would settle "
            "that hop."
        )
    )


def promotion_for(dream: Dream) -> Promotion:
    """The promotion rule: workbench → prophecy → vault.

    Nothing moved a dream off the workbench before this existed. `is_offerable`
    was defined and never called, `dreamer.py` never moved anything, and
    `confer.py` reads only `Vault.VAULT` — so the vault was permanently empty
    and the conference permanently a no-op. Three real dreams were generated
    against the live model to find that, because every test built the shelf it
    was testing by hand.

    The rule, and each clause is a decision rather than plumbing:

    - **A `keep` verdict is necessary and not sufficient.** A dream that reached
      a conclusion is not automatically worth offering; the trading agent's
      attention is the scarce thing here and `caps.vault` is 12.
    - **At least one CHECKABLE condition, or it stays put.** A conclusion with
      no number pre-registered against it is an opinion, and the prophecy shelf
      exists precisely to hold claims that can be graded. This is `is_checkable`
      rather than `is_gradeable` on purpose — see that property.
    - **And one of them must settle the chain's WEAKEST HOP.** The operator's
      correction, and the clause that turns the prophecy shelf back into what it
      was for: *dreams parked awaiting the prophecy to be fulfilled.* A
      checkable condition about a link nobody doubted grades cleanly, promotes
      cleanly and settles nothing, which makes the shelf a filing decision
      rather than a claim. `weakest_hop` names the hop that could kill the
      chain, so that is the hop a condition has to bear on.

      Three edge cases, each answered rather than forced:

      - **A chain with no unchecked hop is not held back.** `awaits_settlement`
        is False when every link is sourced, so there is no link awaiting
        anything and demanding a threshold on one would be asking for a number
        against a hop already nailed down. The badge reads SOURCED and is
        visible on every surface, which is where that claim gets checked.
      - **No chain at all is not held back either**, and for a different
        reason: this clause pins a chain's weakest link and does not require a
        chain to exist. Refusing a chainless keep is a separate rule about
        whether a dream needs an argument, and smuggling it in here would be
        adding a limit nobody agreed to.
      - **A FUSION needs no exemption**, because the verdict clause above
        already catches it. A fresh fusion has no verdict and no `weakest_hop`
        by design — nobody has attacked it yet — so it is refused for the
        verdict, never for the pin. By the time a dreamer has taken it apart and
        reached a keep it names a weakest hop like any other dream, and the
        ordinary rule applies. `tests/test_dreaming.py` pins that order.

      **It gates leaving the WORKBENCH, not arriving at the vault**, and the
      asymmetry is deliberate. A dream already on the prophecy shelf is by
      definition parked awaiting its conditions, so re-testing the pin when they
      fire would strand it there — including every prophecy promoted before this
      clause existed, which would turn a tightening into a silent deletion. What
      the clause buys is the ticket off the workbench, and it sits AHEAD of the
      all-conditions-met branch so the direct workbench-to-vault route cannot
      slip past it.
    - **All conditions met sends it to the VAULT**, the one shelf the trading
      agent can see. `all_conditions_met` is False on an empty list, which is
      the whole reason it and `has_conditions` are separate questions: an
      absence of conditions must never read as conditions satisfied, or every
      dream with a conclusion and nothing pre-registered would land in front of
      the agent claiming to have been proven.
    - **Otherwise the prophecy shelf**, where the conditions are graded against
      later readings until they fire.

    Note what is NOT here: any view on whether the idea is any good. Same
    posture as `is_offerable` — the shelf says who is holding a dream, and
    nothing in this module has an opinion on whether it should be traded.
    """
    if dream.vault not in PROMOTABLE_FROM:
        return Promotion(
            reason=(
                f"In {dream.vault}, which promotion does not move. The vault is "
                "where this ends, adoption is the trading agent's to make, and "
                "the archive is deliberate."
            )
        )

    if dream.verdict is not DreamVerdict.KEEP:
        reached = str(dream.verdict) if dream.verdict else "no verdict yet"
        return Promotion(
            reason=(
                f"Stays on the workbench: the verdict is {reached}, and only a "
                "keep is a dream worth offering."
            )
        )

    if not any(c.is_checkable for c in dream.conditions):
        return Promotion(
            reason=(
                "Stays on the workbench: a keep with no checkable condition is "
                "a conclusion nobody can grade. Pre-register a field, an "
                "operator and a number before this becomes a prophecy."
            )
        )

    if dream.vault is Vault.WORKBENCH and dream.awaits_settlement:
        unpinned = _weakest_hop_refusal(dream)
        if unpinned is not None:
            return unpinned

    if dream.all_conditions_met:
        return Promotion(
            to=Vault.VAULT,
            reason=(
                f"Every one of its {len(dream.conditions)} condition(s) has "
                "been met, so it is offered to the trading agent."
            ),
        )

    if dream.vault is Vault.PROPHECY:
        # Already on the right shelf. Returning VAULT here would be refused as
        # ALREADY_THERE, and worse, a caller that retried would reset
        # `vault_entered_at` on every pass and nothing would ever age out.
        return Promotion(
            reason=(
                f"Already a prophecy, with {len(dream.unmet_conditions)} of "
                f"{len(dream.conditions)} condition(s) still unmet."
            )
        )

    return Promotion(
        to=Vault.PROPHECY,
        reason=(
            f"A keep with {len(dream.conditions)} pre-registered condition(s), "
            f"{len(dream.unmet_conditions)} still unmet. Tracked as a prophecy "
            "until they fire."
        ),
    )


# ---------------------------------------------------------------- grading them


@dataclass(frozen=True)
class ConditionGrading:
    """One dream's conditions, checked against readings the loop recorded.

    `conditions` is the WHOLE list in its original order, fulfilled or not, so a
    caller can write it back without reassembling anything. `newly_fulfilled`
    names only what this pass settled, because "two conditions are met" and
    "two conditions were met just now" are different facts and the second is the
    one worth a log line.
    """

    dream_id: int
    conditions: tuple[DreamCondition, ...] = ()
    newly_fulfilled: tuple[str, ...] = ()
    # Conditions no reading could ever settle: prose with no triple, or a triple
    # with no symbol. Counted rather than dropped, the same as
    # `WatchReport.watches_with_prose_only`, because a prophecy nobody can grade
    # is the interesting failure and an invisible one looks like patience.
    ungradeable: int = 0
    cycles_checked: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.newly_fulfilled)

    @property
    def can_grade_anything(self) -> bool:
        """Whether this rests on any evidence at all.

        Separate from "did anything fire", the same rule as
        `WatchReport.can_grade_anything` and `news_history.has_cycles`. No
        conditions fulfilled because no cycle recorded a figure, and none
        fulfilled because the thresholds genuinely have not been reached, are
        opposite findings that produce identical empty tuples.
        """
        return self.cycles_checked > 0


def grade_conditions(
    dream: Dream, later: Sequence[CycleReadings]
) -> ConditionGrading:
    """Settle a dream's conditions against figures the decision loop recorded.

    The counterpart to `triggers.grade_watch`, and it deliberately reuses that
    module's `CycleReadings` and `AssessmentTrigger.holds` rather than growing a
    second comparison. `DreamCondition.as_trigger` exists exactly so there is
    one comparison in this repository instead of two that can drift.

    **There is no horizon here, and that is the difference from a watch.** A
    watch expires after five days because the reading behind it is gone by then.
    A prophecy is a long-horizon claim by construction — that is why its TTL is
    365 days against the vault's 90 — so the condition stands until it fires or
    the shelf's own expiry retires it. Adding a horizon would quietly make the
    prophecy shelf a five-day shelf.

    **A fulfilled condition is never un-fulfilled.** The claim was that the
    figure would reach a level, not that it would stay there, so a reading that
    fires it settles it for good. Re-opening one on a later reading would make
    a prophecy that fired in March unfire in April.

    Pure: no I/O and no clock. `later` should be ascending by time; the first
    cycle that meets a condition is the one recorded as having fired it.
    """
    graded: list[DreamCondition] = []
    fired: list[str] = []
    ungradeable = 0

    for condition in dream.conditions:
        if condition.fulfilled:
            graded.append(condition)
            continue

        trigger = condition.as_trigger()
        if trigger is None or not condition.symbol.strip():
            ungradeable += 1
            graded.append(condition)
            continue

        symbol = condition.symbol.strip().upper()
        settled: DreamCondition | None = None
        for cycle in later:
            reading = cycle.readings.get(symbol)
            value = reading.get(trigger.field) if reading else None
            # `holds` answers None for a figure the bars could not supply, and
            # None is not False: a symbol with no 200-day average has not failed
            # a test against one. Only an explicit True settles anything.
            if trigger.holds(value) is not True:
                continue
            settled = replace(
                condition,
                fulfilled=True,
                fulfilled_at=cycle.at,
                note=_trim(
                    f"{symbol} {trigger.render()} — read "
                    f"{value:,.4g} at {cycle.at.isoformat(timespec='minutes')}."
                ),
            )
            break

        if settled is None:
            graded.append(condition)
        else:
            graded.append(settled)
            fired.append(settled.text or symbol)

    return ConditionGrading(
        dream_id=int(dream.id or 0),
        conditions=tuple(graded),
        newly_fulfilled=tuple(fired),
        ungradeable=ungradeable,
        cycles_checked=len(later),
    )


def carry_forward_grading(
    existing: Sequence[DreamCondition], incoming: Sequence[DreamCondition]
) -> list[DreamCondition]:
    """Keep what has already been graded when a dream restates its conditions.

    A later dream step may return the whole condition list again, and taking it
    as written would wipe every `fulfilled` flag already earned. That is not a
    cosmetic loss: the vault is reached by `all_conditions_met`, so a dream
    whose grading was reset on every step could never be promoted at all, and it
    would be re-checked forever against readings that had already fired it.

    Matched on `DreamCondition.key`, so a reworded sentence over an unchanged
    threshold keeps its verdict and a MOVED threshold does not. That asymmetry
    is the point: a number changed after the fact is a new claim, and letting it
    inherit the old claim's fulfilment would be back-dating a prediction.
    """
    graded = {c.key: c for c in existing if c.fulfilled}
    out: list[DreamCondition] = []
    for condition in incoming:
        was = graded.get(condition.key)
        if was is None:
            out.append(condition)
            continue
        out.append(
            replace(
                condition,
                fulfilled=True,
                fulfilled_at=was.fulfilled_at,
                note=was.note,
            )
        )
    return out


# ------------------------------------------------------------------- symbiosis
#
# Two chains that meet at the same hop. See the module docstring for why this is
# a feature rather than a flourish: the shared hop is the mechanism, and a
# mechanism two independent routes arrive at is better evidenced than either
# route alone — which is a claim about the ARGUMENT and not about the evidence,
# and `verification_ceiling` is what stops it being read as the second thing.

# One parent is not a fusion. Four is a hop so general that the shared-hop
# argument stops meaning anything — a link four separate chains reach is a
# truism, and a truism dressed as a mechanism is the kind of confident nothing
# this whole module is arranged against.
MIN_FUSION_PARENTS = 2
MAX_FUSION_PARENTS = 3


def _claim_key(claim: str) -> str:
    """What makes two hops the SAME claim across two dreams.

    Whitespace-normalised and case-folded, and nothing cleverer. Fuzzy matching
    would decide that two differently-worded claims are one, which is precisely
    the judgement a reader has to be able to check — and a fusion built on a
    similarity score is a fusion whose reason cannot be verified by reading it.
    """
    return " ".join(claim.split()).casefold()


def _repin(
    condition: DreamCondition, parent: Dream, position_of: dict[str, int]
) -> DreamCondition:
    """Renumber a condition's hop pins from its parent's chain onto the child's.

    `plan_fusion` builds the child's chain as a deduped union in parent order,
    so hop 2 of a parent is rarely hop 2 of the child. A pin carried across
    unchanged would name a different link while still reporting as covered,
    which is the one failure mode worse than no pin at all — so anything that
    cannot be mapped is DROPPED and the condition arrives honestly unpinned.

    A condition with nothing pinned is returned unchanged rather than rebuilt,
    so the common case allocates nothing.
    """
    if not condition.settles_hops:
        return condition
    mapped: list[int] = []
    for index in condition.settles_hops:
        if not 1 <= index <= len(parent.chain):
            continue
        target = position_of.get(_claim_key(parent.chain[index - 1].claim))
        if target is not None and target not in mapped:
            mapped.append(target)
    return replace(condition, settles_hops=tuple(mapped))


@dataclass(frozen=True)
class Fusion:
    """What a set of parents would combine into, computed before anything is written.

    Pure, so the merge rules can be read and tested without a database, exactly
    as `promotion_for` holds the promotion rule away from `DreamStore.promote`.
    `DreamStore.fuse` is the half that needs a connection, the caps and a clock.
    """

    parents: tuple[int, ...] = ()
    chain: tuple[Hop, ...] = ()
    # The claims that appeared in more than one parent, in chain order.
    shared_hops: tuple[str, ...] = ()
    conditions: tuple[DreamCondition, ...] = ()
    symbols: tuple[str, ...] = ()
    asset_class_key: str = ""
    instruments: tuple[str, ...] = ()
    # The weakest parent's badge. The child may never read better than this.
    verification_ceiling: Verification = Verification.UNVERIFIED

    @property
    def has_overlap(self) -> bool:
        """Whether the parents actually meet anywhere.

        Deliberately NOT a refusal. Two chains with no shared hop are a weaker
        fusion and the operator may still want it — the dreamer confirms every
        fusion, and refusing one here would be this module forming a view on
        whether an idea is any good, which is the posture `promotion_for` and
        `is_offerable` both keep. It is reported so the page can say so.
        """
        return bool(self.shared_hops)


def plan_fusion(parents: Sequence[Dream]) -> Fusion:
    """Merge two or three dreams into the child they would produce.

    Every rule here is stated in the module docstring and each one is a rule
    from somewhere else in this file:

    - **The chain is the UNION, deduped on the claim**, in the order the
      parents were given. Nothing is dropped, so the child carries the whole
      argument rather than a summary of it.
    - **A hop is checked in the child only when EVERY parent that made the claim
      had it checked.** A link one parent sourced and another did not is a link
      whose sourcing is in dispute, and taking the more flattering of the two
      readings is how a fusion would launder evidence. The source is not lost:
      it is on the parent, which survives.
    - **`verification_ceiling` is the weakest parent's badge.** Two unverified
      chains do not make a sourced one, and the child must never render as
      better evidenced than the worse argument it came from.
    - **Conditions are the union**, deduped on `DreamCondition.key` and carried
      through `carry_forward_grading` so a claim already fulfilled on one parent
      arrives fulfilled. That is the one grading rule in the repository rather
      than a second copy of it. `all_conditions_met` then needs all of them,
      which makes a fusion harder to promote than either parent — correct, for
      a strictly stronger claim.
    - **`symbols` is the union and the class resolves only when the parents
      agree.** A disagreement leaves it empty, which grants nothing: the
      `scope_symbols` rule that one class or none may be claimed, arriving from
      the other direction. A fusion can therefore never name a symbol no parent
      claimed, so it is not a route around the class hard-block.

    Pure: no store, no clock, no network. Takes the parents in the order they
    should read.
    """
    order: list[str] = []
    first: dict[str, Hop] = {}
    checked: dict[str, bool] = {}
    source: dict[str, str] = {}
    seen_in: dict[str, set[int]] = {}

    for index, parent in enumerate(parents):
        for hop in parent.chain:
            key = _claim_key(hop.claim)
            if not key:
                continue
            if key not in first:
                order.append(key)
                first[key] = hop
                checked[key] = hop.checked
                source[key] = hop.source
                seen_in[key] = set()
            else:
                # AND, never OR. See the docstring: the disputed reading wins.
                checked[key] = checked[key] and hop.checked
                if not source[key] and hop.checked:
                    source[key] = hop.source
            seen_in[key].add(index)

    chain = tuple(
        Hop(
            claim=first[key].claim,
            checked=checked[key],
            source=source[key] if checked[key] else "",
        )
        for key in order
    )
    shared = tuple(first[key].claim for key in order if len(seen_in[key]) > 1)

    # Where each claim ended up in the CHILD's chain, so a parent's hop pins can
    # be renumbered onto it. The union is deduped and reordered, so an index
    # carried across untouched would point at whichever hop happened to land in
    # that slot — a pin silently naming a different link, which is worse than an
    # unpinned condition because it reads as covered. Anything that will not map
    # is dropped for the same reason.
    position_of = {key: index for index, key in enumerate(order, 1)}

    incoming: list[DreamCondition] = []
    keys: set[tuple[str, str, str, float | None, str]] = set()
    for parent in parents:
        for condition in parent.conditions:
            if condition.key in keys:
                continue
            keys.add(condition.key)
            incoming.append(_repin(condition, parent, position_of))
    conditions = tuple(
        carry_forward_grading(
            [c for parent in parents for c in parent.conditions if c.fulfilled],
            incoming,
        )
    )

    instruments: list[str] = []
    for parent in parents:
        for subject in parent.instruments:
            if subject and subject not in instruments:
                instruments.append(subject)

    claimed = {p.asset_class_key.strip() for p in parents if p.symbols}
    ceiling = Verification.SOURCED
    for parent in parents:
        ceiling = weaker_of(ceiling, parent.verification)

    return Fusion(
        parents=tuple(int(p.id or 0) for p in parents),
        chain=chain,
        shared_hops=shared,
        conditions=conditions,
        symbols=tuple(_symbols([s for p in parents for s in p.symbols])),
        # One class or nothing. A parent claiming symbols with no class resolved
        # puts `""` in the set, which is exactly the disagreement that has to
        # come back empty — a symbol whose class is unknown is a symbol whose
        # limits are unknown.
        asset_class_key=(
            next(iter(claimed)) if len(claimed) == 1 and "" not in claimed else ""
        ),
        instruments=tuple(instruments),
        verification_ceiling=ceiling,
    )


@dataclass(frozen=True)
class FusionCandidate:
    """Two or three dreams that share a hop, offered for a human or an agent to confirm.

    Produced by arithmetic over stored chains — no model call, no judgement —
    because "these two chains meet here" is a fact and "these two ideas belong
    together" is not. The dreamer is shown the fact and decides.
    """

    dream_ids: tuple[int, ...] = ()
    shared_hops: tuple[str, ...] = ()
    titles: tuple[str, ...] = ()

    @property
    def overlap(self) -> int:
        return len(self.shared_hops)


def fusion_candidates(
    dreams: Sequence[Dream], *, limit: int = 4
) -> list[FusionCandidate]:
    """Which dreams share a hop, best overlap first. Pure; no store, no model.

    **This is deliberately a finder and not a fuser.** It proposes; the dreamer
    confirms and `DreamStore.fuse` writes. An unattended process that combined
    dreams on its own would be generating confident new claims out of arithmetic
    on old ones, which is the one thing this module is furthest from being
    allowed to do.

    Three exclusions, and each is a stated reason rather than a filter somebody
    will delete as redundant:

    - **A dream that is already a fusion is not a candidate.** It shares every
      hop with its own parents by construction, so a finder that could see one
      would propose re-fusing it with them forever. A deliberate
      second-generation fusion is still allowed through `fuse`; it is just not
      something to suggest.
    - **Only dreams the dreamer holds.** An adopted dream has a live grant
      pointing at it and `fuse` refuses one anyway, so offering it would be
      offering a move that cannot be made.
    - **A hop shared by more than `MAX_FUSION_PARENTS` dreams is skipped
      entirely**, rather than truncated to the first three. A link four chains
      reach is a truism — "energy prices matter to smelters" — and the fusion
      it would suggest rests on nothing. Truncating would hide that by picking
      an arbitrary three of them.
    """
    holdable = [
        d
        for d in dreams
        if d.id is not None and d.vault in DREAMER_VAULTS and not d.is_fusion and d.chain
    ]
    by_claim: dict[str, list[Dream]] = {}
    for dream in holdable:
        for key in dict.fromkeys(_claim_key(h.claim) for h in dream.chain if h.claim):
            by_claim.setdefault(key, []).append(dream)

    groups: dict[tuple[int, ...], list[str]] = {}
    for key, sharers in by_claim.items():
        if not MIN_FUSION_PARENTS <= len(sharers) <= MAX_FUSION_PARENTS:
            continue
        ids = tuple(sorted(int(d.id or 0) for d in sharers))
        claim = next(h.claim for h in sharers[0].chain if _claim_key(h.claim) == key)
        groups.setdefault(ids, []).append(claim)

    titles = {int(d.id or 0): d.title for d in holdable}
    candidates = [
        FusionCandidate(
            dream_ids=ids,
            shared_hops=tuple(claims),
            titles=tuple(titles.get(i, "") for i in ids),
        )
        for ids, claims in groups.items()
    ]
    # Most overlap first, then by id, so the order is stable across runs rather
    # than SQLite's or a dict's. A candidate list that reshuffled between runs
    # would make "the model picked the second one" unreproducible.
    candidates.sort(key=lambda c: (-c.overlap, c.dream_ids))
    return candidates[:limit]


def _as_utc(moment: datetime) -> datetime:
    """The same instant, in UTC. A naive stamp is read as UTC rather than local.

    Everything in this repository reasons in UTC on purpose — `sessions_utc`,
    every journal timestamp, every figure the dashboard renders — so a stamp
    with no offset is a UTC stamp somebody forgot to label, and reading it as
    the box's local time would silently reinterpret it. Attaching the zone is
    also what keeps `is_live` from raising on a naive `now`.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _iso_utc(moment: datetime) -> str:
    """The stamp SQLite stores. Always UTC, so text ordering is instant ordering.

    Every timestamp in this store is compared as text somewhere — `ORDER BY
    adopted_at`, `expires_at > ?` before that comparison was moved into Python —
    and ISO text only sorts by instant when every row carries the same offset. A
    `+13:00` stamp sorts as though it were thirteen hours later than it is, which
    is how a lapsed grant read as live. Normalise on write and the question never
    arises again.
    """
    return _as_utc(moment).isoformat()


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


def _ids(values: object) -> list[int]:
    """Normalise a stored id list. Structural, so it is cleaned and never trimmed.

    Anything that will not read as an int is DROPPED rather than defaulted to
    zero. A parent id of 0 points at no row, and a dream claiming a parent that
    does not exist is worse than one claiming none: `children_of` would never
    find it and a reader would go looking.
    """
    if not isinstance(values, list):
        return []
    out: list[int] = []
    for value in values:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0 and parsed not in out:
            out.append(parsed)
    return out


def _hop_index(value: object) -> int | None:
    """A one-based hop position, or `None` for "not said".

    Structural, so it is cleaned rather than trimmed, and anything that will not
    read as a POSITIVE integer comes back `None` rather than 0. Hop zero is not
    a hop, and a stored pin that points at no link would be reported as a pin —
    the same reason `_ids` drops a parent id it cannot parse instead of
    defaulting it.
    """
    if value is None:
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _verification(value: object) -> Verification | None:
    """A stored ceiling, or `None` for "no cap".

    An unreadable value comes back `None` rather than `UNVERIFIED`. That looks
    like the wrong direction and is not: `None` means the dream's own chain
    decides, which is what an ordinary dream does, whereas guessing `UNVERIFIED`
    would silently rewrite the badge of a fusion whose ceiling column was
    written by a version this one cannot read.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return Verification(text)
    except ValueError:
        log.warning("dream_verification_ceiling_unreadable", value=text)
        return None


def _dream_payload(dream: Dream) -> tuple[object, ...]:
    """Every column of a dream row, in the order `SCHEMA` declares them.

    One tuple shared by the INSERT and the UPDATE, so a column added to one
    cannot be forgotten in the other — which is a silent write of a stale value
    rather than an error.
    """
    return (
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
        str(dream.vault),
        dream.vault_entered_at.isoformat(),
        json.dumps([c.to_row() for c in dream.conditions]),
        json.dumps(_symbols(dream.symbols)),
        dream.asset_class_key,
        _trim(dream.wisp),
        json.dumps(_ids(dream.parents)),
        json.dumps([_trim(h) for h in dream.shared_hops]),
        str(dream.verification_ceiling) if dream.verification_ceiling else "",
        # Structural, so it is cleaned rather than trimmed and a value that is
        # not a position comes back as "not said". Writing 0 or a negative would
        # store a pin pointing at no hop, which `resolved_weakest_hop` would
        # then have to defend against on every read.
        _hop_index(dream.weakest_hop_index),
    )


def _to_adoption(row: sqlite3.Row) -> Adoption | None:
    """One adoption row, or `None` if it will not parse.

    Forgiving in the same way every other read here is, and for a sharper
    reason: this table is what `granted_symbols` counts, and a raise from a bad
    row would put an exception into the path that answers "what may be traded".
    Skipping it is the fail-closed direction — one unreadable grant permits
    nothing rather than everything.
    """
    try:
        symbols = _symbols(json.loads(row["symbols_granted"] or "[]"))
    except (json.JSONDecodeError, TypeError) as exc:
        log.warning("adoption_unreadable", adoption_id=row["id"], error=str(exc))
        return None
    returned = row["returned_at"]
    expires = row["expires_at"]
    return Adoption(
        id=int(row["id"]),
        dream_id=int(row["dream_id"]),
        adopted_at=_dt(row["adopted_at"]),
        symbols_granted=symbols,
        asset_class=str(row["asset_class"] or ""),
        returned_at=_dt(returned) if returned else None,
        return_reason=str(row["return_reason"] or ""),
        expires_at=_dt(expires) if expires else None,
    )


def _fusion_title(parents: Sequence[Dream]) -> str:
    """A name for a fusion whose caller supplied none.

    Derived rather than refused, because a blank title is a formatting problem
    and refusing the fusion over one would lose the argument. It names the
    parents rather than inventing a subject: a generated title that read like a
    thesis would be this module writing a claim nobody made.
    """
    named = " + ".join(p.title.strip() for p in parents if p.title.strip())
    return _trim(named) or "Fused dream"


def _fusion_seed(plan: Fusion) -> str:
    """The spark of a fusion: the hop the parents meet at, or the fact they do not.

    **Says so plainly when there is no overlap.** Two chains fused with nothing
    in common is a weaker fusion and a legal one — the dreamer confirmed it —
    and a seed that implied a shared mechanism where there is none would be the
    confident wrong sentence this repository refuses, written by the store
    itself.
    """
    if plan.shared_hops:
        return _trim(
            "Two chains meet here: " + "; ".join(plan.shared_hops) + ". "
            "That shared link is the mechanism, and a mechanism two independent "
            "routes arrive at is better evidenced than either route alone."
        )
    return _trim(
        "Fused from chains that share no hop. The combination is the claim; "
        "nothing here is corroborated by the overlap, because there is none."
    )


def _default_wisp(dream: Dream, symbols: list[str], at: datetime) -> str:
    """The trace the dreamer is left with when a dream is taken.

    Deliberately thin — a title, a date and what was granted. It is not a
    summary of the chain and must not become one: the point of a wisp is that
    the dreamer stops carrying the whole thing around, and a wisp that restated
    the argument would leave it carrying the whole thing around in fewer words.

    None of the dream is destroyed to produce this. See the module docstring.
    """
    named = ", ".join(symbols) if symbols else "no symbols"
    return _trim(
        f"Taken by the trading agent on {at:%d %b %Y} — {dream.title} ({named}). "
        "The chain is in the vault; what is left here is the shape of it."
    )


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
    # The adoption asked for something the dream does not offer. Two refusals
    # rather than one because they are fixed differently — narrow the symbol
    # list, or take the class the dream actually claims — and `MoveResult`
    # collects every reason so a caller is not told one at a time.
    SYMBOLS_NOT_OFFERED = "symbols_not_offered"
    CLASS_NOT_OFFERED = "class_not_offered"
    # The dream is not in the vault this operation moves out of — adopting
    # something that is not in the vault, returning something that was never
    # adopted.
    WRONG_VAULT = "wrong_vault"
    # A fusion was asked for with the wrong number of parents. Two refusals
    # rather than one because they are opposite mistakes: one dream is not a
    # fusion at all, and four or more is a shared hop so general that the
    # argument for fusing stops meaning anything.
    NEEDS_PARENTS = "needs_parents"
    TOO_MANY_PARENTS = "too_many_parents"
    # A parent is on the ADOPTED shelf. A live grant points at that row and the
    # trading agent is holding it, which is the same reason the dreamer may not
    # move or delete one.
    PARENT_ADOPTED = "parent_adopted"
    # `promote` looked at the dream and the rule said it stays where it is. The
    # commonest answer by far — most dreams on most steps are still being worked
    # on — and an ordinary outcome rather than a fault, which is why `promote`
    # reports it through the same collected-refusal shape as everything else
    # instead of raising or returning None. `MoveResult.detail` carries the
    # sentence from `promotion_for` saying which clause held it back.
    NOT_PROMOTABLE = "not_promotable"


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


@dataclass(frozen=True)
class FusionResult:
    """What a fusion produced, or every reason it did not happen.

    Its own type rather than a `MoveResult`, because a fusion is not a move: no
    dream changes shelf, the id that matters is one that did not exist before,
    and `moved_from`/`moved_to` would be two fields with nothing to say. What it
    keeps from `MoveResult` is the part that matters — **refusals are collected
    rather than short-circuited**, exactly as `RiskGate` collects them, and
    **nothing raises**. A caller told one reason at a time fixes one thing at a
    time and asks again.
    """

    ok: bool
    # The child, when one was made. `None` on every refusal, because a fusion
    # that did not happen has no dream to point at — and returning a parent's id
    # here would read as though something had been written.
    dream_id: int | None = None
    parents: tuple[int, ...] = ()
    shared_hops: tuple[str, ...] = ()
    refusals: tuple[MoveRefusal, ...] = ()
    detail: str = ""

    @property
    def refused(self) -> bool:
        return not self.ok

    def __bool__(self) -> bool:
        return self.ok


def _refused_fusion(
    parents: tuple[int, ...], refusals: list[MoveRefusal], details: list[str]
) -> FusionResult:
    return FusionResult(
        ok=False, parents=parents, refusals=tuple(refusals), detail=" ".join(details)
    )


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
  updated_at   TEXT NOT NULL,
  -- Everything added after this table first shipped. Listed here so a FRESH
  -- database gets them directly and `SCHEMA` keeps describing the real table,
  -- and added to an existing one by `_add_dream_columns`. Both halves are
  -- needed and neither substitutes for the other: without this a new store
  -- would be built old-shaped and then immediately migrated, which works but
  -- means the migration warning fires on every first run and this block stops
  -- being the answer to "what shape is the table". The order matches
  -- `_ADDED_DREAM_COLUMNS`, so a migrated database and a fresh one end up
  -- column-for-column identical.
  vault           TEXT NOT NULL DEFAULT 'workbench',
  vault_entered_at TEXT NOT NULL DEFAULT '',
  conditions      TEXT NOT NULL DEFAULT '[]',
  symbols         TEXT NOT NULL DEFAULT '[]',
  asset_class_key TEXT NOT NULL DEFAULT '',
  wisp            TEXT NOT NULL DEFAULT '',
  -- The symbiosis columns. `parents` is the only stored half of the
  -- relationship; a parent's children are derived by `children_of`.
  parents              TEXT NOT NULL DEFAULT '[]',
  shared_hops          TEXT NOT NULL DEFAULT '[]',
  verification_ceiling TEXT NOT NULL DEFAULT '',
  -- WHICH hop `weakest_hop` names, one-based. Nullable rather than defaulted to
  -- 0, because "the dreamer did not say" and "hop zero" are different answers
  -- and only one of them is a position in a chain. The CONDITIONS' own pins
  -- needed no column at all: they live inside the `conditions` JSON blob.
  weakest_hop_index    INTEGER
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
# the SQL to add each one. They are ALSO in `SCHEMA`, in this order, so a fresh
# database is built with them and a migrated one ends up identical to it; keep
# the two lists in step. Order matters here only in that `vault_entered_at` is
# backfilled below and must exist first.
#
# **Append to this list; never reorder it.** A droplet database is migrated in
# whatever order it first met these, so reordering would make a freshly-built
# table and a migrated one disagree column-for-column — which is the one thing
# `test_a_migrated_database_ends_up_identical_to_a_fresh_one` exists to catch.
_ADDED_DREAM_COLUMNS: tuple[tuple[str, str], ...] = (
    ("vault", "TEXT NOT NULL DEFAULT 'workbench'"),
    ("vault_entered_at", "TEXT NOT NULL DEFAULT ''"),
    ("conditions", "TEXT NOT NULL DEFAULT '[]'"),
    ("symbols", "TEXT NOT NULL DEFAULT '[]'"),
    ("asset_class_key", "TEXT NOT NULL DEFAULT ''"),
    ("wisp", "TEXT NOT NULL DEFAULT ''"),
    ("parents", "TEXT NOT NULL DEFAULT '[]'"),
    ("shared_hops", "TEXT NOT NULL DEFAULT '[]'"),
    ("verification_ceiling", "TEXT NOT NULL DEFAULT ''"),
    ("weakest_hop_index", "INTEGER"),
)


def _add_dream_columns(conn: sqlite3.Connection) -> None:
    """Bring a `dreams` table written by an older version up to the new shape.

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

    **It covers more than one generation of the table, and the backfill is the
    only part that does not.** A droplet database may be missing the vault
    columns, or have them and be missing the symbiosis ones; the loop adds
    whatever is absent in either case. The `vault_entered_at` sweep below runs
    on any open that added something, and is a no-op when the column was
    already populated — which is what makes adding a later column safe rather
    than a reason to guard it.

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
        "dreams_migrating_columns",
        added=added,
        detail=(
            "This dreams database was written by an older version. Adding the "
            "new columns in place; no existing row is modified except to "
            "backfill the vault clock from updated_at."
        ),
    )
    # Backfills the rows that were just migrated. It sits AFTER the `if not
    # added: return` above, so it runs only on the open that adds the columns —
    # the comment here used to describe an unconditional sweep that would also
    # catch a row written by an older binary against an already-migrated file,
    # and that case is unreachable from where the statement actually is. Left
    # where it is rather than moved: an older binary writing to a newer database
    # is not a state this deployment can reach, and a sweep on every open would
    # be a write on a path that should be a read.
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
            _add_dream_columns(conn)

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
        """Insert or update. Returns the row id.

        Note what this does NOT do: it does not move a dream between vaults, and
        it does not fuse one. `vault`, `vault_entered_at`, `parents` and
        `verification_ceiling` are all written from whatever the object carries,
        so a caller that mutated them by hand and saved would bypass every actor
        rule, cap and merge rule in `move` and `fuse`. That is why those rules
        live on the store — a save is for the contents of a dream, not for where
        it lives or where it came from.
        """
        with self._connect() as conn:
            if dream.id is None:
                dream.id = self._insert_dream(conn, dream)
            else:
                conn.execute(
                    "UPDATE dreams SET title=?, seed=?, stage=?, verdict=?,"
                    " weakest_hop=?, trigger_note=?, origin=?, chain=?, thoughts=?,"
                    " instruments=?, created_at=?, updated_at=?, vault=?,"
                    " vault_entered_at=?, conditions=?, symbols=?, asset_class_key=?,"
                    " wisp=?, parents=?, shared_hops=?, verification_ceiling=?,"
                    " weakest_hop_index=?"
                    " WHERE id=?",
                    (*_dream_payload(dream), dream.id),
                )
        return dream.id or 0

    @staticmethod
    def _insert_dream(conn: sqlite3.Connection, dream: Dream) -> int:
        """One INSERT, on a connection the caller owns.

        Split out of `save` so `fuse` can write the child, its parents' notes
        and nothing else in ONE transaction — the same reason `_apply_move` is
        split out of `move`. A child that survived while its parents' record of
        the fusion did not would be a dream whose reason nobody could find.
        """
        cursor = conn.execute(
            "INSERT INTO dreams (title, seed, stage, verdict, weakest_hop,"
            " trigger_note, origin, chain, thoughts, instruments, created_at,"
            " updated_at, vault, vault_entered_at, conditions, symbols,"
            " asset_class_key, wisp, parents, shared_hops, verification_ceiling,"
            " weakest_hop_index)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            _dream_payload(dream),
        )
        return int(cursor.lastrowid or 0)

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
    def _col(row: sqlite3.Row, name: str, default: object = "") -> object:
        """Read a column that may not be there yet.

        `sqlite3.Row` raises `IndexError` on an unknown key, so a store opened
        against a file some other process is still migrating would take a page
        down over a column rather than over data. The migration runs on every
        open and this should never fire; it costs one `try` and removes a class
        of failure whose symptom would be indistinguishable from corruption.
        """
        try:
            value = row[name]
        except IndexError:
            return default
        return default if value is None else value

    @classmethod
    def _to_dream(cls, row: sqlite3.Row) -> Dream | None:
        try:
            chain = [Hop.from_row(h) for h in json.loads(row["chain"] or "[]")]
            thoughts = [Thought.from_row(t) for t in json.loads(row["thoughts"] or "[]")]
            instruments = [str(i) for i in json.loads(row["instruments"] or "[]")]
            conditions = [
                DreamCondition.from_row(c)
                for c in json.loads(str(cls._col(row, "conditions", "[]")) or "[]")
            ]
            symbols = _symbols(json.loads(str(cls._col(row, "symbols", "[]")) or "[]"))
            parents = _ids(json.loads(str(cls._col(row, "parents", "[]")) or "[]"))
            shared = [
                str(h)
                for h in json.loads(str(cls._col(row, "shared_hops", "[]")) or "[]")
            ]
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

        # A row migrated from before the vaults has an empty clock only if the
        # backfill could not find an `updated_at` either, in which case the row
        # time is the best available reading and is what `_dt` returns.
        entered = cls._col(row, "vault_entered_at", "")
        return Dream(
            id=int(row["id"]),
            title=str(row["title"]),
            seed=str(row["seed"]),
            stage=_stage(row["stage"]),
            verdict=verdict,
            weakest_hop=str(row["weakest_hop"] or ""),
            # `None` on every row written before the pin existed, which reads as
            # "the dreamer did not say" and falls back to an exact claim match
            # on the prose. That is the honest answer: a row from before this
            # shipped genuinely does not record which hop its sentence meant.
            weakest_hop_index=_hop_index(cls._col(row, "weakest_hop_index", None)),
            trigger=str(row["trigger_note"] or ""),
            origin=str(row["origin"] or ""),
            chain=chain,
            thoughts=thoughts,
            instruments=instruments,
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
            vault=_vault(cls._col(row, "vault", str(Vault.WORKBENCH))),
            vault_entered_at=_dt(entered if entered else row["updated_at"]),
            conditions=conditions,
            symbols=symbols,
            asset_class_key=str(cls._col(row, "asset_class_key", "")),
            wisp=str(cls._col(row, "wisp", "")),
            parents=parents,
            shared_hops=shared,
            verification_ceiling=_verification(cls._col(row, "verification_ceiling", "")),
        )

    # ------------------------------------------------------------- the vaults

    def in_vault(self, vault: Vault, limit: int = 50) -> list[Dream]:
        """Everything on one shelf, most recently ARRIVED first.

        **`vault_entered_at`, not `updated_at`, and the difference is who gets
        starved.** `confer.run` reverses this list to answer the longest-waiting
        offer first, precisely so a productive dreamer cannot bury its own older
        offers. Ordered by `updated_at` that reversal answered the least
        recently EDITED first, which is a different dream: one vaulted 150 days
        ago and touched this morning sorted as the newest thing on the shelf and
        went to the back of the queue — the exact starvation the reversal exists
        to prevent, with the code that prevents it still in place.

        Two clocks, one meaning each: `vault_entered_at` is how long it has been
        waiting here, `updated_at` is when it last changed. Waiting is the
        question this answers. `id` breaks a tie, so two dreams shelved in the
        same second have a stable order rather than SQLite's.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM dreams WHERE vault=?"
                " ORDER BY vault_entered_at DESC, id DESC LIMIT ?",
                (str(vault), limit),
            ).fetchall()
        return [d for d in (self._to_dream(r) for r in rows) if d is not None]

    def counts_by_vault(self) -> dict[Vault, int]:
        """How many dreams are on each shelf.

        **Every vault is a key, including the empty ones.** A missing key reads
        as "no data" to a renderer and a zero reads as a stated fact, and the
        difference matters most for ADOPTED: a page showing nothing where the
        adopted count should be looks the same whether nothing is adopted or the
        query failed. Same reason `stop_watch` puts a breach count of zero on
        every `cycle_complete` line instead of staying quiet.
        """
        counts = dict.fromkeys(Vault, 0)
        with self._connect() as conn:
            for row in conn.execute("SELECT vault, COUNT(*) n FROM dreams GROUP BY vault"):
                counts[_vault(row["vault"])] += int(row["n"])
        return counts

    def move(
        self,
        dream_id: int,
        to: Vault,
        *,
        by: str,
        reason: str = "",
        at: datetime | None = None,
        caps: VaultCaps | None = None,
    ) -> MoveResult:
        """Move a dream between vaults, enforcing who may do what.

        The actor rules, which are the point of this method existing at all
        rather than callers setting `dream.vault` and saving:

        - **The dreamer** may move freely between WORKBENCH, PROPHECY, VAULT and
          ARCHIVE. It may not move anything into ADOPTED — adoption is the
          trading agent taking something, and a dreamer that could adopt on the
          agent's behalf would be granting itself a symbol permission. It may
          not move anything OUT of ADOPTED either: once taken, the dreamer holds
          a wisp, and pulling a dream back out from under a live grant would
          leave the permission pointing at a row nobody is watching.
        - **The trading agent** may do exactly two things, and both are their own
          method: `adopt` (VAULT → ADOPTED) and `return_to_vault` (ADOPTED →
          VAULT, with a reason). It may never delete and may never move a dream
          anywhere else. Calling this directly as the trader is refused with
          `FORBIDDEN_ACTOR`, which points at the two methods that are allowed.
        - **Anyone else**, including the operator and any unrecognised name, is
          refused. Failing open on an authorisation question is how a rule that
          is the entire reason for the method stops applying, and the operator
          already has the file and the CLI.

        Refusals are collected rather than short-circuited and nothing raises.
        See `MoveResult`.

        A move to the vault a dream is already in is refused rather than
        silently succeeding, because the alternative is a caller with a refresh
        loop resetting `vault_entered_at` on every pass and nothing ever ageing
        out.
        """
        stamp = at or datetime.now(UTC)
        limits = caps or DEFAULT_CAPS

        dream = self.get(dream_id)
        if dream is None:
            return _refused(
                dream_id,
                [MoveRefusal.NOT_FOUND],
                [f"No dream with id {dream_id}."],
                moved_to=to,
            )

        refusals: list[MoveRefusal] = []
        details: list[str] = []

        if by == TRADER:
            refusals.append(MoveRefusal.FORBIDDEN_ACTOR)
            details.append(
                "The trading agent moves dreams with adopt() and "
                "return_to_vault(), not with move(). It may take a dream out of "
                "the vault and hand it back with a reason; nothing else."
            )
        elif by != DREAMER:
            refusals.append(MoveRefusal.FORBIDDEN_ACTOR)
            details.append(
                f"'{by}' is not a mover this store recognises. Only the dreamer "
                "moves dreams between shelves."
            )
        else:
            if dream.vault not in DREAMER_VAULTS:
                refusals.append(MoveRefusal.FORBIDDEN_ACTOR)
                details.append(
                    f"This dream is in {dream.vault}, which the dreamer does not "
                    "hold. The trading agent has it; it comes back with "
                    "return_to_vault()."
                )
            if to not in DREAMER_VAULTS:
                refusals.append(MoveRefusal.FORBIDDEN_ACTOR)
                details.append(
                    f"The dreamer cannot move a dream into {to}. Adoption is the "
                    "trading agent taking something, not the dreamer offering it."
                )

        if dream.vault is to:
            refusals.append(MoveRefusal.ALREADY_THERE)
            details.append(
                f"Already in {to}, so nothing moved and the expiry clock was not "
                "reset."
            )

        full = self._is_full(to, limits, now=stamp, exclude=dream_id)
        if full is not None:
            refusals.append(MoveRefusal.FULL)
            details.append(full)

        if refusals:
            return _refused(
                dream_id, refusals, details, moved_from=dream.vault, moved_to=to
            )

        return self._commit_move(dream, to, at=stamp, reason=reason)

    def promote(
        self,
        dream_id: int,
        *,
        at: datetime | None = None,
        caps: VaultCaps | None = None,
    ) -> MoveResult:
        """Apply `promotion_for` and move the dream if it says to.

        The one thing that was missing between the dreamer and the vault. The
        rule itself is pure and lives in `promotion_for`; this is the part that
        needs a database, the caps and a clock, and it is kept thin so that
        "what promotes a dream" is answerable by reading one function.

        Goes through `move` with `by=DREAMER` rather than writing the row
        itself, so promotion inherits every actor rule, the cap check and the
        `vault_entered_at` reset for free. A promotion is the dreamer shelving
        its own work; it is emphatically NOT adoption, which is the trading
        agent taking something and stays its own method.

        **A refusal here is ordinary.** A dream still being worked on, a full
        prophecy shelf, a dream already where it belongs — all come back as
        `MoveResult.refused` with the reason, and no caller has to treat any of
        them as an error. Safe to call on every dream on every step: the rule is
        a pure function of the dream, so re-running it changes nothing.
        """
        dream = self.get(dream_id)
        if dream is None:
            return _refused(
                dream_id, [MoveRefusal.NOT_FOUND], [f"No dream with id {dream_id}."]
            )

        decision = promotion_for(dream)
        if decision.to is None:
            return _refused(
                dream_id,
                [MoveRefusal.NOT_PROMOTABLE],
                [decision.reason],
                moved_from=dream.vault,
            )
        return self.move(
            dream_id,
            decision.to,
            by=DREAMER,
            reason=decision.reason,
            at=at,
            caps=caps,
        )

    # ------------------------------------------------------------- symbiosis

    def fuse(
        self,
        parent_ids: Sequence[int],
        *,
        by: str,
        title: str = "",
        seed: str = "",
        thought: str = "",
        symbols: Sequence[str] | None = None,
        origin: str = "",
        at: datetime | None = None,
        caps: VaultCaps | None = None,
    ) -> FusionResult:
        """Combine two or three dreams into a child. **The parents survive.**

        Nothing is consumed and nothing is deleted. The child carries
        `parents`, the parents keep everything they had, and the shared hops are
        named on the child because the overlap is the reason the fusion exists.
        A fused dream that ate its sources would destroy the ability to attack
        either of them, which is the activity this whole module is for.

        The rules, and every refusal is typed and collected rather than raised:

        - **Only the dreamer.** Same actor table as `move`: the trading agent
          has `adopt` and `return_to_vault` and nothing else, and an
          unrecognised name is refused rather than waved through.
        - **Two or three parents.** One is not a fusion; four is a hop so
          general that the argument stops meaning anything.
        - **No ADOPTED parent.** A live grant points at that row and the
          trading agent is holding it — the same reason the dreamer may neither
          move nor delete one. It comes back with `return_to_vault` first.
        - **The workbench cap applies**, because a fusion is a dream on the
          workbench like any other and the cap is about what a person can hold
          in their head.
        - **`symbols` may only NARROW the union.** The `adopt` lock in a second
          place and for the same reason: the child must not be able to claim a
          symbol no parent did, or a fusion becomes a route round the class
          hard-block. `plan_fusion` resolves the class only when the parents
          agree, so a mixed-class fusion grants nothing.

        The merge itself is `plan_fusion`, which is pure and testable without a
        database. This is the half that needs a connection, the caps and a
        clock — the same split as `promotion_for` and `promote`.

        **One transaction.** The child, its opening thought and the note in each
        parent's transcript are one event; a child written without the parents'
        record of it would be a dream whose reason nobody could find.
        """
        stamp = _as_utc(at or datetime.now(UTC))
        limits = caps or DEFAULT_CAPS
        wanted = _ids(list(parent_ids))

        refusals: list[MoveRefusal] = []
        details: list[str] = []

        if by == TRADER:
            refusals.append(MoveRefusal.FORBIDDEN_ACTOR)
            details.append(
                "The trading agent does not fuse dreams. It may adopt one out "
                "of the vault and hand it back with a reason; combining chains "
                "is the dreamer's work."
            )
        elif by != DREAMER:
            refusals.append(MoveRefusal.FORBIDDEN_ACTOR)
            details.append(f"'{by}' is not allowed to fuse dreams.")

        if len(wanted) < MIN_FUSION_PARENTS:
            refusals.append(MoveRefusal.NEEDS_PARENTS)
            details.append(
                f"A fusion needs {MIN_FUSION_PARENTS} or {MAX_FUSION_PARENTS} "
                f"distinct dreams and was given {len(wanted)}. One dream is not "
                "a fusion; it is the dream you already have."
            )
        elif len(wanted) > MAX_FUSION_PARENTS:
            refusals.append(MoveRefusal.TOO_MANY_PARENTS)
            details.append(
                f"{len(wanted)} parents is more than {MAX_FUSION_PARENTS}. A hop "
                "that many separate chains reach is a truism rather than a "
                "mechanism, and the shared-hop argument stops meaning anything."
            )

        parents: list[Dream] = []
        missing: list[int] = []
        adopted: list[int] = []
        for dream_id in wanted:
            dream = self.get(dream_id)
            if dream is None:
                missing.append(dream_id)
                continue
            if dream.vault is Vault.ADOPTED:
                adopted.append(dream_id)
            parents.append(dream)

        if missing:
            refusals.append(MoveRefusal.NOT_FOUND)
            details.append(
                f"No dream with id {', '.join(str(i) for i in missing)}."
            )
        if adopted:
            refusals.append(MoveRefusal.PARENT_ADOPTED)
            details.append(
                f"Dream {', '.join(str(i) for i in adopted)} is on the adopted "
                "shelf, so a live grant points at it. Hand it back with "
                "return_to_vault() before fusing it into anything."
            )

        plan = plan_fusion(parents) if parents else Fusion()
        granted = tuple(_symbols(list(symbols))) if symbols is not None else plan.symbols
        overreach = [s for s in granted if s not in plan.symbols]
        if overreach:
            refusals.append(MoveRefusal.SYMBOLS_NOT_OFFERED)
            details.append(
                f"The parents do not claim {', '.join(overreach)}. A fusion "
                "unions what its parents already claimed, or narrows it; a "
                "symbol arriving here from nowhere would be a permission with "
                "no argument behind it."
            )

        with self._connect() as conn:
            full = self._is_full(Vault.WORKBENCH, limits, now=stamp, conn=conn)
            if full is not None:
                refusals.append(MoveRefusal.FULL)
                details.append(full)

            if refusals:
                return _refused_fusion(tuple(wanted), refusals, details)

            child = Dream(
                title=_trim(title) or _fusion_title(parents),
                seed=_trim(seed) or _fusion_seed(plan),
                # EXPLORE rather than SEED: the chain exists from the first
                # moment. And no verdict, so `promotion_for` keeps it on the
                # workbench until the dreamer has actually worked it — a fusion
                # must be harder to promote than either parent, not easier.
                stage=DreamStage.EXPLORE,
                chain=list(plan.chain),
                conditions=list(plan.conditions),
                symbols=list(granted),
                asset_class_key=plan.asset_class_key,
                instruments=list(plan.instruments),
                origin=_trim(origin) or "fused from " + _fusion_title(parents),
                parents=list(plan.parents),
                shared_hops=list(plan.shared_hops),
                verification_ceiling=plan.verification_ceiling,
                created_at=stamp,
                updated_at=stamp,
                vault_entered_at=stamp,
                # Deliberately NOT carried across from either parent. Nobody has
                # attacked the fusion yet, and inheriting a parent's weakest hop
                # would claim the combined chain had been examined when it has
                # not. `DreamLedger.unattacked` counting it is the honest result.
                #
                # The index goes with the sentence, and stating it here rather
                # than leaning on the field default is the point: a number
                # carried across would survive the renumbering the union does to
                # the chain and pin the rule to whichever link landed in that
                # slot. `promotion_for` refuses a fusion on its VERDICT long
                # before it looks at either, so nothing is made unpromotable by
                # this pair being empty.
                weakest_hop="",
                weakest_hop_index=None,
            )
            child.thoughts = [
                Thought(
                    stage=DreamStage.EXPLORE,
                    text=_trim(thought) or _fusion_seed(plan),
                    at=stamp,
                    by=DREAMER,
                )
            ]
            child.id = self._insert_dream(conn, child)

            for parent in parents:
                self._insert_message(
                    conn,
                    int(parent.id or 0),
                    speaker=FUSION,
                    kind="fusion",
                    text=(
                        f"Fused into dream {child.id} — {child.title} — with "
                        f"{', '.join(str(i) for i in plan.parents if i != parent.id)}. "
                        "This dream is unchanged and still yours; the fusion is "
                        "a new chain that carries both."
                    ),
                    at=stamp,
                )

        log.info(
            "dreams_fused",
            dream_id=child.id,
            parents=list(plan.parents),
            shared_hops=len(plan.shared_hops),
            hops=len(plan.chain),
            symbols=list(granted),
            # Named rather than left to be read off the chain, because "the
            # child is no better evidenced than its worst parent" is the
            # property a reader has to be able to check.
            verification=str(child.verification),
            verification_ceiling=str(plan.verification_ceiling),
        )
        return FusionResult(
            ok=True,
            dream_id=child.id,
            parents=plan.parents,
            shared_hops=plan.shared_hops,
            detail=(
                f"Fused {len(parents)} dreams into {child.id}: "
                f"{len(plan.chain)} hop(s), {len(plan.shared_hops)} shared, "
                f"verification {child.verification} (no better than the weakest "
                "parent)."
            ),
        )

    def children_of(self, dream_id: int) -> list[int]:
        """Which fusions this dream is a parent of. Derived, never stored.

        A back-reference written on the parent would be a second copy of one
        fact, and two copies of a fact eventually disagree — the reasoning that
        keeps `Adoption.is_live` computed rather than flagged, and that a second
        SQL answer to "is this grant live" already cost once. `parents` on the
        child is the single stored half; this is the query that reads it the
        other way round.

        Scans, because `parents` is JSON in a TEXT column and a LIKE over it
        would match id 1 inside id 12. The store holds tens of dreams, not
        millions, and a correct scan beats a fast wrong answer.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, parents FROM dreams ORDER BY id"
            ).fetchall()
        out: list[int] = []
        for row in rows:
            try:
                parents = _ids(json.loads(str(row["parents"] or "[]")))
            except (json.JSONDecodeError, TypeError):
                # Counted nowhere and skipped, like every other unreadable row
                # here: one bad `parents` column must not take down the page
                # that renders the family.
                log.warning("dream_parents_unreadable", dream_id=row["id"])
                continue
            if dream_id in parents:
                out.append(int(row["id"]))
        return out

    def grade(
        self,
        dream_id: int,
        later: Sequence[CycleReadings],
        *,
        at: datetime | None = None,
    ) -> ConditionGrading:
        """Check a dream's conditions against recorded readings and store what fired.

        Writes only when something actually changed, so a prophecy nobody's
        figures have reached costs one read and no write. `updated_at` moves
        when a condition fires and **`vault_entered_at` never does** — the
        expiry clock belongs to the shelf, and a condition firing is not the
        dream entering the prophecy shelf again.

        Failures are swallowed to an ungraded result rather than raised. This
        runs on a scheduled command beside the model call, and a store error
        here must cost the grading and not the dream that was just written.
        """
        dream = self.get(dream_id)
        if dream is None:
            return ConditionGrading(dream_id=dream_id)

        grading = grade_conditions(dream, later)
        if not grading.changed:
            return grading

        stamp = _iso_utc(at or datetime.now(UTC))
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE dreams SET conditions=?, updated_at=? WHERE id=?",
                    (
                        json.dumps([c.to_row() for c in grading.conditions]),
                        stamp,
                        dream_id,
                    ),
                )
        except sqlite3.Error as exc:
            log.warning(
                "dream_condition_grading_not_stored",
                dream_id=dream_id,
                error=f"{type(exc).__name__}: {exc}",
                detail=(
                    "The conditions that fired were not written back, so this "
                    "dream will be graded again next run. Nothing was promoted "
                    "on the strength of a grading that is not on disk."
                ),
            )
            return ConditionGrading(
                dream_id=dream_id,
                conditions=tuple(dream.conditions),
                ungradeable=grading.ungradeable,
                cycles_checked=grading.cycles_checked,
            )

        log.info(
            "dream_conditions_fulfilled",
            dream_id=dream_id,
            fired=list(grading.newly_fulfilled),
            still_unmet=sum(1 for c in grading.conditions if not c.fulfilled),
            cycles_checked=grading.cycles_checked,
        )
        return grading

    def has_room(
        self, vault: Vault, *, now: datetime, caps: VaultCaps | None = None
    ) -> bool:
        """Whether one more dream would fit on `vault` right now.

        The same arithmetic `move` and `adopt` refuse on, exposed so a caller
        can ask BEFORE spending a model call — `confer` re-tests a dream whose
        adoption the shelf refused, and asking that question a second way would
        eventually give a second answer.
        """
        return self._is_full(vault, caps or DEFAULT_CAPS, now=now) is None

    def _is_full(
        self,
        to: Vault,
        caps: VaultCaps,
        *,
        now: datetime,
        exclude: int | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> str | None:
        """The sentence to report if `to` has no room, else `None`.

        `exclude` keeps a dream from counting against the vault it is moving
        into, which cannot happen through `move` — it refuses a move to the
        vault a dream is already in — but would be a nasty off-by-one if some
        later caller retried a partially-applied move.

        **ADOPTED counts LIVE GRANTS, not rows on the shelf**, and the two stop
        agreeing the moment a grant expires. The cap is there because
        `caps.adopted` matches `account.max_concurrent_positions`: an adoption
        is a promise the trading agent may act on it, so a fourth would be a
        promise the account has no slot to keep. A dream whose grant lapsed is
        no longer such a promise, and counting it bricked the shelf — three
        expiries and nothing could be adopted again, with `delete` refusing an
        ADOPTED dream, `move` refusing every actor, and `return_to_vault` the
        only exit. `electrum-bot vault-expire` hands those back; until it runs,
        they no longer occupy a slot they are not using.

        `conn` lets a caller do this check inside its own transaction, which is
        what closes the gap between reading the count and writing the row.
        """
        limit = caps.limit_for(to)
        if limit is None:
            return None
        if conn is not None:
            held = self._count_toward(to, now=now, exclude=exclude, conn=conn)
        else:
            with self._connect() as owned:
                held = self._count_toward(to, now=now, exclude=exclude, conn=owned)
        if held < limit:
            return None
        return (
            f"{to} is full: {held} of {limit}. Archive or retire something "
            "there first — the cap is about what a person can hold in their "
            "head, not about risk."
        )

    def _count_toward(
        self,
        to: Vault,
        *,
        now: datetime,
        exclude: int | None,
        conn: sqlite3.Connection,
    ) -> int:
        """How many dreams count against `to`'s cap. See `_is_full`."""
        rows = conn.execute(
            "SELECT id FROM dreams WHERE vault=? AND id IS NOT ?",
            (str(to), exclude),
        ).fetchall()
        if to is not Vault.ADOPTED:
            return len(rows)
        held = 0
        for row in rows:
            if any(
                a.is_live(now)
                for a in self._adoptions_on(conn, int(row["id"]))
            ):
                held += 1
        return held

    def _adoptions_on(
        self, conn: sqlite3.Connection, dream_id: int
    ) -> list[Adoption]:
        """Every adoption row for one dream, on a connection the caller owns."""
        rows = conn.execute(
            "SELECT * FROM adoptions WHERE dream_id=? ORDER BY adopted_at DESC, id DESC",
            (dream_id,),
        ).fetchall()
        return [a for a in (_to_adoption(r) for r in rows) if a is not None]

    def _commit_move(
        self, dream: Dream, to: Vault, *, at: datetime, reason: str
    ) -> MoveResult:
        """Write the move on its own connection. No checking; callers have done it."""
        with self._connect() as conn:
            return self._apply_move(conn, dream, to, at=at, reason=reason)

    def _apply_move(
        self,
        conn: sqlite3.Connection,
        dream: Dream,
        to: Vault,
        *,
        at: datetime,
        reason: str,
    ) -> MoveResult:
        """The move itself, on a connection the caller owns.

        Split out so `adopt` can put the grant, the transcript entry and the
        move in one transaction. A grant that survived while the move did not
        was a permission attached to a dream still sitting in the vault.
        """
        was = dream.vault
        stamp = _iso_utc(at)
        conn.execute(
            "UPDATE dreams SET vault=?, vault_entered_at=?, updated_at=? WHERE id=?",
            (str(to), stamp, stamp, dream.id),
        )
        log.info(
            "dream_moved",
            dream_id=dream.id,
            moved_from=str(was),
            moved_to=str(to),
            reason=_trim(reason),
        )
        return MoveResult(
            ok=True,
            dream_id=int(dream.id or 0),
            moved_from=was,
            moved_to=to,
            detail=_trim(reason),
        )

    def delete(self, dream_id: int, *, by: str) -> MoveResult:
        """Remove a dream and everything hanging off it.

        Refused for the trading agent, always. Deleting is how a record of a
        disagreement disappears, and the agent with a route to the broker is the
        one that must not be able to do that — the same asymmetry as the whole
        move table above.

        Refused for an ADOPTED dream whoever asks, because a live grant whose
        dream has been deleted is a symbol permission nobody can explain.
        `granted_symbols` joins back to `dreams` and to the ADOPTED shelf as a
        second lock on that, but the first lock is not creating the state at all.

        **It stays refused once the grant has EXPIRED, and the refusal names the
        way out.** An audit read this as a shelf that bricks itself — three
        expired adoptions and nothing can be adopted again — and the cap was the
        real fault there, not this: `_is_full` counts live grants now, so an
        expired adoption occupies no slot. Deleting would still be the wrong
        exit, because the adoption rows go with the dream and those rows are the
        only record that a permission existed and what it covered. The exit is
        `return_to_vault`, which closes the grant and keeps the history, and
        `electrum-bot vault-expire` does it unprompted for anything lapsed.

        The messages and adoption rows go with it. An adoption row with no dream
        behind it is a record of a permission that cannot be read, which is
        worse than not keeping it — and the dream being deleted is, by the rule
        above, one that was never taken while it mattered.
        """
        if by == TRADER:
            return _refused(
                dream_id,
                [MoveRefusal.FORBIDDEN_ACTOR],
                [
                    "The trading agent cannot delete a dream. Hand it back to "
                    "the vault with a reason instead; the dreamer decides what "
                    "is retired."
                ],
            )
        if by != DREAMER and by != OPERATOR:
            return _refused(
                dream_id,
                [MoveRefusal.FORBIDDEN_ACTOR],
                [f"'{by}' is not allowed to delete dreams."],
            )

        dream = self.get(dream_id)
        if dream is None:
            return _refused(
                dream_id, [MoveRefusal.NOT_FOUND], [f"No dream with id {dream_id}."]
            )
        if dream.vault is Vault.ADOPTED:
            return _refused(
                dream_id,
                [MoveRefusal.FORBIDDEN_ACTOR],
                [
                    "This dream is on the adopted shelf, so an adoption row "
                    "points at it. Hand it back with return_to_vault(), which "
                    "closes the grant and keeps the record of what it covered — "
                    "`electrum-bot vault-expire` does that for anything already "
                    "lapsed. Then it can be retired."
                ],
                moved_from=dream.vault,
            )

        with self._connect() as conn:
            conn.execute("DELETE FROM dream_messages WHERE dream_id=?", (dream_id,))
            conn.execute("DELETE FROM adoptions WHERE dream_id=?", (dream_id,))
            conn.execute("DELETE FROM dreams WHERE id=?", (dream_id,))
        log.info("dream_deleted", dream_id=dream_id, by=by, vault=str(dream.vault))
        return MoveResult(ok=True, dream_id=dream_id, moved_from=dream.vault)

    # ------------------------------------------------------------- the talking

    def add_message(
        self,
        dream_id: int,
        *,
        speaker: str,
        text: str,
        kind: str = "note",
        at: datetime | None = None,
    ) -> DreamMessage:
        """Append one turn of the conversation. Never overwrites.

        Append-only for the audit log's reason: the interesting part of a
        negotiation is where somebody changed their mind. Prose is trimmed
        rather than rejected, because a message that would not store is a turn
        of the conversation lost to a character count.

        Not validated against `MESSAGE_KINDS` or the known speakers. An
        unfamiliar kind costs a page a badge; refusing it would lose the turn.
        """
        with self._connect() as conn:
            return self._insert_message(
                conn, dream_id, speaker=speaker, text=text, kind=kind, at=at
            )

    def _insert_message(
        self,
        conn: sqlite3.Connection,
        dream_id: int,
        *,
        speaker: str,
        text: str,
        kind: str = "note",
        at: datetime | None = None,
    ) -> DreamMessage:
        """One turn, on a connection the caller owns. See `add_message`."""
        stamp = _as_utc(at or datetime.now(UTC))
        trimmed = _trim(text)
        cursor = conn.execute(
            "INSERT INTO dream_messages (dream_id, at, speaker, kind, text)"
            " VALUES (?,?,?,?,?)",
            (dream_id, _iso_utc(stamp), speaker, kind, trimmed),
        )
        return DreamMessage(
            dream_id=dream_id,
            at=stamp,
            speaker=speaker,
            text=trimmed,
            kind=kind,
            id=int(cursor.lastrowid or 0),
        )

    def messages(self, dream_id: int, limit: int = 200) -> list[DreamMessage]:
        """The conversation on one dream, oldest first.

        Oldest first because this is a transcript rather than a feed: a
        negotiation read newest-first is a negotiation read backwards.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM dream_messages WHERE dream_id=? ORDER BY at, id LIMIT ?",
                (dream_id, limit),
            ).fetchall()
        return [
            DreamMessage(
                id=int(r["id"]),
                dream_id=int(r["dream_id"]),
                at=_dt(r["at"]),
                speaker=str(r["speaker"]),
                kind=str(r["kind"] or "note"),
                text=str(r["text"] or ""),
            )
            for r in rows
        ]

    # ------------------------------------------------------------- the handover

    def adopt(
        self,
        dream_id: int,
        *,
        symbols: list[str] | None = None,
        asset_class: str = "",
        at: datetime | None = None,
        ttl_days: int | None = None,
        wisp: str = "",
        caps: VaultCaps | None = None,
    ) -> MoveResult:
        """The trading agent takes a dream out of the vault.

        Writes the `adoptions` row, moves the dream to ADOPTED and leaves the
        dreamer a wisp. One call, because the three are one event: an adoption
        row with the dream still sitting in the vault would be a grant nobody
        could see, and a dream in ADOPTED with no row would be a grant that
        grants nothing.

        `symbols` and `asset_class` default to the dream's own claim, which is
        the offer as it was made. They are then **copied onto the adoption row
        and never read back off the dream**, because the dream can be edited
        afterwards and a permission has to be a fixed record of what was agreed.
        Same reasoning as the journal recording the proposal rather than
        re-deriving it later.

        **An override may only ever NARROW the offer, and that is the lock.**
        Both arguments used to be taken as given, so a dream carrying no symbols
        and no class could be adopted with invented ones: every dream in the
        vault was a blank cheque, and `mcp_server.adopt_dream` exposes both
        fields to a model. The check lives here rather than in the caller
        precisely because there is more than one caller and there will be more
        — a tool, a conference, an operator command — and a rule enforced in a
        caller is a rule the next caller does not have. Asking for a symbol the
        dream does not claim is `SYMBOLS_NOT_OFFERED`; asking for a different
        class is `CLASS_NOT_OFFERED`. Taking two of the three symbols a dream
        offers is fine: that is the trading agent being careful.

        Refused when the dream is not in VAULT — the only shelf the trading
        agent can see — when it names no symbols, when its class is unresolved,
        and when the adopted vault is full. The last of those matters more than
        it looks: `VaultCaps.adopted` is set to match `max_concurrent_positions`,
        so a fourth adoption would be a promise the account has no slot to keep.

        An empty `asset_class` is refused here rather than allowed through to
        `granted_symbols`, which drops it. Both checks are deliberate and the
        second is the lock that matters — the same arrangement as `mode=ro` plus
        the statement guard in `insight.py`, where the first check is the useful
        error message and the second is the guarantee.

        **All four writes go through ONE connection in ONE transaction**, which
        is what makes the first paragraph true. They used to be three separate
        connections, so an interruption between them left a live grant on a
        dream still sitting in the vault — a permission no page describes,
        which `return_to_vault` then refuses to close with `WRONG_VAULT`,
        leaving deletion as the only exit. The full check moved inside the same
        transaction with them, closing the gap between reading the count and
        writing the row.
        """
        stamp = _as_utc(at or datetime.now(UTC))
        limits = caps or DEFAULT_CAPS

        dream = self.get(dream_id)
        if dream is None:
            return _refused(
                dream_id,
                [MoveRefusal.NOT_FOUND],
                [f"No dream with id {dream_id}."],
                moved_to=Vault.ADOPTED,
            )

        refusals: list[MoveRefusal] = []
        details: list[str] = []

        if dream.vault is not Vault.VAULT:
            refusals.append(MoveRefusal.WRONG_VAULT)
            details.append(
                f"Only a dream in {Vault.VAULT} can be adopted; this one is in "
                f"{dream.vault}. The vault is the only shelf the trading agent "
                "can see."
            )

        offered = _symbols(dream.symbols)
        granted = _symbols(symbols) if symbols is not None else offered
        overreach = [s for s in granted if s not in offered]
        if overreach:
            refusals.append(MoveRefusal.SYMBOLS_NOT_OFFERED)
            details.append(
                f"This dream does not offer {', '.join(overreach)}. An adoption "
                f"grants what the dream claims — {', '.join(offered) or 'nothing'}"
                " — or some of it, never something else. A grant is the argument "
                "the dreamer won, not a blank permission with a dream id on it."
            )
        if not granted:
            refusals.append(MoveRefusal.NEEDS_SYMBOLS)
            details.append(
                "An adoption with no symbols is a permission that permits "
                "nothing, and it would read on the page as a grant. Name the "
                "symbols or leave the dream in the vault."
            )

        claimed_class = dream.asset_class_key.strip()
        asked_class = asset_class.strip()
        if asked_class and asked_class != claimed_class:
            refusals.append(MoveRefusal.CLASS_NOT_OFFERED)
            details.append(
                f"This dream claims the instrument class "
                f"'{claimed_class or 'none'}', not '{asked_class}'. The class "
                "decides which limits the granted symbols face, so choosing a "
                "different one at adoption is choosing the caps rather than "
                "accepting them."
            )
        resolved_class = claimed_class
        if not resolved_class:
            refusals.append(MoveRefusal.NEEDS_ASSET_CLASS)
            details.append(
                "The instrument class is unresolved, so nothing can say which "
                "limits apply to these symbols. Set it on the dream, to a key "
                "from the instruments block of config/rules.yaml."
            )

        days = ttl_days if ttl_days is not None else DEFAULT_TTLS.adopted
        expires = stamp + timedelta(days=days)
        trace = _trim(wisp) or _default_wisp(dream, granted, stamp)

        with self._connect() as conn:
            full = self._is_full(
                Vault.ADOPTED, limits, now=stamp, exclude=dream_id, conn=conn
            )
            if full is not None:
                refusals.append(MoveRefusal.FULL)
                details.append(full)

            if refusals:
                return _refused(
                    dream_id,
                    refusals,
                    details,
                    moved_from=dream.vault,
                    moved_to=Vault.ADOPTED,
                )

            conn.execute(
                "INSERT INTO adoptions (dream_id, adopted_at, symbols_granted,"
                " asset_class, expires_at) VALUES (?,?,?,?,?)",
                (
                    dream_id,
                    _iso_utc(stamp),
                    json.dumps(granted),
                    resolved_class,
                    _iso_utc(expires),
                ),
            )
            conn.execute(
                "UPDATE dreams SET wisp=? WHERE id=?",
                (trace, dream_id),
            )
            # Written by the store rather than left to the caller, so the
            # transcript is complete even when neither agent thought to narrate
            # the handover — and written on THIS connection, so a grant cannot
            # exist without the move that makes it visible.
            self._insert_message(
                conn,
                dream_id,
                speaker=TRADER,
                kind="accept",
                text=(
                    f"Adopted {', '.join(granted)} ({resolved_class}) until "
                    f"{expires:%d %b %Y}."
                ),
                at=stamp,
            )
            result = self._apply_move(
                conn, dream, Vault.ADOPTED, at=stamp, reason="adopted"
            )

        log.info(
            "dream_adopted",
            dream_id=dream_id,
            symbols=granted,
            asset_class=resolved_class,
            expires_at=_iso_utc(expires),
        )
        return result

    def return_to_vault(
        self, dream_id: int, *, reason: str, at: datetime | None = None
    ) -> MoveResult:
        """The trading agent hands an adopted dream back, saying why.

        **A blank reason is refused**, and that is the only thing standing
        between this and a silent drop. An adoption is an argument the dreamer
        won; giving it back without a word is the argument being reversed with
        no record, and the record is most of what the Dreaming page is for. It
        does not have to be a good reason — nothing here judges it — it has to
        exist.

        Closes the live adoption rather than deleting it, so the history of what
        was granted and for how long stays readable, and clears the wisp,
        because the dream is the dreamer's again and a trace of something it now
        holds whole would be misleading.
        """
        stamp = _as_utc(at or datetime.now(UTC))
        clean = _trim(reason)

        dream = self.get(dream_id)
        if dream is None:
            return _refused(
                dream_id,
                [MoveRefusal.NOT_FOUND],
                [f"No dream with id {dream_id}."],
                moved_to=Vault.VAULT,
            )

        refusals: list[MoveRefusal] = []
        details: list[str] = []
        if not clean:
            refusals.append(MoveRefusal.NEEDS_REASON)
            details.append(
                "Handing a dream back needs a stated reason. It does not have to "
                "be a good one, but a return with no record is an argument "
                "reversed silently."
            )
        if dream.vault is not Vault.ADOPTED:
            refusals.append(MoveRefusal.WRONG_VAULT)
            details.append(
                f"This dream is in {dream.vault}, not {Vault.ADOPTED}, so there "
                "is nothing to hand back."
            )
        if refusals:
            return _refused(
                dream_id, refusals, details, moved_from=dream.vault, moved_to=Vault.VAULT
            )

        # One transaction, for the mirror of `adopt`'s reason: closing the grant
        # without moving the dream would leave a dream in ADOPTED that grants
        # nothing, and moving it without closing the grant would leave a live
        # permission on a dream sitting back in the vault.
        with self._connect() as conn:
            conn.execute(
                "UPDATE adoptions SET returned_at=?, return_reason=? WHERE dream_id=?"
                " AND returned_at IS NULL",
                (_iso_utc(stamp), clean, dream_id),
            )
            conn.execute("UPDATE dreams SET wisp='' WHERE id=?", (dream_id,))
            self._insert_message(
                conn, dream_id, speaker=TRADER, kind="return", text=clean, at=stamp
            )
            result = self._apply_move(
                conn, dream, Vault.VAULT, at=stamp, reason=clean
            )
        log.info("dream_returned", dream_id=dream_id, reason=clean)
        return result

    def adoptions(self, dream_id: int | None = None) -> list[Adoption]:
        """Every adoption, or every adoption of one dream. Newest first."""
        sql = "SELECT * FROM adoptions"
        params: tuple[object, ...] = ()
        if dream_id is not None:
            sql += " WHERE dream_id=?"
            params = (dream_id,)
        sql += " ORDER BY adopted_at DESC, id DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        out: list[Adoption] = []
        for row in rows:
            parsed = _to_adoption(row)
            if parsed is not None:
                out.append(parsed)
        return out

    def granted_symbols(self, now: datetime) -> dict[str, str]:
        """Which symbols are permitted right now, and under which class key.

        **Written for the risk path that will consume it.** Read this before
        wiring it to anything that decides what may be traded:

        - **Deterministic given `now`.** No clock of its own, no network, no
          cache. Two calls with the same `now` over the same file return the
          same mapping — the same property `RiskGate` is built around, for the
          same reason.
        - **Live only, and `Adoption.is_live` is what decides.** An adoption
          handed back, expired, or carrying no expiry at all is excluded. This
          used to be answered a second time in SQL, comparing ISO timestamps as
          TEXT — so a grant written with a `+13:00` offset, which is what the
          dream timer's Pacific/Auckland schedule produces, read as live here
          for six hours after `is_live` correctly called it dead. Two answers to
          one question, and the permission path had the lenient one. The query
          now filters only on what a string can hold — returned, and the dream
          still adopted — and liveness is arithmetic on datetimes, once.
        - **The dream must still be ADOPTED.** The join used to check only that
          the dream existed, so mutating `dream.vault` and calling `save()` —
          which deliberately bypasses `move` — left an archived dream holding a
          live grant, and an interrupted `adopt` left one on a dream still in
          the vault. A permission that no shelf accounts for is a permission
          nobody is watching.
        - **An unresolved class grants nothing.** An adoption with an empty
          `asset_class` is dropped, because a symbol whose class is unknown is a
          symbol whose limits are unknown, and defaulting it would be choosing
          which risk cap applies by accident.
        - **A symbol claimed by two live grants with different classes is
          dropped**, not resolved. There is no correct answer to which cap
          applies, and picking one would be a plausible wrong figure, which is
          the failure this repository exists to prevent.
        - **A failure to resolve yields an EMPTY mapping, so the caller fails
          closed.** A database error, a torn row, anything: the answer is "no
          symbols are granted", never a partial mapping presented as complete
          and never an exception into a decision path. Erring towards granting
          nothing is the only safe direction here; the account carries on
          trading exactly what `config/rules.yaml` already allows.
        - **This function does not talk to the gate and must not learn how.**
          It answers a question about the dream store. The caller is what maps
          the answer onto whatever `RiskGate` is given, and keeping that seam
          means nothing in this module ever imports the risk path — which is the
          same structural argument as `Dream` carrying no order fields.
        """
        granted: dict[str, str] = {}
        conflicted: set[str] = set()
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT a.* FROM adoptions a"
                    # Joined to `dreams` so an adoption whose dream is gone can
                    # never grant anything, and constrained to the ADOPTED shelf
                    # so one whose dream has been moved off it cannot either.
                    # `delete` already refuses a dream with a live grant; this is
                    # the lock that does not depend on that having been got right.
                    " JOIN dreams d ON d.id = a.dream_id"
                    " WHERE a.returned_at IS NULL AND d.vault = ?",
                    (str(Vault.ADOPTED),),
                ).fetchall()
        except sqlite3.Error as exc:
            log.warning("granted_symbols_unavailable", error=str(exc))
            return {}

        for row in rows:
            adoption = _to_adoption(row)
            if adoption is None:
                continue
            # The one definition of live, rather than a second one in SQL.
            if not adoption.is_live(now):
                continue
            asset_class = adoption.asset_class.strip()
            if not asset_class:
                log.warning("adoption_without_asset_class", dream_id=adoption.dream_id)
                continue
            for symbol in adoption.symbols_granted:
                held = granted.get(symbol)
                if held is not None and held != asset_class:
                    conflicted.add(symbol)
                    continue
                granted[symbol] = asset_class

        for symbol in conflicted:
            log.warning("adoption_class_conflict", symbol=symbol)
            granted.pop(symbol, None)
        return granted

    def expired(
        self, now: datetime, ttls: VaultTTLs | None = None
    ) -> list[Dream]:
        """Dreams that have outstayed their vault's TTL at `now`.

        **Expiry marks; it never deletes.** Nothing is removed, nothing is
        moved, and no state is written — this is a pure read, and what to do
        about the answer is the caller's. Same shape as `stop_watch`, which
        reports a breached stop and does not close the position: an automatic
        action nobody watched is a different proposition from a loud report, and
        the second is the honest intermediate. A function here that quietly
        archived a chain somebody was halfway through would be the worse of the
        two.

        Measured from `vault_entered_at`, so a dream pulled back for another
        pass gets a fresh clock rather than inheriting a nearly-dead one. See
        the module docstring.

        A vault with a `None` TTL — the archive — never expires. The archive IS
        the retirement state, so an expiry on it would be an expiry on an
        expiry.
        """
        windows = ttls or DEFAULT_TTLS
        out: list[Dream] = []
        for dream in self.recent(limit=10_000):
            days = windows.days_for(dream.vault)
            if days is None:
                continue
            if now - dream.vault_entered_at > timedelta(days=days):
                out.append(dream)
        return out


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

    # A prophecy is a claim that the world will do something. One with no
    # condition attached is a claim nobody can ever settle, which is the
    # `untriggered_keeps` failure wearing the new costume: it reads as a view
    # while committing to nothing.
    #
    # Optional with a default, like every field added after the fact, so a
    # caller constructing this positionally from before the vaults still works.
    conditionless_prophecies: int = 0

    # Prophecies whose conditions are ALL met and which are still sitting in the
    # prophecy vault. Not a failure — the dreamer may have good reason not to
    # offer it — but it is the queue worth looking at, and nothing else on the
    # page would show it.
    fulfilled_not_offered: int = 0

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
            conditionless_prophecies=sum(
                1
                for d in dreams
                if d.vault is Vault.PROPHECY and not d.has_conditions
            ),
            fulfilled_not_offered=sum(
                1 for d in dreams if d.vault is Vault.PROPHECY and d.all_conditions_met
            ),
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

    # How many dreams sit on each shelf, over the same list every other count
    # here is taken from.
    #
    # **Every vault is a key, including the empty ones**, so a renderer that
    # loops over this shows a zero rather than skipping a shelf. A missing row
    # reads as "no data" and a zero reads as a stated fact; on ADOPTED the two
    # are very different claims. Same rule as `DreamStore.counts_by_vault`,
    # which answers over the whole store rather than over a page's worth.
    #
    # Added after the fact, so it carries a default: this is constructed by
    # `.of` in one place today, and a field with no default would break any
    # caller that had built one positionally.
    by_vault: dict[Vault, int] = field(default_factory=dict)

    # How many of these dreams were fused from others. A count rather than a
    # rate, because the interesting question is "has anything been combined
    # yet", and a percentage over a handful of dreams is noise wearing a
    # decimal point — the `sample_is_thin` reasoning, arriving early.
    fusions: int = 0

    @property
    def workbench(self) -> int:
        return self.by_vault.get(Vault.WORKBENCH, 0)

    @property
    def prophecies(self) -> int:
        return self.by_vault.get(Vault.PROPHECY, 0)

    @property
    def vaulted(self) -> int:
        """In the dream vault: fulfilled, offered, visible to the trading agent."""
        return self.by_vault.get(Vault.VAULT, 0)

    @property
    def adopted(self) -> int:
        return self.by_vault.get(Vault.ADOPTED, 0)

    @property
    def archived(self) -> int:
        return self.by_vault.get(Vault.ARCHIVE, 0)

    @classmethod
    def of(cls, dreams: list[Dream]) -> DreamSummary:
        by_vault = dict.fromkeys(Vault, 0)
        for dream in dreams:
            by_vault[dream.vault] += 1
        return cls(
            total=len(dreams),
            open_dreams=sum(1 for d in dreams if d.is_open),
            kept=sum(1 for d in dreams if d.verdict is DreamVerdict.KEEP),
            parked=sum(1 for d in dreams if d.verdict is DreamVerdict.PARK),
            dropped=sum(1 for d in dreams if d.verdict is DreamVerdict.DROP),
            unverified=sum(
                1 for d in dreams if d.verification is Verification.UNVERIFIED
            ),
            by_vault=by_vault,
            fusions=sum(1 for d in dreams if d.is_fusion),
        )
