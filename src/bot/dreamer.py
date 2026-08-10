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

**Not shown: profit and loss.** Not the equity curve, not the win rate, not
whether any of it made money. `souls/grogu.md` forbids learning from the track
record and this is where that is enforced rather than requested: the figures
never enter the prompt, so there is nothing to overfit to. What closed is given
as an EVENT ("this position closed on its stop"), never as an outcome ("this
position made $340"). The distinction is the whole reason this module can read
the journal at all.

## Failure

Same shape as the decision loop's model call, learned the same way. The call is
wrapped, a failure logs and returns `None`, and nothing is written. A dream that
could not be had must not be recorded as a dream that decided nothing — and a
`ValidationError` escaping here would kill whatever timer is driving it and
restart straight into the same failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import anthropic
import structlog
from pydantic import BaseModel, Field

from .claude_client import CallUsage, ClaudeClient
from .config import Env, Rules
from .dreaming import Dream, DreamStage, DreamStore, DreamVerdict, Hop
from .journal import Journal
from .souls import GROGU, load_soul

log = structlog.get_logger(__name__)

# How many existing dreams to offer back for a next step. Small on purpose: the
# model picks one to advance, and a long list turns the choice into a survey.
CARRY_FORWARD = 4

# How much journal history to describe as events. Enough to notice a pattern,
# short enough that it cannot become a performance narrative.
RECENT_CLOSURES = 8


class DreamHop(BaseModel):
    """One link, as the model returns it."""

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


class DreamStep(BaseModel):
    """What one run produces.

    `advance_id` is what makes this a mini-project rather than a stream of
    unrelated notions: the model may pick up a dream it already started instead
    of beginning a new one, and iterate on it.
    """

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
    verdict: DreamVerdict | None = Field(
        default=None, description="Set only when stage is verdict."
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

You are shown what the bot watches, recent headlines and posts, and which
positions recently opened or closed. You are NOT shown profit or loss, and you
must not ask for it or reason about it. Forty trades is noise; a dreamer that
chases what recently worked is a momentum strategy with a personality.
"""


def build_prompt(
    rules: Rules,
    journal: Journal,
    open_dreams: list[Dream],
    *,
    headlines: list[str] | None = None,
    posts: list[str] | None = None,
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
            if dream.weakest_hop:
                out.append(f"      weakest: {dream.weakest_hop}")
        out.append("")

    out.append(
        "Produce one step. Advance one of the dreams above if any is worth "
        "advancing, otherwise start a new one."
    )
    return "\n".join(out)


@dataclass
class DreamerResult:
    dream: Dream
    usage: CallUsage | None
    advanced: bool


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
    ) -> None:
        self._rules = rules
        self._store = store
        self._journal = journal
        # The soul is part of the system prompt here rather than prepended per
        # call, because unlike the chat bridge there is exactly one character
        # this process ever speaks in, and putting it in the cached system block
        # means it is not re-billed on every run.
        soul = load_soul(GROGU)
        system = SYSTEM_PROMPT
        if soul.found:
            system = f"{soul.prompt_prefix()}\n{SYSTEM_PROMPT}"
        self._client = client or ClaudeClient(
            env,
            system,
            # Its own tier, because Haiku cannot think and thinking is how a
            # dream gets past its first hop.
            tier=env.dream_tier,
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
        """
        existing = [d for d in self._store.recent(limit=CARRY_FORWARD * 3) if d.is_open]
        prompt = build_prompt(
            self._rules,
            self._journal,
            existing[:CARRY_FORWARD],
            headlines=headlines,
            posts=posts,
            now=now,
        )

        try:
            step, usage = self._client.dream(prompt)
        except (anthropic.APIError, ValueError, RuntimeError) as exc:
            log.warning("dream_call_failed", error=str(exc))
            return None
        except Exception as exc:
            # Same reasoning as `fetch_market_ticks`: there is no exception from
            # an HTTP client or a schema validator worth crashing a scheduled
            # job over, and an unanticipated one is exactly when crashing is
            # worst.
            log.warning("dream_call_failed_unexpectedly", error=repr(exc))
            return None

        dream, advanced = self._apply(step, existing, now=now)
        self._store.save(dream)
        log.info(
            "dream_step",
            dream_id=dream.id,
            stage=str(dream.stage),
            advanced=advanced,
            hops=len(dream.chain),
            unchecked=len(dream.unverified_hops),
        )
        return DreamerResult(dream=dream, usage=usage, advanced=advanced)

    def _apply(
        self, step: DreamStep, existing: list[Dream], *, now: datetime | None = None
    ) -> tuple[Dream, bool]:
        """Fold a step into a new or existing dream.

        The id is looked up in what we actually offered rather than trusted: a
        model returning an id for a dream that does not exist, or one belonging
        to somebody else's row, starts a new dream instead of writing over an
        unrelated one.
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
        if step.trigger:
            dream.trigger = step.trigger
        if step.instruments:
            dream.instruments = step.instruments

        # A verdict is only honoured on a verdict step, so a stray value on an
        # explore step cannot silently close a dream that is still running.
        if step.stage is DreamStage.VERDICT:
            dream.verdict = step.verdict
        dream.add_thought(step.stage, step.thought, at=now)
        return dream, advanced
