"""The transport, and the one property of it that no other test could see.

`electrum-bot dream` could not make its model call at all. `ModelClient.dream`
asks the API to constrain the response to `DreamStep`, and the API refused the
schema every time — 400 "Schema is too complex", 400 "Grammar compilation timed
out", or a plain timeout after ninety seconds. **866 tests were green over it**,
because every one of them hands `Dreamer` a stub client and a stub never
compiles a grammar.

That is the same shape as the journal schema that could not store what the
models had just been changed to allow, and as the `.gitignore` pattern that hid
three modules from the repository while the suite passed over the copies on
disk: a green local suite says nothing about the thing it never exercises.

So this file stands behind the call in two ways, and they answer different
questions:

- **Offline, on every run** — the structural property that was violated. An
  optional property is what makes a structured-output schema expensive, the
  cost concentrates per object, and the dreamer's schemas must carry none. This
  is the guard that fails in CI when somebody adds a field with a default.
- **Live, opt-in** — the schema actually compiles. It is skipped unless the
  operator asks for it explicitly, because `tests/` may not touch the network
  and CI holds no credentials. Nothing in this file reaches the API without
  BOTH `MUDHORN_LIVE_SCHEMA_PROBE=1` and a key in the environment.

The offline test is the one that catches a regression; the live test is the one
that establishes the fix. Neither replaces the other, and a stub client must
never again be the only thing standing behind this call.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from bot.confer import DreamerTurn, TraderTurn
from bot.dreamer import DreamHop, DreamStep, StepCondition
from bot.model_client import (
    DREAM_MAX_RETRIES,
    DREAM_MAX_TOKENS,
    DREAM_TIMEOUT_SECONDS,
    ModelDecision,
)

SRC = Path(__file__).resolve().parent.parent / "src" / "bot"

# Every Pydantic model this repository hands to `messages.parse` as an
# `output_format`. Not a hand-maintained list that can quietly go stale — the
# test below enumerates the call sites out of the source and fails if one names
# a schema that is not here. Same arrangement as `tests/test_auth.py`
# enumerating routes from the application rather than from a list somebody
# remembered to update.
STRUCTURED_OUTPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    "ModelDecision": ModelDecision,  # the decision loop, every 15 minutes
    "DreamStep": DreamStep,  # the dreamer, once a day
    "DreamerTurn": DreamerTurn,  # the conference, the dreamer's side
    "TraderTurn": TraderTurn,  # the conference, the trading agent's side
}

# The schemas the dreamer sends. These must carry NO optional property at all:
# eleven at the top plus seven across two nested models is what made the call
# impossible, and the margin here was never worth having.
DREAMER_SCHEMAS: dict[str, type[BaseModel]] = {
    "DreamStep": DreamStep,
    "DreamHop": DreamHop,
    "StepCondition": StepCondition,
}

# The measured danger line, per object.
#
# Against `claude-sonnet-5` on 2026-08-10, on synthetic models of N fields and
# nothing else: 8 optional compiled but took 18 seconds cold, 10 was borderline,
# and 12 timed out at 150 seconds. Fifteen REQUIRED nullable fields compiled in
# 10.5 seconds, which is the whole point — a null is cheap and an absence is
# not.
#
# Eight is therefore where it is already slow rather than where it breaks, and
# a schema arriving at this cap should be read as a warning rather than as a
# budget to spend.
#
# **Nothing in this repository is anywhere near it any more**, and that is not a
# reason to relax it. `ModelDecision` was the worst at five on `PositionPlan`
# and went to zero when the trailing exit needed a field on `OrderProposal` —
# measured 2026-08-11, three shapes alternating within one run, four cold
# compiles each: 12 optional took 10.9-14.2s, adding the trail as a 13th took
# 11.7-14.4s, and the shipped all-required shape took 8.0-11.7s WITH the extra
# field. The cap is what stands between here and that, for the next model that
# grows a field with a default.
SAFE_OPTIONAL_PROPERTIES_PER_OBJECT = 8


def object_nodes(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every object in a JSON schema: the root, and each `$defs` entry.

    Nested models are separate nodes rather than inlined ones, which is exactly
    the reading that matters here — the cost concentrates on a single object,
    and a schema whose optional fields are spread thinly across four models
    compiles where one that piles eleven onto a single model does not.
    """
    nodes: dict[str, dict[str, Any]] = {}
    root_title = str(schema.get("title", "root"))
    if schema.get("type") == "object":
        nodes[root_title] = schema
    for name, node in schema.get("$defs", {}).items():
        if isinstance(node, dict) and node.get("type") == "object":
            nodes[str(name)] = node
    return nodes


def optional_properties(node: dict[str, Any]) -> list[str]:
    """The properties this object does NOT require, which is what costs."""
    required = set(node.get("required", []))
    return [name for name in node.get("properties", {}) if name not in required]


def output_format_sites() -> list[tuple[str, str]]:
    """Every place in `src/bot/` that names an `output_format`.

    Read out of the source rather than listed by hand. Two shapes reach the
    SDK — a `"output_format"` key in the kwargs dict `ModelClient` builds, and
    a plain keyword argument — and both are collected. A site that passes a
    variable through (which is how `confer` takes its schema from its caller)
    is recorded as `<dynamic>` and checked separately below.
    """
    sites: list[tuple[str, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module = f"bot.{path.relative_to(SRC).with_suffix('')}".replace("/", ".")
        for node in ast.walk(tree):
            value: ast.expr | None = None
            if isinstance(node, ast.Dict):
                for key, item in zip(node.keys, node.values, strict=True):
                    if isinstance(key, ast.Constant) and key.value == "output_format":
                        value = item
                        break
            elif isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "output_format":
                        value = keyword.value
                        break
            if value is None:
                continue
            name = value.id if isinstance(value, ast.Name) else "<expression>"
            resolved = name if name in STRUCTURED_OUTPUT_SCHEMAS else "<dynamic>"
            sites.append((module, resolved))
    return sites


def test_every_output_format_names_a_schema_this_file_checks():
    """A new structured-output schema must be classified, not merely added.

    The list above is the thing most likely to go stale, and a stale list is
    how `/live` was left out of the authentication test entirely — it was not a
    *page*, so it never came up when somebody wrote "every page is refused".
    This reads the call sites out of the source instead, so a schema that has
    never been measured cannot reach the API with the suite green.
    """
    sites = output_format_sites()
    assert sites, "no output_format call sites found — has the transport moved?"

    unknown = sorted({name for _, name in sites if name == "<expression>"})
    assert not unknown, (
        "an output_format is built by an expression this test cannot read. "
        "Name the schema at the call site, or extend output_format_sites()."
    )

    dynamic = [module for module, name in sites if name == "<dynamic>"]
    assert dynamic == ["bot.model_client"], (
        "only ModelClient.confer may take its schema from its caller; a new "
        f"pass-through appeared in {sorted(set(dynamic))}. Add its schemas to "
        "STRUCTURED_OUTPUT_SCHEMAS and check them here."
    )


def test_confers_callers_pass_a_schema_this_file_checks():
    """The one pass-through resolves in `confer.py`, so read it there too.

    `ModelClient.confer` takes its schema as an argument, deliberately: an
    exchange has two speakers and they return different shapes. That makes the
    conference the only place its schemas are named, so this reads them there —
    every model defined in `confer.py` that is HANDED to something is a
    candidate `output_format` and has to be accounted for.

    `isinstance` and `issubclass` are excluded: a model named in a type check is
    being read, not sent. `WakeCondition` is the live example — it reaches the
    API nested inside `TraderTurn`, where the per-object test above already
    walks it, and it is never an `output_format` of its own.
    """
    tree = ast.parse((SRC / "confer.py").read_text(encoding="utf-8"))

    defined: set[str] = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and any(isinstance(b, ast.Name) and b.id == "BaseModel" for b in node.bases)
    }
    assert defined, "confer.py defines no models — has the conference moved?"

    handed_over: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in ("isinstance", "issubclass"):
            continue
        for argument in [*node.args, *(kw.value for kw in node.keywords)]:
            if isinstance(argument, ast.Name) and argument.id in defined:
                handed_over.add(argument.id)

    assert handed_over, "confer.py hands no model to anything — has it moved?"
    assert handed_over <= set(STRUCTURED_OUTPUT_SCHEMAS), (
        f"conference schemas {sorted(handed_over - set(STRUCTURED_OUTPUT_SCHEMAS))} "
        "may be sent to the API and are not checked here."
    )


@pytest.mark.parametrize("name", sorted(STRUCTURED_OUTPUT_SCHEMAS))
def test_no_output_schema_concentrates_optional_properties(name):
    """The measured cause, pinned as arithmetic over the schema.

    A property the schema does not require may be present or absent, so the
    grammar has to accept every subset of the optional set — one more field
    doubles the space. This is the check that would have caught the dreamer
    before it shipped, and it is why the fix is not simply "make it work once".
    """
    schema = STRUCTURED_OUTPUT_SCHEMAS[name].model_json_schema()
    for object_name, node in object_nodes(schema).items():
        optional = optional_properties(node)
        assert len(optional) <= SAFE_OPTIONAL_PROPERTIES_PER_OBJECT, (
            f"{name}.{object_name} leaves {len(optional)} properties optional "
            f"({', '.join(optional)}). Measured: 8 optional properties on one "
            "object already takes 18 seconds to compile and 12 does not "
            "compile at all, while 15 REQUIRED nullable fields take 10. Make "
            "them required with model_client.EVERY_FIELD_REQUIRED, or split "
            "the call."
        )


@pytest.mark.parametrize("name", sorted(DREAMER_SCHEMAS))
def test_the_dreamers_schemas_leave_nothing_optional(name):
    """Zero, not "under the cap" — this is the schema that could not compile.

    `DreamStep` carried eleven optional fields of its own, `StepCondition` five
    and `DreamHop` two. `DreamStep` minus `conditions` compiled and
    `StepCondition` alone compiled; the two together did not, which is what
    established that the cost compounds across nested models rather than being
    a property of one of them.
    """
    schema = DREAMER_SCHEMAS[name].model_json_schema()
    node = object_nodes(schema)[name]
    assert optional_properties(node) == [], (
        f"{name} declares optional properties again. Every field the dreamer "
        "returns must be REQUIRED on the wire — a null and an empty list are "
        "still answers, and an absent key is what the grammar compiler cannot "
        "afford. Add model_client.EVERY_FIELD_REQUIRED to the model."
    )


def test_the_decision_schema_leaves_nothing_optional_either():
    """The slowest schema this repository sends, and now the cheapest shape.

    It was 12 optional properties across five objects — five of them on
    `PositionPlan` — and 14.7 seconds against 3.1 for the fixed `DreamStep`. The
    trailing exit needed a field on `OrderProposal`, which sits inside exactly
    that schema, so the cheap move was to spend nothing rather than to spend a
    little: every property on every object here is required on the wire and
    none in Python.

    Zero rather than "under the cap", for the same reason the dreamer's is
    zero. This is the object graph a field gets added to whenever the prompt
    asks for one more thing, and the cap above is where compilation is already
    slow rather than where it breaks.
    """
    schema = ModelDecision.model_json_schema()
    nodes = object_nodes(schema)
    assert set(nodes) >= {
        "ModelDecision",
        "OrderProposal",
        "SymbolAssessment",
        "PositionPlan",
    }, sorted(nodes)

    for name, node in nodes.items():
        assert optional_properties(node) == [], (
            f"{name} declares optional properties again "
            f"({', '.join(optional_properties(node))}). Add "
            "model_client.EVERY_FIELD_REQUIRED to the model — a null costs "
            "nothing on the wire and an absent key is what the grammar "
            "compiler cannot afford."
        )


def test_a_proposal_can_still_be_built_without_naming_an_exit():
    """Required on the wire must not become required in Python.

    The same guarantee as `test_required_on_the_wire_does_not_make_anything_
    required_in_python` one file over, and it matters more here: `conftest`,
    `mcp_server._build_proposal` and most of `test_risk.py` construct proposals
    with a handful of fields. If this had reached past the transport into the
    domain, the fix for a schema cost would have changed what a valid order is.
    """
    from bot.models import Direction, OrderProposal

    proposal = OrderProposal(
        symbol="SPY",
        direction=Direction.BUY,
        qty=3,
        limit_price=580.0,
        stop_loss_price=575.0,
        rationale="No target, no trail, and both of those are real answers.",
    )

    assert proposal.take_profit_price is None
    assert proposal.trail_percent is None
    assert proposal.exit_is_trailing is False


def test_required_on_the_wire_does_not_make_anything_required_in_python():
    """The fix is a wire format and must not become a validation change.

    Every existing caller — `_apply`, `_fuse_if_asked`, every test in
    `test_dreamer.py` — constructs these models with a handful of fields and
    lets the defaults carry the rest. If that stopped working, the fix would
    have reached past the transport into the domain, which is not what it is
    for.
    """
    step = DreamStep(title="t", seed="s", stage="explore", thought="th")  # type: ignore[arg-type]
    assert step.advance_id is None
    assert step.chain == []
    assert step.conditions == []
    assert step.symbols == []
    assert step.origin == ""

    assert DreamHop(claim="c").checked is False
    assert StepCondition(text="t").value is None


def test_a_fully_stated_step_round_trips():
    """What the model now returns: every key present, empties said out loud.

    Worth pinning because the change moves work onto the model — it can no
    longer omit `symbols` or `conditions`, it has to state them empty. A parse
    that choked on an explicit null would turn a schema fix into a runtime one.
    """
    payload: dict[str, Any] = {
        "advance_id": None,
        "title": "t",
        "seed": "s",
        "origin": "",
        "stage": "explore",
        "thought": "th",
        "chain": [{"claim": "c", "checked": False, "source": ""}],
        "weakest_hop": "",
        "weakest_hop_index": None,
        "trigger": "",
        "instruments": [],
        "symbols": [],
        "conditions": [
            {
                "text": "t",
                "symbol": "",
                "settles_hops": [],
                "field": None,
                "op": None,
                "value": None,
            }
        ],
        "verdict": None,
        "fuse_ids": [],
    }
    step = DreamStep.model_validate(payload)
    assert step.chain[0].claim == "c"
    assert step.conditions[0].field is None
    assert step.verdict is None


def test_a_failed_dream_step_cannot_occupy_the_timer_for_three_quarters_of_an_hour():
    """The bound is the product of the timeout and the retries, not either one.

    900 seconds against the SDK's default of two retries was 45 minutes of a
    dream timer held by a call that had already failed — and the failure mode
    was silence, because `dream_call_failed` is logged at the END. Both halves
    are set explicitly here so the ceiling can be multiplied out by anybody
    reading the file.
    """
    assert DREAM_TIMEOUT_SECONDS * (DREAM_MAX_RETRIES + 1) <= 600
    # Still patient enough for the measured worst case: 109.3s for the first
    # call of the day, which carries the one-time grammar compile.
    assert DREAM_TIMEOUT_SECONDS >= 180


# ------------------------------------------------- what the prompt asks for
#
# The prompt and the schema are one contract in two halves, and only one of them
# is checked by the API. A prompt that names a field the model no longer has to
# supply — or that stays silent about one it now does — is a contract that
# disagrees with itself, and the model resolves the disagreement on its own.


def test_the_prompt_no_longer_calls_the_target_a_field_a_proposal_NEEDS():
    """It said so for months after the field became optional.

    `take_profit_price` stopped being required when an invented target became a
    live OCO leg resting at the broker at a price nobody chose. The prompt kept
    listing it among the fields each proposal "needs", so the model was still
    being told to produce one — which is the invented-target problem surviving
    the fix that was supposed to end it.
    """
    from bot.model_client import SYSTEM_PROMPT_TEMPLATE

    # The sentence that enumerates them, not the paragraph — the target is
    # named right afterwards, as something to CHOOSE rather than to supply, and
    # that distinction is the whole content of this test.
    needs = SYSTEM_PROMPT_TEMPLATE.split("Each proposal needs", 1)[1].split(".", 1)[0]

    assert "stop_loss_price" in needs
    assert "take_profit_price" not in needs, (
        "the prompt lists the target among the fields a proposal must supply. "
        "It has been optional since brackets became real orders."
    )
    assert "trail_percent" not in needs
    assert "are the exit and are yours to choose" in SYSTEM_PROMPT_TEMPLATE


def test_the_prompt_offers_all_three_exits_and_names_none_as_the_default():
    """The exit is the agent's decision, so all three have to be on the page.

    A prompt naming two of them makes the third unreachable however good the
    schema is, which is the state this replaces: the model could not express a
    trail because nothing ever told it one existed.
    """
    from bot.model_client import SYSTEM_PROMPT_TEMPLATE

    assert "trail_percent" in SYSTEM_PROMPT_TEMPLATE
    assert "take_profit_price" in SYSTEM_PROMPT_TEMPLATE
    # And the empty answer stays a respectable one, in the prompt's own words.
    assert "Do not invent a target" in SYSTEM_PROMPT_TEMPLATE


def test_the_prompt_does_not_promise_the_broker_is_holding_the_trail():
    """The one claim that would be false.

    Alpaca accepts no trailing leg on an entry, so a trailing proposal rests a
    FIXED stop and the trail is recorded rather than executed. A prompt that let
    the model believe otherwise would have it sizing and holding as though
    something were following the price, which is the confident-partial-answer
    failure with the model on the receiving end of it.
    """
    from bot.model_client import SYSTEM_PROMPT_TEMPLATE

    section = SYSTEM_PROMPT_TEMPLATE.split("### the exit is yours", 1)[1]

    assert "does not reach the broker" in section
    assert "can only ever tighten" in section
    # Size still comes from the stop, and the prompt must keep saying so — a
    # trail is the exact excuse somebody would use to loosen one.
    assert "Do not widen the stop" in section


# --------------------------------------- which model, what it costs, what it sends
#
# `ModelSpec` replaced "one of three Claude tiers" as the answer to "which
# model". Three things were welded to that enum and each is checked here: the
# id, the prices, and the two ANTHROPIC-only request fields.
#
# The rule the whole change is held to: an existing deployment must build a
# byte-identical request and compute an identical cost. This is a decoupling,
# not a migration.


class _Recorder:
    """Stands in for the SDK client. Captures the kwargs and refuses to answer."""

    def __init__(self) -> None:
        self.captured: dict[str, Any] = {}

        outer = self

        class _Messages:
            def parse(self, **kw: Any) -> Any:
                outer.captured.update(kw)
                raise RuntimeError("stop")

        self.messages = _Messages()

    def with_options(self, **kw: Any) -> _Recorder:
        self.captured["options"] = kw
        return self


def _client(spec: Any, monkeypatch: Any) -> Any:
    from bot.config import Env
    from bot.model_client import ModelClient

    env = Env(_env_file=None)  # type: ignore[call-arg]
    env.anthropic_api_key = "test"
    client = ModelClient(env, "system text", spec=spec, cache_system=False)
    monkeypatch.setattr(client, "_client", _Recorder())
    return client


@pytest.mark.parametrize(
    ("tier", "thinks"),
    [("haiku", False), ("sonnet", True), ("opus", True)],
)
def test_a_claude_tier_sends_exactly_what_it_always_sent(tier, thinks, monkeypatch):
    """The `if self._tier in (SONNET, OPUS)` test, moved rather than changed.

    Haiku has no extended thinking, so it was sent neither field; Sonnet and
    Opus were sent both. That decision now lives on the spec, and the request
    has to come out identical either way — a decoupling that quietly stopped
    sending `thinking` would make every proposal shallower with nothing on any
    surface saying so.
    """
    from bot.config import CLAUDE_MODEL_SPECS, ClaudeTier

    spec = CLAUDE_MODEL_SPECS[ClaudeTier(tier)]
    client = _client(spec, monkeypatch)

    with pytest.raises(RuntimeError):
        client.propose("context")

    captured = client._client.captured
    assert captured["model"] == spec.model_id
    if thinks:
        assert captured["thinking"] == {"type": "adaptive"}
        assert captured["output_config"] == {"effort": "medium"}
    else:
        assert "thinking" not in captured
        assert "output_config" not in captured


def test_a_model_that_takes_neither_field_is_sent_a_plain_request(monkeypatch):
    """`thinking` and `output_config` are ANTHROPIC fields with an Anthropic
    shape.

    DigitalOcean's schema lists a flat `reasoning_effort` and carries no
    `output_config` at all, so sending Anthropic's pair at a non-Anthropic
    endpoint is wrong in two ways at once. A spec that takes neither gets
    neither: correct-and-plain rather than wrong. Emitting `reasoning_effort`
    where it is wanted is a separate change with its own evidence.
    """
    from bot.config import ModelSpec

    client = _client(ModelSpec(model_id="some-open-model"), monkeypatch)

    with pytest.raises(RuntimeError):
        client.propose("context")

    captured = client._client.captured
    assert captured["model"] == "some-open-model"
    assert "thinking" not in captured
    assert "output_config" not in captured
    # And the parts that are not vendor-specific still go out unchanged.
    assert captured["output_format"] is ModelDecision
    assert captured["max_tokens"] == 4096


class _Usage:
    input_tokens = 2_000
    output_tokens = 500
    cache_read_input_tokens = 10_000
    cache_creation_input_tokens = 1_000


class _Response:
    usage = _Usage()


def test_a_priced_model_computes_the_same_cost_it_always_did(monkeypatch):
    """The arithmetic, spelled out here rather than trusted to the table.

    Base input, output, cache read at a tenth of input, and a 1-hour cache
    WRITE at twice base input. If any of that had shifted while the prices moved
    onto the spec, every cost figure in the repository would be quietly wrong in
    a way no other test would notice.
    """
    from bot.config import CLAUDE_MODEL_SPECS, ClaudeTier

    client = _client(CLAUDE_MODEL_SPECS[ClaudeTier.SONNET], monkeypatch)
    usage = client._usage_from(_Response())

    expected = (2_000 * 2.0 + 500 * 10.0 + 10_000 * 0.20 + 1_000 * 2.0 * 2.0) / 1_000_000
    assert usage.estimated_cost_usd == pytest.approx(expected)
    assert usage.input_tokens == 2_000
    assert usage.cache_write_tokens == 1_000


def test_an_unpriced_model_reports_an_unknown_cost_and_never_a_zero(monkeypatch):
    """**The trap this whole change exists around.**

    `CallUsage.estimated_cost_usd` was a plain float, so a model whose price is
    not on file would report 0.00 — which reads as *free* on the Settings page
    and in every log line. That is the missing-versus-zero rule with money
    attached, and it is the same class of error as an invented indicator.

    The field carries the absence instead. Note what is NOT lost: the token
    counts are still exact, because they come off the response and are known
    whoever served it. "How much did it think" and "what did that cost" are
    different questions and only the second one is unanswerable.
    """
    from bot.config import ModelSpec

    client = _client(ModelSpec(model_id="some-open-model"), monkeypatch)
    usage = client._usage_from(_Response())

    assert usage.estimated_cost_usd is None, (
        "an unpriced model reported a number. A zero here is indistinguishable "
        "from a free call on every surface that renders it."
    )
    assert usage.input_tokens == 2_000
    assert usage.output_tokens == 500
    assert usage.cache_read_tokens == 10_000
    assert client.price_is_known is False


def test_the_client_can_name_the_model_it_is_actually_using(monkeypatch):
    """`loop_start` recorded the TIER and never the served model.

    That was survivable while a tier could only mean one of three Claude
    strings. It stops being survivable the moment `DECISION_MODEL_ID` can name
    anything, because the record would then describe a setting rather than a
    run.
    """
    from bot.config import ModelSpec

    client = _client(ModelSpec(model_id="some-open-model"), monkeypatch)
    assert client.model_id == "some-open-model"


LIVE_FLAG = "MUDHORN_LIVE_SCHEMA_PROBE"
LIVE_KEY = "ANTHROPIC_API_KEY_ELECTRUM"


@pytest.mark.skipif(
    os.environ.get(LIVE_FLAG) != "1" or not os.environ.get(LIVE_KEY),
    reason=(
        f"live schema probe is opt-in: set {LIVE_FLAG}=1 and {LIVE_KEY}. "
        "Tests may not touch the network by default, and CI holds no key."
    ),
)
def test_the_real_api_compiles_the_real_dream_schema():
    """The only check that establishes the fix rather than describing it.

    Deliberately not part of the ordinary suite. The convention in this
    repository is that no test touches the network, and it is a good one — but
    it is precisely what let a schema the API refuses ship green, so the answer
    is an opt-in probe rather than an exemption or a stub.

    Two gates, not one: the flag AND the key. A developer who happens to have
    credentials exported still runs the suite offline.

    Bounded and unretried on purpose. The failure being checked for is a 400 or
    a compile timeout, and both should be reported in under two minutes rather
    than waited out.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ[LIVE_KEY]).with_options(
        timeout=120.0, max_retries=0
    )
    response = client.messages.parse(
        model="claude-sonnet-5",
        max_tokens=DREAM_MAX_TOKENS // 8,
        messages=[
            {
                "role": "user",
                "content": (
                    "Produce one seed step about anything at all. Keep it to a "
                    "sentence per field."
                ),
            }
        ],
        output_format=DreamStep,
    )
    step = response.parsed_output
    assert isinstance(step, DreamStep), "the API returned no parsable dream step"
    assert step.title


# ------------------------------------- nothing on the wire names a vendor


def _wire_schemas() -> list[type[BaseModel]]:
    """Every Pydantic model actually sent to a model as an output schema.

    Found by the marker rather than by a hand-kept list, so a schema added
    later is covered without anybody remembering to add it here.
    `EVERY_FIELD_REQUIRED` is documented in `model_client` as the config to
    "attach to any Pydantic model handed to `messages.parse` as an
    `output_format`", which makes it exactly the set of wire schemas.
    """
    import importlib
    import inspect
    import pkgutil

    import bot
    from bot.models import EVERY_FIELD_REQUIRED

    marker = EVERY_FIELD_REQUIRED["json_schema_extra"]
    found: dict[str, type[BaseModel]] = {}
    for info in pkgutil.walk_packages(bot.__path__, prefix="bot."):
        try:
            module = importlib.import_module(info.name)
        except Exception:  # a module needing optional deps is not a wire schema
            continue
        for _, obj in vars(module).items():
            if not (inspect.isclass(obj) and issubclass(obj, BaseModel)):
                continue
            if obj.model_config.get("json_schema_extra") is marker:
                found[f"{obj.__module__}.{obj.__name__}"] = obj
    return list(found.values())


def test_no_schema_sent_to_a_model_names_a_vendor():
    """**A Pydantic docstring becomes the schema's `description`, and the schema
    is sent to the model.**

    Caught by the operator asking whether the Anthropic environment variable
    names could influence a model. They cannot — those are read by the client
    library to choose an endpoint and a credential, and never leave the
    process. But the question was right about the *category*, and this is where
    it was true: `OrderProposal`'s docstring read "What Claude proposes", and
    that string went over the wire inside `output_config.format`.

    The souls moved to `llama-4-maverick` on 12 Aug 2026, so a schema saying
    "Claude" tells whichever model is reading it that the output belongs to a
    different one. At best noise in the context; at worst an invitation to
    answer as something it is not.

    Env var names are deliberately NOT covered by this: `ANTHROPIC_API_KEY` and
    `ANTHROPIC_BASE_URL` are the SDK's own, renaming them breaks the client,
    and they never reach a model. The rule is about what crosses the wire.
    """
    import json

    offenders: list[str] = []
    for model in _wire_schemas():
        schema = json.dumps(model.model_json_schema())
        for word in ("Claude", "claude", "Anthropic", "anthropic"):
            if word in schema:
                offenders.append(f"{model.__module__}.{model.__name__} contains {word!r}")

    assert not offenders, (
        "A vendor name reaches the model inside an output schema:\n  "
        + "\n  ".join(offenders)
        + "\n\nPydantic turns a class docstring into the schema's `description` "
        "and a field name into its `title`, so both are sent. Reword them."
    )


def test_the_wire_schema_marker_actually_finds_something():
    """Or the check above passes by finding nothing to check."""
    names = {f"{m.__module__}.{m.__name__}" for m in _wire_schemas()}
    assert "bot.model_client.ModelDecision" in names, names
