"""Wraps the Anthropic SDK for the trading bot's decision step.

Design:
- Frozen system prompt (no datetimes, IDs, or volatile content) so prompt caching works
- Volatile market state goes in the user message, after the cache breakpoint
- Structured outputs via `messages.parse()` against a Pydantic schema
- Spec-aware: the model, its prices and which optional request fields it takes
  all travel together on a `ModelSpec`, so naming a model is not the same thing
  as choosing one of three Claude tiers

Caching note: the system prompt is marked with a 1-hour TTL. At the default
15-minute decision cadence a 5-minute cache would expire between every call and
never pay for itself, whereas a 1-hour cache is read roughly four times per
write. See docs/COSTS.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import anthropic
from pydantic import BaseModel, Field

from .config import DAY_NAMES, Env, ModelSpec, Rules
from .models import (
    # `x as x` is mypy's explicit re-export form under --strict, and the
    # re-export is the point: the models this applies to live in `models.py`,
    # which this module imports, so the ConfigDict has to be defined there or
    # the import closes a cycle. It is documented HERE, beside the measurements
    # that are the only reason anybody would reach for it, and `dreamer.py` and
    # every reader keeps importing it from where the reasoning is.
    EVERY_FIELD_REQUIRED as EVERY_FIELD_REQUIRED,
)
from .models import OrderProposal, PositionPlan, SymbolAssessment

# Attach to any Pydantic model handed to `messages.parse` as an `output_format`.
#
# **The count of PROPERTIES on a schema is cheap. The count of OPTIONAL
# properties is not, and it is what killed the dreamer.** A property the schema
# does not require may be present or absent, so the grammar the API compiles has
# to accept every subset of the optional set, in any order — one more optional
# field doubles that space. Nothing warns on the way in, because the request is
# perfectly valid; the compiler simply runs out of time.
#
# Measured against `claude-sonnet-5` on 2026-08-10, on synthetic models of N
# fields and nothing else:
#
#     8 optional                           compiles, 18s cold — already slow
#     10 optional                          compiles, borderline
#     12 optional                          TIMES OUT at 150s
#     11 required-nullable                 compiles in 5.3s
#     15 required-nullable                 compiles in 10.5s
#     15 optional, declared required here  compiles in 10.7s
#
# Two things follow, and the second is the one that is easy to get wrong:
#
# - **A nullable field is free; an ABSENT field is what costs.** Fifteen fields
#   that must each be stated — as a value, an empty list or a null — compile in
#   ten seconds, where twelve that may simply be missing do not compile at all.
# - **It compounds across nested models, so read the schema whole.** `DreamStep`
#   minus `conditions` compiled and `StepCondition` on its own compiled; the two
#   together did not. Eleven optional fields at the top plus seven more across
#   two nested models put `electrum-bot dream` past the line, and every call it
#   made came back 400 "Schema is too complex", 400 "Grammar compilation timed
#   out", or a plain timeout.
#
# What is NOT the rule is a budget on the total. `ModelDecision` carried twelve
# optional properties across four models, never more than five on any one of
# them, and it compiled — concentration is what bites, not the sum.
#
# It is worth knowing WHICH schema has the least room, though, and it was not the
# dreamer's. Measured the same day, same model, one call each:
#
#     DreamStep, fixed    3.1s      DreamerTurn   3.4s
#     TraderTurn          5.3s      ModelDecision  14.7s
#
# **`ModelDecision` was the slowest thing this repository sends, and it now
# leaves nothing optional at all.** That happened when the trailing exit was
# added: the field it needed sits on `OrderProposal`, inside the schema that had
# the least room, so the cheap move was to spend nothing rather than to spend a
# little. Measured on 2026-08-11 against `claude-sonnet-5`, the three shapes
# alternating within one run, four cold compiles each:
#
#     before   12 optional across five objects   10.9 / 11.3 / 13.5 / 14.2s
#     naive    the trail added as a 13th         11.7 / 13.2 / 13.5 / 14.4s
#     shipped  0 optional, WITH the new field     8.0 /  8.8 /  8.9 / 11.7s
#
# So the field is free and the shape it was added in pays for itself. The
# synthetic control reproduces unchanged on the same afternoon — 12 optional
# properties on ONE object timed out at 170s, 15 required-nullable compiled in
# 3.1s, and 20 optional was refused outright with 400 "too many partitions".
#
# **Two things about measuring this, both of which got it wrong first.**
# Alternate the shapes rather than taking a before and an after an hour apart:
# the spread within one shape is wider than the gap between two, so a single
# pair of readings measures the afternoon. And rebuild EVERY shape under a fresh
# name, including the one already in the file — the server caches a compiled
# grammar, and a schema sent earlier that day reads back in about 2.5 seconds.
# The first attempt here subclassed the live `ModelDecision`, left its nested
# `$defs` byte-identical to something already compiled, and measured the cache.
#
# The alternative was to drop `output_format` and validate the JSON on this
# side, which hands back to the model the freedom to return a value the schema
# forbids — a number this repository would then have to trust. This keeps the
# guarantee and costs a handful of `null`s on the wire.


class ModelDecision(BaseModel):
    """Top-level output the model must produce on every call.

    `assessments` and `position_plans` cost output tokens on a cycle that
    proposes nothing, which is most cycles. That is the point. Without them a
    quiet cycle records only "no proposals", and "nothing met the conditions"
    is indistinguishable from "the loop never looked", which are very different
    states to be in and only one of them is fine.

    Every property is REQUIRED on the wire and none in Python, here and on all
    three nested models. An empty list is still the right answer on a quiet
    cycle; what changes is that the model has to SAY `[]` rather than leave the
    key out. See `EVERY_FIELD_REQUIRED` for why that is the cheap direction.
    """

    model_config = EVERY_FIELD_REQUIRED

    market_assessment: str = Field(description="One-paragraph read of the current market.")
    proposals: list[OrderProposal] = Field(
        default_factory=list,
        description=(
            "Zero or more order proposals. An empty list means stand pat, "
            "which is a valid and often correct answer."
        ),
    )
    assessments: list[SymbolAssessment] = Field(
        default_factory=list,
        description=(
            "One entry for EVERY tradeable symbol you were given indicators for, "
            "including the ones you are not proposing. This is how the operator "
            "sees what you considered."
        ),
    )
    position_plans: list[PositionPlan] = Field(
        default_factory=list,
        description=(
            "One entry per position currently open, saying why it is still held "
            "and what would close it. Advisory only: nothing here is executed."
        ),
    )


# The dreamer's budget. Generous on purpose: it runs once a day, nothing waits
# on it, and depth is the entire product. Thinking tokens count against
# max_tokens, so a proposal-sized budget would truncate the chain that the
# thinking was spent producing.
DREAM_MAX_TOKENS = 16000

# **"Nothing waits on it" is a reason to be patient, not a reason to be
# unbounded.** This was 900 seconds against an SDK that retries twice by
# default, so a call that could not succeed occupied the dream timer for up to
# FORTY-FIVE MINUTES and then logged `dream_call_failed`. That is not a slow
# dream, it is an invisible one: the timer is still running, the process looks
# alive, and nothing on any surface says the dreamer has stopped working. A
# failure that takes three quarters of an hour to admit itself is the
# confident-partial-answer problem wearing a stopwatch.
#
# Measured against `claude-sonnet-5` on 2026-08-10, running the shipped
# `Dreamer.run_once` end to end: 109.3s on the first call of the day, which
# carries the one-time grammar compile, then 33.1s and 39.6s. So 240 seconds is
# a little over twice the worst observed call, and a call still running at four
# minutes is not a deep one, it is a stuck one.
#
# **The retry count is set here rather than left to the SDK, because the bound
# is the product of the two and a default nobody wrote down is not a bound.**
# One retry buys back a dropped connection on a job that only gets one attempt a
# day; it caps the worst case at roughly eight minutes. Note the failure this
# whole change is about — a 400 from the grammar compiler — is not retried by
# the SDK at all, so retries never multiply a schema problem.
DREAM_TIMEOUT_SECONDS = 240.0
DREAM_MAX_RETRIES = 1


@dataclass(frozen=True)
class CallUsage:
    """What one model call spent, and what that cost — when that is knowable.

    **`estimated_cost_usd` is `float | None`, and `None` means UNKNOWN rather
    than free.** It was a plain float while the only reachable models were the
    three Claude tiers, every one of which has prices on file. The moment an
    arbitrary model can be named, a model nobody has priced computes a cost of
    0.00 — which reads as *this call was free* on the Settings page and in every
    log line, and is the missing-versus-zero rule with money attached.

    The fix is not a default price. An invented cost is the same class of error
    as an invented indicator: it is a figure nobody measured, presented beside
    figures that were. So the field carries the absence, and every consumer has
    to say "unknown" rather than print a zero or sum it silently into a total.
    A total that quietly drops an unknown component is the same bug one level
    up, which is why `ConferenceReport.cost_usd` is `float | None` too.

    The token counts are never None. They come off the response, so they are
    known whoever served it; it is only the price that can be missing.
    """

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    estimated_cost_usd: float | None


SYSTEM_PROMPT_TEMPLATE = """\
You are the decision engine inside an automated trading bot running against an
Alpaca **paper-trading** account (US equities and, when enabled, crypto).

You DO NOT execute trades. You PROPOSE orders. A separate, deterministic risk
module accepts or rejects each proposal against the rules below. That module is
code, not a prompt — you cannot negotiate with it, and arguing with a rejection
will not change the outcome. Your job is to make proposals it will approve.

## Hard rules (mirrored from config/rules.yaml)

{rules_summary}

## Output contract

Return JSON with:

- `market_assessment`: one paragraph on the current market read.
- `proposals`: a list of order proposals. An empty list is a valid answer and is
  the right one whenever no high-quality setup exists.
- `assessments`: **one entry for every symbol you were given indicators for**,
  including the ones you are passing on.
- `position_plans`: one entry for every position currently open.

Each proposal needs `symbol`, `asset_class`, `direction` (buy or sell), `qty`
(shares or coin units, not lots), `limit_price`, `stop_loss_price` and a
`rationale` of at least one full sentence. `take_profit_price` and
`trail_percent` are the exit and are yours to choose — see below. State every
field: where you have nothing to say, say `null` or an empty list rather than
leaving the key out.

### the exit is yours, and there are three of them

`stop_loss_price` is required and is not part of this choice. It is the hard
stop, it is what your size is computed from, and every exit below sits on top of
it. What you are choosing is how the trade ENDS when it goes your way.

- **A fixed target.** Set `take_profit_price` and leave `trail_percent` null.
  The order goes out as a bracket: entry, stop, and a limit at your target, all
  resting at the broker.
- **No target.** Leave both null. The order is an entry plus a stop, and the
  trade runs until the stop fills or something closes it. This is a normal
  trade, not an unfinished one — the operator's rules require a stop and have
  never required an exit. **Do not invent a target to fill the field.** A number
  chosen to look complete is a live order at a price you did not mean.
- **A trail.** Set `trail_percent` — how far behind the best price the stop
  follows, as a percentage — for a trade whose thesis is "let it run", where a
  fixed target would cap the part you were trying to catch. You may set a
  target as well, but a trail with a tight target is two answers to one
  question.

Three things about a trail that are properties of the broker rather than
settings, and none of them can be configured away:

- **It does not reach the broker attached to your entry.** Alpaca's bracket and
  OTO orders carry a FIXED stop trigger; a trailing stop is a separate order
  type that can only be placed against a position that already exists. So the
  leg resting behind your entry is at `stop_loss_price`, exactly as if you had
  asked for a fixed stop, and the trail is recorded rather than executed.
- **It can only ever tighten.** A trail is measured from the best price the
  trade has seen, floored by the stop already in force, so it never gives room
  back. Choosing a trail wider than your initial stop distance therefore does
  nothing at all until price has run far enough to earn it.
- **It changes nothing about your size.** Risk is `|limit_price -
  stop_loss_price| x qty` either way. Do not widen the stop because a trail
  feels safer — that buys a bigger loss at the same 1%, and the gate will
  approve it, because the gate checks arithmetic and not intent.

### assessments — say what you looked at, not only what you took

A cycle that proposes nothing must still record what you examined. Otherwise
"nothing met the conditions" and "the loop never looked at QQQ" are the same
entry afterwards, and only one of them is a working bot.

Each assessment needs `symbol`, `stance`, `reasoning`, and — when the stance is
`watch` — both `waiting_for` and `trigger`.

- `take` — you are proposing it this cycle.
- `watch` — the setup is forming but a condition is not met yet. `waiting_for`
  must name **the observable thing that would trigger it**: a price level, a
  figure, a session. "SPY closing below 641.20, roughly 1 ATR under the 20-day"
  is useful. "More confirmation" is not, and will be shown to the operator as
  the empty statement it is.
- `pass` — you examined it and there is nothing there. Say briefly why.
- `blocked` — you would take it, but a rule forbids it or the data is missing.
  Name which.

Ground every `reasoning` in the figures supplied in the Indicators section. Do
not restate a number that is reported as unavailable, and do not compute one.

### trigger — the same condition, in a form that can be checked

A `watch` also needs `trigger`: `field`, `op` and `value`. It says the same
thing as `waiting_for`, but as something code can evaluate.

    waiting_for: "SPY closing below 641.20, roughly 1 ATR under the 20-day"
    trigger:     {{field: "close", op: "below", value: 641.20}}

`field` must be one the Indicators section actually reports: `close`, `sma_20`,
`sma_200`, `atr_14`, `volume_ratio`, `distance_from_sma_20_atr`, `swing_high`,
`swing_low`, `highest_close`, `lowest_close`. `op` is `above`, `below`,
`at_or_above` or `at_or_below`.

**`value` is a number, never the name of another figure.** "Above the 20-day"
is not a trigger: the 20-day moves, so re-checking it next week tests a level
you never saw. Read the current average off the Indicators section and write
the number, which pins the claim to the moment you made it.

Do not invent a level for a field reported as unavailable. If the only
condition worth naming rests on a figure you do not have, the stance is
`blocked`, not `watch`.

Write the trigger you would actually act on. It is recorded, and later checked
against what the figures did — so a level chosen to look decisive rather than
one you would trade is a claim that will be scored as written.

### the watch list is checked, so write triggers that can be

The market context carries a "What you said last cycle" section holding your
own previous stances and the `waiting_for` you attached to each. You have no
memory between cycles; that section is the only thing you know about what you
said before, and it exists so a watch is a commitment rather than a note to
nobody.

For every symbol carried over, say in this cycle's `reasoning` whether the
trigger has fired, judged against the figures in front of you now:

- **Fired** — say so, and either propose it or state plainly what stopped you.
- **Not yet** — say which part is still missing, and repeat the trigger only if
  you would write it again today.
- **Stale** — the reading that produced it no longer holds. Drop it and say so.
  A trigger you wrote is not evidence. Restating one you can no longer justify
  is worse than passing, because it looks like conviction.

The section states how long ago it was recorded. Cycles are skipped while the
market is shut, so "last cycle" on a Monday can be Friday afternoon. Treat an
old trigger as a note to re-check, never as a standing order.

### position_plans — why you are still in, and what gets you out

For each open position: `symbol`, `action` (`hold`, `close` or `tighten_stop`),
`thesis_intact`, `reasoning`, `waiting_for`, `invalidation`, and for a
tighten, `new_stop_price`.

Write them for a person who wants to know why the position is still on and what
would end it — name the level or the target, not a mood.

**A stop may only be TIGHTENED, never widened.** Tighter means toward entry: on
a long that is a HIGHER stop, on a short a LOWER one. A level further from entry
is refused in code, on either side of the market, and the refusal is not
arguable — widening a stop on an open position increases the loss at unchanged
size, and no gate in this system sees a position move. Loosening a stop to feel
safer buys a bigger loss at the same size. If the risk is the problem, close
part of the position; do not buy room by moving the stop.

**A `tighten_stop` needs `reasoning` and `new_stop_price` together or it does
nothing.** The level is a number, in the same units as the quotes you were
shown. A plan that names the intention and no level is refused rather than
filled in with a guess: a stop invented to complete the field would be an exit
nobody chose. The reason is required for the same kind of reason — a move with
no reason on file is reported to the operator as unexplained, so an empty one
would be worse than no move at all.

**Whether any of this is ACTED ON is the operator's switch, not yours.**
`position_actions.enabled` in `config/rules.yaml` ships false, and while it is
false every plan here is recorded and none is executed: the position stays
exactly as it is. Write the plan you would act on and say what you mean; do not
write as though the move has already happened, and do not restate a tighten as
done on the next cycle. What you can rely on is that the level in force is
shown to you each cycle in the position's own line — if a stop you asked for is
in force, you will see it there.

## Sessions, and what an out-of-hours proposal actually becomes

The gate's window now permits pre-market and after hours, so you may propose
outside the regular session and the gate will not refuse you for the hour alone.
What changes is not whether you are allowed to trade — it is what your order
turns into. Every clause below is a property of the order path or of how a stop
works at any broker. None of it is a setting that can be changed.

- **Your entry does not fill out of hours. It rests.** Each entry is submitted
  as a bracket or an OTO so the stop reaches the broker with it, and Alpaca
  refuses an extended-hours flag on either. The order therefore sits until the
  regular session and fills there.
- **So the fill price is the next open, not the quote you were shown.** An
  out-of-hours quote on the free IEX feed is thin and often one-sided. A limit
  read off it is a level that may not exist by the time the order is live. If
  the setup only works at the price on the screen in front of you, it is not a
  trade you can place right now.
- **Your stop rests at the broker but cannot fire out of hours.** A stop becomes
  a market order when it triggers, and extended-hours venues accept limit orders
  only. So the leg sits through the evening, the overnight session and the
  weekend, and becomes eligible again at the next regular open. Price gapping
  through your stop overnight fills at the open, not at the stop. No broker
  offers a different answer; do not size as though the stop is a guaranteed exit.
- **Widen nothing to compensate.** The correct response to an uncertain fill is a
  smaller size or no trade, never a looser stop — the stop is what your size is
  computed from, so loosening it to feel safer buys you a larger loss.

The market context carries a **Session** block naming the phase for each
instrument class, the countdown to the next change, and whether Alpaca's own
clock disagrees. Read it before the Market snapshot. If it says the broker
reports the session closed while the computed hours say open, that is a market
holiday or an early close — this bot computes New York hours and cannot see a
holiday calendar — and the broker is right.

Crypto has no sessions. None of the above applies to it: an order is live when
it is placed, and it carries no bracket, so its stop exists in the journal and
in `stop_watch` rather than at the broker.

## Symbols an adopted dream may add to that list

The symbol lists above are not always the whole set. A separate agent — the
dreamer — builds second-order hypotheses, and when one is ADOPTED it grants
permission to trade the symbols it names, for a limited time, inside an
instrument class that is already enabled.

When that has happened, the market context carries a final section naming those
symbols. It is the ONLY place they appear. Four things about them:

- **They are tradeable this cycle.** The risk gate has been handed the same
  permission and will not reject them for being unlisted. You do not need to
  mark them `blocked` for that reason, and you should assess them like any
  other symbol you were given figures for.
- **The permission EXPIRES, and can be handed back before it does.** The
  section states when. Do not build a plan that depends on the symbol still
  being available next week.
- **A dream permits a symbol; it does not propose a position.** It is a reason
  the symbol is on the table and never a reason to be in it. Direction, entry,
  stop and size have to be justified on the figures in front of you, exactly as
  for a listed symbol. "The dream says so" is not a rationale, and a chain that
  argues a direction is still not evidence that the setup is there today.
- **The chain is speculative by construction and arrives labelled as such.**
  Every hop carries checked or UNCHECKED and the dream carries a verification
  badge and its weakest hop. An unchecked hop is a sentence the dreamer was not
  shown anything to support. Weight it accordingly, and never restate one back
  as though it were a fact you were given.

Everything else applies to a granted symbol unchanged: it faces its class's own
risk, concentration, concurrency and session limits in full. Adoption buys
entry to the list and nothing else.

## How to behave

- **Prefer doing nothing.** In a controlled experiment where six frontier models
  traded real money for two weeks, the model that made 238 trades lost 57% of its
  stake while the most selective made 38. Fees and churn, not stock picking, did
  most of the damage. Trading less is a strategy, not a failure.
- **Size down, never up.** If a setup only fits within the risk cap at a smaller
  size, propose the smaller size.
- **Defer when volatility spikes or news is imminent.**
- **State the invalidation level.** Every rationale must say what signal you are
  acting on and what price would prove you wrong. Vague rationales get flagged in
  the audit log.
- **Never propose a market order.** Limit prices only, and keep them close to the
  current market.
"""


# A strategy is a label plus one line of guidance. Deliberately thin: the point
# is that every trade is tagged with which strategy produced it, so metrics.py
# can tell them apart. Deciding what these should actually be is the operator's
# job and is the genuinely hard part of the whole project.
def _strategy_block(name: str) -> str:
    """Indent a strategy's guidance so it sits under its instrument heading."""
    from .strategy import guidance_for

    return "\n".join(f"      {line}" for line in guidance_for(name).splitlines())


def build_system_prompt(rules: Rules) -> str:
    """Render the (static) system prompt from rules.

    Same rules in, same bytes out, so the prompt cache actually hits.
    """
    # One block per enabled class, since each carries its own session window,
    # symbol list and strategy. Disabled classes are omitted entirely rather
    # than listed as unavailable, which would only invite proposals for them.
    instrument_lines: list[str] = []
    for name, instrument in sorted(rules.enabled_instruments.items()):
        cap = (
            f", max {instrument.capital_cap_pct:.0f}% of equity"
            if instrument.capital_cap_pct is not None
            else ""
        )
        instrument_lines.append(
            f"- {name} — strategy '{instrument.strategy}'{cap}\n"
            f"    symbols: {', '.join(sorted(instrument.allowed_symbols))}\n"
            # Rendered rather than repr'd. The raw field is a list on a
            # fixed-window class and a weekday-keyed mapping on a Globex one, so
            # dumping it would put two different shapes in the prompt for the
            # same idea and change the cached system prompt's bytes for no
            # reason.
            f"    sessions (UTC): {instrument.render_sessions()}\n"
            f"    trading days: "
            f"{', '.join(DAY_NAMES[d] for d in instrument.session_days_utc)}\n"
            f"    approach:\n{_strategy_block(instrument.strategy)}"
        )

    rules_summary = "\n".join(
        [
            "## Instruments you may trade\n",
            *instrument_lines,
            # Stated here, beside the lists it qualifies, and NOT filled with
            # the live grant. This prompt is cached for an hour and built once
            # at loop start; a granted symbol interpolated into it would be
            # stale within the day, would change the cached bytes every time an
            # adoption moved, and could not reflect a permission handed back an
            # hour later. The per-cycle context carries the actual symbols,
            # which is the only place that can be current.
            "\nA symbol list above may be widened for a time by an adopted "
            "dream. Any such symbol is named in the market context, never here; "
            "if the context names none, these lists are the whole set.",
            "\n## Portfolio limits (apply across every instrument)\n",
            f"- Max risk per trade: {rules.account.max_risk_per_trade_pct:.2f}% of equity",
            f"- Max COMBINED risk across all open positions: "
            f"{rules.account.max_total_risk_pct:.2f}% of equity. This is the "
            f"binding constraint most of the time — size each trade so the total "
            f"stays under it.",
            f"- Max single position value: {rules.account.max_position_pct:.0f}% of "
            f"equity (a concentration check, rarely the limit that binds)",
            f"- Max concurrent positions: {rules.account.max_concurrent_positions}",
            f"- Max gross exposure: {rules.margin.max_gross_notional_pct:.0f}% of equity",
            f"- Daily loss kill-switch: {rules.account.daily_loss_kill_pct:.1f}%",
            f"- Stand-down: {rules.stand_down.consecutive_losses_trigger} consecutive "
            f"losses beyond {rules.stand_down.loss_threshold_r:.2f}R suspends live "
            f"trading for {rules.stand_down.stage_one_days} days "
            f"({rules.stand_down.stage_two_days} on a repeat within "
            f"{rules.stand_down.repeat_window_days} days). Paper trading continues.",
            f"- Max trades: {rules.frequency.max_trades_per_day}/day, "
            f"{rules.frequency.max_trades_per_week}/week",
            f"- Cooldown per symbol: {rules.frequency.min_seconds_between_trades_per_symbol}s",
            f"- Max buying power per order: "
            f"{rules.margin.max_buying_power_utilisation_pct:.0f}%",
            f"- News blackout: {rules.news_blackout_minutes_before} min before / "
            f"{rules.news_blackout_minutes_after} min after high-impact events",
        ]
    )
    return SYSTEM_PROMPT_TEMPLATE.format(rules_summary=rules_summary)


class ModelClient:
    def __init__(
        self,
        env: Env,
        system_prompt: str,
        *,
        spec: ModelSpec | None = None,
        cache_system: bool = True,
    ) -> None:
        """
        `spec` overrides `Env.decision_spec` for this client. The decision loop
        and the dreamer run at wildly different cadences, so the model that is
        right for one is not automatically right for the other: see
        `Env.dream_spec`.

        It used to be a `ClaudeTier`, and the type is the change. A tier could
        only ever name one of three Anthropic strings, so "which model" and
        "which of three Claude tiers" were the same question. A `ModelSpec`
        carries the id, the prices and which optional request fields the
        endpoint accepts, which is everything this class needs to know about
        the far end.

        `cache_system` must be FALSE for anything that runs less often than the
        cache TTL, and getting this wrong costs money rather than saving it.
        A 1-hour cache write bills at 2x base input and a read at 0.1x, so a
        caller that always misses pays DOUBLE the system prompt on every call
        instead of once. Measured on the dreamer's ~2,400-token system block:
        $0.0095 a run cached-and-missing against $0.0071 uncached, a third more
        for a feature sold as an optimisation. The loop wakes every fifteen
        minutes and gets roughly four reads per write, so it keeps caching on.
        """
        self._client = anthropic.Anthropic(api_key=env.anthropic_api_key)
        self._spec = spec or env.decision_spec
        self._model = self._spec.model_id
        self._system_prompt = system_prompt
        self._cache_system = cache_system

    @property
    def model_id(self) -> str:
        """What actually goes on the wire, for a caller that wants to say so.

        `loop_start` records the TIER and never recorded the served model, so a
        run on a named model was invisible in the record afterwards. A tier
        cannot describe a model this repository did not pick from a list of
        three; the id can.
        """
        return self._model

    @property
    def price_is_known(self) -> bool:
        """Whether this client can put a figure on what a call cost."""
        return self._spec.price_is_known

    def _reasoning_kwargs(self, effort: str) -> dict[str, Any]:
        """The optional request fields, for endpoints that take them.

        Both are ANTHROPIC fields with an Anthropic shape, which is why the
        spec's flags are named after the vendor. A model that does not take them
        gets neither — a plain request rather than a wrong one. DigitalOcean's
        schema lists a flat `reasoning_effort` and no `output_config` at all;
        emitting that is a separate change with its own evidence and it is
        deliberately not guessed at here.
        """
        kwargs: dict[str, Any] = {}
        if self._spec.sends_anthropic_thinking:
            kwargs["thinking"] = {"type": "adaptive"}
        if self._spec.sends_anthropic_effort:
            kwargs["output_config"] = {"effort": effort}
        return kwargs

    def _system_block(self) -> list[dict[str, Any]]:
        block: dict[str, Any] = {"type": "text", "text": self._system_prompt}
        if self._cache_system:
            block["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
        return [block]

    def propose(self, market_context: str) -> tuple[ModelDecision, CallUsage]:
        """Send the market context, get back a structured decision plus token accounting."""
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 4096,
            "system": self._system_block(),
            "messages": [{"role": "user", "content": market_context}],
            "output_format": ModelDecision,
        }
        kwargs.update(self._reasoning_kwargs("medium"))

        response = self._client.messages.parse(**kwargs)
        decision = response.parsed_output
        if decision is None:
            raise RuntimeError(
                "Claude returned no parsable decision; check the output schema "
                "and whether the response hit max_tokens."
            )
        return decision, self._usage_from(response)

    def dream(self, prompt: str) -> tuple[Any, CallUsage]:
        """One dream step. Same transport as `propose`, tuned the opposite way.

        `propose` runs 96 times a day against a market that has already moved,
        so it is bought cheap and quick. This runs once a day and nothing is
        waiting on it, so it is bought **deep**: high effort, a large thinking
        budget and a generous timeout.

        That is not a preference, it is what the output needs. Following a
        causal chain two hops out and then genuinely attacking it is reasoning
        work, and reasoning work is what thinking tokens buy. A fast shallow
        answer here produces "AI is big so buy chips", which is one hop and
        already priced.

        Note it does NOT need a top-tier model. It needs a thinking one, run
        patiently. See `Env.dream_tier`.

        Imported inside the method rather than at module scope: `dreamer`
        imports this module for `ModelClient` and `CallUsage`, so a top-level
        import of `DreamStep` would close the cycle.
        """
        from .dreamer import DreamStep

        kwargs: dict[str, Any] = {
            # Room for a long chain plus the thinking that produced it. Thinking
            # tokens count against this, so the budget that is fine for a
            # proposal is not fine here.
            "max_tokens": DREAM_MAX_TOKENS,
            "model": self._model,
            "system": self._system_block(),
            "messages": [{"role": "user", "content": prompt}],
            "output_format": DreamStep,
        }
        kwargs.update(self._reasoning_kwargs("high"))

        # Long enough for a full thinking pass, short enough that a call which
        # is never going to answer says so while somebody could still act on it.
        # The retry count travels with it: the bound is the product of the two,
        # and an SDK default nobody wrote down is not a bound. See
        # `DREAM_TIMEOUT_SECONDS`.
        client = self._client.with_options(
            timeout=DREAM_TIMEOUT_SECONDS, max_retries=DREAM_MAX_RETRIES
        )
        response = client.messages.parse(**kwargs)
        step = response.parsed_output
        if step is None:
            raise RuntimeError(
                "Claude returned no parsable dream step; check the output schema "
                "and whether the response hit max_tokens."
            )
        return step, self._usage_from(response)

    def confer(
        self, prompt: str, output_format: type[BaseModel]
    ) -> tuple[Any, CallUsage]:
        """One turn of the agent-to-agent conference. See `bot/confer.py`.

        Takes its schema rather than naming one, which is the only structural
        difference from `dream`: an exchange has two speakers and they return
        different shapes — the dreamer offers text, the trading agent may also
        carry a verdict. A single schema covering both would let either side
        return the other's fields, and the one that matters is `verdict`.

        Bought in between `propose` and `dream`. It runs once a day like the
        dreamer, so it thinks; but an exchange is up to six calls arguing over a
        chain that has already been reasoned out, rather than the one call that
        reasons it out, so it takes the default budget and medium effort instead
        of `DREAM_MAX_TOKENS` and high. A turn is a few sentences by
        instruction, and paying a dream's budget six times to produce them would
        be spending on depth that the prompt explicitly asks not to be used.

        It takes the dreamer's bound as well, and that is now a ceiling rather
        than a licence: nothing waits on an exchange either, but an exchange is
        up to twelve calls, so the old 900-second timeout times three attempts
        times twelve calls was most of a working day of hanging. The bound here
        is generous for a turn that is a few sentences long, deliberately —
        being wrong in the direction of patience costs one slow run, and being
        wrong in the other direction costs the exchange.

        Lives here rather than in `confer.py` so there is ONE piece of cost
        arithmetic in the repository. `_usage_from` is what turns a response
        into money, and a second copy of it beside a second transport is two
        places to disagree about what a run cost.
        """
        kwargs: dict[str, Any] = {
            "max_tokens": 4096,
            "model": self._model,
            "system": self._system_block(),
            "messages": [{"role": "user", "content": prompt}],
            "output_format": output_format,
        }
        kwargs.update(self._reasoning_kwargs("medium"))

        client = self._client.with_options(
            timeout=DREAM_TIMEOUT_SECONDS, max_retries=DREAM_MAX_RETRIES
        )
        response = client.messages.parse(**kwargs)
        turn = response.parsed_output
        if turn is None:
            raise RuntimeError(
                "Claude returned no parsable conference turn; check the output "
                "schema and whether the response hit max_tokens."
            )
        return turn, self._usage_from(response)

    def _usage_from(self, response: anthropic.types.Message) -> CallUsage:
        u = response.usage
        in_tokens = u.input_tokens
        out_tokens = u.output_tokens
        cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0

        # **`None` rather than a fallback price.** A model with no entry in
        # `MODEL_SPECS` costs an amount this process cannot compute, and the
        # only two honest things to do with that are to report it as unknown or
        # to refuse the call. Refusing would make an unpriced model unusable,
        # which is the opposite of what naming one is for, so the cost carries
        # the absence and the surfaces say so.
        #
        # Note the tokens are still counted and still reported. "How much did it
        # think" and "what did that cost" are different questions and only the
        # second one is unanswerable here.
        pricing = self._spec.pricing
        cost: float | None = None
        if pricing is not None:
            # 1-hour cache writes bill at 2x base input.
            cost = (
                in_tokens * pricing.input_usd_per_mtok
                + out_tokens * pricing.output_usd_per_mtok
                + cache_read * pricing.cache_read_usd_per_mtok
                + cache_write * pricing.input_usd_per_mtok * 2.0
            ) / 1_000_000

        return CallUsage(
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            estimated_cost_usd=cost,
        )
