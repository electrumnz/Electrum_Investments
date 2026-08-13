"""The thing that actually dreams.

`dreaming.py` holds the shape of a dream and the store. This is what fills it:
one Claude call per run, producing either a new chain or the next step on an
existing one, written to `data/dreams.db` and rendered on `/dreaming`.

Driven by `electrum-bot dream`, deliberately not by the decision loop. Two
reasons, and the second matters more:

- **Cadence.** The loop wakes every fifteen minutes because a price moves that
  fast. A second-order supply-chain idea does not, and paying for a lateral
  thought every quarter hour would produce ninety-six shallow ones a day.
- **Separation.** The decision loop proposes orders. This does not, and keeping
  them in different processes means no future refactor accidentally lets a
  dream reach the code path that places one. `run_once` returns a `Dream`, which
  structurally cannot describe an order at all.

## What it is shown, and what it is deliberately not

Shown: the symbols under watch, recent headlines, recent posts, what closed
recently and what the gate refused. Enough to have something to pull on.

Also shown: **considerations** — things a chat agent put up with
`raise_consideration` while talking to the operator. They arrive as candidate
sparks the model may ignore, with their age and the operator's own words beside
them, and they are read out of the audit log by `news_history`. A consideration
is a note and never a dream: nothing in `dreaming.py` reads the audit log, so
there is no path from a conversation to a shelf row, and one becomes a seed only
by this model choosing to make it one — exactly as it chooses the seeds it had
by itself. **Do not add a path that promotes a consideration directly.** A dream
is the first link of a chain that ends in a live trading permission, and the
whole point of the tool being a note is that a conversation cannot insert at the
top of it.

Which run has seen which is recorded as its own audit event
(`dream_considerations_seen`), carrying the exact keys that were rendered.
Never a high-water stamp, never `now` — see `_mark_considerations_seen`.

**Not shown: profit and loss.** Not the equity curve, not the win rate, not
whether any of it made money. `souls/grogu.md` forbids learning from the track
record and this is where that is enforced rather than requested: the figures
never enter the prompt, so there is nothing to overfit to. What closed is given
as an EVENT ("this position closed on its stop"), never as an outcome ("this
position made $340"). The distinction is the whole reason this module can read
the journal at all.

## Where it may look, and where it may not

The operator's rule: **the dreamer may look outside `allowed_symbols` to other
Alpaca instruments, as long as it does not go around the hard blocks on GROUPS
of instruments.** Crypto disabled means the dreamer cannot see, name or dream
crypto. Enable crypto and it can — and the reverse the moment it is switched
off again.

So the watch list is a *starting point* here and the instrument CLASS is the
fence. `scope_symbols` is that fence in code, and the prompt states it in
words, and both are needed for the reason belt and braces is usually needed:
one of them is an instruction to a model and the other is arithmetic, and only
the second is a guarantee.

**A dropped symbol is recorded rather than quietly discarded.** It goes into
the log, onto `DreamerResult`, and into the dream's own transcript, because a
dreamer that keeps reaching for a blocked class is worth knowing about and an
invisible filter would look like a model that had simply stopped naming
symbols.

## Failure

Same shape as the decision loop's model call, learned the same way. The call is
wrapped, a failure logs and returns `None`, and nothing is written. A dream that
could not be had must not be recorded as a dream that decided nothing — and a
`ValidationError` escaping here would kill whatever timer is driving it and
restart straight into the same failure.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anthropic
import structlog
from pydantic import BaseModel, Field

from .audit import AuditLog
from .config import Env, ModelSpec, Rules
from .dreaming import (
    DREAMER,
    MAX_FUSION_PARENTS,
    MIN_FUSION_PARENTS,
    Dream,
    DreamCondition,
    DreamStage,
    DreamStore,
    DreamVerdict,
    FusionCandidate,
    FusionResult,
    Hop,
    Vault,
    VaultCaps,
    carry_forward_grading,
    fusion_candidates,
)
from .journal import Journal
from .model_client import EVERY_FIELD_REQUIRED, CallUsage, ModelClient
from .models import TriggerField, TriggerOp
from .models import class_key_for_symbol as _class_key_for_symbol
from .news_history import (
    CONSIDERATIONS_SEEN_EVENT,
    DEFAULT_CONSIDERATION_HOURS,
    SHOWN_FIELD,
    ConsiderationRecall,
    describe_age,
    recall_considerations,
)
from .souls import GROGU, load_soul
from .triggers import CycleReadings

log = structlog.get_logger(__name__)

# How many existing dreams to offer back for a next step. Small on purpose: the
# model picks one to advance, and a long list turns the choice into a survey.
CARRY_FORWARD = 4

# How much journal history to describe as events. Enough to notice a pattern,
# short enough that it cannot become a performance narrative.
RECENT_CLOSURES = 8

# How many stored dreams to scan for shared hops. Matched to the workbench cap
# rather than to `CARRY_FORWARD`: a fusion candidate is not something to advance,
# so the pool it comes from is "what the dreamer is holding" rather than "what it
# is working on this week". Scanning is arithmetic over chains already in memory,
# so the cost is a read rather than a model call.
FUSION_POOL = 24

# How many candidate fusions to offer. Small for the reason `CARRY_FORWARD` is
# small: the model picks at most one, and a long list turns a choice into a
# survey.
FUSION_OFFERS = 3

# Who a scoping note is from, in the dream's transcript. Deliberately neither
# `DREAMER` nor `TRADER`: it is a fact about the plumbing rather than a turn of
# anybody's conversation, and `confer.last_agent_turn_at` would otherwise read
# it as one.
SCOPE = "scope"

# How much of the audit log to open when looking for considerations, and how
# many dated files back.
#
# The read is bounded by entries rather than by time, so the bound has to
# comfortably outrun the window: a 96-cycle day means 400 entries is a little
# over four days, against a window of two. `AuditLog.read` stops as soon as it
# has enough, so the ordinary cost is one or two files.
CONSIDERATION_SCAN = 400
CONSIDERATION_DAYS = 7


# Which `instruments:` key a symbol's SHAPE says it belongs to. Re-exported from
# `models` rather than defined here, because the same question is asked by the
# broker's routing (`is_crypto_symbol`), by `grants.py` and by `RiskGate`, and an
# audit found this copy and the router's disagreeing about `BTC/USD`. It is a
# shape test rather than a lookup on purpose: the point of this feature is that
# the dreamer may name symbols nobody has listed anywhere, so a table of known
# symbols would refuse exactly the case it exists to permit.
class_key_for_symbol = _class_key_for_symbol


@dataclass(frozen=True)
class SymbolScope:
    """What survived the class fence, and what did not and why.

    `dropped` is a tuple of pairs rather than a dict so the order is the order
    the model named them, which is what a person reading the note wants, and so
    the whole thing stays comparable in a test.
    """

    kept: tuple[str, ...] = ()
    dropped: tuple[tuple[str, str], ...] = ()
    # The single `instruments:` key every kept symbol belongs to, or empty when
    # they span more than one. Empty is the fail-closed answer: `DreamStore.adopt`
    # refuses an unresolved class with `NEEDS_ASSET_CLASS`, and
    # `granted_symbols` drops a grant without one, because a symbol whose class
    # is unknown is a symbol whose limits are unknown.
    asset_class_key: str = ""

    @property
    def summary(self) -> str:
        """The sentence written into the dream's transcript. Empty if nothing went."""
        if not self.dropped:
            return ""
        named = "; ".join(f"{symbol} ({why})" for symbol, why in self.dropped)
        return (
            f"Dropped {len(self.dropped)} symbol(s) before storing this dream: "
            f"{named}. A blocked instrument class is a decision the operator "
            "made in config/rules.yaml, not a gap for a dream to route around."
        )


def scope_symbols(symbols: Sequence[str], rules: Rules) -> SymbolScope:
    """Keep only symbols that could belong to an ENABLED instrument class.

    The operator's rule in code: the dreamer may look past `allowed_symbols` to
    other Alpaca instruments, and may not look past the hard block on a class.
    So membership of the watch list is not required and membership of an enabled
    class is.

    **This is the second lock, not the only one.** `grants.resolve_granted_symbols`
    enforces the same block deterministically at permission time, which is what
    actually decides whether a symbol may be traded; do not treat this as the
    guarantee and do not delete it as a duplicate. One of the two is an
    instruction to a model, one of them is arithmetic, and only the second can
    be relied on — which is exactly why both are here. Same arrangement as
    `mode=ro` plus the statement guard in `insight.py`, and as `adopt` checking
    the asset class that `granted_symbols` also drops: the first check is the
    useful message, the second is the promise.

    Pure, and it takes `rules` rather than reading a file, so a test can turn a
    class on and off without touching the shipped config.
    """
    enabled = set(rules.enabled_instruments)
    kept: list[str] = []
    dropped: list[tuple[str, str]] = []
    classes: set[str] = set()
    seen: set[str] = set()

    for raw in symbols:
        symbol = str(raw).strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        # The watch list answers first, because a listed symbol's class is a
        # fact rather than an inference. Only an unlisted one falls back to its
        # shape, which is the case this feature exists for.
        key = rules.class_name_for(symbol) or class_key_for_symbol(symbol)
        if key and key in enabled:
            kept.append(symbol)
            classes.add(key)
        else:
            dropped.append(
                (symbol, f"{key or 'unclassifiable'} is not an enabled class")
            )

    return SymbolScope(
        kept=tuple(kept),
        dropped=tuple(dropped),
        # One class or nothing. A dream spanning two classes cannot say which
        # limits apply to it, and picking one would be choosing a risk cap by
        # accident — the same reason `granted_symbols` DROPS a symbol claimed by
        # two live grants rather than resolving it.
        asset_class_key=classes.pop() if len(classes) == 1 else "",
    )


class DreamHop(BaseModel):
    """One link, as the model returns it."""

    # Every field below is OPTIONAL in Python and REQUIRED on the wire. See
    # `model_client.EVERY_FIELD_REQUIRED`: an optional property doubles the
    # grammar the API has to compile, the cost multiplies across nested models,
    # and this schema was over the line — the dreamer could not make its call.
    model_config = EVERY_FIELD_REQUIRED

    claim: str = Field(description="One checkable claim. A physical or public fact.")
    checked: bool = Field(
        default=False,
        description=(
            "True ONLY if you were shown something that establishes this, or it "
            "is a matter of public record you can name. Guessing True here is "
            "the single worst thing you can do on this call."
        ),
    )
    source: str = Field(
        default="",
        description="Where it comes from. Empty when checked is false.",
    )


class StepCondition(BaseModel):
    """One pre-registered condition, as the model returns it.

    The same split `SymbolAssessment` and `AssessmentTrigger` already use, and
    it is here for the same reason: `text` is the sentence a person reads and
    the triple is what code grades. A condition with prose and no triple is
    legal and is counted as ungradeable rather than rejected — refusing it would
    push the dreamer towards inventing a number to satisfy a validator, and an
    invented threshold is worse than an honest sentence saying it cannot be
    checked yet.

    The triple stays optional in the sense that matters — `field`, `op` and
    `value` may all come back null and a condition with prose and no triple is
    still legal. What `EVERY_FIELD_REQUIRED` changes is that the model has to
    SAY null rather than leave the key out, which is a wire format and not a
    change to what a condition may be.

    **There is deliberately no field here that could carry an ANSWER**, and that
    absence is the whole safety argument for the observation half. A dream whose
    conditions are all met reaches the vault, a vaulted dream can be adopted,
    and an adoption is a live permission to trade a symbol that is in no
    allowlist — so a model able to say "this one is met" would be a model able
    to write itself a permission. It can propose the question; only
    `DreamStore.settle_condition` records the answer, and only the operator may
    call it. Structural rather than asked for politely, exactly as `Dream`
    carries no order fields. `tests/test_dreamer.py` pins the absence.
    """

    model_config = EVERY_FIELD_REQUIRED

    text: str = Field(
        description="The condition in one sentence, as a person would read it."
    )
    symbol: str = Field(
        default="",
        description=(
            "Which LISTED symbol the figure below belongs to. Without it the "
            "triple is a comparison with no subject and nothing can look the "
            "reading up. It does not have to be a symbol in `symbols` — a "
            "condition may hinge on something you would never trade."
        ),
    )
    settles_hops: list[int] = Field(
        default_factory=list,
        description=(
            "Which hop number(s) of the chain this condition would settle, "
            "using the `hop 1`, `hop 2` numbering shown above. A prophecy is a "
            "dream parked awaiting the link that could KILL it, so at least one "
            "checkable condition must settle the hop you named as weakest — a "
            "threshold on a link nobody doubted grades cleanly and settles "
            "nothing. List more than one hop where the condition honestly bears "
            "on more than one."
        ),
    )
    field: TriggerField | None = Field(
        default=None,
        description=(
            "The figure to check: close, sma_20, sma_200, atr_14, "
            "volume_ratio, distance_from_sma_20_atr, swing_high, swing_low, "
            "highest_close, lowest_close. Null when this condition is one only "
            "a person can settle."
        ),
    )
    op: TriggerOp | None = Field(
        default=None, description="above, below, at_or_above or at_or_below."
    )
    value: float | None = Field(
        default=None,
        description=(
            "The threshold, as a NUMBER and never the name of another figure. "
            "'Above the 20-day' re-checked next month tests a level nobody ever "
            "saw, because the average moved."
        ),
    )
    subject: str = Field(
        default="",
        description=(
            "OBSERVATION, part 1 of 3. The specific, findable thing a person "
            "would go and look at: a named report, a named register, a named "
            "company's own release. Not 'the market' and not 'the news'. Fill "
            "this trio INSTEAD of the triple above when no figure the loop "
            "records measures your claim — which is the normal case for a "
            "supply-chain chain. Leave all three empty otherwise."
        ),
    )
    observable: str = Field(
        default="",
        description=(
            "OBSERVATION, part 2 of 3. What that thing would have to SHOW for "
            "this condition to be met, written so a person can answer yes or "
            "no. No number is wanted here: this shape exists precisely for "
            "claims that are not about a figure."
        ),
    )
    observe_by: str = Field(
        default="",
        description=(
            "OBSERVATION, part 3 of 3. The date by which that answer should "
            "exist, as YYYY-MM-DD. A review date and not a deadline — nothing "
            "expires and nothing fails when it passes, it simply moves to the "
            "operator's list as due. Required: 'someday' can never come due, so "
            "nobody is ever asked and the dream waits forever."
        ),
    )


class DreamStep(BaseModel):
    """What one run produces.

    `advance_id` is what makes this a mini-project rather than a stream of
    unrelated notions: the model may pick up a dream it already started instead
    of beginning a new one, and iterate on it.

    **Every field here is REQUIRED on the wire and optional in Python**, which
    is what makes this schema compilable at all — see
    `model_client.EVERY_FIELD_REQUIRED` for the measurements. Nothing about
    what a step may contain has changed: an empty list, an empty string and a
    null are all still answers. A field added with a default and no entry in
    `required` is what made `electrum-bot dream` unable to call the model at
    all, and `tests/test_model_client.py` fails the build if it happens again.
    """

    model_config = EVERY_FIELD_REQUIRED

    advance_id: int | None = Field(
        default=None,
        description=(
            "The id of an existing dream to advance, or null to start a new one. "
            "Prefer advancing: a chain attacked twice is worth more than two "
            "chains attacked once."
        ),
    )
    title: str = Field(description="Short title for the dream.")
    seed: str = Field(description="The spark, in one sentence.")
    origin: str = Field(
        default="", description="Where the spark came from: a headline, a post, a pattern."
    )
    stage: DreamStage = Field(description="Which stage this step is.")
    thought: str = Field(description="The step itself, thinking out loud. Short.")
    chain: list[DreamHop] = Field(
        default_factory=list, description="The chain so far, hop by hop."
    )
    weakest_hop: str = Field(
        default="",
        description=(
            "The hop most likely to break the whole thing. Name it every time a "
            "chain exists. Confidence is the minimum across links, not the average."
        ),
    )
    weakest_hop_index: int | None = Field(
        default=None,
        description=(
            "WHICH hop that is, as a number, counting the chain above from 1. "
            "The sentence is what a person reads and this is what code can act "
            "on, and code will not guess: a paraphrase that matches no hop "
            "leaves the dream on the workbench, because nothing can then be "
            "shown to settle the link the chain rests on."
        ),
    )
    trigger: str = Field(
        default="",
        description=(
            "The observable event that would confirm or kill this, and by when. "
            "Required for a keep or a park: a watch with no trigger is a note."
        ),
    )
    instruments: list[str] = Field(
        default_factory=list,
        description=(
            "What this is ABOUT: a commodity, a region, an industry. Subject "
            "matter, never a ticker to trade and never an instruction."
        ),
    )
    symbols: list[str] = Field(
        default_factory=list,
        description=(
            "LISTED tickers this chain reaches, if any, that the broker could "
            "actually route — a US equity or ETF, or a crypto pair like "
            "BTC/USD, in an instrument class the prompt marks ENABLED. You MAY "
            "name symbols that are not on the watch list; that is the point of "
            "you. You may NOT name a private company, a co-operative, a foreign "
            "unlisted producer or a bare commodity, because none of those has a "
            "quote. If the thing your chain is really about is not listed, name "
            "the listed instrument whose fortunes it moves and put the hop that "
            "gets you there in `chain`. If there is no such instrument, leave "
            "this EMPTY — an empty list is a good answer and a weak proxy is "
            "not. This is the only field here that can become a permission."
        ),
    )
    conditions: list[StepCondition] = Field(
        default_factory=list,
        description=(
            "What would have to be TRUE before this dream is worth offering to "
            "the trading agent. Required for a keep verdict: a keep with no "
            "checkable condition stays on the workbench and reaches nobody. "
            "Each needs a sentence and, where you can, the checkable triple."
        ),
    )
    verdict: DreamVerdict | None = Field(
        default=None, description="Set only when stage is verdict."
    )
    fuse_ids: list[int] = Field(
        default_factory=list,
        description=(
            "Two or three dream ids to COMBINE into one, chosen from the shared-hop "
            "candidates you were shown, or empty to combine nothing. When you set "
            "this, `title`, `seed` and `thought` describe the COMBINED dream and no "
            "separate dream is written. The parents are not consumed: they survive "
            "untouched and the new dream records which ones it came from. Empty is "
            "the normal answer — fuse when the shared hop is genuinely the same "
            "mechanism reached by two routes, not because two ideas are both about "
            "energy."
        ),
    )


SYSTEM_PROMPT = """\
You are the dreamer for a private paper-trading vehicle.

You look for SECOND-ORDER effects: the consequence two hops away from what
everyone is watching. You do not forecast prices, you do not propose trades, and
you have no way to place one.

Your output is a single step in a mini-project. Each project moves through four
stages and you say which one you are in:

  seed     the spark, one sentence, and where it came from
  explore  the chain, hop by hop, each hop a separate checkable claim
  iterate  attack it. what must be true that you have not verified? who already
           knows this? is it in the price? what would make it wrong?
  verdict  keep (chain holds, worth watching), park (interesting, not now), or
           drop (it broke, and say which hop broke)

A good chain looks like this, and the shape matters more than the subject:

  Cicadas emerge on fixed multi-year broods, and the map is published.
  Sesame production is concentrated in a few countries.
  Two of the three largest fall inside overlapping brood ranges this season.
  Indonesia has no periodical cicadas, so it becomes the marginal supplier.

Every link there is a separate physical fact that can be checked on its own, and
any one of them breaking kills the whole thing. That is a hypothesis. "AI is big
so buy chips" is not: it is one hop and it is already priced.

Hard rules:

- NEVER state a number you were not shown. No invented production figures,
  market shares, prices, correlations or percentages. If a hop needs a figure
  you do not have, mark the hop unchecked and say what would verify it. A
  plausible fabricated statistic is the most dangerous thing you can produce,
  because it is the part nobody thinks to check.
- `checked` is true ONLY for something you were shown or a matter of public
  record you can name. Everything else is false, and an unchecked hop is a
  useful, honest output rather than a failure.
- Name the weakest hop every time. Confidence in a chain is the minimum across
  its links.
- Assume the obvious version of your idea is already known. Ask what is left.
- A dropped dream is a GOOD outcome. Say which hop broke so the idea stays
  dropped rather than returning next month in a new headline.
- Prefer advancing an existing dream over starting a new one.

Where you may look:

- The watch list is a starting point, not a fence. You may dream about any
  instrument in a class the prompt marks ENABLED, including ones nobody has
  listed. That is the point of you.
- **A BLOCKED class is a hard stop.** It is a decision the operator made about a
  whole group of instruments, not a gap for a dream to route around. Do not name
  a symbol in one, do not build a chain that only pays off through one, and do
  not suggest enabling one. Anything you name there is dropped before it is
  stored, and the drop is recorded against the dream.
- `instruments` is what the dream is ABOUT and is free text — a commodity, a
  region, an industry. `symbols` is different and much narrower: the tradeable
  tickers the dream claims. It is the only field you write that could ever
  become a permission, so leave it empty unless the dream really is about
  something the bot could hold.

## symbols: what you may reason about, and what you may NAME

These are two different questions and conflating them is the commonest way to
get this wrong.

**Your SUBJECT is unrestricted.** Cicada broods, cooling water at a French
reactor, a sesame co-operative in Indonesia, a privately-held smelter, a
shipping lane. None of that has to be listed anywhere. It goes in `instruments`
as free text, and that is exactly where second-order thinking starts.

**A `symbols` entry must be an instrument the broker can actually route.** It
needs a live quote at Alpaca and it must sit in a class the fence above marks
ENABLED. A private supplier, a foreign co-operative, a bare commodity with no
fund behind it and an index with no tradeable vehicle are all things you may
reason about at length and none of them may appear here.

So when the thing your chain is about is not listed — which is the normal case
for a good dream — your job is the bridge:

    the unlisted thing you are reasoning about
      → the LISTED instrument whose fortunes it moves
      → that ticker in `symbols`

The customer that depends on the supplier. The ETF that holds the sector. The
listed competitor that gains when the marginal producer stumbles.

**That bridge is a claim, so write it as a HOP like any other.** It is the hop
most likely to be wrong and the one nobody checks, because it arrives looking
like bookkeeping rather than an argument: "this matters to that company" needs
the same evidence as every other link, and if it is a large part of that
company's revenue, say so and mark the hop unchecked if you were not shown it.
A ticker that appears in `symbols` with no hop explaining how the chain reaches
it is an unexplained leap, and it will be read as one.

**If no listed instrument exists, leave `symbols` empty.** That is a good
answer and a common one. Do NOT reach for a loose proxy to fill the field: a
weak proxy is worse than nothing, because an empty list reads as "this is not
tradeable" and a bad ticker reads as a trade. A dream about reactor cooling
water with no symbol is still a dream worth having.

## conditions: what would have to be true first

A verdict of `keep` says the chain holds. It does not say the moment has come,
and those are different claims. `conditions` is what would have to happen
before this is worth putting in front of the trading agent.

- **A keep with nothing pre-registered goes nowhere.** It stays on the
  workbench, and reaches nobody, because a conclusion nobody can settle is an
  opinion. If you want a dream to travel, pre-register something.

There are TWO ways to pre-register one, and choosing the wrong one is how a
good chain gets stuck or a bad number gets invented.

  **A THRESHOLD** — `symbol`, `field`, `op`, `value`. Settled by CODE against
  the figures the decision loop records. Use it when the claim really is about
  one of those figures.

  **AN OBSERVATION** — `subject`, `observable`, `observe_by`. Settled by a
  PERSON, who goes and looks. Use it when no figure measures your claim, which
  is the normal case for the hop a supply-chain chain actually rests on.

**Every field the loop computes is a price or a technical figure**, and the hop
that could kill a second-order chain almost never is. A smelter restarting, a
brood map showing overlap, a regulator granting a licence, a plant coming back
after an outage — nothing in `close`, `sma_20` or `atr_14` measures any of
those, and a listed company's share price does not test your claim, it prices
the market's own guess about it.

So do NOT reach for a price threshold to make a non-price claim promotable.
That is the invented-number failure with better manners, and it is worse than
leaving the field null, because the dream then travels carrying a test of
something nobody claimed. Write the observation instead.

An observation needs all three parts and each one does a job:

  `subject`    the specific, findable thing to look at. A named report, a named
               register, a named company's own release. "The market" is not a
               subject and neither is "the news".
  `observable` what it would have to show, phrased so a person answers yes or no.
  `observe_by` the date that answer should exist by, as YYYY-MM-DD.

The date is a REVIEW date, not a deadline. Nothing expires when it passes and
your dream does not fail; the question simply appears on the operator's list as
due. Give the honest date the thing becomes knowable — when the report is
published, when the quarter ends — and if it is already knowable, give a near
one. What you may not do is leave it out: a question that can never be late is
a question nobody is ever asked.

**You do not answer your own observation, and there is no field here you could
answer it with.** A person does, and their answer is what moves the dream.
- **A prophecy is a claim about the WEAKEST LINK, and that is what the shelf is
  for: a dream parked awaiting the thing that would settle it.** So name the
  weakest hop, give its number in `weakest_hop_index`, and make sure at least
  one PRE-REGISTERED condition — threshold or observation — names that same
  number in `settles_hops`. A condition on a link nobody doubted grades
  cleanly, promotes cleanly and settles nothing, which turns the shelf into a
  filing decision. The weakest hop is usually the one no figure can measure, so
  it is usually an observation that belongs on it.
- One condition may settle several hops, and several conditions may settle the
  same hop. Pin what the condition honestly bears on and nothing more.
- Each condition needs `text` — the sentence, with the reasoning in it — and,
  wherever the claim allows, `symbol`, `field`, `op`, `value` and
  `settles_hops`.
- **`value` is a NUMBER, never the name of another figure.** "Above the 20-day"
  re-checked next month tests a level nobody ever saw, because the average
  moved in the meantime. Read the level off the figures and write it down. That
  is what pins the claim to the moment you made it. This holds hardest for the
  condition carrying your weakest hop: it is the one that has to still mean
  something months later, which a moving figure cannot.
- `symbol` says whose figure it is. It may be something you would never trade —
  a condition about the marginal producer is fine even when the symbol you
  claim is its customer.
- A condition with only a sentence — no triple and no observation — is still
  legal, and is counted honestly as one nobody can settle. It promotes nothing.
  What is NOT acceptable is inventing a number so a field looks filled.

## symbiosis: two chains that meet at the same hop

Sometimes two of your dreams share a link. A chain about drought cutting hydro
output in one region and a chain about smelters chasing cheap power are
separately interesting; the hop they have in common — that region's power gets
scarce and dear — is the mechanism, and a mechanism two independent routes
arrive at is better evidenced than either route alone.

When that is genuinely the case, set `fuse_ids` to two or three of the ids you
were offered and write `title`, `seed` and `thought` about the COMBINED dream.

What fusing does and does not do, because it is easy to overrate:

- **The parents survive.** Nothing is consumed. The new dream records where it
  came from and both originals stay exactly as they were, so either can still
  be attacked on its own.
- **It does not make anything more verified.** The badge on the fusion can
  never read better than the WORSE of its parents, and a hop one parent sourced
  and the other did not comes across UNCHECKED. Two unverified chains do not
  make a sourced one. Combining is an argument, not evidence.
- **It is harder to promote, not easier.** The fusion carries every condition
  both parents pre-registered and needs all of them met.
- **It cannot reach a symbol neither parent claimed**, and if the parents
  disagree about the instrument class the fusion claims none.

Fuse when the shared hop is the same physical claim reached two ways. Do NOT
fuse because two dreams are both about energy, or both about shipping — a link
that four chains reach is a truism, and a truism dressed as a mechanism is
worse than either dream on its own.

## considerations: things put to you in conversation

You may be shown notes raised by the chat agents — a place somebody thought it
was worth you looking. They are notes and never dreams: nothing is on a shelf,
nothing is offered to the trading agent, and none of them permits a symbol.

**You may ignore any of them, and most of the time you should.** Nothing counts
them, nothing follows from leaving one alone, and a chain written because you
were asked for one is worth less than the chain you were already having. There
is no credit for responsiveness here. If you do pick one up it is a spark like
any other: you write the chain, every hop still has to be checkable, and being
suggested by somebody else establishes nothing at all.

Each carries the time it was raised, and where a person prompted it, their own
words verbatim beside the agent's. Read the two together — a spark that is the
operator's sentence given back is one nobody actually had.

You do not mark your own conditions fulfilled. Code checks them against the
figures the decision loop recorded, and moves the dream when they fire.

You are shown what the bot watches, recent headlines and posts, and which
positions recently opened or closed. You are NOT shown profit or loss, and you
must not ask for it or reason about it. Forty trades is noise; a dreamer that
chases what recently worked is a momentum strategy with a personality.
"""


def render_class_fence(rules: Rules) -> list[str]:
    """Which instrument classes are open to the dreamer, and which are shut.

    **A disabled class is NAMED rather than omitted**, which is the opposite of
    what `build_system_prompt` does for the decision loop and is deliberate in
    both places. There, listing a class the bot cannot trade only invites
    proposals for it. Here the whole permission is "look outside the watch list,
    inside the fence", so the dreamer has to be able to SEE the fence — and the
    operator's rule runs both ways: enable crypto and the dreamer should notice,
    disable it and it should notice that too. A dreamer left to infer a block
    from an absence infers it wrongly and spends a run on ideas that get
    dropped.

    Returned as lines rather than a string so `build_prompt` keeps assembling
    one list, and so a class list that is somehow empty contributes nothing
    rather than an empty heading.
    """
    if not rules.instruments:
        return []
    out = ["Instrument classes. The watch list above is a starting point; this is a fence:"]
    for name, instrument in sorted(rules.instruments.items()):
        if instrument.enabled:
            out.append(
                f"  {name} — ENABLED. Any symbol in this class is fair game, "
                "including ones not on the watch list."
            )
        else:
            out.append(
                f"  {name} — BLOCKED. Do not name a symbol here and do not build "
                "a chain that only pays off through one."
            )
    out.append("")
    # Stated with the fence rather than left to the system prompt, because this
    # is the half that decides whether a named symbol survives storage at all.
    # `scope_symbols` drops anything outside an enabled class, and it derives
    # the class from `Rules.true_class_key` and the same shape rule the broker
    # routes on — so a dream naming a private supplier or a bare commodity loses
    # the symbol silently from the model's point of view.
    out.append(
        "A `symbols` entry must be something the broker can route inside an "
        "ENABLED class above: a listed US ticker or ETF, or a crypto pair such "
        "as BTC/USD. Anything else — a private company, a co-operative, an "
        "unlisted foreign producer, a commodity with no fund — is dropped "
        "before storage. Reason about those freely and name them in "
        "`instruments`; bridge to a listed instrument in a hop if one exists, "
        "and leave `symbols` empty if none does."
    )
    out.append("")
    return out


def render_fusion_candidates(candidates: Sequence[FusionCandidate]) -> list[str]:
    """The dreams that share a hop, offered for the dreamer to confirm or ignore.

    **Arithmetic proposes and the dreamer decides.** `fusion_candidates` finds
    the overlap by comparing stored claims — a fact — and this is where that
    fact is put in front of the one thing that can judge whether the two chains
    are really the same mechanism. Nothing fuses unattended, which is the same
    posture as `stop_watch` reporting a breach rather than closing a position:
    a machine that combined hypotheses on its own would be generating confident
    new claims out of arithmetic over old ones.

    The shared hop is rendered VERBATIM rather than summarised, because it is
    the thing being asked about. A paraphrase would ask the model to confirm a
    match it cannot see.
    """
    if not candidates:
        return []
    out = [
        "Dreams that share a hop. Set `fuse_ids` to combine two or three of "
        "them, or leave it empty:"
    ]
    for candidate in candidates:
        named = " + ".join(
            f"[id {i}] {t or 'untitled'}"
            for i, t in zip(candidate.dream_ids, candidate.titles, strict=False)
        )
        out.append(f"  {named}")
        for hop in candidate.shared_hops:
            out.append(f"      shared hop: {hop}")
    out.append(
        "  Fusing keeps both parents, cannot improve either one's verification, "
        "and inherits every condition both of them pre-registered."
    )
    out.append("")
    return out


def render_considerations(
    recall: ConsiderationRecall | None, *, now: datetime
) -> list[str]:
    """What the chat agents put up, offered as sparks the dreamer may ignore.

    **A consideration is not a dream and this is not a route to becoming one.**
    `raise_consideration` writes one line to the audit log and never opens
    `data/dreams.db`; this reads that line back into the prompt. Everything
    downstream is unchanged: the model writes its own step, and a consideration
    becomes a seed only by the model choosing to make one, exactly as it chooses
    the seeds it had by itself. Nothing here can put a row on a shelf, and
    nothing here names a symbol, because the record carries none.

    Four properties, each an existing rule arriving somewhere new:

    - **The age travels with the item.** A spark put up six hours ago, rendered
      with no time on it, is being presented as something said just now — the
      confident-partial-answer failure arriving through the prompt rather than
      through a feed.
    - **`prompted_by` is marked as the operator's own words**, verbatim and
      apart from the agent's spark, so the model can tell what a person said
      from what an agent said about it. A smoothed or merged quote takes that
      judgement away from the reader, which is the same reason the tool refuses
      to paraphrase it.
    - **Nothing recorded is its own state.** An empty section says which kind of
      empty it is, because "nobody put anything up", "the log was silent",
      "you were already shown them" and "the record could not be read" are four
      different findings that produce one empty list. Same rule as `has_cycles`
      and `can_grade_anything`.
    - **The prompt says the dreamer may ignore them.** A suggestion the model
      feels obliged to honour is the operator steering the dreamer by accident,
      and two brains was the point rather than agreement.

    `None` means nobody looked — no audit log was wired in, or the read failed —
    and renders nothing at all, which is a different claim from "the log was
    read and held nothing". Do not collapse the two.
    """
    if recall is None:
        return []

    window = f"{recall.window_hours:g}h"
    out: list[str] = []

    if recall.considerations:
        out.append(
            "Raised in conversation and not yet put in front of you. These are "
            "notes from the chat agents — not dreams, not shelved, not "
            "instructions, and none of them permits a symbol:"
        )
        for item in recall.considerations:
            who = item.speaker or "a chat agent"
            out.append(
                f"  [{describe_age(item.age_minutes(now))}] {who}: {item.spark}"
            )
            if item.why_now:
                out.append(f"      why now ({who}'s words): {item.why_now}")
            if item.prompted_by:
                # Quoted and attributed to the OPERATOR, never folded into the
                # agent's own sentence above.
                #
                # `prompt_echo` is deliberately NOT rendered beside it. The
                # ratio exists so a reader can judge whether a spark is the
                # operator's sentence given back, and the two texts sitting one
                # line apart already let this reader do exactly that. Printing
                # the number as well would invite a model to treat 0.4 as a
                # threshold — a figure enforced by inference, which is the
                # thing the tool refused to enforce in code.
                out.append(
                    f'      the operator said, verbatim: "{item.prompted_by}"'
                )
            else:
                out.append(
                    "      nobody prompted this one; it is the agent's own."
                )
        if recall.omitted_for_limit:
            out.append(
                f"  {recall.omitted_for_limit} older note(s) were not shown here "
                "and are still waiting; you will be offered them next run."
            )
        out.append(
            "  **You may ignore every one of these.** A consideration is "
            "somebody saying it might be worth you looking, not a task and not "
            "a seed. Nothing counts them, nothing follows from leaving one "
            "alone, and a chain you write only because you were asked to is "
            "worth less than the one you were already having. If you do take "
            "one up, it earns no shortcut: it is a spark like any other and "
            "every hop still has to be checkable."
        )
    else:
        out.append(f"Nothing new has been put to you in conversation in the last {window}.")
        out.append(
            "  That is the ordinary state and it is NOT evidence that nobody "
            "had anything. A note appears here only if somebody typed one, so "
            "an empty list says nothing either way — carry on with your own "
            "seeds."
        )

    # The four things an empty list can mean, said out loud rather than left for
    # the model to guess at. Rendered under a populated list too: a partial read
    # that returned two notes is still a partial read.
    if recall.seen_previously:
        out.append(
            f"  {recall.seen_previously} other note(s) went up in this window "
            "and were already in front of you on an earlier run. They are not "
            "repeated — you have seen them, and being asked again daily until "
            "you agreed would not be a suggestion."
        )
    if not recall.has_record:
        out.append(
            f"  The log holds no records at all for the last {window} — no "
            "cycles, no events. That is a silence from this box rather than a "
            "quiet week, so treat the line above as unknown rather than as no."
        )
    if recall.is_degraded:
        out.append(
            f"  The log could not be fully read ({recall.malformed_lines} "
            f"unparseable line(s), {len(recall.unreadable_files)} unreadable "
            "file(s)), so this list may be short by an unknown amount."
        )
    if recall.rows_without_a_spark:
        out.append(
            f"  {recall.rows_without_a_spark} recorded note(s) carried no spark "
            "and could not be shown."
        )

    out.append("")
    return out


def build_prompt(
    rules: Rules,
    journal: Journal,
    open_dreams: list[Dream],
    *,
    headlines: list[str] | None = None,
    posts: list[str] | None = None,
    fusions: Sequence[FusionCandidate] = (),
    considerations: ConsiderationRecall | None = None,
    now: datetime | None = None,
) -> str:
    """Everything the dreamer is shown, and nothing else.

    Assembled here rather than in `context.py` because the two prompts want
    almost opposite things: the decision loop wants precise current figures for
    six symbols, and this wants breadth and no figures at all.
    """
    stamp = now or datetime.now(UTC)
    out: list[str] = [f"Time now: {stamp.strftime('%Y-%m-%d %H:%M')} UTC", ""]

    out.append("What the bot watches (context only, not a list to have ideas about):")
    out.append("  " + ", ".join(sorted(rules.allowed_symbols)) or "  nothing enabled")
    out.append("")

    out.extend(render_class_fence(rules))

    if headlines:
        out.append("Recent headlines:")
        out.extend(f"  - {h}" for h in headlines)
        out.append("")

    if posts:
        out.append("Recent posts from watched accounts:")
        out.extend(f"  - {p}" for p in posts)
        out.append("")

    # Events, never outcomes. "Closed on its stop" is a fact about the market;
    # "made $340" is a result, and results are what a dreamer must not learn
    # from. See the module docstring.
    closed = journal.closed_trades()[-RECENT_CLOSURES:]
    if closed:
        out.append("Positions that recently closed (what happened, not what it earned):")
        for trade in closed:
            when = trade.exit_time.strftime("%d %b") if trade.exit_time else "unknown date"
            out.append(f"  - {trade.symbol} closed {when}, opened on {trade.strategy}")
        out.append("")

    if open_dreams:
        out.append("Dreams already in progress. Prefer advancing one of these:")
        for dream in open_dreams:
            age = (stamp - dream.updated_at).days
            out.append(
                f"  [id {dream.id}] {dream.title} — stage {dream.stage}, "
                f"last touched {age} day(s) ago"
            )
            out.append(f"      spark: {dream.seed}")
            for i, hop in enumerate(dream.chain, 1):
                mark = "checked" if hop.checked else "UNCHECKED"
                out.append(f"      hop {i} ({mark}): {hop.claim}")
            if dream.weakest_hop or dream.weakest_hop_index is not None:
                # The hop NUMBER travels with the sentence, and says plainly
                # when it could not be established. A dream held back for an
                # unresolvable weakest hop would otherwise look identical on the
                # next run to one held back for anything else, and the model
                # would rewrite the sentence forever instead of giving a number.
                pinned = dream.resolved_weakest_hop
                where = f"hop {pinned}" if pinned is not None else "WHICH HOP NOT ESTABLISHED"
                out.append(
                    f"      weakest ({where}): "
                    f"{dream.weakest_hop or 'named by number only'}"
                )
            # The symbols and conditions already on the dream, so an advancing
            # step edits what is there instead of writing over it blind. A model
            # shown neither restates both from scratch every time, and a
            # restated condition is a condition whose grading has to be carried
            # forward by hand — see `carry_forward_grading`.
            if dream.symbols:
                out.append(f"      symbols claimed: {', '.join(dream.symbols)}")
            for condition in dream.conditions:
                # The five-valued state rather than met/not-met. "Nobody has
                # looked yet" and "a person looked and the answer was no" are
                # opposite facts, and a dreamer shown one word for both would
                # keep re-proposing a chain whose weakest link has been refuted.
                mark = str(condition.state(stamp)).upper()
                trigger = condition.as_trigger()
                if trigger is not None and condition.symbol:
                    shape = f" [{condition.symbol} {trigger.render()}]"
                elif condition.is_observable:
                    due = (
                        condition.observe_by.date().isoformat()
                        if condition.observe_by
                        else "no date"
                    )
                    shape = (
                        f" [observation: {condition.subject} — "
                        f"{condition.observable}; due {due}]"
                    )
                else:
                    shape = " [nothing pre-registered]"
                # Which hop it claims to settle, and the absence stated rather
                # than left blank. An unpinned condition is why a finished keep
                # sits on the workbench, and a renderer that showed nothing
                # there would hide the one field that has to change.
                pins = (
                    ", ".join(f"hop {h}" for h in condition.settles_hops)
                    or "settles no hop yet"
                )
                out.append(f"      condition ({mark}): {condition.text}{shape} [{pins}]")
        out.append("")

    # AFTER the open dreams, so a shared hop is read against the chains it was
    # found in rather than from a list of ids with no context — the same
    # ordering reason `build_market_context` puts last cycle's watches after
    # the indicators they are checked against.
    out.extend(render_fusion_candidates(fusions))

    # LAST of the inputs, after the dreamer's own chains and after every feed.
    #
    # The ordering is the same argument the grant block is rendered last for: a
    # section carrying somebody else's suggestion is the one thing here with a
    # pull that is not evidence, and a model that reads it before its own open
    # chains anchors on it. Reading it after them makes it what it is — one more
    # spark, weighed against the work already in progress — which is exactly the
    # posture "you may ignore it" describes. It sits before the closing
    # instruction so the instruction still lands last.
    out.extend(render_considerations(considerations, now=stamp))

    out.append(
        "Produce one step. Advance one of the dreams above if any is worth "
        "advancing, otherwise start a new one."
    )
    # Asked for explicitly at call time, not only in the output schema's field
    # descriptions. Three real dreams generated against the live model came
    # back with `symbols: []` and no conditions at all — nothing was filtered,
    # the fields were simply never filled — which left the vault permanently
    # empty and the whole permission path inert.
    out.append("")
    out.append(
        "Two fields decide whether this dream ever reaches anybody, so answer "
        "them deliberately:"
    )
    out.append(
        "  - `symbols`: the LISTED instrument this chain reaches, inside an "
        "enabled class. If what you are reasoning about is not listed, bridge "
        "to one that is and write that bridge as a hop. If there is no honest "
        "bridge, leave it empty — that is a respectable answer and much better "
        "than a proxy you would not trade."
    )
    out.append(
        "  - `conditions`: what would have to be true before this is worth "
        "offering. A keep verdict with nothing pre-registered never leaves the "
        "workbench. Give each a sentence, and then EITHER a "
        "symbol/field/op/value where the claim really is about one of the "
        "figures above — the value a number you read off them, never the name "
        "of another figure — OR, where no figure measures it, "
        "subject/observable/observe_by for a person to settle. Do not force a "
        "price threshold onto a claim that is not about a price."
    )
    # Third, and separate from the two above because it is the one that decides
    # whether the prophecy shelf means anything. The other two were added after
    # three live runs came back with the fields simply never filled; this one is
    # the operator's correction — the shelf is for dreams parked awaiting the
    # link that could kill them, not for whichever claim was easiest to number.
    out.append(
        "  - the WEAKEST HOP has to be covered. Name it, give its number in "
        "`weakest_hop_index`, and put that number in `settles_hops` on a "
        "pre-registered condition. A prophecy is a dream parked awaiting the "
        "link that could kill it; a condition on a link nobody doubted settles "
        "nothing and the dream stays on the workbench. If no figure could "
        "settle that hop — which is usual — pin an OBSERVATION to it rather "
        "than inventing a threshold."
    )
    return "\n".join(out)


def _observe_by(value: str) -> datetime | None:
    """A review date the model wrote, as an instant. `None` when unreadable.

    A bare `YYYY-MM-DD` becomes the END of that day in UTC, because "by the
    31st" means by the end of the 31st and the alternative reading calls a
    condition overdue for the whole of the day it is due on.

    **Unreadable is `None`, never "now" and never a clamp.** A date that cannot
    be parsed makes the condition not an observation, which leaves the dream on
    the workbench with the refusal saying what is missing — the direction that
    holds a dream where it already was. Defaulting to now would put a garbled
    value straight to the top of the operator's worklist as already overdue.
    """
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if (parsed.hour, parsed.minute, parsed.second, parsed.microsecond) == (0, 0, 0, 0):
        return parsed + timedelta(days=1) - timedelta(microseconds=1)
    return parsed


@dataclass(frozen=True)
class PromotionRun:
    """What one pass of `promote_dreams` graded and moved.

    Counts rather than prose, and every one of them says something a zero could
    not. `considered` separates "nothing was promotable" from "nothing was
    looked at", which is the `has_cycles` rule again: an empty `promoted` after
    a run that examined nine dreams and an empty one after a run that examined
    none are opposite findings.
    """

    considered: int = 0
    conditions_fulfilled: int = 0
    # Conditions no figure can settle, waiting on a PERSON to look. Its own
    # count because a still shelf has two very different explanations — the
    # figures have not moved, or nobody has been asked — and a zero here says
    # which. The same reason `cycles_available` is on the line: a shelf that
    # cannot move explains itself, rather than looking like patience.
    awaiting_operator: int = 0
    # (dream_id, vault it landed on)
    promoted: tuple[tuple[int, str], ...] = ()
    # (dream_id, why it stayed). Includes the ordinary "still being worked on",
    # which is most of them.
    held: tuple[tuple[int, str], ...] = ()
    cycles_available: int = 0

    @property
    def moved(self) -> int:
        return len(self.promoted)


def promote_dreams(
    store: DreamStore,
    *,
    readings: Sequence[CycleReadings] = (),
    at: datetime | None = None,
    caps: VaultCaps | None = None,
) -> PromotionRun:
    """Grade every prophecy, then promote whatever the rule says has earned it.

    **This is the step that was missing**, and it is why the vault was
    permanently empty and `confer` permanently a no-op: `Dream.is_offerable`
    existed and was never called, nothing moved a dream off the workbench, and
    the conference reads only `Vault.VAULT`.

    Driven by `electrum-bot dream`, immediately after a step is written, and
    **never by the decision loop.** The loop wakes every fifteen minutes and
    proposes orders; a shelf that moved on that pulse would put the dreamer's
    output on the same clock as the thing that trades, which is precisely the
    separation `dreamer.py` exists to keep. Once a day is far slower than a
    price moves, which is the right speed for deciding whether a second-order
    hypothesis is worth putting in front of the trading agent.

    Grading runs BEFORE promotion, deliberately: a condition that fires on this
    pass should move the dream on this pass. The other order would hold every
    prophecy back by a full day for no reason.

    Nothing here raises. A refusal — a full shelf, a dream still being worked
    on — is an ordinary answer and is recorded in `held`.
    """
    stamp = at or datetime.now(UTC)
    fulfilled = 0
    awaiting_operator = 0
    promoted: list[tuple[int, str]] = []
    held: list[tuple[int, str]] = []

    # The two shelves promotion moves off. Read separately rather than through
    # one query so the order is stable: prophecies first, so a condition firing
    # today puts its dream in front of the trading agent before a brand-new keep
    # can take the last slot on the vault.
    candidates: list[Dream] = [
        *store.in_vault(Vault.PROPHECY),
        *store.in_vault(Vault.WORKBENCH),
    ]

    for dream in candidates:
        dream_id = int(dream.id or 0)
        if not dream_id:
            continue
        if readings and dream.conditions:
            grading = store.grade(dream_id, readings, at=stamp)
            fulfilled += len(grading.newly_fulfilled)
            awaiting_operator += grading.awaiting_operator
        else:
            # Counted whether or not there were readings to grade against,
            # because an observation does not need any: it needs a person. With
            # no cycles recorded the grading step is skipped entirely, and a
            # count that skipped with it would report zero questions waiting on
            # the operator at exactly the moment nothing else could move either.
            awaiting_operator += sum(
                1
                for c in dream.conditions
                if c.is_observable and not c.is_answered
            )

        result = store.promote(dream_id, at=stamp, caps=caps)
        if result.ok and result.moved_to is not None:
            promoted.append((dream_id, str(result.moved_to)))
        else:
            held.append(
                (dream_id, result.detail or ", ".join(str(r) for r in result.refusals))
            )

    run = PromotionRun(
        considered=len(candidates),
        conditions_fulfilled=fulfilled,
        awaiting_operator=awaiting_operator,
        promoted=tuple(promoted),
        held=tuple(held),
        cycles_available=len(readings),
    )
    log.info(
        "dream_promotion",
        considered=run.considered,
        promoted=[f"{i} -> {v}" for i, v in run.promoted],
        conditions_fulfilled=run.conditions_fulfilled,
        # A prophecy waiting on a person looks, from here, exactly like one
        # whose figures have not moved. This is the difference, and without it
        # the shelf reports patience either way.
        awaiting_operator=run.awaiting_operator,
        # Named for the same reason `calendar_degraded` is: with no recorded
        # cycles nothing can fire, and that is a fact about the audit log rather
        # than about the prophecies. A zero here explains an unchanging shelf.
        cycles_available=run.cycles_available,
    )
    return run


@dataclass
class DreamerResult:
    dream: Dream
    usage: CallUsage | None
    advanced: bool

    # What the class fence took off this step, and why. Optional with a default
    # because a caller built before the fence existed still constructs this
    # positionally, which is the same rule every field added after the fact in
    # this repository carries.
    #
    # Reported rather than swallowed: a dreamer repeatedly reaching for a
    # blocked class is a fact worth having, and a filter that left no trace
    # would be indistinguishable from a model that had simply stopped naming
    # symbols. Same reasoning as `symbols_without_history` on the loop's
    # `cycle_complete` line.
    scope: SymbolScope = field(default_factory=SymbolScope)

    # What happened when a step asked for a fusion, INCLUDING a refusal. `None`
    # means none was asked for, which is most runs.
    #
    # A refusal travels rather than being swallowed, for the `scope` reason
    # above: a dreamer that keeps proposing fusions the store keeps refusing —
    # a full workbench, an adopted parent — is a fact worth having, and a silent
    # refusal is indistinguishable from a model that stopped suggesting them.
    fusion: FusionResult | None = None

    # What the chat surface had put up and this run was shown, or `None` when
    # nobody looked — no audit log wired in, or a read that failed.
    #
    # `None` and an empty recall are different findings and a caller must be
    # able to tell them apart: one says the dreamer has no view of the chat
    # surface at all, the other says it looked and nothing was waiting. The
    # `has_cycles` rule, arriving on a result object.
    considerations: ConsiderationRecall | None = None


# Where the timer unit lives once bootstrap has installed it, and the repo copy
# it was installed from.
INSTALLED_TIMER = Path("/etc/systemd/system/mudhorn-dream.timer")
REPO_TIMER = Path("deploy/systemd/mudhorn-dream.timer")


@dataclass(frozen=True)
class Schedule:
    """What the timer unit says, and how much of that is actually true here.

    Three states, and collapsing them would be the usual mistake:

    - **installed** — a unit exists in /etc/systemd/system. What it says is what
      systemd would use.
    - **repo only** — the unit exists in the checkout but was never installed, so
      the schedule below is an intention rather than a fact.
    - **absent** — no unit anywhere.

    Note what none of these tells you: whether the timer is **enabled**. Nothing
    readable from inside this process answers that, and a card claiming a daily
    dream because a file exists would be exactly the confident-partial-answer
    failure this repository keeps guarding against. So the page says which of
    the three it found and names the command that answers the rest.
    """

    calendar: str
    installed: bool
    found: bool

    @property
    def state(self) -> str:
        if not self.found:
            return "no timer unit found"
        return "installed" if self.installed else "in the repo, not installed"


def read_schedule(
    *, installed: Path | None = None, repo: Path | None = None
) -> Schedule:
    """Read `OnCalendar=` out of the timer unit. Never raises.

    Parsed rather than hardcoded so the page cannot drift from the unit: an
    operator who edits the schedule on the box should see the edit here, and a
    Settings screen quoting a cadence from a constant would keep saying the old
    one forever.
    """
    for path, is_installed in ((installed or INSTALLED_TIMER, True), (repo or REPO_TIMER, False)):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("OnCalendar="):
                return Schedule(
                    calendar=stripped.split("=", 1)[1].strip(),
                    installed=is_installed,
                    found=True,
                )
        return Schedule(calendar="", installed=is_installed, found=True)
    return Schedule(calendar="", installed=False, found=False)


def estimated_cost_usd(spec: ModelSpec) -> tuple[float, float] | None:
    """Rough cost of one run, and of a year of daily runs. `None` if unknowable.

    Measured against the real prompt rather than guessed: about 3,600 input
    tokens with a few chains open, and a few hundred output on top of the
    thinking pass. Thinking bills as output.

    Presented as an estimate and labelled as one on the page. The exact figure
    for a run that actually happened is logged with it as `cost_usd`.

    **A model with no prices on file returns `None`, and the page must say
    unknown rather than draw `~$0.000 a run`.** This used to key on the tier
    twice over — the price table, and `0 if HAIKU else 4_000` for the thinking
    budget — so it could only describe one of three Claude models and had no way
    to express a fourth. Both halves now come off the spec: the prices are the
    spec's or absent, and the thinking allowance follows whether the model is
    actually SENT a thinking field, which is the property that made Haiku's
    estimate zero in the first place. The three Claude tiers produce exactly the
    figures they always did.
    """
    pricing = spec.pricing
    if pricing is None:
        return None
    thinking = 4_000 if spec.sends_anthropic_thinking else 0
    per_run = (
        3_600 * pricing.input_usd_per_mtok
        + (thinking + 700) * pricing.output_usd_per_mtok
    ) / 1_000_000
    return per_run, per_run * 365


class Dreamer:
    """One dream step per call.

    Takes the store and the journal rather than reaching for them, so a test
    never touches `data/`.
    """

    def __init__(
        self,
        env: Env,
        rules: Rules,
        store: DreamStore,
        journal: Journal,
        *,
        client: Any | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self._rules = rules
        self._store = store
        self._journal = journal
        # Both directions of the consideration path, or neither. The log is
        # where a chat agent's note is read FROM and where "this run was shown
        # it" is written TO, so a dreamer holding one and not the other could
        # offer the same note forever or mark one it never displayed.
        #
        # Injected with no default rather than constructed here, for the reason
        # every store in this repository takes its path: a default would write
        # to the real `audit/` from any test that built a `Dreamer`.
        self._audit = audit
        # The soul is part of the system prompt here rather than prepended per
        # call, because unlike the chat bridge there is exactly one character
        # this process ever speaks in, and putting it in the cached system block
        # means it is not re-billed on every run.
        soul = load_soul(GROGU)
        system = SYSTEM_PROMPT
        if soul.found:
            system = f"{soul.prompt_prefix()}\n{SYSTEM_PROMPT}"
        self._client = client or ModelClient(
            env,
            system,
            # Its own model, because Haiku cannot think and thinking is how a
            # dream gets past its first hop.
            spec=env.dream_spec,
            # NOT cached. A 1h cache write bills at 2x input and a read at 0.1x,
            # so a caller running once a day misses every time and pays double
            # the system block on every call. The loop caches because it wakes
            # every fifteen minutes; this is the opposite case.
            cache_system=False,
        )

    def run_once(
        self,
        *,
        headlines: list[str] | None = None,
        posts: list[str] | None = None,
        now: datetime | None = None,
    ) -> DreamerResult | None:
        """One step. Returns None on any failure, having written nothing.

        Wrapped for the reason the decision loop's call is wrapped: a
        `ValidationError` out of the SDK would kill whatever timer drives this
        and restart into the same failure. A dream that could not be had must
        not be recorded as one that decided nothing.

        Anything the chat surface has put up is read into the prompt here as
        candidate sparks — and nothing else happens to it. The model writes its
        own step; a consideration becomes a seed only if it chooses to make one.
        """
        moment = now or datetime.now(UTC)
        pool = self._store.recent(limit=FUSION_POOL)
        existing = [d for d in pool if d.is_open][: CARRY_FORWARD * 3]
        candidates = fusion_candidates(pool, limit=FUSION_OFFERS)
        considerations = self._recall_considerations(moment)
        prompt = build_prompt(
            self._rules,
            self._journal,
            existing[:CARRY_FORWARD],
            headlines=headlines,
            posts=posts,
            fusions=candidates,
            considerations=considerations,
            now=moment,
        )

        try:
            step, usage = self._client.dream(prompt)
        except (anthropic.APIError, ValueError, RuntimeError) as exc:
            log.warning("dream_call_failed", error=str(exc))
            # Nothing is marked seen. A run whose call failed never showed
            # anybody anything, and the notes are offered again next time —
            # the same direction as writing no dream at all.
            return None
        except Exception as exc:
            # Same reasoning as `fetch_market_ticks`: there is no exception from
            # an HTTP client or a schema validator worth crashing a scheduled
            # job over, and an unanticipated one is exactly when crashing is
            # worst.
            log.warning("dream_call_failed_unexpectedly", error=repr(exc))
            return None

        # Marked HERE — after the model answered, before the branch below. What
        # the marker records is that these reached the model, and a fusion step
        # and an ordinary step are equally runs that saw them. Marking after the
        # write would make "was it considered" depend on which kind of step came
        # back, which is a different question.
        self._mark_considerations_seen(considerations)

        fused, refusal = self._fuse_if_asked(step, candidates, usage=usage, now=now)
        if fused is not None:
            fused.considerations = considerations
            return fused

        dream, advanced, scope = self._apply(step, existing, now=now)
        self._store.save(dream)

        if scope.dropped:
            # Three records, and each is for a different reader. The log line is
            # what an operator greps; `DreamerResult` is what a caller can act
            # on; the transcript note is the durable one a human sees beside the
            # dream itself on the Dreaming page, which is where the question
            # "why does this dream name nothing" actually gets asked.
            log.warning(
                "dream_symbols_dropped",
                dream_id=dream.id,
                dropped=[s for s, _ in scope.dropped],
                kept=list(scope.kept),
            )
            self._store.add_message(
                int(dream.id or 0), speaker=SCOPE, kind="note", text=scope.summary
            )

        log.info(
            "dream_step",
            dream_id=dream.id,
            stage=str(dream.stage),
            advanced=advanced,
            hops=len(dream.chain),
            unchecked=len(dream.unverified_hops),
            symbols=list(scope.kept),
            symbols_dropped=len(scope.dropped),
        )
        return DreamerResult(
            dream=dream,
            usage=usage,
            advanced=advanced,
            scope=scope,
            # Only ever a refusal here: a fusion that succeeded returned above.
            fusion=refusal,
            considerations=considerations,
        )

    def _recall_considerations(self, now: datetime) -> ConsiderationRecall | None:
        """What the chat surface has put up that no run has been shown yet.

        `None` on any failure, and on no audit log at all, so the section is
        simply absent from the prompt. That is the honest shape: an empty recall
        is a claim ("the log was read and nothing was waiting") and this cannot
        make it. Costs the section and nothing else — same direction as
        `dream_condition_readings_unavailable` costing the grading.
        """
        if self._audit is None:
            return None
        try:
            view = self._audit.read(limit=CONSIDERATION_SCAN, days=CONSIDERATION_DAYS)
        except Exception as exc:
            log.warning(
                "dream_considerations_unavailable",
                error=f"{type(exc).__name__}: {exc}",
                detail=(
                    "Nothing raised in conversation reaches this run's prompt, "
                    "and nothing is marked seen, so anything waiting is offered "
                    "again next run."
                ),
            )
            return None
        return recall_considerations(
            view, hours=DEFAULT_CONSIDERATION_HOURS, now=now
        )

    def _mark_considerations_seen(self, recall: ConsiderationRecall | None) -> None:
        """Record which considerations this run was actually shown.

        **The exact set that was rendered, never a high-water stamp and never
        `now`.** `seen.py` established the rule in its own shape — a marker
        stamped at the current moment marks as seen whatever arrived while the
        page was being built — and the model call here takes long enough for
        that to be a real window. Writing the keys that went into the prompt
        cannot over-claim, whatever a limit trimmed or however long the call
        took.

        A new audit EVENT rather than a column: the log is append-only and must
        never be migrated, and a run stating what it saw is a fact about the
        run, in the same shape as `loop_start`.

        Never raises. A failed write costs the marker, and the consequence is
        one note offered twice — visibly, to a model that is told it may ignore
        it. The other failure direction is a note marked seen that nobody read.
        """
        if self._audit is None or recall is None or not recall.considerations:
            # Nothing rendered, nothing to mark. A run that was shown an empty
            # list has not "considered" anything, and an event saying it had
            # would put an empty claim on the record every single day.
            return
        keys = recall.shown_keys
        try:
            self._audit.record_event(
                CONSIDERATIONS_SEEN_EVENT,
                {SHOWN_FIELD: keys, "count": len(keys)},
            )
        except OSError as exc:
            log.warning(
                "dream_considerations_not_marked",
                error=str(exc),
                count=len(keys),
                detail="They will be offered again on the next run.",
            )

    def _fuse_if_asked(
        self,
        step: DreamStep,
        candidates: Sequence[FusionCandidate],
        *,
        usage: CallUsage | None,
        now: datetime | None,
    ) -> tuple[DreamerResult | None, FusionResult | None]:
        """Combine dreams if the step asked to, within what was actually offered.

        **The ids are looked up in what we offered, never trusted**, exactly as
        `advance_id` is. A model returning ids for rows it was never shown would
        otherwise combine two unrelated chains, and the result would carry both
        their symbols — which is a permission assembled out of a hallucinated
        number.

        When a fusion is written, this run produced the FUSION and no separate
        dream: the step's title, seed and thought describe the child, so folding
        the same text into a second row would put the same idea on the workbench
        twice.

        Two failure directions, and both keep the run's work:

        - **A refusal is returned rather than raised**, and the caller carries on
          with the ordinary step. A full workbench or an adopted parent must not
          cost the thought the model just had.
        - **Nothing is guessed.** Fewer than `MIN_FUSION_PARENTS` surviving ids
          is not silently topped up from the candidate list; it is logged and
          the run continues as an ordinary step.

        The union of the parents' symbols goes through `scope_symbols` first, so
        a class the operator has disabled SINCE those dreams were written is
        dropped here rather than inherited. `DreamStore.fuse` then refuses
        anything wider than the union, which is the lock that does not depend on
        this having been got right — the same two-lock arrangement as `adopt`
        and `granted_symbols`.
        """
        if not step.fuse_ids:
            return None, None

        offered = {i for c in candidates for i in c.dream_ids}
        wanted = [i for i in dict.fromkeys(step.fuse_ids) if i in offered]
        unknown = [i for i in dict.fromkeys(step.fuse_ids) if i not in offered]
        if unknown:
            log.warning("dream_fusion_ids_unknown", requested=unknown, offered=sorted(offered))
        if len(wanted) < MIN_FUSION_PARENTS or len(wanted) > MAX_FUSION_PARENTS:
            log.warning(
                "dream_fusion_not_attempted",
                requested=list(step.fuse_ids),
                usable=wanted,
                detail=(
                    f"A fusion needs {MIN_FUSION_PARENTS} to {MAX_FUSION_PARENTS} "
                    "ids from the candidates offered. Continuing as an ordinary "
                    "step; nothing was combined."
                ),
            )
            return None, None

        parents = [d for d in (self._store.get(i) for i in wanted) if d is not None]
        scope = scope_symbols([s for p in parents for s in p.symbols], self._rules)
        result = self._store.fuse(
            wanted,
            by=DREAMER,
            title=step.title,
            seed=step.seed,
            thought=step.thought,
            symbols=list(scope.kept),
            origin=step.origin,
            at=now,
            # From the rules file rather than the dataclass default, so the cap
            # an operator can read is the one that applies. Same miss as
            # `Conference._caps` was written for.
            caps=self._rules.dreaming.vault_caps(),
        )
        if not result.ok:
            log.warning(
                "dream_fusion_refused",
                parents=wanted,
                refusals=[str(r) for r in result.refusals],
                detail=result.detail,
            )
            return None, result

        child = self._store.get(int(result.dream_id or 0))
        if child is None:  # pragma: no cover - fuse has just written the row
            return None, result

        if scope.dropped:
            log.warning(
                "dream_symbols_dropped",
                dream_id=child.id,
                dropped=[s for s, _ in scope.dropped],
                kept=list(scope.kept),
            )
            self._store.add_message(
                int(child.id or 0), speaker=SCOPE, kind="note", text=scope.summary
            )

        log.info(
            "dream_step",
            dream_id=child.id,
            stage=str(child.stage),
            advanced=False,
            fused_from=list(result.parents),
            shared_hops=len(result.shared_hops),
            hops=len(child.chain),
            unchecked=len(child.unverified_hops),
            symbols=list(child.symbols),
            symbols_dropped=len(scope.dropped),
        )
        return (
            DreamerResult(
                dream=child, usage=usage, advanced=False, scope=scope, fusion=result
            ),
            result,
        )

    def _apply(
        self, step: DreamStep, existing: list[Dream], *, now: datetime | None = None
    ) -> tuple[Dream, bool, SymbolScope]:
        """Fold a step into a new or existing dream.

        The id is looked up in what we actually offered rather than trusted: a
        model returning an id for a dream that does not exist, or one belonging
        to somebody else's row, starts a new dream instead of writing over an
        unrelated one.

        Symbols go through `scope_symbols` on the way in, so a blocked class is
        refused at the point of storage rather than at the point of use. That
        ordering matters: a dream that reached the vault naming a crypto pair
        would be an offer the trading agent could accept, and the refusal would
        then have to happen inside a permission check that nobody is reading.
        """
        target: Dream | None = None
        if step.advance_id is not None:
            target = next((d for d in existing if d.id == step.advance_id), None)
            if target is None:
                log.warning("dream_advance_id_unknown", requested=step.advance_id)

        advanced = target is not None
        dream = target or Dream(title=step.title, seed=step.seed, origin=step.origin)

        if step.chain:
            dream.chain = [
                Hop(claim=h.claim, checked=h.checked, source=h.source if h.checked else "")
                for h in step.chain
            ]
        if step.weakest_hop:
            dream.weakest_hop = step.weakest_hop
            # Written TOGETHER, including when the step gave no number. A new
            # sentence with the previous step's index still attached would pin
            # the promotion rule to whichever hop the last step thought was
            # weakest — a stale answer wearing a current claim, which is the
            # shape `Adoption.symbols_granted` is copied at adoption to avoid.
            # Clearing it costs a run and is recoverable; keeping it is wrong
            # silently.
            dream.weakest_hop_index = step.weakest_hop_index
        elif step.weakest_hop_index is not None:
            # A number on its own still numbers the sentence already on the
            # dream, which is the case where a step is fixing exactly the thing
            # `promotion_for` refused it for.
            dream.weakest_hop_index = step.weakest_hop_index
        if step.trigger:
            dream.trigger = step.trigger
        if step.instruments:
            dream.instruments = step.instruments

        if step.conditions:
            # **Carried forward rather than taken as written.** An advancing
            # step may restate the whole condition list, and a condition that
            # came back unfulfilled after grading had already fired it would
            # leave the dream stuck below the vault forever — `all_conditions_met`
            # is what promotes it, so a grading that resets on every step is a
            # promotion that can never happen. Matched on the claim, so a
            # reworded sentence keeps its verdict and a moved threshold does not.
            dream.conditions = carry_forward_grading(
                dream.conditions,
                [
                    DreamCondition(
                        text=c.text,
                        symbol=c.symbol.strip().upper(),
                        # Structural, so it is cleaned rather than trusted:
                        # deduped, order kept, and anything that is not a
                        # position in the chain dropped rather than clamped. A
                        # pin at hop 0 or past the end names no link, and one
                        # reported as covering the weakest hop when it names
                        # nothing is worse than no pin at all.
                        settles_hops=tuple(
                            dict.fromkeys(
                                h for h in c.settles_hops if 1 <= h <= len(dream.chain)
                            )
                        ),
                        field=c.field,
                        op=c.op,
                        value=c.value,
                        # The observation half. Prose is trimmed and the date is
                        # PARSED rather than trusted — an unreadable one becomes
                        # None, which makes the condition not an observation and
                        # leaves the dream exactly where it was. That is the
                        # direction to fail in: the alternative is a dream on
                        # the prophecy shelf whose question can never come due.
                        #
                        # Nothing here carries an ANSWER, and there is no field
                        # on `StepCondition` that could. `fulfilled`,
                        # `ruled_out` and `observed_by` are written by
                        # `DreamStore.settle_condition` and by grading, never
                        # from a model's step.
                        subject=c.subject.strip(),
                        observable=c.observable.strip(),
                        observe_by=_observe_by(c.observe_by),
                    )
                    for c in step.conditions
                ],
            )

        scope = scope_symbols(step.symbols, self._rules)
        if scope.kept:
            dream.symbols = list(scope.kept)
            # Written unconditionally alongside the symbols, including when it
            # is empty. A step that widens a dream across two classes leaves the
            # class UNRESOLVED, and carrying the previous step's key forward
            # would be a permission described by a claim that is no longer true.
            # Unresolved grants nothing, which is the direction to fail in.
            dream.asset_class_key = scope.asset_class_key

        # A verdict is only honoured on a verdict step, so a stray value on an
        # explore step cannot silently close a dream that is still running.
        if step.stage is DreamStage.VERDICT:
            dream.verdict = step.verdict
        dream.add_thought(step.stage, step.thought, at=now)
        return dream, advanced, scope
