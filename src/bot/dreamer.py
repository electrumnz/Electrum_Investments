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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic
import structlog
from pydantic import BaseModel, Field

from .claude_client import CallUsage, ClaudeClient
from .config import CLAUDE_PRICING_USD_PER_MTOK, ClaudeTier, Env, Rules
from .dreaming import Dream, DreamStage, DreamStore, DreamVerdict, Hop
from .journal import Journal
from .models import class_key_for_symbol as _class_key_for_symbol
from .souls import GROGU, load_soul

log = structlog.get_logger(__name__)

# How many existing dreams to offer back for a next step. Small on purpose: the
# model picks one to advance, and a long list turns the choice into a survey.
CARRY_FORWARD = 4

# How much journal history to describe as events. Enough to notice a pattern,
# short enough that it cannot become a performance narrative.
RECENT_CLOSURES = 8

# Who a scoping note is from, in the dream's transcript. Deliberately neither
# `DREAMER` nor `TRADER`: it is a fact about the plumbing rather than a turn of
# anybody's conversation, and `confer.last_agent_turn_at` would otherwise read
# it as one.
SCOPE = "scope"


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
    symbols: list[str] = Field(
        default_factory=list,
        description=(
            "Tradeable tickers this dream claims, if any. You MAY name symbols "
            "that are not on the watch list, as long as they belong to an "
            "instrument class the prompt says is ENABLED. Anything in a blocked "
            "class is dropped before storage. Leave empty unless the dream is "
            "genuinely about something the bot could trade — this is the only "
            "field here that can ever become a permission."
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
    return out


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


def estimated_cost_usd(tier: ClaudeTier) -> tuple[float, float]:
    """Rough cost of one run, and of a year of daily runs.

    Measured against the real prompt rather than guessed: about 3,600 input
    tokens with a few chains open, and a few hundred output on top of the
    thinking pass. Thinking bills as output, and Haiku has none.

    Presented as an estimate and labelled as one on the page. The exact figure
    for a run that actually happened is logged with it as `cost_usd`.
    """
    input_price, output_price, _ = CLAUDE_PRICING_USD_PER_MTOK[tier]
    thinking = 0 if tier is ClaudeTier.HAIKU else 4_000
    per_run = (3_600 * input_price + (thinking + 700) * output_price) / 1_000_000
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
        return DreamerResult(dream=dream, usage=usage, advanced=advanced, scope=scope)

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
        if step.trigger:
            dream.trigger = step.trigger
        if step.instruments:
            dream.instruments = step.instruments

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
