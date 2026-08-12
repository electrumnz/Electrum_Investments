"""The conference: the dreamer offers a dream, the trading agent decides.

`dreaming.py` holds the vaults and the transcript. `dreamer.py` fills the
workbench. This is the third piece: the two agents actually talking, on one
dream, in a bounded exchange whose whole record is written to
`dream_messages` where a human can read it.

The shape of one exchange:

    dreamer  offer     here is the chain, here is what it claims, here is the
                       hop I would attack first
    trader   question  what would kill it? what does adopting it actually buy?
    dreamer  answer    ...
    trader   note      accept / decline / defer, with a reason — and a defer
                       carries the condition that would wake it

The dreamer speaks on the even turns and the trading agent on the odd ones,
dreamer first, and the trading agent is the one that decides whether to TAKE the
dream. That asymmetry is the same one `DreamStore.move` enforces: the agent with
a route to the broker gets the smaller set of verbs, and here it gets that
decision because it is about whether IT will take responsibility for a symbol.

The dreamer's one decision is the mirror of it — **withdrawing its own dream**,
which is the only thing in this module that archives anything and is a verb the
dreamer already holds everywhere else. Either agent can therefore end an
exchange, and each can only end it about its own side of the arrangement.

## Why this is its own module and its own command

**It runs on the DREAM TIMER, once a day. Never on the loop's fifteen-minute
pulse.** Ninety-six unattended negotiations a day, on the same process that
proposes orders, is the Alpha Arena failure shape with two models instead of
one — six frontier models traded real money on confident prose and every US
flagship finished underwater. A day is far slower than a price moves, which is
exactly the right speed for deciding whether a second-order hypothesis is worth
acting on.

Keeping it in its own module and behind its own entry point is the same
structural argument that keeps `dreamer.py` out of `cmd_loop`: a separation
that is a file boundary survives a refactor, and a separation that is a comment
does not.

## What the trading agent's side can and cannot do here

**It cannot place an order.** Nothing in this module imports a broker, an
`OrderProposal`, the MCP server or the risk gate, and `tests/test_confer.py`
asserts that. Its only powers are the two `DreamStore` already enforces for the
`TRADER` actor, and they are collected on `TraderPowers` so the list is
readable rather than scattered: adopt a dream out of the vault, or hand an
adopted one back with a stated reason.

An adoption is a **permission and not an order**. It widens what may be
considered; `RiskGate.evaluate` still runs on anything actually traded under it,
the four operator rules still hold, and the size still follows from the stop.
See the `dreaming.py` module docstring, which is where that argument lives.

## The caps, and why the last one is the one that matters

Two of them bound a conversation, two bound the *series* of them, and one is
the ceiling under which the whole arrangement sits:

- `MAX_TURNS_PER_EXCHANGE` — six turns, three each.
- `MAX_DREAMS_PER_RUN` — two dreams a day.
- `MAX_EXCHANGES_PER_EPOCH` — three exchanges since the last thing that changed.
- `MAX_EXCHANGES_LIFETIME` — twelve exchanges on one dream, ever.
- `TEXT_MAX_CHARS` — reused from `dreaming.py`, not redefined here.
- `has_something_changed` — a dream may only be conferred AGAIN if something
  changed since the last exchange.

Read `has_something_changed`'s own docstring before touching any of it. A turn
limit bounds one conversation and says nothing whatever about having the same
conversation again tomorrow, politely, at cost, with every other cap still
holding while it happens.

**The exchange budget is an EPOCH and not a life sentence.** It used to be
three exchanges counted over the whole life of a dream, so a dream that spent
them was closed to discussion forever — including after its prophecy fired,
after the operator posted a note, and after the trading agent adopted it and
handed it back with a new reason. That is the cap doing something it was never
for. The cap exists to stop the SAME argument repeating, not to stop a new one
starting, so the count runs from the last resetting event: see
`epoch_started_at`, which reads the same signals `has_something_changed` does
rather than growing a second definition of "changed".

`MAX_EXCHANGES_LIFETIME` is what makes that safe. Both exist and they do
different jobs, which is the sentence to keep: **the per-epoch cap stops the
same argument, and the lifetime cap stops an infinite sequence of new ones.**
Without the second, a dream edited every morning would buy a fresh three every
morning forever.

## Every exchange ends in exactly one recorded verdict

The transcript records what was SAID. `ConferenceDecision` records what was
DECIDED, and the two answer different questions. Before it existed, "what did
they decide about this dream" had to be reconstructed by reading six turns of
prose and a scattering of side effects — an adoption row here, a vault move
there, the absence of both somewhere else — and arriving at an answer nobody
had written down.

**The verdict is what the AGENT decided, and never what this module inferred
from a side effect.** It is taken off the turn the agent actually took. Whether
the store then did the thing is a separate field, because the trading agent
agreeing to adopt and the adopted shelf being full are two facts and an
exchange where the first happened is not one that decided nothing.

Five verdicts, four of which are decisions. `ConferenceVerdict` carries the
argument for each; the two worth knowing before touching this module:

- **`NO_DECISION` must never collapse into `DEFER`.** A turn cap, a failed
  model call and a deferral that named nothing to wake it are all "nobody
  decided", and reporting the absence of a decision as the mildest real one is
  how a silent failure starts looking healthy.
- **`ARCHIVE` is the dreamer's, about its own dream, and the trading agent has
  no route to it — not even a recommendation.** See "who may archive" below.

A verdict is written for every exchange that reached the model, including the
ones that decided nothing. None is written for an exchange the caps SKIPPED: no
model was called and no conversation happened, so there is nothing to have
decided, and a row a day saying so would fill the only permanent record with
the noise the change gate exists to prevent.

## Who may archive, and why it is not the trading agent

The operator's commission for the trading agent is that it "cannot delete, can
only action or send back to vault with reasons". An adopted dream carries a
live symbol grant, so the agent with a route to the broker is the one that gets
the smaller set of verbs — which is the same asymmetry `DreamStore.move`
enforces and `TraderPowers` states.

**So archival is not a power a conversation can hand it, and it is not offered
as a recommendation either.** A trading agent that could recommend retirement
would be putting pressure on the dreamer to destroy a chain it did not want,
recorded on the dream, from the party with the least standing to ask. `DECLINE`
already says everything the trading agent has standing to say: I will not take
this, and here is why. The dreamer reads the transcript; if the reason kills the
chain, the dreamer archives it, which is a verb it already holds.

**What archives a dream here is the DREAMER withdrawing its own dream**, on its
own turn, with a stated reason — `DreamerTurn.withdraw`. That is not a new
power: `DREAMER_VAULTS` already contains `ARCHIVE` and `DreamStore.move`
already permits `dreamer → archive` from anywhere the dreamer holds. It is the
same verb through a new door, and the defensible trigger is the one the
conference is built to produce: the trading agent asks the one question that
would kill the chain and the dreamer cannot answer it.

Nothing is destroyed by it. The archive is a shelf, `delete` is a different
method, and this module never calls it — `tests/test_confer.py` asserts the
name does not appear here at all.

## A deferral names what would wake it, or it is not a deferral

`ConferenceVerdict.DEFER` requires a `DreamCondition` with a symbol, a field, an
operator and a **number**. The threshold is a number and never the name of
another figure, for the reason `DreamCondition` gives at length: "above the
20-day" re-checked next month tests a level nobody ever saw.

A deferral that names nothing is "we ran out of things to say" wearing a
decision's clothes, so it is refused: the exchange is recorded as `NO_DECISION`
with `NoDecision.DEFER_WITHOUT_WAKE`, and the transcript says which half was
missing. That is a rule that REJECTS, and `tests/test_confer.py` proves it does
rather than proving it exists.

The wake condition is recorded on the decision and deliberately **not** appended
to the dream's own conditions. Those are the dreamer's pre-registered claims and
they decide what `promotion_for` and `all_conditions_met` do; writing the
trading agent's condition into that list would let one agent change what the
other's dream needs in order to be promoted.

## Failure

Same rule as the decision loop's model call and the dreamer's, learned the same
way: a `ValidationError` out of the SDK killed a live cycle once and systemd
restarted it straight into the same failure. A failed call here ends the
exchange, writes a note saying so, records `NO_DECISION` with
`NoDecision.CALL_FAILED`, and returns `ConferOutcome.CALL_FAILED`.

**It is never recorded as a completed exchange that decided nothing.** A failure
before the opening offer writes no `offer` message and no agent turn, so it
consumes neither the dream's three exchanges nor its eligibility, and tomorrow's
run tries again. A cycle that could not get a decision must not be recorded as
one that decided to do nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

import anthropic
import structlog
from pydantic import BaseModel, Field

from .config import Env, Rules
from .dreaming import (
    DREAMER,
    TEXT_MAX_CHARS,
    TRADER,
    ConferenceDecision,
    ConferenceVerdict,
    Dream,
    DreamCondition,
    DreamMessage,
    DreamStore,
    MoveResult,
    NoDecision,
    TriggerField,
    TriggerOp,
    Vault,
    VaultCaps,
)
from .model_client import CallUsage, ModelClient
from .souls import GROGU, YODA, load_soul

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------- the caps
#
# All five are here, with the reasoning, so that somebody tidying up can see
# what each one is for before deciding one of them is redundant.

# Six turns, three each. Enough to make the case, ask the one question that
# would kill it, answer, and reach a verdict.
#
# **Hard stop, no extension.** A conversation that has not converged in six
# turns is not one turn away from converging; it is two agents that disagree,
# and the honest record of that is an exchange that ended without a verdict.
# An extension mechanism would be a cap that can be argued with, and the whole
# reason the caps are numbers rather than judgement is that neither party here
# is a person.
MAX_TURNS_PER_EXCHANGE = 6

# Two dreams conferred per daily run. So a run costs at most twelve model calls
# on top of the dream step itself and the bill is predictable — which is the
# point, because an unpredictable bill on an unattended timer is discovered a
# month later.
#
# Skipped dreams do NOT count against this. A skip costs no model call, so
# counting it would make a quiet day eat the budget of a busy one.
MAX_DREAMS_PER_RUN = 2

# Three exchanges per EPOCH — that is, since the last thing that changed about
# the dream. If the two of them cannot agree in three conferences on an
# unchanged dream it parks: a fourth would be the same argument again, and a
# fourth in a different order is still the same argument.
#
# Counted off the transcript rather than from a counter column, deliberately.
# A counter is a second fact about the same thing and eventually disagrees with
# the first, which is the reasoning that keeps `Adoption.is_live` computed; and
# a column would need a migration in `dreaming.SCHEMA`, which this module has no
# business owning. An epoch needs no column either — `epoch_started_at` reads
# the same stamps the change gate already reads.
MAX_EXCHANGES_PER_EPOCH = 3

# Twelve exchanges on one dream, ever, whatever changes.
#
# **Both caps exist and they do different jobs.** The per-epoch cap stops the
# same argument repeating; this one stops an infinite sequence of new ones. A
# dream the dreamer edits every morning legitimately clears the epoch cap every
# morning, and without a ceiling that is a conversation with no end and a bill
# with no end either — the exact failure `has_something_changed` was written
# for, arriving through the door the epoch opened.
#
# Twelve is four epochs' worth. It is a working constraint rather than a risk
# rule — nothing in this module can lose money — and when it bites it is logged
# at warning rather than info, because a dream that has been conferred twelve
# times is a fact about the two agents worth reading.
MAX_EXCHANGES_LIFETIME = 12

# The fourth cap is `TEXT_MAX_CHARS`, imported from `dreaming.py` rather than
# redefined. `DreamStore.add_message` already trims to it, so a long turn is
# stored short rather than lost — prose truncates, and nothing structural goes
# through that path. A second cap here would be a second number to keep in step
# with the first, and the one that is wrong would be the one nobody is reading.
#
# The fifth is `has_something_changed`, below.


# Who says what. `DREAMER` and `TRADER` come from `dreaming.py` so there is one
# spelling of each in the repository.
#
# `CONFERENCE` is this module narrating itself: a call that failed, a turn cap
# reached, an adoption the store refused. It is deliberately NOT `OPERATOR` —
# an operator note is one of the things that makes a dream worth reopening, so
# a machine note wearing the operator's name would trip the change gate on its
# own writing and the two agents would talk forever about nothing.
CONFERENCE = "conference"

# The `kind` on the note this module writes when the two agents agreed and the
# STORE refused. Its own kind rather than a `note` so `_consider` can find it
# without reading prose — the same reasoning as `main.EXPIRY_MARK_KIND`, and it
# is load-bearing here: the change gate measures the DREAM, and the thing that
# blocked this one was the shelf. Nothing about the dream will ever change to
# clear it, so without this mark the dream is skipped `nothing_new` forever
# while the adoption it was refused becomes possible again the moment a slot
# frees. Not one of `dreaming.MESSAGE_KINDS`, which is allowed: the store does
# not validate the field, and an unfamiliar kind costs a page a badge where
# refusing it would lose the turn.
REFUSAL_MARK_KIND = "adoption_refused"

# The speakers whose turns mark an exchange as having happened. See
# `last_agent_turn_at`.
AGENT_SPEAKERS = frozenset({DREAMER, TRADER})


class TraderVerdict(StrEnum):
    """What the trading agent decided about an offered dream.

    Three, and none of them is a trade. The most this can produce is a symbol
    permission with an expiry on it. **None of them is an archive either**, and
    that absence is the operator's commission rather than an oversight — see the
    module docstring under "who may archive".

    `DEFER` was called `park` while it meant "stop for now". It means "come back
    when this happens" now, and carries a condition that says what: a rename
    rather than a tidy-up, because the verb's meaning changed.
    """

    # Take it: `DreamStore.adopt` moves it to ADOPTED and writes the grant.
    ACCEPT = "accept"
    # Not for me, on the merits. The dream stays in the vault, where the dreamer
    # can rework it or offer it again once something has changed. No wake
    # condition, because there is nothing the world could do to change the
    # answer — and requiring one would push the trading agent into inventing a
    # threshold, which is worse than an honest sentence.
    DECLINE = "decline"
    # Not now, and here is what would change that. Requires a checkable wake
    # condition; without one the exchange is recorded as having decided nothing.
    # Also leaves the dream in the vault: the trading agent may not move a dream
    # anywhere, and pretending otherwise here would be this module routing
    # around `DreamStore.move`.
    DEFER = "defer"


class ConferOutcome(StrEnum):
    """How one dream's turn in a conference ended.

    **Not the same thing as the stored verdict, and both exist.** This is the
    run's bookkeeping — it distinguishes the endings a caller has to count and
    the caps that produced them, including the three skips that never reached a
    model. `ConferenceVerdict` is what the agents decided, and the skips have no
    entry there because nobody decided anything.

    Every value is an ordinary answer. `NOTHING_NEW` and `EXCHANGES_EXHAUSTED`
    are the two that cost no model call at all, and they are the two that make
    the whole arrangement affordable.
    """

    ADOPTED = "adopted"
    DECLINED = "declined"
    DEFERRED = "deferred"
    # The dreamer withdrew its own dream to the archive rather than keep
    # offering it. The one ending that is the DREAMER's decision.
    WITHDRAWN = "withdrawn"
    # Six turns and no verdict. Recorded as its own outcome rather than folded
    # into `DEFERRED`, because "they could not agree" and "they agreed to wait
    # for something" are different facts about the dreamer and about the trader.
    TURNS_EXHAUSTED = "turns_exhausted"
    # The trading agent said accept and `DreamStore.adopt` refused — a full
    # adopted vault, no symbols, an unresolved class. Not a failure of the
    # conversation, and it must not be reported as one.
    ADOPTION_REFUSED = "adoption_refused"
    # The dreamer withdrew and `DreamStore.move` refused. Its own value for the
    # same reason `ADOPTION_REFUSED` is: the decision happened and the shelf
    # would not take it, which is not the same as no decision.
    WITHDRAWAL_REFUSED = "withdrawal_refused"
    # The trading agent said defer and named nothing that would wake it. The
    # verdict is refused and the exchange is recorded as having decided nothing.
    DEFER_REFUSED = "defer_refused"
    CALL_FAILED = "call_failed"
    NOTHING_NEW = "nothing_new"
    # Three exchanges since the last thing that changed. NOT the end of the
    # conversation: something changing starts a fresh epoch and a fresh three.
    EXCHANGES_EXHAUSTED = "exchanges_exhausted"
    # Twelve exchanges, ever. This one IS the end, whatever changes, and it is
    # kept apart from the epoch cap rather than folded into it because "not
    # until something changes" and "never again" are different facts about a
    # dream and only one of them is worth an operator's attention.
    LIFETIME_EXHAUSTED = "lifetime_exhausted"


# The outcomes that cost nothing, because no model was called. Named rather than
# inlined so `ExchangeResult.conferred` and the per-run cap read off one list.
SKIPPED_OUTCOMES = frozenset(
    {
        ConferOutcome.NOTHING_NEW,
        ConferOutcome.EXCHANGES_EXHAUSTED,
        ConferOutcome.LIFETIME_EXHAUSTED,
    }
)


@dataclass(frozen=True)
class Change:
    """Whether a dream is worth conferring again, and what changed.

    The reasons are prose for a person and are the whole value of the type: a
    bare `False` on the Dreaming page reads as a bug, and "nothing has changed
    since the exchange on 14 May" reads as the working system it is.
    """

    changed: bool
    reasons: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.changed

    @property
    def summary(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "nothing new to discuss"


@dataclass(frozen=True)
class ExchangeResult:
    """What happened to one dream in one run.

    Carries the usage of every call made, rather than a total, so a caller can
    report the cost of a run and the number of calls separately. A run that made
    six cheap calls and one that made one expensive one are different things to
    know about an unattended timer.
    """

    dream_id: int
    outcome: ConferOutcome
    turns: int = 0
    detail: str = ""
    usage: tuple[CallUsage, ...] = ()

    # The stored verdict, handed back so a caller need not go to the database
    # for what it just watched happen. `None` for a SKIP and only for a skip:
    # nothing was decided because nothing was said. A caller that treats `None`
    # as "decided nothing" is making the mistake `ConferenceVerdict.NO_DECISION`
    # exists to stop, one layer up.
    decision: ConferenceDecision | None = None

    @property
    def conferred(self) -> bool:
        """Whether a model was actually called about this dream."""
        return self.outcome not in SKIPPED_OUTCOMES

    @property
    def cost_usd(self) -> float:
        return sum(u.estimated_cost_usd for u in self.usage)


@dataclass(frozen=True)
class ConferenceReport:
    """One run. Pure arithmetic over the exchanges.

    `considered` is separate from `len(exchanges)` on purpose, and it is the
    same rule as `news_history.has_cycles` and `WatchReport.can_grade_anything`:
    an empty report because the vault is empty and an empty report because
    everything in it was skipped are opposite findings that produce identical
    lists. A caller handed only a list reaches for the wrong reading every time.
    """

    exchanges: tuple[ExchangeResult, ...] = ()
    considered: int = 0

    @property
    def conferred(self) -> int:
        return sum(1 for e in self.exchanges if e.conferred)

    @property
    def skipped(self) -> int:
        return sum(1 for e in self.exchanges if not e.conferred)

    @property
    def adopted(self) -> int:
        return sum(1 for e in self.exchanges if e.outcome is ConferOutcome.ADOPTED)

    @property
    def decisions(self) -> tuple[ConferenceDecision, ...]:
        """Every verdict this run recorded, in the order the exchanges ran.

        One per conferred exchange, including the ones that decided nothing.
        Skips contribute none, so this is shorter than `exchanges` on a quiet
        day and that is the honest shape: a skip is the caps working, not a
        conference.
        """
        return tuple(e.decision for e in self.exchanges if e.decision is not None)

    @property
    def decided(self) -> int:
        """Exchanges that reached an actual decision."""
        return sum(1 for d in self.decisions if d.decided)

    @property
    def undecided(self) -> int:
        """Exchanges that happened and settled nothing.

        Counted apart from `decided` rather than derived by subtracting it from
        `conferred`, so the two numbers can be read side by side. A run where
        every exchange came back undecided is a fact about the two agents, and
        one where none did is a different one; both produce the same
        `conferred`.
        """
        return sum(1 for d in self.decisions if not d.decided)

    @property
    def calls(self) -> int:
        return sum(len(e.usage) for e in self.exchanges)

    @property
    def cost_usd(self) -> float:
        return sum(e.cost_usd for e in self.exchanges)


# ------------------------------------------------- reading the transcript back


def last_agent_turn_at(messages: Sequence[DreamMessage]) -> datetime | None:
    """When the last exchange ended, or `None` if the two have never spoken.

    **The marker is the last AGENT turn, never simply the last message**, and
    that distinction is the whole reason this is a function rather than
    `messages[-1].at`.

    An operator note posted after an exchange is one of the five things that
    makes a dream worth reopening. Taking the last message of any kind would
    move the marker past the very event it exists to notice, so the note would
    silence itself and the operator would be the only participant who could not
    restart the conversation. Same shape as `seen.py`, where the marker advances
    to the PREVIOUS request rather than to now, for the same reason: a marker
    must not swallow the thing it is measuring against.

    `CONFERENCE` notes are excluded for the mirror-image reason — this module
    writing "the call failed" must not read as the two of them having talked.
    """
    for message in reversed(list(messages)):
        if message.speaker in AGENT_SPEAKERS:
            return message.at
    return None


def exchanges_so_far(
    messages: Sequence[DreamMessage], *, since: datetime | None = None
) -> int:
    """How many exchanges this dream has had, in total or since a moment.

    Counted as the number of opening `offer` turns from the dreamer, because an
    exchange is defined by its opening: every conversation this module starts
    writes exactly one, and a conversation that never got that far never
    happened. A failed first call therefore costs the dream nothing, which is
    what "never recorded as a completed exchange that decided nothing" means in
    practice.

    An offer written by hand from the Dreaming page counts too. That is the
    conservative direction — it can only make this module talk less — and it is
    also the honest reading, since an offer is an offer whoever wrote it.

    `since` is what makes this answer the epoch question as well as the lifetime
    one, so there is ONE definition of "an exchange" rather than two that can
    drift. **The comparison is `>=`, where the change gate's is `>`.** They are
    asking opposite questions: the gate asks whether anything happened AFTER the
    last conversation and must not be tripped by the conversation's own ending,
    while this asks how much talking has happened since the epoch opened — and
    an offer stamped at the same instant as the reset belongs to the new epoch,
    which is the direction that talks less.
    """
    offers = [m for m in messages if m.speaker == DREAMER and m.kind == "offer"]
    if since is None:
        return len(offers)
    return sum(1 for m in offers if m.at >= since)


class ChangeKind(StrEnum):
    """What kind of thing changed. Machine-readable so a page can group them."""

    CONDITION = "condition"
    VAULT_MOVE = "vault_move"
    EDIT = "edit"
    VOICE = "voice"


@dataclass(frozen=True)
class ChangeSignal:
    """One dated thing that could reopen the conversation on a dream.

    Dated, because two different questions are asked of the same set of facts
    and only one of them is a boolean. `has_something_changed` asks whether any
    of these lands after a marker; `epoch_started_at` asks when the most recent
    one was. Splitting the signals out is what lets both read the same
    definition of "changed" rather than growing a second one that drifts.
    """

    at: datetime
    kind: ChangeKind
    # The vault moved into, or the speaker who spoke. Empty for an edit.
    subject: str = ""


def change_signals(
    dream: Dream, messages: Sequence[DreamMessage] = ()
) -> tuple[ChangeSignal, ...]:
    """Every dated event that makes a dream worth conferring again. Pure.

    **The single definition of "something changed" in this repository.** Both
    the change gate and the exchange epoch read it, so there is no second list
    to fall out of step with this one.

    The signals, and each is a decision rather than plumbing:

    - **A condition was fulfilled.** The prophecy did something. A condition
      fulfilled with no stamp yields NO signal: an undated fact cannot say when
      it happened, and guessing that it happened recently would be a plausible
      wrong answer.
    - **The dream was edited** — a hop added, a `checked` flag flipped, the
      chain reworked, a thought recorded, a fusion written. Observed through
      `updated_at`, which every write path in `DreamStore.save` and `move`
      stamps. Detecting hop-level deltas directly would need a snapshot of the
      chain taken at the last exchange, and a second copy of a dream is a second
      thing that can disagree with the dream. One stamp, one answer.
    - **A vault move**, through `vault_entered_at`, which nothing else sets.
      This is the dreamer pulling a chain back for a rework and returning it,
      and it is also the trading agent handing an adopted dream back.
    - **A new voice.** An operator note, a fusion note, or anything from a
      speaker that is neither of the two conferring agents nor this module
      narrating itself. Written as an exclusion rather than as
      `speaker == OPERATOR` so that a second dreamer — the intended direction,
      and why `DreamMessage` keeps `speaker` open — counts as the new voice it
      is.
    """
    signals: list[ChangeSignal] = [
        ChangeSignal(at=dream.updated_at, kind=ChangeKind.EDIT),
        ChangeSignal(
            at=dream.vault_entered_at,
            kind=ChangeKind.VAULT_MOVE,
            subject=str(dream.vault),
        ),
    ]
    signals += [
        ChangeSignal(at=c.fulfilled_at, kind=ChangeKind.CONDITION)
        for c in dream.conditions
        if c.fulfilled and c.fulfilled_at is not None
    ]
    signals += [
        ChangeSignal(at=m.at, kind=ChangeKind.VOICE, subject=m.speaker)
        for m in messages
        if m.speaker not in AGENT_SPEAKERS and m.speaker != CONFERENCE
    ]
    return tuple(sorted(signals, key=lambda s: s.at))


def epoch_started_at(
    dream: Dream, messages: Sequence[DreamMessage] = ()
) -> datetime:
    """When the current exchange budget last reset. Pure; the same signals as above.

    **The cap exists to stop the same argument repeating, not to stop a new one
    starting.** So the three exchanges are counted from the most recent thing
    that genuinely changed about the dream rather than over its whole life: a
    prophecy that fired, a hop that got checked, an operator's note, a rework,
    or the trading agent handing it back — that last one being a new situation
    by definition, since it was adopted, it came back, and the reason it came
    back is new information.

    Reads `change_signals` rather than listing the events again. A second
    definition of "changed" would be a second thing to keep in step, and the one
    that was wrong would be the one nobody was reading.

    Never `None`: every dream carries `updated_at`, so there is always a moment
    to count from. A dream nothing has ever happened to counts its exchanges
    from the moment it was last written, which is the whole of its life.
    """
    signals = change_signals(dream, messages)
    # `change_signals` always yields the edit and vault stamps, so this default
    # is unreachable — it is here so a future signal list that could be empty
    # degrades to "count everything" rather than raising inside a scheduled job.
    return max((s.at for s in signals), default=dream.updated_at)


def exchanges_in_epoch(
    dream: Dream, messages: Sequence[DreamMessage]
) -> int:
    """How many exchanges have happened since the last resetting event.

    The composition of the two functions above, named because both `_consider`
    and `_closing` need it and two call sites spelling it out is two places to
    get the argument order wrong.
    """
    return exchanges_so_far(messages, since=epoch_started_at(dream, messages))


def has_something_changed(
    dream: Dream,
    since: datetime | None,
    *,
    messages: Sequence[DreamMessage] = (),
) -> Change:
    """**The cap that actually stops them talking forever.** Read this one.

    Every other cap bounds a single conversation. None of them says anything
    about having the same conversation again tomorrow — politely, at cost, six
    turns each time, with every individual limit satisfied on every pass. Two
    agents re-litigating an unchanged dream every day until one of them is
    switched off is a system that is working exactly as specified and is
    obviously broken, and it is the failure mode this function exists for.

    It is also the cap most likely to look redundant to somebody tidying up
    later, because the other four are visible in the code and this one is
    visible only in a bill. Do not remove it, and do not soften it into a
    cooldown: a cooldown says "wait a while and have the same argument", which
    is the same failure with a delay in front of it.

    A dream qualifies again when one of the signals in `change_signals` lands
    after `since`, or when there has never been an exchange at all, which is
    `since is None`. That list is read rather than repeated here, so the gate
    and the exchange epoch cannot come to disagree about what "changed" means.

    Comparisons are STRICT. That is not fussiness: `DreamStore.return_to_vault`
    writes its `return` message and stamps `updated_at` and `vault_entered_at`
    with the same moment, so a non-strict test would report a change created by
    the end of the exchange itself and hand the two agents a fresh round
    immediately. The same is true of `adopt`. A hand-back genuinely IS a new
    situation and is meant to reopen the conversation — but it is a fact about
    the ADOPTION rather than about the dream, so it is answered by
    `Conference._handed_back_since_the_last_offer`, which can see that it
    happened without loosening a comparison that has to stay strict for
    everything else.

    Pure. No store, no clock, no network — everything it needs is the dream and
    the transcript the caller already read.
    """
    if since is None:
        return Change(True, ("no exchange on this dream yet",))

    fired = [s for s in change_signals(dream, messages) if s.at > since]
    reasons: list[str] = []

    conditions = [s for s in fired if s.kind is ChangeKind.CONDITION]
    if conditions:
        reasons.append(
            f"{len(conditions)} condition(s) fulfilled since the last exchange"
        )

    for signal in fired:
        if signal.kind is ChangeKind.VAULT_MOVE:
            reasons.append(f"moved into {signal.subject} since the last exchange")

    if any(s.kind is ChangeKind.EDIT for s in fired):
        reasons.append("the dream was edited since the last exchange")

    voices = sorted({s.subject for s in fired if s.kind is ChangeKind.VOICE})
    if voices:
        reasons.append(f"new message(s) from {', '.join(voices)}")

    return Change(bool(reasons), tuple(reasons))


# ------------------------------------------------ what the trading agent may do


class TraderPowers:
    """Everything the trading agent's side of a conference is able to do.

    Two methods, and the list being short is the point rather than an accident
    of what has been needed so far. `DreamStore` already refuses the trading
    agent every other verb — it cannot delete, it cannot archive, it cannot move
    a dream between the dreamer's shelves — so this class is not the guarantee.
    It is the readable statement of the guarantee, in one place, so that "the
    conference cannot place an order" is a claim somebody can check in ten
    seconds instead of by reading the whole module.

    **There is no broker here, no `OrderProposal`, and no route to one.** An
    adoption grants a symbol permission with an expiry; it does not open a
    position, it does not size one, and everything that decides whether a
    considered trade actually happens is untouched. `tests/test_confer.py`
    asserts both the method list and the absence of the imports, because a class
    that grew a third method quietly is exactly how this stops being true.
    """

    def __init__(self, store: DreamStore) -> None:
        self._store = store

    def adopt(
        self,
        dream_id: int,
        *,
        at: datetime,
        caps: VaultCaps | None = None,
        ttl_days: int | None = None,
    ) -> MoveResult:
        """Take a dream out of the vault. Refusals come back, never raised.

        `ttl_days` exists because it was missing, and the miss was invisible:
        without it every conference-made grant took `DEFAULT_TTLS.adopted` from
        the dataclass and the `ttl_days.adopted` an operator can read in
        `config/rules.yaml` applied to nothing. A limit in a file nobody reads
        is worse than no limit, because the file is what an operator checks.
        """
        return self._store.adopt(dream_id, at=at, caps=caps, ttl_days=ttl_days)

    def hand_back(self, dream_id: int, *, reason: str, at: datetime) -> MoveResult:
        """Return an adopted dream to the vault, saying why.

        Not reachable from an exchange, which only ever confers dreams sitting
        IN the vault. It is here because it is the trading agent's other power
        and a list of powers that omitted one would be a list nobody could
        trust. A caller reviewing adopted dreams uses it.
        """
        return self._store.return_to_vault(dream_id, reason=reason, at=at)


# ------------------------------------------------------------------- the turns


class DreamerTurn(BaseModel):
    """One turn from the dreamer's side, and possibly a withdrawal.

    The KIND of the turn — offer, answer — is decided by this module from where
    the turn sits in the exchange, never by the model, because a speaker that
    chose its own label could write "accept" on its own turn and the transcript
    would misreport who decided what.

    **`withdraw` is the one decision this side of the conversation can reach**,
    and it is the only route to `ConferenceVerdict.ARCHIVE` anywhere in this
    module. See the module docstring: it is a verb the dreamer already holds
    over its own dream, arriving through a new door rather than a new power.
    """

    text: str = Field(
        description=(
            "Your turn, in a few sentences. Make the case, or answer what you "
            "were asked. No numbers you were not shown. If you are withdrawing, "
            "this is the reason, in your own words."
        )
    )
    withdraw: bool = Field(
        default=False,
        description=(
            "true only to WITHDRAW this dream to the archive, because you now "
            "think the chain is dead — a hop you cannot check and cannot argue "
            "for, or a condition the world has already failed. It stops being "
            "offered and nothing is destroyed: the chain, the thoughts and this "
            "whole conversation stay on the record. Withdrawing a chain you "
            "still believe in because you were asked a hard question is the "
            "wrong reason; say you cannot check it instead."
        ),
    )


class WakeCondition(BaseModel):
    """What would end a deferral. The checkable half is not optional.

    Same split as `DreamCondition` and `AssessmentTrigger`, and it converts
    straight into the first of those rather than being a fourth shape: `text` is
    the sentence a person reads, and symbol/field/op/value is the claim code can
    settle.

    **The threshold is a number and never the name of another figure.** "Above
    the 20-day" re-checked next month tests a level nobody ever saw, because the
    average moved in the meantime — so it is not the claim that was made.
    """

    text: str = Field(
        description=(
            "The condition in a sentence, including why it is the thing that "
            "would change your answer."
        )
    )
    symbol: str = Field(description="Whose figure this is, e.g. SPY.")
    field: TriggerField = Field(description="Which figure. One the loop computes.")
    op: TriggerOp = Field(description="above, below, at_or_above or at_or_below.")
    value: float = Field(
        description=(
            "The threshold, as a NUMBER. Never the name of another figure: an "
            "average moves, so 'above the 20-day' checked later is a different "
            "claim from the one you are making now."
        )
    )

    def as_condition(self) -> DreamCondition:
        """The same claim as the type the prophecy shelf already grades."""
        return DreamCondition(
            text=self.text,
            symbol=self.symbol.strip().upper(),
            field=self.field,
            op=self.op,
            value=self.value,
        )


class TraderTurn(BaseModel):
    """One turn from the trading agent's side, and possibly the verdict."""

    text: str = Field(
        description=(
            "Your turn, in a few sentences. Either the question you need "
            "answered, or the reasoning behind your verdict."
        )
    )
    verdict: TraderVerdict | None = Field(
        default=None,
        description=(
            "null to keep asking. accept to take the dream and grant its "
            "symbols. decline to leave it in the vault for good reasons of "
            "merit. defer to wait for something specific — which needs `wake`. "
            "On your last turn you must choose one."
        ),
    )
    wake: WakeCondition | None = Field(
        default=None,
        description=(
            "REQUIRED with a defer, ignored otherwise. What would have to "
            "happen for you to look at this again, as a symbol, a figure, an "
            "operator and a number. A deferral that names nothing is not a "
            "deferral and is recorded as having decided nothing, so decline "
            "instead if you cannot name one."
        ),
    )


class TurnCaller(Protocol):
    """The one seam a model call goes through.

    Everything else in this module is pure or is a store write, so a test can
    drive every cap — six turns, two dreams, three exchanges, the change gate —
    with a stub that returns canned turns and never touches the network.
    `ModelClient` satisfies this.
    """

    def confer(
        self, prompt: str, output_format: type[BaseModel]
    ) -> tuple[Any, CallUsage]: ...


DREAMER_SYSTEM_PROMPT = """\
You are the dreamer, offering one of your dreams to the trading agent.

The dream is already written. You are not extending it here and you are not
allowed to add to the chain in this conversation: you are explaining what it
claims, what it rests on, and what would break it, so that somebody who has to
answer for the consequence can decide whether to take it.

How to be useful:

- Lead with what the dream would let the trading agent DO, which is very
  little: adopting it grants permission to consider some symbols for a while.
  It does not propose a position and it never could.
- Name the weakest hop yourself, before you are asked. Confidence in a chain is
  the minimum across its links, and a chain presented as stronger than its
  weakest hop is the one failure that would make this whole arrangement not
  worth having.
- Say which hops are unchecked. An unchecked hop is an honest output.
- NEVER state a number you were not shown in this conversation. No production
  figures, market shares, prices, correlations or percentages. A plausible
  fabricated statistic is the most dangerous thing you can produce here,
  because it is the part nobody thinks to check.
- Answer the question you were actually asked. If the answer is "I cannot check
  that", say so — it is a better answer than a confident one, and the trading
  agent declining for a stated reason is a perfectly good outcome.
- Short. A turn is a few sentences.

**You may WITHDRAW this dream, and you are the only one who can.** The trading
agent cannot archive a dream and cannot delete one; if it does not want this, it
declines and the dream stays in the vault. Set `withdraw` when the question you
have just been asked has shown you the chain is dead — a hop you cannot check
and cannot argue for, or a condition the world has already failed. Nothing is
destroyed by it: the chain, the thoughts and this conversation stay on the
record, and the dream stops being offered.

Withdrawing because the question was hard is the wrong reason. "I cannot check
that hop" is an answer, not a retirement.

You have no view on position size, entry, stop or direction, and no way to
express one. That is the trading agent's work and it does not begin until long
after this conversation.
"""

TRADER_SYSTEM_PROMPT = """\
You are the trading agent, deciding whether to adopt a dream out of the vault.

**What adopting actually does, and it is less than it sounds.** It adds the
dream's symbols to what you may consider, for as long as the adoption is alive,
and it does nothing else. It does not open a position. It does not size one. It
is not a view on direction, entry or stop, and it is not evidence for a trade.
Every gate still applies to anything you later propose in those symbols — the
per-trade risk cap, the total-risk cap, concentration, session, concurrency,
cooldown, and a required stop that the size is computed from. The risk gate is
deterministic code and cannot be argued with.

**You cannot place an order in this conversation and there is no path to one.**
Your only moves are: accept the dream, decline it, or defer it.

**You cannot archive a dream and you cannot delete one, and you are not being
asked to recommend either.** A dream you will not take is a DECLINE with your
reason; whether the chain is worth retiring is the dreamer's call about its own
work, and it can read what you said.

The dream you are shown is SPECULATIVE BY CONSTRUCTION. It arrives with a
verification badge counted from how many of its hops name a source, and with
its weakest hop stated. Read both before the chain, not after. An unverified
chain that reads persuasively is exactly the thing the badge exists for.

How to decide:

- Ask the ONE question that would kill it, not three that would decorate it.
  You have three turns; a question you already know the answer to wastes one.
- A dream is worth adopting when the symbols it names are ones you would
  plausibly want to consider and the chain is not obviously broken. It is not
  worth adopting because it is interesting. Interesting is the dreamer's job.
- Decline for a stated reason. A decline is a normal outcome and a frequent one,
  and the record of why is worth more than a reluctant acceptance.
- **Defer only when you can name what would end the wait.** A deferral carries a
  `wake` condition: a symbol, a figure, an operator and a NUMBER, plus the
  sentence saying why that is the thing that would change your answer. The
  number is a number and never the name of another figure — an average moves, so
  "above the 20-day" checked next month is a different claim from the one you
  made today.

  If you cannot name one, DECLINE. A deferral that names nothing is recorded as
  having decided nothing at all, which is worse for you than a decline: it says
  the two of you talked and got nowhere, and the dreamer learns nothing it can
  act on.
- NEVER state a number you were not shown.
- Short. A turn is a few sentences.

Doing nothing is a valid and frequently correct output. Two agents agreeing to
adopt something neither of them would act on is the worst available result,
because it spends a slot the account has no room for.
"""


def render_dream(dream: Dream) -> str:
    """The dream as both agents see it.

    **The chain never appears without its verification badge and its weakest
    hop.** They are rendered above it rather than below, because a reader who
    stops early must stop having read the qualification rather than the claim.
    An unqualified chain reads as established fact, and the whole reason
    `Hop.checked` exists is that some of those sentences were invented.
    """
    out: list[str] = [
        f"Dream {dream.id}: {dream.title}",
        f"  spark: {dream.seed}",
        f"  verification: {dream.verification} "
        f"({sum(1 for h in dream.chain if h.checked)} of {len(dream.chain)} hops sourced)",
        f"  weakest hop: {dream.weakest_hop or 'NOT NAMED — nobody has attacked this yet'}",
    ]
    if dream.instruments:
        out.append(f"  about: {', '.join(dream.instruments)}")
    out.append(
        "  symbols claimed: "
        + (", ".join(dream.symbols) if dream.symbols else "none — nothing to grant")
    )
    out.append(f"  instrument class: {dream.asset_class_key or 'UNRESOLVED'}")

    if dream.chain:
        out.append("  chain:")
        for index, hop in enumerate(dream.chain, 1):
            mark = f"checked — {hop.source}" if hop.checked else "UNCHECKED"
            out.append(f"    hop {index} ({mark}): {hop.claim}")
    else:
        out.append("  chain: empty, so nothing here has been made checkable")

    if dream.conditions:
        out.append("  conditions:")
        for condition in dream.conditions:
            # Three shapes, not two. "Prose only" was true of everything
            # without a number until an OBSERVATION — a subject, a claim and a
            # review date, settled by the operator — became the other way onto
            # the prophecy shelf, and calling one of those prose would tell the
            # trading agent that a claim somebody deliberately answered was
            # never checkable at all.
            #
            # Harmless today, because a dream only reaches a conference once
            # every condition is answered. Recorded here rather than left for
            # the day that stops being true.
            if condition.is_checkable:
                shape = "checkable"
            elif condition.is_observable:
                shape = "observed, settled by the operator"
            else:
                shape = "prose only"
            # `ruled_out` is its own answer. Folding it into "not met" would
            # report a claim a person looked at and refuted as one nobody has
            # got to yet.
            if condition.fulfilled:
                state = "met"
            elif condition.ruled_out:
                state = "ruled out"
            else:
                state = "not met"
            out.append(f"    [{state}, {shape}] {condition.text}")
    else:
        # `all_conditions_met` is False on an empty list on purpose, and the
        # sentence has to say the same thing: no conditions is not all
        # conditions met.
        out.append("  conditions: none were ever pre-registered on this dream")

    if dream.trigger:
        out.append(f"  trigger: {dream.trigger}")
    return "\n".join(out)


def render_transcript(messages: Sequence[DreamMessage]) -> str:
    """Everything said about this dream so far, oldest first.

    The whole history rather than the current exchange, because the interesting
    part of a negotiation is usually where somebody changed their mind, and an
    agent shown only today's turns would ask a question it was answered last
    month. Bounded by the caps above rather than by a slice here: three
    exchanges of six turns is a short document.
    """
    if not messages:
        return "Nothing has been said about this dream yet."
    return "\n".join(
        f"[{m.at:%d %b %H:%M}] {m.speaker} ({m.kind}): {m.text}" for m in messages
    )


def render_classes(rules: Rules) -> str:
    """Which instrument classes are open, for the trading agent's side.

    Disabled classes are NAMED rather than omitted, which is the opposite of
    what `build_system_prompt` does for the decision loop and is deliberate.
    There, listing a disabled class only invites proposals for it. Here the
    trading agent is being asked whether to grant a symbol permission, and a
    grant in a blocked class is the one thing that must never happen, so it has
    to be able to see the block rather than infer it from an absence.
    """
    lines: list[str] = []
    for name, instrument in sorted(rules.instruments.items()):
        if instrument.enabled:
            lines.append(f"  {name} — ENABLED ({len(instrument.allowed_symbols)} listed symbols)")
        else:
            lines.append(f"  {name} — BLOCKED. No dream may grant a symbol here.")
    return "\n".join(lines) if lines else "  none configured"


def _turn_prompt(
    dream: Dream,
    messages: Sequence[DreamMessage],
    *,
    instruction: str,
    classes: str = "",
) -> str:
    blocks = [render_dream(dream), "", "Conversation so far:", render_transcript(messages)]
    if classes:
        blocks += ["", "Instrument classes:", classes]
    blocks += ["", instruction]
    return "\n".join(blocks)


class Conference:
    """One run of the agent-to-agent conference.

    Takes the store rather than reaching for one, exactly as `Dreamer` does, so
    a test never touches `data/`. Takes its two clients too, so every cap is
    exercised offline.
    """

    def __init__(
        self,
        env: Env,
        rules: Rules,
        store: DreamStore,
        *,
        dreamer_client: TurnCaller | None = None,
        trader_client: TurnCaller | None = None,
    ) -> None:
        self._rules = rules
        self._store = store
        self._powers = TraderPowers(store)
        self._dreamer = dreamer_client or self._build(env, GROGU, DREAMER_SYSTEM_PROMPT)
        self._trader = trader_client or self._build(env, YODA, TRADER_SYSTEM_PROMPT)

    def _caps(self, caps: VaultCaps | None) -> VaultCaps:
        """The caller's caps, or the ones in `config/rules.yaml`.

        Never `None` onward, so the store's dataclass defaults are unreachable
        from this path. They were reachable: `caps` travelled all the way down
        as an optional and `DreamingRules.vault_caps()` had exactly one caller,
        so a `caps.vault: 1` in the file admitted three. A limit an operator can
        read has to be the limit that applies, or the file is decoration.
        """
        return caps or self._rules.dreaming.vault_caps()

    @staticmethod
    def _build(env: Env, soul_name: str, system: str) -> TurnCaller:
        """One client per speaker, because they are two different characters.

        A missing soul degrades to a voiceless prompt rather than raising, which
        is `souls.py`'s own rule: a character is decoration on a surface whose
        job is to keep working, and an unreadable file on the box must not stop
        two agents talking.

        **Not cached, same as the dreamer, and it is a closer call here.** A 1h
        cache write bills at 2x base input and a read at 0.1x, so caching pays
        only from the third call to the same party. An exchange that ends on the
        trader's first turn — an immediate accept or decline, which is a normal
        and desirable outcome — makes exactly one call per party and would pay
        double for the privilege. Runs are a day apart, so nothing carries over
        between them. The conservative choice is the one that cannot be worse
        than 2x.
        """
        soul = load_soul(soul_name)
        prompt = f"{soul.prompt_prefix()}\n{system}" if soul.found else system
        return ModelClient(env, prompt, tier=env.dream_tier, cache_system=False)

    # ------------------------------------------------------------- the run

    def run(
        self, *, now: datetime | None = None, caps: VaultCaps | None = None
    ) -> ConferenceReport:
        """Confer up to `MAX_DREAMS_PER_RUN` dreams out of the vault.

        Candidates are the dreams in `Vault.VAULT`, which is the only shelf the
        trading agent can see, **longest-waiting first**. `DreamStore.in_vault`
        answers newest-first, so the order is reversed: an offer nobody has
        answered is the one worth answering, and taking the newest first would
        let a productive dreamer starve its own older offers indefinitely.

        Skips cost no model call and are recorded in the report and in the log
        rather than in the transcript. That is on purpose: a `note` written
        every day saying "nothing has changed" would fill the record a human
        reads with the exact noise the change gate exists to prevent, and would
        do it in the one place where the noise is permanent.
        """
        stamp = now or datetime.now(UTC)
        limits = self._caps(caps)
        candidates = list(reversed(self._store.in_vault(Vault.VAULT)))

        results: list[ExchangeResult] = []
        conferred = 0
        for dream in candidates:
            if conferred >= MAX_DREAMS_PER_RUN:
                break
            if dream.id is None:  # pragma: no cover - in_vault only returns saved rows
                continue

            result = self._consider(dream, now=stamp, caps=limits)
            results.append(result)
            if result.conferred:
                conferred += 1

        report = ConferenceReport(exchanges=tuple(results), considered=len(candidates))
        log.info(
            "conference_complete",
            considered=report.considered,
            conferred=report.conferred,
            skipped=report.skipped,
            adopted=report.adopted,
            # Both, rather than one and a subtraction. A run where every
            # exchange settled nothing is a fact about the two agents, and a
            # zero stated each run is a fact rather than the absence of a
            # warning — the same reason the cycle line carries its breach count.
            decided=report.decided,
            undecided=report.undecided,
            calls=report.calls,
            cost_usd=round(report.cost_usd, 4),
        )
        return report

    def _consider(
        self, dream: Dream, *, now: datetime, caps: VaultCaps | None
    ) -> ExchangeResult:
        """Apply the free caps, then confer if all of them pass.

        Three questions, in order of how final the answer is: has this dream
        used its whole life, has it used the current epoch, and is there
        anything new to talk about. The first two cost a transcript read and the
        third costs nothing more, so a skip never reaches a model.
        """
        dream_id = int(dream.id or 0)
        messages = self._store.messages(dream_id)

        lifetime = exchanges_so_far(messages)
        if lifetime >= MAX_EXCHANGES_LIFETIME:
            # Warning rather than info, unlike every other skip here. The epoch
            # cap is an ordinary Tuesday; twelve exchanges on one dream is a
            # fact about the two agents that somebody should read.
            log.warning(
                "confer_lifetime_ceiling",
                dream_id=dream_id,
                exchanges=lifetime,
                detail=(
                    "This dream has been conferred to its lifetime ceiling. It "
                    "will not be conferred again whatever changes about it; the "
                    "dreamer can still rework it and the operator can still act "
                    "on it."
                ),
            )
            return ExchangeResult(
                dream_id=dream_id,
                outcome=ConferOutcome.LIFETIME_EXHAUSTED,
                detail=(
                    f"{lifetime} exchanges over this dream's life, which is the "
                    f"ceiling of {MAX_EXCHANGES_LIFETIME}. Nothing reopens it."
                ),
            )

        held = exchanges_in_epoch(dream, messages)
        if held >= MAX_EXCHANGES_PER_EPOCH:
            log.info(
                "confer_skipped",
                dream_id=dream_id,
                reason="exchanges_exhausted",
                exchanges=held,
                lifetime=lifetime,
            )
            return ExchangeResult(
                dream_id=dream_id,
                outcome=ConferOutcome.EXCHANGES_EXHAUSTED,
                detail=(
                    f"{held} exchanges since the last thing that changed, which "
                    "is the cap. A fourth would be the same argument again. "
                    "Something changing about the dream starts a fresh three."
                ),
            )

        change = has_something_changed(
            dream, last_agent_turn_at(messages), messages=messages
        )
        if (
            not change
            and not self._handed_back_since_the_last_offer(messages)
            and not self._blocked_by_a_full_shelf(messages, now=now, caps=caps)
        ):
            log.info("confer_skipped", dream_id=dream_id, reason="nothing_new")
            return ExchangeResult(
                dream_id=dream_id,
                outcome=ConferOutcome.NOTHING_NEW,
                detail=change.summary,
            )

        return self.confer_once(dream, now=now, caps=caps)

    @staticmethod
    def _handed_back_since_the_last_offer(messages: Sequence[DreamMessage]) -> bool:
        """Did the trading agent take this dream and give it back since they last spoke?

        **A hand-back is a new situation by definition** — it was adopted, it
        came back, and the reason it came back is information neither of them
        had during the last exchange. So it reopens the conversation and it
        resets the exchange epoch.

        It needs its own answer for a mechanical reason worth knowing.
        `DreamStore.return_to_vault` writes the `return` message and stamps
        `updated_at` and `vault_entered_at` in one transaction, at one instant,
        so the marker `has_something_changed` measures against IS the moment of
        the change — and that gate's comparison is strict on purpose, because a
        non-strict one would hand the two agents a fresh round created by the
        end of every exchange. Loosening it to catch this would break the thing
        it is for. This looks at the fact instead: a `return` later than the
        last offer means an adoption happened and ended since they last talked.

        Deliberately narrow, and self-clearing. Once the next exchange writes
        its offer, the same `return` is older than it and stops qualifying, so
        one hand-back buys one conversation. Same shape as
        `_blocked_by_a_full_shelf`, which is the other thing that changes
        without the dream changing.
        """
        returns = [m for m in messages if m.speaker == TRADER and m.kind == "return"]
        if not returns:
            return False
        offers = [m.at for m in messages if m.speaker == DREAMER and m.kind == "offer"]
        # No offer at all means the dream was adopted and handed back without
        # the two of them ever conferring — through the MCP tool, or the vault
        # expiry command. That is the strongest case for a conversation, not the
        # weakest.
        return not offers or returns[-1].at > max(offers)

    def _blocked_by_a_full_shelf(
        self, messages: Sequence[DreamMessage], *, now: datetime, caps: VaultCaps | None
    ) -> bool:
        """Did the last exchange end in a refused adoption that could now succeed?

        **The change gate measures the DREAM, and this blocker was the STORE.**
        An exchange that ended `ADOPTION_REFUSED` — almost always a full adopted
        shelf — leaves the dream in the vault with nothing about it altered, so
        `has_something_changed` says no and says no again tomorrow. The two
        agents agreed, the shelf was full, and the dream was then skipped
        forever, including after a slot freed. It is the one blocker that clears
        without anybody touching the dream.

        Deliberately narrow. It re-qualifies only when the shelf actually has
        room now, so a dream refused for a reason that has not changed — no
        symbols, an unresolved class, symbols it does not offer — stays skipped
        rather than buying a model call a day to be told the same thing. That is
        the whole point of the fifth cap and this must not become a way round
        it.
        """
        if not messages:
            return False
        marker = last_agent_turn_at(messages)
        refused = [m for m in messages if m.kind == REFUSAL_MARK_KIND]
        if not refused:
            return False
        # The mark is written at the end of the exchange it belongs to, so a
        # later agent turn means a later exchange has happened since and this
        # one is no longer the thing standing in the way.
        if marker is not None and refused[-1].at < marker:
            return False
        return self._store.has_room(Vault.ADOPTED, now=now, caps=self._caps(caps))

    # -------------------------------------------------------- one exchange

    def confer_once(
        self, dream: Dream, *, now: datetime, caps: VaultCaps | None = None
    ) -> ExchangeResult:
        """One bounded exchange on one dream. Never raises.

        Turns alternate, dreamer first, up to `MAX_TURNS_PER_EXCHANGE`. The
        exchange ends EARLY on a decision — the trading agent accepting,
        declining or deferring, or the dreamer withdrawing its own dream; it
        ends on a failed call with `CALL_FAILED`; and it ends at the turn cap
        with `TURNS_EXHAUSTED`.

        **Every ending is written to the transcript AND recorded as exactly one
        verdict, including the endings that decided nothing.** A dream the
        trading agent kept declining is a fact about the dreamer worth having, a
        conversation that ran out of turns is a fact about both of them, and a
        record holding only the exchanges that concluded would flatter everyone
        in it. The verdict for an ending that settled nothing is
        `ConferenceVerdict.NO_DECISION` with a stated cause — never the mildest
        real decision, which is what makes a silent failure look healthy.
        """
        dream_id = int(dream.id or 0)
        usage: list[CallUsage] = []
        turns = 0

        for index in range(MAX_TURNS_PER_EXCHANGE):
            speaks_first = index % 2 == 0
            messages = self._store.messages(dream_id)

            try:
                if speaks_first:
                    turn = self._dreamer_turn(dream, messages, opening=index == 0)
                else:
                    turn = self._trader_turn(
                        dream,
                        messages,
                        # The trading agent gets one turn after each of the
                        # dreamer's, so its LAST is the final turn of the
                        # exchange. It is told so, because a model that does not
                        # know it is out of turns writes another question.
                        final=index >= MAX_TURNS_PER_EXCHANGE - 1,
                    )
                usage.append(turn.usage)
            except _TurnFailed as failure:
                return self._failed(
                    dream_id, failure, turns=turns, usage=usage, now=now
                )

            turns += 1
            self._store.add_message(
                dream_id,
                speaker=DREAMER if speaks_first else TRADER,
                kind=_kind_for(index, decided=turn.decided),
                text=turn.text,
                at=now,
            )

            if turn.decided:
                return self._settle(
                    dream_id,
                    turn,
                    turns=turns,
                    usage=usage,
                    now=now,
                    caps=caps,
                )

        remaining = MAX_EXCHANGES_PER_EPOCH - exchanges_in_epoch(
            dream, self._store.messages(dream_id)
        )
        self._note(
            dream_id,
            f"Six turns and no verdict. The exchange is closed; "
            f"{max(remaining, 0)} exchange(s) remain before something has to "
            "change about this dream.",
            at=now,
        )
        log.info("confer_turns_exhausted", dream_id=dream_id, turns=turns)
        return self._closing(
            ExchangeResult(
                dream_id=dream_id,
                outcome=ConferOutcome.TURNS_EXHAUSTED,
                turns=turns,
                detail="six turns, no verdict",
                usage=tuple(usage),
                decision=self._record(
                    dream_id,
                    ConferenceVerdict.NO_DECISION,
                    by=CONFERENCE,
                    reason=(
                        "Six turns and neither of them reached a verdict. They "
                        "talked and did not converge; nobody decided anything."
                    ),
                    at=now,
                    turns=turns,
                    undecided=NoDecision.TURNS_EXHAUSTED,
                ),
            ),
            now=now,
        )

    # ------------------------------------------------------------- verdicts

    def _settle(
        self,
        dream_id: int,
        turn: _Turn,
        *,
        turns: int,
        usage: list[CallUsage],
        now: datetime,
        caps: VaultCaps | None,
    ) -> ExchangeResult:
        """Act on whatever was decided, and record it as exactly one verdict.

        Four decisions are reachable from here and each is dispatched to its own
        method, because they differ in who decided, what the store is asked to
        do, and whether anything can refuse. What they share is the last line of
        each: one `ConferenceDecision`, carrying the deciding agent's own words.

        `decline` and `defer` move nothing, and that is not an omission. The
        trading agent may not move a dream between the dreamer's shelves —
        `DreamStore.move` refuses it by actor — so a deferral here is a state of
        the conversation and never a state of the vault. Implementing it as a
        move would be this module routing around the store, which is the one
        thing the store's actor rules exist to prevent.
        """
        if turn.withdraw:
            return self._withdraw(
                dream_id, turn, turns=turns, usage=usage, now=now, caps=caps
            )
        if turn.verdict is TraderVerdict.ACCEPT:
            return self._promote(
                dream_id, turn, turns=turns, usage=usage, now=now, caps=caps
            )
        if turn.verdict is TraderVerdict.DEFER:
            return self._defer(dream_id, turn, turns=turns, usage=usage, now=now)
        return self._decline(dream_id, turn, turns=turns, usage=usage, now=now)

    def _promote(
        self,
        dream_id: int,
        turn: _Turn,
        *,
        turns: int,
        usage: list[CallUsage],
        now: datetime,
        caps: VaultCaps | None,
    ) -> ExchangeResult:
        """The trading agent adopted. The verdict is PROMOTE either way.

        **A refused adoption is still a PROMOTE verdict**, with `effected=False`
        and the store's reason beside it. The two of them agreed and the shelf
        had no room, and those are two facts: recording the refusal as though
        nothing had been decided would throw away the agreement, which is the
        half that says something about the agents. It is a refusal rather than a
        failure — a full adopted vault is a normal Tuesday — so it is reported
        and never raised.
        """
        result = self._powers.adopt(
            dream_id,
            at=now,
            caps=self._caps(caps),
            # From the rules file rather than the dataclass default, so the
            # expiry an operator can read is the one that actually applies.
            ttl_days=self._rules.dreaming.ttl_days.adopted,
        )
        decision = self._record(
            dream_id,
            ConferenceVerdict.PROMOTE,
            by=TRADER,
            reason=turn.text,
            at=now,
            turns=turns,
            effected=result.ok,
            effect_detail=result.detail,
        )

        if not result.ok:
            self._note(
                dream_id,
                "The trading agent accepted, and the store refused the "
                f"adoption: {result.detail}",
                at=now,
                kind=REFUSAL_MARK_KIND,
            )
            log.info(
                "confer_adoption_refused",
                dream_id=dream_id,
                refusals=[str(r) for r in result.refusals],
            )
            return self._closing(
                ExchangeResult(
                    dream_id=dream_id,
                    outcome=ConferOutcome.ADOPTION_REFUSED,
                    turns=turns,
                    detail=result.detail,
                    usage=tuple(usage),
                    decision=decision,
                ),
                now=now,
            )

        log.info("confer_adopted", dream_id=dream_id, turns=turns)
        return ExchangeResult(
            dream_id=dream_id,
            outcome=ConferOutcome.ADOPTED,
            turns=turns,
            detail=result.detail,
            usage=tuple(usage),
            decision=decision,
        )

    def _decline(
        self,
        dream_id: int,
        turn: _Turn,
        *,
        turns: int,
        usage: list[CallUsage],
        now: datetime,
    ) -> ExchangeResult:
        """Not for me, on the merits. Nothing moves and nothing is asked of the store.

        `effected` stays `None` rather than `True`: this verdict asks nothing of
        any shelf, and "nothing was required" is a different answer from "what
        was required was done".
        """
        log.info("confer_verdict", dream_id=dream_id, verdict="decline")
        return self._closing(
            ExchangeResult(
                dream_id=dream_id,
                outcome=ConferOutcome.DECLINED,
                turns=turns,
                detail="the trading agent declined it",
                usage=tuple(usage),
                decision=self._record(
                    dream_id,
                    ConferenceVerdict.DECLINE,
                    by=TRADER,
                    reason=turn.text,
                    at=now,
                    turns=turns,
                ),
            ),
            now=now,
        )

    def _defer(
        self,
        dream_id: int,
        turn: _Turn,
        *,
        turns: int,
        usage: list[CallUsage],
        now: datetime,
    ) -> ExchangeResult:
        """Not now, and here is what would change that — or it is not a deferral.

        **The rule that REJECTS.** A wake condition has to be gradeable: a
        symbol, a figure, an operator and a number. Prose alone, or a triple with
        nothing to look it up against, is a deferral that names nothing, and a
        deferral that names nothing is "we ran out of things to say" wearing a
        decision's clothes. It is recorded as `NO_DECISION` with
        `DEFER_WITHOUT_WAKE` rather than as the mildest decision available.

        The transcript is told which half was missing, because the dreamer reads
        it and "no decision" with no explanation teaches nobody anything.
        """
        wake = turn.wake
        if wake is None or not wake.is_gradeable:
            missing = (
                "no wake condition at all"
                if wake is None
                else "a wake condition with no symbol and threshold to check"
            )
            self._note(
                dream_id,
                "The trading agent said defer and gave "
                f"{missing}. A deferral that names nothing is not a deferral, "
                "so this exchange is recorded as having decided nothing. A "
                "condition needs a symbol, a figure, an operator and a number; "
                "a decline is the honest answer when none can be named.",
                at=now,
            )
            log.warning(
                "confer_defer_without_wake", dream_id=dream_id, turns=turns
            )
            return self._closing(
                ExchangeResult(
                    dream_id=dream_id,
                    outcome=ConferOutcome.DEFER_REFUSED,
                    turns=turns,
                    detail=f"a deferral with {missing}",
                    usage=tuple(usage),
                    decision=self._record(
                        dream_id,
                        ConferenceVerdict.NO_DECISION,
                        by=CONFERENCE,
                        reason=(
                            "The trading agent reached for a deferral and gave "
                            f"{missing}. Nothing was decided, and nothing would "
                            "wake it."
                        ),
                        at=now,
                        turns=turns,
                        undecided=NoDecision.DEFER_WITHOUT_WAKE,
                    ),
                ),
                now=now,
            )

        log.info(
            "confer_verdict",
            dream_id=dream_id,
            verdict="defer",
            wake=f"{wake.symbol} {wake.field} {wake.op} {wake.value}",
        )
        return self._closing(
            ExchangeResult(
                dream_id=dream_id,
                outcome=ConferOutcome.DEFERRED,
                turns=turns,
                detail=f"deferred until {wake.text}",
                usage=tuple(usage),
                decision=self._record(
                    dream_id,
                    ConferenceVerdict.DEFER,
                    by=TRADER,
                    reason=turn.text,
                    at=now,
                    turns=turns,
                    wake=wake,
                ),
            ),
            now=now,
        )

    def _withdraw(
        self,
        dream_id: int,
        turn: _Turn,
        *,
        turns: int,
        usage: list[CallUsage],
        now: datetime,
        caps: VaultCaps | None,
    ) -> ExchangeResult:
        """The dreamer retired its own dream. **The only route to ARCHIVE here.**

        `by=DREAMER`, and the store is what enforces that: `DreamStore.move`
        refuses `TRADER` outright and this module has no way to ask as anybody
        else. Nothing is destroyed — the archive is a shelf, and `delete` is a
        different method that this module never calls and whose name
        `tests/test_confer.py` asserts does not appear in this file.

        A refusal from the store is reported the same way a refused adoption is:
        the verdict stands, `effected` is False, and the reason travels with it.
        The caps travel too, so the archive limit an operator can read in
        `config/rules.yaml` is the one that applies here — the miss
        `TraderPowers.adopt` already made once, where a `ttl_days` that never
        reached the store left a figure in the file governing nothing.
        """
        result = self._store.move(
            dream_id,
            Vault.ARCHIVE,
            by=DREAMER,
            reason=turn.text,
            at=now,
            caps=self._caps(caps),
        )
        decision = self._record(
            dream_id,
            ConferenceVerdict.ARCHIVE,
            by=DREAMER,
            reason=turn.text,
            at=now,
            turns=turns,
            effected=result.ok,
            effect_detail=result.detail,
        )

        if not result.ok:
            self._note(
                dream_id,
                "The dreamer withdrew this dream, and the store refused the "
                f"move: {result.detail}",
                at=now,
            )
            log.warning(
                "confer_withdrawal_refused",
                dream_id=dream_id,
                refusals=[str(r) for r in result.refusals],
            )
            return self._closing(
                ExchangeResult(
                    dream_id=dream_id,
                    outcome=ConferOutcome.WITHDRAWAL_REFUSED,
                    turns=turns,
                    detail=result.detail,
                    usage=tuple(usage),
                    decision=decision,
                ),
                now=now,
            )

        log.info("confer_withdrawn", dream_id=dream_id, turns=turns)
        return ExchangeResult(
            dream_id=dream_id,
            outcome=ConferOutcome.WITHDRAWN,
            turns=turns,
            detail=result.detail,
            usage=tuple(usage),
            decision=decision,
        )

    def _failed(
        self,
        dream_id: int,
        failure: _TurnFailed,
        *,
        turns: int,
        usage: list[CallUsage],
        now: datetime,
    ) -> ExchangeResult:
        """A model call that produced no turn. **Recorded as a failure.**

        Never as a completed exchange that decided nothing, and never as a
        deferral: the verdict is `NO_DECISION` with `CALL_FAILED`, which says the
        machinery broke rather than that the two of them weighed it up. Nothing
        can be concluded about either agent from a call that did not return.

        `_closing` is deliberately not called. That method says a dream has
        parked or is closed for good, and a failed call has decided nothing about
        the dream's budget — a failure before the opening offer does not even
        count as an exchange, so tomorrow's run tries again.
        """
        self._note(
            dream_id,
            f"The exchange ended after {turns} turn(s): the model call "
            f"failed ({failure.detail}). Nothing was decided.",
            at=now,
        )
        log.warning(
            "confer_call_failed",
            dream_id=dream_id,
            turns=turns,
            error=failure.detail,
        )
        return ExchangeResult(
            dream_id=dream_id,
            outcome=ConferOutcome.CALL_FAILED,
            turns=turns,
            detail=failure.detail,
            usage=tuple(usage),
            decision=self._record(
                dream_id,
                ConferenceVerdict.NO_DECISION,
                by=CONFERENCE,
                reason=(
                    f"The model call failed after {turns} turn(s): "
                    f"{failure.detail}. Nobody decided anything, and nothing "
                    "about either agent can be read off this."
                ),
                at=now,
                turns=turns,
                undecided=NoDecision.CALL_FAILED,
            ),
        )

    def _record(
        self,
        dream_id: int,
        verdict: ConferenceVerdict,
        *,
        by: str,
        reason: str,
        at: datetime,
        turns: int,
        wake: DreamCondition | None = None,
        effected: bool | None = None,
        effect_detail: str = "",
        undecided: NoDecision | None = None,
    ) -> ConferenceDecision:
        """Store the one verdict this exchange reached. The single write path.

        Every ending goes through here, so "does every exchange record a
        verdict" is answerable by counting the callers rather than by reading
        the whole class. `DreamStore.record_conference_decision` refuses an
        incoherent row, and the callers above are the reason it can never see
        one: a deferral is only ever built with a gradeable condition, and a
        `NO_DECISION` only ever with a cause.
        """
        return self._store.record_conference_decision(
            ConferenceDecision(
                dream_id=dream_id,
                verdict=verdict,
                decided_by=by,
                at=at,
                reason=reason,
                wake=wake,
                effected=effected,
                effect_detail=effect_detail,
                undecided=undecided,
                turns=turns,
            )
        )

    def _closing(self, result: ExchangeResult, *, now: datetime) -> ExchangeResult:
        """Say once, at the moment it becomes true, that a dream has parked.

        **This is the CAP parking a dream and never the trading agent deferring
        one**, and the two are worth keeping apart now that a deferral is a
        verdict with a wake condition on it. What is said here is that the
        exchange budget is spent; what would restart it is something changing
        about the dream, not a threshold either agent named.

        Written here rather than on the next run's skip, because "this was the
        third exchange" is a fact with a moment attached and the skip path has
        no way to know whether it has already said so. One note, at the boundary
        — the same reasoning as the tailnet banner naming the command that
        clears it: a warning that repeats teaches an operator to ignore it.

        **Two boundaries, two sentences, and they must not be one.** The epoch
        cap parks the dream UNTIL SOMETHING CHANGES, and the note says what
        would restart it, because a note that read "they will not confer this
        again" when a fired condition would reopen it tomorrow is a plausible
        wrong statement about the system's own behaviour. The lifetime ceiling
        is the one that means never, and it says so.

        An adopted dream never reaches here: it has left the vault, so it is not
        a candidate again and either sentence would be false.

        **The comparisons are `==` rather than `>=`, and that is what "once"
        means.** An exchange adds exactly one offer, so the count passes through
        the cap rather than jumping it, and `confer_once` is reachable directly
        from a command — with `>=` it would repeat the note on every exchange
        past the cap, which is the thing this method exists to prevent.
        """
        messages = self._store.messages(result.dream_id)
        lifetime = exchanges_so_far(messages)
        dream = self._store.get(result.dream_id)
        held = exchanges_in_epoch(dream, messages) if dream is not None else lifetime

        if lifetime == MAX_EXCHANGES_LIFETIME:
            self._note(
                result.dream_id,
                f"That was exchange {lifetime}, the lifetime ceiling of "
                f"{MAX_EXCHANGES_LIFETIME}. The two of them will not confer this "
                "dream again whatever changes about it. The dreamer can still "
                "rework it, and the operator can still act on it.",
                at=now,
            )
            log.warning(
                "confer_dream_closed", dream_id=result.dream_id, exchanges=lifetime
            )
        elif held == MAX_EXCHANGES_PER_EPOCH:
            self._note(
                result.dream_id,
                f"That was exchange {held} of {MAX_EXCHANGES_PER_EPOCH} since "
                "the last thing that changed. This dream parks until something "
                "does change — a condition firing, a hop checked or added, an "
                "operator note, a rework, or the trading agent handing it back. "
                "Any of those starts a fresh three.",
                at=now,
            )
            log.info(
                "confer_dream_parked",
                dream_id=result.dream_id,
                exchanges=held,
                lifetime=lifetime,
            )
        return result

    # ---------------------------------------------------------------- calls
    #
    # Both return the same `_Turn` — what was said, what if anything was decided,
    # and what the call cost — so the exchange loop treats the two speakers
    # identically. The asymmetry is in what each CAN decide: the trading agent
    # reaches a verdict about taking the dream, and the dreamer's one decision is
    # withdrawing its own.

    def _dreamer_turn(
        self, dream: Dream, messages: Sequence[DreamMessage], *, opening: bool
    ) -> _Turn:
        instruction = (
            "Offer this dream to the trading agent. Say what it claims, what it "
            "rests on, and what would break it first."
            if opening
            else "Answer the trading agent's last turn. Do not add to the chain."
        )
        turn, usage = self._call(
            self._dreamer,
            _turn_prompt(dream, messages, instruction=instruction),
            DreamerTurn,
        )
        # The dreamer carries no TRADER verdict — it offers, and the agent that
        # has to answer for the consequence is the one that decides whether to
        # take it, which is the asymmetry `DreamStore.move` enforces from the
        # other direction. What it can decide is to withdraw its own dream, and
        # that is the only ARCHIVE anywhere in this module.
        return _Turn(
            text=str(turn.text), usage=usage, withdraw=bool(turn.withdraw)
        )

    def _trader_turn(
        self, dream: Dream, messages: Sequence[DreamMessage], *, final: bool
    ) -> _Turn:
        instruction = (
            "This is your LAST turn. You must set a verdict: accept, decline or "
            "defer — and a defer needs its wake condition. A question here is a "
            "wasted turn and the exchange closes without a decision."
            if final
            else "Ask the one question that would settle this, or set a verdict "
            "now if you already know."
        )
        turn, usage = self._call(
            self._trader,
            _turn_prompt(
                dream,
                messages,
                instruction=instruction,
                classes=render_classes(self._rules),
            ),
            TraderTurn,
        )
        verdict = turn.verdict
        wake = turn.wake
        return _Turn(
            text=str(turn.text),
            usage=usage,
            verdict=verdict if isinstance(verdict, TraderVerdict) else None,
            # Converted here rather than in `_defer`, so the only shape that
            # travels past this line is the one the prophecy shelf already
            # grades. A wake condition on anything but a deferral is dropped: a
            # condition attached to an acceptance says nothing, and storing one
            # would put a threshold on a decision that does not wait for it.
            wake=(
                wake.as_condition()
                if isinstance(wake, WakeCondition) and verdict is TraderVerdict.DEFER
                else None
            ),
        )

    def _call(
        self, client: TurnCaller, prompt: str, schema: type[BaseModel]
    ) -> tuple[Any, CallUsage]:
        """One model call, with every failure turned into `_TurnFailed`.

        The broad `except` is the same decision as `fetch_market_ticks` and
        `Dreamer.run_once`, taken for the same reason: there is no exception
        from an HTTP client or a schema validator worth crashing a scheduled job
        over, and an unanticipated one is exactly the case where crashing is
        worst. This runs unattended on a timer, and the thing it would take down
        is not this exchange — it is whatever else the command was going to do.
        """
        try:
            turn, usage = client.confer(prompt, schema)
        except (anthropic.APIError, ValueError, RuntimeError) as exc:
            raise _TurnFailed(str(exc)) from exc
        except Exception as exc:
            raise _TurnFailed(repr(exc)) from exc
        if turn is None:  # pragma: no cover - the client raises instead
            raise _TurnFailed("the model returned no parsable turn")
        return turn, usage

    def _note(
        self, dream_id: int, text: str, *, at: datetime, kind: str = "note"
    ) -> None:
        """This module narrating itself into the transcript.

        Speaker `CONFERENCE`, never `OPERATOR` and never one of the agents, so
        it cannot be mistaken for a turn and cannot move the change gate's
        marker. See `last_agent_turn_at`.

        `kind` is how the refusal note stays findable. Matching on the prose
        would break the moment somebody reworded the sentence, which is the same
        reason `vault-expire` writes its own kind rather than looking for its
        own wording.
        """
        self._store.add_message(
            dream_id, speaker=CONFERENCE, kind=kind, text=text, at=at
        )


class _TurnFailed(Exception):
    """A model call that did not produce a turn. Never escapes this module."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class _Turn:
    """One speaker's turn, as the exchange loop needs it.

    Private, and deliberately not the pydantic model the API returned. The
    schemas are what a MODEL may write; this is what this module has decided the
    turn means, with the wake condition already converted into the shape the
    store keeps and a verdict that could not survive validation already dropped.
    Keeping them apart is what stops a model's field reaching a database
    unexamined.
    """

    text: str
    usage: CallUsage
    verdict: TraderVerdict | None = None
    wake: DreamCondition | None = None
    withdraw: bool = False

    @property
    def decided(self) -> bool:
        """Whether this turn ends the exchange.

        The dreamer's withdrawal counts, which is the whole reason this is a
        property rather than `verdict is not None` written at the call site: an
        exchange now has two ways to end in a decision and only one of them is
        the trading agent's.
        """
        return self.verdict is not None or self.withdraw


def _kind_for(index: int, *, decided: bool) -> str:
    """Which `MESSAGE_KINDS` badge a turn gets.

    Decided from the turn's position and never by the model, so a speaker cannot
    mislabel its own turn.

    A verdict turn is a `note` rather than a new kind. `MESSAGE_KINDS` is the
    vocabulary the operator named — question, answer, offer, accept, return,
    note — and `accept` in it means the handover itself, which
    `DreamStore.adopt` writes for us so the transcript is complete even when
    neither agent thought to narrate it. Writing a second `accept` here would
    put two of them in the record for one event.

    **A withdrawal on the opening turn is a `note` and not an `offer`**, which
    matters beyond tidiness: `exchanges_so_far` counts opening offers, so filing
    a retirement as one would record an offer that was never made. A dreamer
    that withdraws instead of offering has not offered.
    """
    if index == 0:
        return "note" if decided else "offer"
    if index % 2 == 0:
        return "note" if decided else "answer"
    return "note" if decided else "question"


def run_conference(
    env: Env,
    rules: Rules,
    store: DreamStore,
    *,
    now: datetime | None = None,
    caps: VaultCaps | None = None,
    dreamer_client: TurnCaller | None = None,
    trader_client: TurnCaller | None = None,
) -> ConferenceReport:
    """One daily conference. **This is what a CLI command calls.**

    Deliberately the only entry point, and deliberately not reachable from
    `cmd_loop`. See the module docstring: the separation between "decides once a
    day whether a hypothesis is worth a permission" and "proposes orders every
    fifteen minutes" is a file boundary and a command boundary, because those
    survive a refactor and a comment does not.

    Never raises on a model failure. A store error propagates, as it does in
    `Dreamer`, because a caller that cannot read the vault has nothing useful to
    report either.
    """
    return Conference(
        env,
        rules,
        store,
        dreamer_client=dreamer_client,
        trader_client=trader_client,
    ).run(now=now, caps=caps)


__all__ = [
    "AGENT_SPEAKERS",
    "CONFERENCE",
    "MAX_DREAMS_PER_RUN",
    "MAX_EXCHANGES_LIFETIME",
    "MAX_EXCHANGES_PER_EPOCH",
    "MAX_TURNS_PER_EXCHANGE",
    "TEXT_MAX_CHARS",
    "Change",
    "ChangeKind",
    "ChangeSignal",
    "ConferOutcome",
    "Conference",
    # Re-exported for a renderer, which reads the DECISION and has no business
    # importing the module that runs the conversation. `dreaming.py` is where
    # these live; naming them here means a page can take everything it needs
    # from one import without that import being the machinery.
    "ConferenceDecision",
    "ConferenceReport",
    "ConferenceVerdict",
    "DreamerTurn",
    "ExchangeResult",
    "NoDecision",
    "TraderPowers",
    "TraderTurn",
    "TraderVerdict",
    "TurnCaller",
    "WakeCondition",
    "change_signals",
    "epoch_started_at",
    "exchanges_in_epoch",
    "exchanges_so_far",
    "has_something_changed",
    "last_agent_turn_at",
    "render_classes",
    "render_dream",
    "render_transcript",
    "run_conference",
]
