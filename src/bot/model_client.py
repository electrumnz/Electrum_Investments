"""Wraps the Anthropic SDK for the trading bot's decision step.

Design:
- Frozen system prompt (no datetimes, IDs, or volatile content) so prompt caching works
- Volatile market state goes in the user message, after the cache breakpoint
- Structured output two ways, chosen by the ENDPOINT rather than by the model:
  `messages.parse()` where the schema is enforced server-side, and a forced tool
  call validated by Pydantic where it is not
- Spec-aware: the model, its prices and which optional request fields it takes
  all travel together on a `ModelSpec`, so naming a model is not the same thing
  as choosing one of three Claude tiers

Caching note: the system prompt is marked with a 1-hour TTL. At the default
15-minute decision cadence a 5-minute cache would expire between every call and
never pay for itself, whereas a 1-hour cache is read roughly four times per
write. See docs/COSTS.md.

## Two transports, and which one is used is a property of the ENDPOINT

`ANTHROPIC_BASE_URL`-style swaps are usually a base URL and nothing else. This
one is not, and the reason was measured rather than reasoned about:
**DigitalOcean's `/v1/messages` accepts `output_config` with HTTP 200 and
silently ignores it.** Not a 400, not an error — a 200 and a paragraph of prose.
That is the worst of the three possible answers, because a caller that only
checked for an error would believe the schema was in force while it was not.
Measured 12 Aug 2026 on four models; `docs/DROPLET_AI.md` §2 has the detail.

So `_forces_tool_call` selects the substitute the same document names: one tool
whose `input_schema` is the model's JSON schema, `tool_choice` set to that tool,
and the arguments validated **here** by Pydantic.

**It keys on `Env.inference_provider.is_digitalocean` and never on the model
id**, and that is the distinction to preserve. Whether a schema is enforced is a
property of the thing serving the request, not of the weights behind it — the
same model is served with enforcement at one endpoint and without it at another,
and a check on the model name would get that exactly backwards the first time a
familiar name appeared in an unfamiliar catalogue.

What is *lost* by the substitute is narrower than it sounds, and what is *kept*
is the part that matters. Pydantic still rejects a malformed object, so a `qty`
or a `stop_loss_price` that comes back wrong is refused rather than coerced, the
cycle logs `model_call_failed` and `RiskGate` never sees it. What degrades is
reliability: the model is asked for the shape rather than constrained to it, so
the rejection rate rises and every rejection costs a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeVar, cast

import anthropic
from pydantic import BaseModel, Field, ValidationError

from .config import DAY_NAMES, Env, ModelSpec, Rules, is_claude_tier_default
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

# What a structured call comes back as. A TypeVar rather than `BaseModel` so
# `propose` keeps its `ModelDecision` return type through the shared transport:
# widening it to `Any` at the one junction every call now passes through would
# quietly switch off type checking for all three of them.
_Schema = TypeVar("_Schema", bound=BaseModel)

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


# The single tool every forced-tool-call request declares. One name, because a
# reply is only accepted when it calls THIS tool — a model that invents a second
# one has not answered the question it was asked.
STRUCTURED_TOOL_NAME = "record"

# The output budget every measurement of this path was taken at. See
# `ModelClient._by_forced_tool_call` for why it is a floor rather than a value.
FORCED_TOOL_CALL_MAX_TOKENS = 8192

# How much model-controlled text any refusal below may quote back.
#
# **Every quote-back in this module is bounded, and one of them was not.** The
# markup-key refusal rendered `sorted(arguments)[:4]` — four keys, each of
# whatever length the far end chose. Measured: a single 200,000-character key
# produced a 200,116-character exception, which becomes a log line, an
# `audit/*.jsonl` row and an `insight.py` index entry, once per cycle, against
# an audit log measured at ~500 KiB a DAY and deliberately never pruned. The
# bound is the count of keys AND the length of each, because either one alone
# leaves the other unbounded.
QUOTED_TEXT_MAX_CHARS = 300
QUOTED_KEYS_MAX = 4


def _has_markup_keys(arguments: dict[str, Any]) -> bool:
    """The `glm-5.2` failure: tool-call markup leaking into the argument keys.

    Checked on the KEYS rather than by looking for a field that should be
    there, because the giveaway is a key nobody named. A check that merely
    confirmed one recognised field would pass `{'<tool_call>record
    <arg_key>symbol': 'SPY', 'market_assessment': '...'}` — nine-tenths markup
    with one real key in it.

    Pydantic would reject most of these anyway, on the required fields the
    mangling swallowed. That is not a reason to drop this: the two produce the
    same outcome and different DIAGNOSES, and "this model cannot serve this
    path" is worth telling apart from "this cycle went wrong".

    **A key that is not a string counts as corrupt**, rather than being asked
    the question. `"<" in key` raises `TypeError` on an int, which escaped
    `ModelCallFailed` entirely and reached the caller as a crash from inside a
    module whose whole contract is that it raises one of three named errors.
    JSON cannot produce one, so the shape only arrives from a proxy or an SDK
    that constructs its own objects — which is precisely the case this check is
    about, and precisely the case where a `TypeError` is the least useful thing
    to say.
    """
    return any(
        not isinstance(key, str) or "<" in key or ">" in key or key.strip() != key
        for key in arguments
    )


def _quote_keys(arguments: dict[str, Any]) -> str:
    """A bounded rendering of the argument keys, for a refusal message."""
    shown = [str(key)[:QUOTED_TEXT_MAX_CHARS] for key in sorted(arguments, key=str)]
    return repr(shown[:QUOTED_KEYS_MAX])


def _tool_arguments(response: Any, what: str) -> dict[str, Any]:
    """The forced tool call's arguments, or a raise naming what came back instead.

    Three refusals, kept apart because they are three different findings about
    the far end. The first is the one that must never become a warning: see
    `UnstructuredReply`.
    """
    blocks = getattr(response, "content", None) or []
    calls = [
        block
        for block in blocks
        if getattr(block, "type", None) == "tool_use"
        and getattr(block, "name", None) == STRUCTURED_TOOL_NAME
    ]
    if not calls:
        # Quoted back deliberately. The whole failure is that this text is
        # plausible, so an operator reading the log line needs to see what the
        # model said instead of answering — a bare "no tool call" reads like a
        # transport fault. Whitespace collapsed to keep it one log line.
        prose = " ".join(
            " ".join(str(getattr(block, "text", "")).split())
            for block in blocks
            if getattr(block, "type", None) == "text"
        ).strip()
        raise UnstructuredReply(
            f"The model returned no {STRUCTURED_TOOL_NAME!r} tool call for the "
            f"{what}, so nothing was recorded. This is a hard failure and not a "
            "partial answer: an empty structured object parses as a completed "
            "cycle that considered nothing. "
            + (
                f"It said instead: {prose[:QUOTED_TEXT_MAX_CHARS]!r}"
                if prose
                else "It said nothing at all."
            )
        )

    arguments = getattr(calls[0], "input", None)
    if not isinstance(arguments, dict):
        raise CorruptToolCall(
            f"The {what} tool call carried {type(arguments).__name__} rather than "
            f"an object: {str(arguments)[:QUOTED_TEXT_MAX_CHARS]!r}"
        )
    if _has_markup_keys(arguments):
        raise CorruptToolCall(
            f"The {what} tool call's argument keys carry the model's own tool-call "
            f"markup, so the endpoint half-parsed it: {_quote_keys(arguments)}"
        )
    return arguments


def _missing_required(
    payload: Any, schema: dict[str, Any], defs: dict[str, Any], path: str = ""
) -> list[str]:
    """Every property the SENT schema marks required that the payload omits.

    **The transport sends one contract and used to check a different one.**
    `EVERY_FIELD_REQUIRED` puts every property of these models into the
    schema's `required` list, and on the server-enforced path the API compiles
    that into a grammar the model cannot leave a key out of. `model_validate`
    does not: it honours the PYTHON defaults, which every one of these fields
    has. Measured on the shapes actually sent — `ModelDecision` accepts three
    absent properties, `OrderProposal` three, `SymbolAssessment` two,
    `PositionPlan` five and `DreamStep` eleven, twenty-four in all.

    The result is not a rejection, which is what makes it worth closing: an
    omission becomes a VALUE, invented here and attributed to the model. A
    `position_plan` of `{"symbol": ..., "reasoning": ...}` is recorded as
    `action=hold, thesis_intact=True` — an opinion about an open position that
    nothing on the far end ever expressed, rendered to the operator as one that
    was. That is a plausible wrong figure, which is the failure this repository
    exists to refuse.

    **It refuses an ABSENT key and never an empty one.** `assessments: []` is a
    real answer and passes here untouched; `main.py` is where an empty list is
    weighed against what the model was shown, and this must not become a second
    opinion about that. The two faults are different: one is the model saying
    nothing met the conditions, the other is the model not answering that part
    of the contract at all.

    Only the four keywords these schemas actually contain are handled —
    `$ref`, `anyOf`, `items` and `properties`/`required` — checked by walking
    the emitted schemas rather than assumed. An `anyOf` is always `[X, null]`
    here, so a null payload is a legitimate answer and anything else is checked
    against the one non-null branch; a union with two real branches is left
    alone rather than guessed at, because requiring both would reject a valid
    payload and picking one would be arbitrary.
    """
    ref = schema.get("$ref")
    if isinstance(ref, str):
        target = defs.get(ref.rsplit("/", 1)[-1])
        return _missing_required(payload, target, defs, path) if target else []

    options = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(options, list):
        real = [o for o in options if isinstance(o, dict) and o.get("type") != "null"]
        if payload is None or len(real) != 1:
            return []
        return _missing_required(payload, real[0], defs, path)

    items = schema.get("items")
    if isinstance(items, dict) and isinstance(payload, list):
        return [
            gap
            for index, element in enumerate(payload)
            for gap in _missing_required(element, items, defs, f"{path}[{index}]")
        ]

    properties = schema.get("properties")
    if not isinstance(properties, dict) or not isinstance(payload, dict):
        return []

    here = f"{path}." if path else ""
    gaps = [
        f"{here}{name}"
        for name in schema.get("required", ())
        if name in properties and name not in payload
    ]
    for name, sub in properties.items():
        if name in payload and isinstance(sub, dict):
            gaps.extend(_missing_required(payload[name], sub, defs, f"{here}{name}"))
    return gaps


class ModelCallFailed(RuntimeError):
    """A model call that cannot produce a usable answer, for any reason.

    A base class rather than three unrelated errors, because every caller wants
    the same thing from all of them — `cmd_loop` logs `model_call_failed` and
    skips the cycle without emitting `cycle_complete`. It subclasses
    `RuntimeError` so the existing `except Exception` wrappers, and the callers
    that were written against the plain `RuntimeError` this module used to
    raise, keep behaving exactly as they did.
    """


class UnstructuredReply(ModelCallFailed):
    """The reply carried no tool call at all. Prose where a schema was forced.

    **This is the one that has to be a hard failure, and it is the reason this
    class has a name of its own.** A dropped schema breaks loudly in every case
    that produces a number and silently in exactly one: the quiet cycle. A model
    that answers `{"market_assessment": "the market is mixed"}` and stops has
    returned something `ModelDecision` accepts — `proposals`, `assessments` and
    `position_plans` all default to `[]` in Python — so a cycle that considered
    NOTHING is recorded as a cycle that looked at six symbols and chose to stand
    pat. Those are opposite findings and only one of them is a working bot.

    It is not hypothetical. `qwen3.8-max` returned prose on 2 of 3 attempts
    against the real `ModelDecision` schema with `tool_choice` forcing the call
    (`docs/DROPLET_AI.md`, 13 Aug 2026), which is why the check is a raise and
    not a log line: a partial answer here is indistinguishable from a good one
    downstream.
    """


class CorruptToolCall(ModelCallFailed):
    """A tool call whose ARGUMENT KEYS are the model's own tool-call markup.

    Kept apart from a plain validation failure because it is a different fault
    with a different remedy: `{'<tool_call>record  <arg_key>symbol': 'SPY'}` is
    the model emitting markup as text and the proxy half-parsing it, which means
    that model cannot serve this path at all, where an invalid payload from a
    capable model is a bad cycle. `glm-5.2` does this, and a check that looked
    for one field it recognised would pass a payload that is nine-tenths markup.
    """


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

    ## What was asked for, and what actually answered

    `ModelClient.model_id` records what was REQUESTED, and until now that was
    the only model id anywhere in the record. DigitalOcean names the model it
    served in the response body and nothing read it, so a request routed to
    something else — a deprecated id silently aliased, a catalogue entry
    repointed, a proxy substituting a cheaper model — was invisible: the audit
    log, the cost arithmetic and the Settings page would all keep naming the
    model nobody had actually run.

    Two raw strings and ONE derived comparison, deliberately. A stored
    "mismatch" flag would be a third fact about the same thing that can
    disagree with the other two, which is the reasoning that keeps
    `Adoption.is_live` computed.

    **`served_model_id` is `""` when the reply named no model at all**, and that
    is the could-not-ask state rather than agreement — the same three-valued
    shape as `FinnhubCalendar.is_degraded` and `BrokerClock`. An absence must
    never read as a match.

    **This REPORTS. Nothing here fails a cycle.** Whether a mismatch should is a
    behaviour change on the path that produces order quantities and is the
    operator's to make deliberately; `docs/DROPLET_AI.md` carries it as an open
    precondition. What is closed is that the fact can now be stated at all.
    """

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    estimated_cost_usd: float | None
    # Defaults, so every existing construction keeps working — including the
    # ones in tests, which is not merely convenience: a caller that never set
    # these gets the honest "nothing was read" answer rather than a claim.
    requested_model_id: str = ""
    served_model_id: str = ""

    @property
    def served_as_requested(self) -> bool | None:
        """Did the endpoint answer with the model that was asked for?

        Three-valued. `None` means the reply carried no model id, or none was
        requested — the question could not be settled, which is not a yes.

        **A dated snapshot of the requested alias counts as the same model.**
        Anthropic resolves `claude-sonnet-5` to `claude-sonnet-5-20260401`, so
        exact equality would report a mismatch on every ordinary Anthropic call
        — and a warning that fires on every call is a warning nobody reads,
        which would leave the real substitution as invisible as it is now.

        The exemption is deliberately NARROW: the suffix has to be a hyphen and
        eight digits. `nemotron-3-ultra` answered by `nemotron-3-ultra-550b` is
        a different model with a plausible name, and it reads as False.
        """
        if not self.requested_model_id or not self.served_model_id:
            return None
        if self.served_model_id == self.requested_model_id:
            return True
        suffix = self.served_model_id.removeprefix(self.requested_model_id)
        return (
            suffix.startswith("-")
            and len(suffix) == 9
            and suffix[1:].isdigit()
        )


SYSTEM_PROMPT_TEMPLATE = """\
You are the decision engine inside an automated trading bot running against an
Alpaca **paper-trading** account (US equities and, when enabled, crypto).

You DO NOT execute trades. You PROPOSE orders. A separate, deterministic risk
module accepts or rejects each proposal against the rules below. That module is
code, not a prompt — you cannot negotiate with it, and arguing with a rejection
will not change the outcome. Your job is to make proposals it will approve.

## Hard rules (mirrored from config/rules.yaml)

{rules_summary}

## Sizing: those limits are PERCENTAGES, and you are handed the dollars

Every limit above is a percentage of equity. Every figure you are given about
this account is in dollars. Converting between the two is arithmetic across two
documents, and it has already been done for you: the market context carries a
**Sizing ceilings, in dollars** block holding each of those limits multiplied
out against the live account, with what is already at risk ALREADY SUBTRACTED.

Read that block. Do not re-derive a ceiling from the percentages above. This is
the same rule as the Indicators section — the averages are computed in Python so
that you never state a figure you worked out yourself — applied to the one
figure that turns directly into an order quantity.

**The combined-risk cap is a BUDGET SHARED with the positions already open, not
a second per-trade allowance.** It is the only ceiling that requires a
subtraction, it is the one that binds most often, and it is the one that has
been got wrong: a proposal sized against the per-trade cap alone came out at
twice the permissible size, because most of the combined budget was already
spent on an open position. The block states what is LEFT after that subtraction.
That figure is the answer; the percentage above is not.

**The ceilings come in TWO UNITS and they cannot be compared as numbers.** RISK
is `|limit_price - stop_loss_price| x qty`; POSITION VALUE is `limit_price x
qty`. A $500 risk ceiling and a $50,000 value ceiling say nothing about each
other until your stop exists to convert between them, so "take the smaller of
the two" is not a rule anyone can follow — the smaller number is not the tighter
constraint.

Which unit binds is decided by how far your stop sits from your entry, and the
block computes that crossover for you, per instrument class, as a percentage of
entry price. So sizing is ONE COMPARISON and then ONE division:

    if |limit_price - stop_loss_price| / limit_price  >=  the crossover
        qty = tightest RISK ceiling / |limit_price - stop_loss_price|
    else
        qty = tightest POSITION VALUE ceiling / limit_price

**Round DOWN, in either branch.** A quantity rounded up is refused by the gate,
and it is refused for a single share.

**Take the branch. Do not do the RISK division and stop.** A stop tighter than
the crossover is the ORDINARY case for an equity — one ATR on a $500 name is
well under 1% of it — so POSITION VALUE is the unit that binds most of the time.
Skipping that branch has been measured at 2x, 7x and once 14x over what the gate
will accept, on proposals whose risk arithmetic was performed perfectly.

The crossover is not a minimum stop distance and not a view on where your stop
belongs; it is where two ceilings cross. Do not widen a stop to land the other
side of it — that buys a larger loss at the same size, and it changes which
division you do rather than which trade is worth making.

Every figure in that block is a ceiling, not a target, and a size that fits it is
not thereby a trade worth making.

**Two of those lines are WORDS instead of a figure. Neither one means a small
budget, and neither is a formatting quirk.**

- `BUDGET SPENT` — the headroom is gone. Nothing fits at ANY size, however
  small. Do not scale down to a token quantity to squeeze in; propose nothing
  there and say why.
- `HEADROOM UNKNOWN` — the ceiling could not be established at all, usually
  because a position is held that the journal has no row for, so its stop and
  therefore its risk cannot be read. Missing is not zero and it is not room.
  Propose nothing that ceiling governs, and name it in the assessment as
  `blocked`.

**Do not move the stop to make a size fit.** Size is computed FROM the stop
distance, so a wider stop buys fewer shares at the same risk and a tighter one
buys more. A stop chosen to justify a quantity is a quantity with no stop behind
it — and the gate will approve it, because the gate checks the arithmetic and
not the intent. If the size you wanted does not fit, the trade is smaller, or it
is not a trade.

If the market context carries no ceilings block at all, then you have not been
given them. Propose nothing rather than working them out from the percentages —
the same rule as a symbol reported with no price history.

## The stop is a claim, not a lever

`stop_loss_price` is your statement of where the thesis is WRONG. It is worth
saying which way round that is, because the arithmetic runs the other way: size
is a ceiling divided by the stop distance, so the tighter the stop the larger
the position — without limit, and with no gate objecting. Nothing in the risk
module has a view on where a stop belongs; it only measures what one costs. That
freedom is yours on purpose and it is not a gap to size into.

Measure the level you have chosen against `atr_14` in the Indicators section,
which is what a day of ordinary movement in that instrument looks like. A stop
well inside a single day's range is not a tight stop, it is a level that noise
reaches before the thesis has been tested — and it will have bought the largest
position the ceilings allow to be stopped out of. If you cannot say what your
stop level MEANS other than that it makes the size work, it is not a stop and
the trade is not ready.

The market context reports the stop width of each of your previous proposals in
ATRs. That is a description of the plan, never a rejection: no proposal has ever
been refused for a tight stop and none will be.

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

        **Which endpoint it talks to comes from the environment, and a broken
        configuration REFUSES here rather than answering from the other one.**
        `Env.inference_provider` has three states precisely so the third one can
        be acted on: a key that is present but unusable must not read as
        "Anthropic, all fine". Quietly serving the loop from a provider the
        operator did not choose is the silent downgrade this whole arrangement
        exists to refuse — it would run, bill the wrong account, and leave
        nothing on any surface to say which model produced the orders.

        Raising is the right shape *here* while `Env.inference_provider` itself
        never raises. That property describes a configuration and is read by
        banners and by the Settings page, where an exception would take a page
        down over a setting nobody was using. This is the point of USE: past it
        there is a model call, and there is no honest way to make one.
        """
        provider = env.inference_provider
        self._spec = spec or env.decision_spec
        self._model = self._spec.model_id

        if provider.is_digitalocean and not provider.usable:
            raise ModelCallFailed(
                f"{provider.detail} No model call is made and nothing falls back "
                "to Anthropic: a provider the operator did not choose is not a "
                "safe default for the path that produces order quantities."
            )

        if provider.is_digitalocean and is_claude_tier_default(self._model):
            # Both halves of this are measured, and it is worth refusing at
            # construction because neither produces a useful error at call time:
            # a wrong id is a 404 and a right one is a 403 about a subscription
            # tier, ninety-six times a day, with the loop's own broad `except`
            # turning each into one skipped cycle.
            raise ModelCallFailed(
                f"Model {self._model!r} is a Claude tier default and this process is "
                f"configured for DigitalOcean at {provider.base_url}. That endpoint "
                "names Anthropic models differently (anthropic-claude-5-sonnet, not "
                "claude-sonnet-5) and 403s the ones it does list on this account's "
                "tier. Name the model outright with DECISION_MODEL_ID or "
                "DREAM_MODEL_ID, or unset DO_INFERENCE_KEY to go back to Anthropic."
            )

        # **`base_url` is passed on BOTH branches, and the Anthropic one is why
        # this line exists.** It used to be omitted there, which is not the same
        # as asking for Anthropic: the SDK resolves its endpoint as *kwarg >
        # `ANTHROPIC_BASE_URL` > a credentials profile on disk*, so a client
        # built without one talks to whatever the environment says while
        # `inference_provider` reports "Anthropic direct" and
        # `_forces_tool_call` stays False. Measured against a stub that behaves
        # like DigitalOcean — 200, `output_config` ignored, prose back — the
        # Anthropic key went to that endpoint, the schema was enforced nowhere,
        # and a JSON-shaped reply of `{"market_assessment": "..."}` was accepted
        # as a completed cycle. Passing the value the provider NAMES is what
        # makes the banner, the Settings page and the wire agree, and it closes
        # the profile-file route too: the SDK consults no other source once a
        # base URL arrives as a kwarg.
        self._client = anthropic.Anthropic(
            api_key=(
                env.do_inference_key.strip()
                if provider.is_digitalocean
                else env.anthropic_api_key
            ),
            base_url=provider.base_url,
        )

        self._forces_tool_call = provider.is_digitalocean
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

    def _structured(
        self,
        client: Any,
        kwargs: dict[str, Any],
        output_format: type[_Schema],
        what: str,
    ) -> tuple[_Schema, CallUsage]:
        """One structured answer, however this endpoint is willing to give one.

        The two branches differ in WHERE the schema is enforced and nowhere
        else. Both return a validated object or raise; neither returns a partial
        one, and neither falls back to the other — a transport that retried the
        forced tool call as prose parsing would be handing back exactly the
        freedom the schema exists to remove.
        """
        if self._forces_tool_call:
            return self._by_forced_tool_call(client, kwargs, output_format, what)

        response = client.messages.parse(**kwargs, output_format=output_format)
        parsed = response.parsed_output
        if parsed is None:
            raise ModelCallFailed(
                f"The model returned no parsable {what}; check the output schema "
                "and whether the response hit max_tokens."
            )
        return cast("_Schema", parsed), self._usage_from(response)

    def _by_forced_tool_call(
        self,
        client: Any,
        kwargs: dict[str, Any],
        output_format: type[_Schema],
        what: str,
    ) -> tuple[_Schema, CallUsage]:
        """The substitute for server-enforced structured output.

        One tool, `tool_choice` pinned to it, and the arguments validated here.
        The schema goes out as `model_json_schema()` **unflattened** — `$defs`
        and `$ref` intact — because that is the shape the fidelity measurement
        was taken against, and resolving refs before sending would silently
        change what is being asked of the model from what was actually graded.
        """
        request = dict(kwargs)
        # Raised rather than replaced, so a caller that asked for more keeps it.
        # 8192 is the budget `scripts/do_schema_fidelity.py` measured these
        # models at, and shipping under a figure the evidence was gathered at
        # would make the evidence describe a different request. It is a ceiling
        # and not a spend: a short answer costs what a short answer costs.
        request["max_tokens"] = max(
            int(kwargs.get("max_tokens", 0)), FORCED_TOOL_CALL_MAX_TOKENS
        )
        # **`output_config` comes off HERE rather than being left to the spec.**
        # A request carrying it reads to anybody debugging as though the schema
        # were being enforced server-side, at the one endpoint measured to
        # accept it with a 200 and ignore it. Today no spec that reaches this
        # branch sets it — the three that do are Claude ids and are refused at
        # construction — so the guarantee rested entirely on that coincidence,
        # and the test asserting its absence was passing by finding nothing to
        # check. Dropping it structurally is what makes the claim true of the
        # branch rather than of today's table. Translating `effort` into
        # DigitalOcean's flat `reasoning_effort` is a separate change with its
        # own evidence and is deliberately not guessed at.
        request.pop("output_config", None)
        schema = output_format.model_json_schema()
        request["tools"] = [
            {
                "name": STRUCTURED_TOOL_NAME,
                "description": (
                    f"Record your {what}. You MUST call this tool, and it is the "
                    "only way to answer: text outside it is discarded."
                ),
                "input_schema": schema,
            }
        ]
        request["tool_choice"] = {"type": "tool", "name": STRUCTURED_TOOL_NAME}

        response = client.messages.create(**request)
        arguments = _tool_arguments(response, what)
        # Checked against the schema that was SENT, before Pydantic gets to fill
        # a default in for a property that schema declared required. See
        # `_missing_required`: the server-enforced path compiles `required` into
        # a grammar, and this path had nothing standing in for it, so an
        # omission became a value invented here and attributed to the model.
        missing = _missing_required(arguments, schema, schema.get("$defs", {}))
        if missing:
            raise ModelCallFailed(
                f"The model's {what} left out {len(missing)} propert"
                f"{'y' if len(missing) == 1 else 'ies'} the schema it was sent "
                f"declares required: {missing[:QUOTED_KEYS_MAX]}. Absent is not "
                "the same as empty -- an omitted key would take this side's "
                "default and be recorded as the model's own answer."
            )
        try:
            parsed = output_format.model_validate(arguments)
        except ValidationError as exc:
            # Loud and safe. The client-side half of the guarantee doing exactly
            # its job: a `qty` or a `stop_loss_price` the schema forbids is
            # refused here rather than coerced and passed to the gate.
            raise ModelCallFailed(
                f"The model's {what} did not satisfy the schema: "
                f"{' '.join(str(exc).split())[:400]}"
            ) from exc
        return parsed, self._usage_from(response)

    def propose(self, market_context: str) -> tuple[ModelDecision, CallUsage]:
        """Send the market context, get back a structured decision plus token accounting."""
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 4096,
            "system": self._system_block(),
            "messages": [{"role": "user", "content": market_context}],
        }
        kwargs.update(self._reasoning_kwargs("medium"))

        decision, usage = self._structured(
            self._client, kwargs, ModelDecision, "decision"
        )
        return decision, usage

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
        return self._structured(client, kwargs, DreamStep, "dream step")

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
        }
        kwargs.update(self._reasoning_kwargs("medium"))

        client = self._client.with_options(
            timeout=DREAM_TIMEOUT_SECONDS, max_retries=DREAM_MAX_RETRIES
        )
        return self._structured(client, kwargs, output_format, "conference turn")

    def _usage_from(self, response: anthropic.types.Message) -> CallUsage:
        # **The SDK does not validate this block, and that was measured rather
        # than assumed.** Responses are built with `construct_type`, which is
        # unchecked construction: a `usage` of `{"input_tokens": "10"}` yields a
        # `Usage` carrying the string, `{"input_tokens": 10}` alone yields
        # `output_tokens=None`, and both reach here. `u.output_tokens` on the
        # second raised `AttributeError` — outside this module's own three named
        # errors — and threw away a decision that had already validated, over an
        # accounting field. The first stored a `str` in a field typed `int` and,
        # on a priced model, made the cost arithmetic raise `TypeError`.
        #
        # An unreadable count is 0 and never a guess, and it makes the COST
        # unknown rather than small: `None` already means "nobody has priced
        # this model", and "nobody could read what it spent" is the same answer
        # to the same question. A zero token count reading as an outage is the
        # existing convention here — `cached_tokens` reading zero across a run
        # of cycles is how caching is checked at all.
        u = response.usage
        readable = True

        def count(name: str, *, optional: bool) -> int:
            """One token count, or 0 with the reading marked unusable.

            `optional` separates the two cache fields, which a response is
            entitled not to carry — an uncached call reports neither, and
            treating that as unknown would make every ordinary Anthropic call
            cost an unknown amount — from the two that the wire format always
            has, where an absence means the block could not be read.
            """
            nonlocal readable
            raw = getattr(u, name, None)
            if raw is None:
                readable = readable and optional
                return 0
            if isinstance(raw, bool) or not isinstance(raw, int | float | str):
                readable = False
                return 0
            try:
                return int(raw)
            except ValueError:
                readable = False
                return 0

        in_tokens = count("input_tokens", optional=False)
        out_tokens = count("output_tokens", optional=False)
        cache_read = count("cache_read_input_tokens", optional=True)
        cache_write = count("cache_creation_input_tokens", optional=True)

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
        if pricing is not None and readable:
            # 1-hour cache writes bill at 2x base input.
            cost = (
                in_tokens * pricing.input_usd_per_mtok
                + out_tokens * pricing.output_usd_per_mtok
                + cache_read * pricing.cache_read_usd_per_mtok
                + cache_write * pricing.input_usd_per_mtok * 2.0
            ) / 1_000_000

        # **What actually served this, off the response body.** Read with the
        # same suspicion as the token counts and for the same measured reason:
        # responses are built with `construct_type`, which is unchecked
        # construction, so `model` can be absent or can be any type at all. A
        # non-string is read as ABSENT rather than coerced with `str()` — a
        # coerced `None` would be the string "None", which is a model id that
        # matches nothing and reads as a mismatch, inventing a finding out of a
        # transport fault.
        served = getattr(response, "model", None)

        return CallUsage(
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            estimated_cost_usd=cost,
            requested_model_id=self._model,
            served_model_id=served if isinstance(served, str) else "",
        )
