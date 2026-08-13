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
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from bot.confer import DreamerTurn, TraderTurn
from bot.dreamer import DreamHop, DreamStep, StepCondition
from bot.model_client import (
    DREAM_MAX_RETRIES,
    DREAM_MAX_TOKENS,
    DREAM_TIMEOUT_SECONDS,
    STRUCTURED_TOOL_NAME,
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


# ------------------------------------------- the endpoint that ignores the schema
#
# DigitalOcean's `/v1/messages` accepts `output_config` with HTTP 200 and
# **silently ignores it**, returning prose. Measured 12 Aug 2026 on four models;
# `docs/DROPLET_AI.md` section 2. That is the worst of the three possible
# answers — a caller checking only for an error would believe the schema was in
# force — so the transport switches to a forced tool call validated here.
#
# Everything below stands behind that substitute. The load-bearing one is
# `test_a_reply_with_no_tool_call_is_a_hard_failure`: it is the only failure in
# the set that is SILENT without a raise.


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_block(arguments: Any, *, name: str = STRUCTURED_TOOL_NAME) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=arguments)


def _reply(*blocks: Any) -> SimpleNamespace:
    return SimpleNamespace(
        content=list(blocks),
        usage=SimpleNamespace(input_tokens=1_000, output_tokens=200),
    )


class _Endpoint:
    """A DigitalOcean-shaped endpoint: it answers `create` and refuses `parse`.

    The refusal is a property under test rather than test scaffolding. This
    endpoint accepts `output_config` and ignores it, so a transport that reached
    for `messages.parse` here would get a 200 and prose — the exact silent
    failure the substitute exists to avoid. Raising makes that a red test
    instead of a plausible one.
    """

    def __init__(self, reply: Any) -> None:
        self.captured: dict[str, Any] = {}
        self.reply = reply
        outer = self

        class _Messages:
            def create(self, **kw: Any) -> Any:
                outer.captured.update(kw)
                return outer.reply

            def parse(self, **kw: Any) -> Any:
                raise AssertionError(
                    "messages.parse was used against an endpoint that accepts "
                    "output_config and ignores it. The schema would not be "
                    "enforced anywhere and nothing would say so."
                )

        self.messages = _Messages()

    def with_options(self, **kw: Any) -> _Endpoint:
        self.captured["options"] = kw
        return self


def _do_env(**overrides: Any) -> Any:
    from bot.config import Env

    env = Env(_env_file=None)  # type: ignore[call-arg]
    env.do_inference_key = "do-model-access-key"
    env.do_inference_base_url = ""
    for key, value in overrides.items():
        setattr(env, key, value)
    return env


def _do_client(reply: Any, monkeypatch: Any, *, model_id: str = "deepseek-v4-pro") -> Any:
    from bot.config import ModelSpec
    from bot.model_client import ModelClient

    client = ModelClient(
        _do_env(), "system text", spec=ModelSpec(model_id=model_id), cache_system=False
    )
    monkeypatch.setattr(client, "_client", _Endpoint(reply))
    return client


def test_a_digitalocean_endpoint_forces_a_tool_call_instead_of_a_schema(monkeypatch):
    """The substitute, and the shape it has to take.

    One tool, `tool_choice` pinned to it, and the schema unflattened — `$defs`
    and `$ref` intact, because that is what `scripts/do_schema_fidelity.py`
    graded these models against. Resolving refs before sending would quietly
    ask something different from what the evidence describes.

    `output_config` must be absent. Sending it costs nothing and would be a
    claim: the endpoint answers 200 and ignores it, so a request carrying both
    would read to anybody debugging as though the schema were being enforced
    server-side.
    """
    decision = {"market_assessment": "quiet", "proposals": [], "assessments": [],
                "position_plans": []}
    client = _do_client(_reply(_tool_block(decision)), monkeypatch)

    got, usage = client.propose("context")

    sent = client._client.captured
    assert sent["tool_choice"] == {"type": "tool", "name": STRUCTURED_TOOL_NAME}
    assert "output_format" not in sent
    assert "output_config" not in sent

    (tool,) = sent["tools"]
    assert tool["name"] == STRUCTURED_TOOL_NAME
    assert tool["input_schema"]["$defs"], (
        "the schema went out flattened. The fidelity measurement was taken "
        "against the nested shape, so flattening makes the evidence describe a "
        "different request."
    )
    assert got.market_assessment == "quiet"
    assert usage.input_tokens == 1_000


def test_a_reply_with_no_tool_call_is_a_hard_failure(monkeypatch):
    """**The one that matters, and the only one here that is silent without it.**

    A dropped schema breaks loudly in every case that produces a number and
    silently in exactly one: the quiet cycle. `ModelDecision` defaults
    `proposals`, `assessments` and `position_plans` to `[]` in Python — that is
    correct, an empty list is a real answer — so a model that returns prose and
    nothing else, or a bare `market_assessment`, produces an object that parses
    as a completed cycle which considered nothing.

    "Nothing met the conditions" and "the loop never looked" are opposite
    findings, and `assessments` exists precisely to keep them apart. A partial
    answer here is indistinguishable from a good one downstream.

    Live rather than hypothetical: `qwen3.8-max` returned prose on 2 of 3
    attempts against this exact schema with `tool_choice` forcing the call
    (`docs/DROPLET_AI.md`, 13 Aug 2026).
    """
    from bot.model_client import ModelDecision, UnstructuredReply

    # The premise, asserted rather than assumed. If this ever stops being true
    # the raise below is guarding nothing and this test should say so first.
    assert ModelDecision.model_validate({"market_assessment": "mixed"}).assessments == []

    client = _do_client(
        _reply(_text_block("The market looks mixed today, with SPY near its 20-day.")),
        monkeypatch,
    )

    with pytest.raises(UnstructuredReply) as raised:
        client.propose("context")

    # The prose is quoted back. A bare "no tool call" reads as a transport
    # fault, when what actually happened is that the model answered a different
    # question convincingly.
    assert "SPY near its 20-day" in str(raised.value)


def test_a_tool_call_under_another_name_is_not_an_answer(monkeypatch):
    """A model that invents its own tool has not answered what it was asked.

    Accepting any `tool_use` block would let a reply that ignored `tool_choice`
    through on the strength of being structured, which is a different claim from
    being the requested structure.
    """
    from bot.model_client import UnstructuredReply

    client = _do_client(
        _reply(_tool_block({"market_assessment": "quiet"}, name="my_own_tool")),
        monkeypatch,
    )

    with pytest.raises(UnstructuredReply):
        client.propose("context")


def test_tool_call_markup_in_the_argument_keys_is_refused_as_corrupt(monkeypatch):
    """The `glm-5.2` failure, kept apart from a plain validation failure.

    It emits its own tool-call markup as text and the proxy half-parses it, so
    the arguments arrive as `{'<tool_call>record  <arg_key>symbol': 'SPY'}`.
    Pydantic would reject most of these anyway, on the required fields the
    mangling swallowed — which is not a reason to drop the check. The two
    produce the same outcome and different DIAGNOSES, and "this model cannot
    serve this path at all" is worth telling apart from "this cycle went wrong".
    """
    from bot.model_client import CorruptToolCall

    client = _do_client(
        _reply(
            _tool_block(
                {
                    "<tool_call>record  <arg_key>market_assessment": "quiet",
                    "market_assessment": "quiet",
                }
            )
        ),
        monkeypatch,
    )

    with pytest.raises(CorruptToolCall):
        client.propose("context")


def test_arguments_the_schema_forbids_are_refused_here(monkeypatch):
    """The client-side half of the guarantee, doing the job the server used to.

    This is what the substitute actually costs and what it keeps. The model is
    ASKED for the shape rather than constrained to it, so this happens more
    often than it did — and a `qty` or a `stop_loss_price` that comes back wrong
    is still refused rather than coerced, so `RiskGate` never sees it and the
    cycle is skipped. Loud and safe.
    """
    from bot.model_client import ModelCallFailed

    client = _do_client(
        # `openai-gpt-oss-20b` and `gemma-4-31B-it` do exactly this: invent
        # their own vocabulary against the schema. 0/3 each.
        _reply(_tool_block({"ticker": "AAPL", "action": "BUY", "shares": "30"})),
        monkeypatch,
    )

    with pytest.raises(ModelCallFailed):
        client.propose("context")


def test_nothing_falls_back_to_prose_or_to_the_other_provider(monkeypatch):
    """One attempt, one transport, no second answer.

    Two silent downgrades are refused by the same property. Retrying a failed
    tool call by parsing the prose hands back exactly the freedom the schema
    exists to remove; retrying against Anthropic bills a different account and
    runs the orders on a model nobody chose. Both would still emit
    `cycle_complete`.
    """
    from bot.model_client import UnstructuredReply

    client = _do_client(_reply(_text_block("I cannot do that.")), monkeypatch)

    with pytest.raises(UnstructuredReply):
        client.propose("context")

    assert client._client.captured["tool_choice"]["name"] == STRUCTURED_TOOL_NAME
    # A single `create`, and the request that was made is the only one made.
    assert "output_format" not in client._client.captured


def test_an_anthropic_endpoint_still_gets_the_server_enforced_schema(monkeypatch):
    """The other branch, unchanged. This is a decoupling, not a migration.

    A deployment that never set `DO_INFERENCE_KEY` must build the request it
    always built: `output_format` on `messages.parse`, no tools, no
    `tool_choice`.
    """
    from bot.config import CLAUDE_MODEL_SPECS, ClaudeTier

    client = _client(CLAUDE_MODEL_SPECS[ClaudeTier.SONNET], monkeypatch)

    with pytest.raises(RuntimeError):  # the recorder refuses to answer
        client.propose("context")

    captured = client._client.captured
    assert captured["output_format"].__name__ == "ModelDecision"
    assert "tools" not in captured
    assert "tool_choice" not in captured


def test_the_transport_is_chosen_by_the_endpoint_and_never_by_the_model(monkeypatch):
    """The distinction to preserve, stated as a test because it is easy to lose.

    Whether a schema is enforced is a property of the thing SERVING the request,
    not of the weights behind it — the same model is served with enforcement at
    one endpoint and without it at another. A check on the model name would get
    that backwards the first time a familiar name appeared in an unfamiliar
    catalogue.
    """
    from bot.config import ModelSpec
    from bot.model_client import ModelClient

    named = ModelSpec(model_id="deepseek-v4-pro")

    on_anthropic = ModelClient(_env_without_do(), "system", spec=named, cache_system=False)
    on_digitalocean = ModelClient(_do_env(), "system", spec=named, cache_system=False)

    assert on_anthropic._forces_tool_call is False
    assert on_digitalocean._forces_tool_call is True


def _env_without_do() -> Any:
    from bot.config import Env

    env = Env(_env_file=None)  # type: ignore[call-arg]
    env.anthropic_api_key = "test"
    env.do_inference_key = ""
    env.do_inference_base_url = ""
    return env


def test_a_configured_but_unusable_provider_refuses_rather_than_falling_back():
    """Three states, and the third one has to be actionable.

    A key that is present but unusable must not read as "Anthropic, all fine".
    Quietly serving the loop from a provider the operator did not choose would
    run, bill the wrong account and leave nothing on any surface saying which
    model produced the orders — a silent downgrade with money attached.
    """
    from bot.model_client import ModelCallFailed, ModelClient

    env = _do_env(do_inference_base_url="http://inference.do-ai.run")  # not https

    with pytest.raises(ModelCallFailed) as raised:
        ModelClient(env, "system text")

    assert "NOT a fall back to Anthropic" in str(raised.value)


def test_a_claude_tier_default_against_digitalocean_refuses_at_construction():
    """A guaranteed failure, caught once instead of ninety-six times a day.

    Two independent reasons, both measured. That endpoint names Anthropic models
    differently — `anthropic-claude-5-sonnet`, not `claude-sonnet-5` — and 403s
    the ones it does list on this account's subscription tier. Neither produces
    a useful error at call time: one is a 404 and the other is an authorisation
    message about a subscription, each arriving inside the loop's broad `except`
    as one more skipped cycle with no cumulative signal.
    """
    from bot.model_client import ModelCallFailed, ModelClient

    with pytest.raises(ModelCallFailed) as raised:
        ModelClient(_do_env(), "system text")  # no DECISION_MODEL_ID: falls to a tier

    assert "DECISION_MODEL_ID" in str(raised.value)


def test_the_digitalocean_client_authenticates_with_the_digitalocean_key():
    """The key is the switch, so it had better be the key that goes out.

    Checked because the failure would be invisible in the good case: an
    Anthropic key sent to DigitalOcean is a 401 on every call, and a
    DigitalOcean key sent to Anthropic is the same — but a process that read the
    WRONG one while a valid other one happened to be set would work, on the
    wrong bill, with nothing to say so.
    """
    from bot.config import DO_INFERENCE_DEFAULT_BASE_URL, ModelSpec
    from bot.model_client import ModelClient

    env = _do_env(anthropic_api_key="anthropic-key-that-must-not-be-used")
    client = ModelClient(env, "system", spec=ModelSpec(model_id="mistral-3-14B"))

    assert client._client.api_key == "do-model-access-key"
    assert str(client._client.base_url).rstrip("/") == DO_INFERENCE_DEFAULT_BASE_URL


def test_a_forced_tool_call_is_not_sent_under_the_budget_it_was_measured_at(monkeypatch):
    """8192 is what `scripts/do_schema_fidelity.py` graded these models at.

    Shipping under it would make the evidence describe a different request —
    and the failure it would buy is the quiet one, because a model that runs out
    of budget mid-reasoning emits no tool call at all. It is a ceiling and not a
    spend: a short answer costs what a short answer costs.

    A caller asking for MORE keeps it. The dreamer's 16,000 is a deliberate
    budget for a long chain plus the thinking that produced it, and a floor that
    silently capped it would undo that.
    """
    from bot.model_client import FORCED_TOOL_CALL_MAX_TOKENS

    client = _do_client(_reply(_tool_block(_QUIET_DECISION)), monkeypatch)

    client.propose("context")  # propose asks for 4096

    assert client._client.captured["max_tokens"] == FORCED_TOOL_CALL_MAX_TOKENS


# ------------------------------------------------ the audit's findings, pinned
#
# Everything below was found by ATTACKING the forced-tool-call transport rather
# than by reading it, and every one of them was invisible to a suite of 2,185
# green tests. They are grouped here because they share one shape: the module
# claimed a property in prose, and the code held a weaker one.


# A complete decision, in the shape the wire schema actually asks for. Used
# wherever a test is about something OTHER than the payload, so that a fixture
# cannot quietly become the thing under test — which is how the budget test
# above ended up demonstrating the hole `_missing_required` now closes.
_QUIET_DECISION: dict[str, Any] = {
    "market_assessment": "quiet",
    "proposals": [],
    "assessments": [],
    "position_plans": [],
}


def _proposal(**overrides: Any) -> dict[str, Any]:
    base = {
        "symbol": "SPY",
        "asset_class": "us_equity",
        "direction": "buy",
        "qty": 10,
        "limit_price": 600.0,
        "stop_loss_price": 590.0,
        "take_profit_price": None,
        "trail_percent": None,
        "rationale": "Long SPY off the 20-day, invalidated below 590.",
    }
    base.update(overrides)
    return base


# ------------------------------------------- the schema sent is the schema checked


def test_a_property_the_sent_schema_requires_may_not_simply_be_absent(monkeypatch):
    """The transport sent one contract and used to check a different one.

    `EVERY_FIELD_REQUIRED` puts every property into the schema's `required`
    list, and the server-enforced path compiles that into a grammar the model
    cannot leave a key out of. `model_validate` honours the PYTHON defaults
    instead — and every one of these fields has one — so on this path an
    omission was not rejected, it became a VALUE, invented here and attributed
    to the model.

    Measured across the shapes actually sent: `ModelDecision` accepted three
    absent properties, `OrderProposal` three, `SymbolAssessment` two,
    `PositionPlan` five and `DreamStep` eleven.
    """
    from bot.model_client import ModelCallFailed

    client = _do_client(_reply(_tool_block({"market_assessment": "quiet"})), monkeypatch)

    with pytest.raises(ModelCallFailed) as raised:
        client.propose("context")

    said = str(raised.value)
    assert "proposals" in said and "assessments" in said and "position_plans" in said


def test_an_omission_and_an_empty_list_are_different_answers(monkeypatch):
    """The check refuses an ABSENT key and never an empty one.

    `assessments: []` is a real answer — a quiet cycle is most cycles — and
    weighing it against what the model was actually shown is `cmd_loop`'s job,
    with the denominator only it holds. This must not become a second opinion
    about that. What it refuses is the model not answering that part of the
    contract at all, which is a different fault with a different remedy.
    """
    client = _do_client(_reply(_tool_block(_QUIET_DECISION)), monkeypatch)

    decision, _ = client.propose("context")

    assert decision.assessments == [] and decision.position_plans == []


def test_a_nested_omission_is_caught_where_the_default_would_be_invented(monkeypatch):
    """The one with a figure on it, and the reason the check walks the whole tree.

    `PositionPlan.action` defaults to `hold` and `thesis_intact` to `True`, so a
    plan of `{"symbol": ..., "reasoning": ...}` used to be recorded and rendered
    as an opinion about an open position that nothing on the far end ever
    expressed. That is a plausible wrong figure, arriving through the transport
    rather than through the model.
    """
    from bot.model_client import ModelCallFailed

    payload: dict[str, Any] = dict(_QUIET_DECISION)
    payload["position_plans"] = [{"symbol": "SPY", "reasoning": "Still working."}]
    client = _do_client(_reply(_tool_block(payload)), monkeypatch)

    with pytest.raises(ModelCallFailed) as raised:
        client.propose("context")

    said = str(raised.value)
    assert "position_plans[0].action" in said
    assert "position_plans[0].thesis_intact" in said


def test_a_complete_nested_payload_still_passes(monkeypatch):
    """Or the check above passes by refusing everything.

    A proposal stating every property — including the two nulls that say "no
    target, no trail" — is what the contract asks for and has to survive it.
    """
    payload: dict[str, Any] = dict(_QUIET_DECISION)
    payload["proposals"] = [_proposal()]
    client = _do_client(_reply(_tool_block(payload)), monkeypatch)

    decision, _ = client.propose("context")

    assert decision.proposals[0].qty == 10
    assert decision.proposals[0].take_profit_price is None


def test_a_null_inside_an_optional_object_is_not_read_as_a_missing_object(monkeypatch):
    """`anyOf: [ref, null]` is how every optional model here is written.

    A null is the answer the schema asks for when there is nothing to say, so
    descending into the non-null branch to demand its required properties would
    reject the correct payload. Pinned because it is the one recursion step that
    fails in the direction of refusing good work.
    """
    payload: dict[str, Any] = dict(_QUIET_DECISION)
    payload["assessments"] = [
        {"symbol": "QQQ", "stance": "pass", "reasoning": "Nothing there today.",
         "waiting_for": "", "trigger": None}
    ]
    client = _do_client(_reply(_tool_block(payload)), monkeypatch)

    decision, _ = client.propose("context")

    assert decision.assessments[0].trigger is None


# --------------------------------------- the endpoint the SDK would have chosen


def test_the_anthropic_branch_names_its_endpoint_instead_of_letting_the_sdk_pick(
    monkeypatch,
):
    """**The SDK has an endpoint switch of its own and this module did not know.**

    `anthropic.Anthropic` resolves its base URL as *kwarg > `ANTHROPIC_BASE_URL`
    > a credentials profile on disk*, so a client built without one talks to
    whatever the environment says. Measured against a stub behaving like
    DigitalOcean — 200, `output_config` accepted and ignored, prose back — the
    ANTHROPIC key went to that endpoint, `_forces_tool_call` stayed False, the
    schema was enforced nowhere, and `inference_provider` reported "Anthropic
    direct" throughout.

    `mudhorn-dream.service` and `mudhorn-confer.service` both carry
    `EnvironmentFile=/opt/mudhorn/.env`, so this is a line in a file rather than
    a hypothetical.
    """
    from bot.config import ANTHROPIC_DEFAULT_BASE_URL, Env, ModelSpec
    from bot.model_client import ModelClient

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://somewhere.else.example")
    env = Env(_env_file=None)  # type: ignore[call-arg]
    env.anthropic_api_key = "sk-ant-test"
    env.do_inference_key = ""
    env.anthropic_base_url = ""  # nobody configured one HERE

    client = ModelClient(env, "system", spec=ModelSpec(model_id="x"), cache_system=False)

    assert str(client._client.base_url).rstrip("/") == ANTHROPIC_DEFAULT_BASE_URL


def test_a_configured_proxy_is_honoured_and_is_the_url_that_was_reported(monkeypatch):
    """Pointing the SDK somewhere deliberately is supported; it just has to be
    the same endpoint every surface names.

    The banner, the Settings page and the wire read one value now, so a swap
    cannot be invisible afterwards — which is the whole argument for announcing
    the provider at startup.
    """
    from bot.config import Env, ModelSpec
    from bot.model_client import ModelClient

    env = Env(_env_file=None)  # type: ignore[call-arg]
    env.anthropic_api_key = "sk-ant-test"
    env.do_inference_key = ""
    env.anthropic_base_url = "https://gateway.example/anthropic"

    provider = env.inference_provider
    client = ModelClient(env, "system", spec=ModelSpec(model_id="x"), cache_system=False)

    assert provider.usable and provider.base_url == "https://gateway.example/anthropic"
    assert str(client._client.base_url).rstrip("/") == "https://gateway.example/anthropic"
    assert client._forces_tool_call is False


def test_the_sdk_switch_pointed_at_digitalocean_without_the_key_refuses():
    """The one arrangement that is wrong in every part at once.

    `output_config` goes to an endpoint measured to accept it with a 200 and
    ignore it, on the transport that assumes it was enforced, carrying the
    Anthropic credential. Refused at construction for the same reason a Claude
    tier default is: nothing about it produces a useful error later, and the
    good outcome must never be what a broken configuration looks like.
    """
    from bot.config import Env
    from bot.model_client import ModelCallFailed, ModelClient

    env = Env(_env_file=None)  # type: ignore[call-arg]
    env.anthropic_api_key = "sk-ant-test"
    env.do_inference_key = ""
    env.anthropic_base_url = "https://inference.do-ai.run/"

    with pytest.raises(ModelCallFailed) as raised:
        ModelClient(env, "system")

    assert "DO_INFERENCE_KEY" in str(raised.value)


# ------------------------------------------------- what a refusal may quote back


def test_a_corrupt_key_refusal_cannot_be_made_arbitrarily_long(monkeypatch):
    """Every quote-back here is bounded, and this one was not.

    Measured: one 200,000-character argument key produced a
    200,116-character exception, which becomes a log line, an `audit/*.jsonl`
    row and an `insight.py` index entry — once per cycle, against an audit log
    measured at ~500 KiB a day and deliberately never pruned.
    """
    from bot.model_client import QUOTED_KEYS_MAX, QUOTED_TEXT_MAX_CHARS, CorruptToolCall

    client = _do_client(_reply(_tool_block({"<" + "z" * 200_000: 1})), monkeypatch)

    with pytest.raises(CorruptToolCall) as raised:
        client.propose("context")

    assert len(str(raised.value)) < 300 + QUOTED_KEYS_MAX * (QUOTED_TEXT_MAX_CHARS + 8)


def test_an_argument_key_that_is_not_a_string_is_corrupt_rather_than_a_crash(
    monkeypatch,
):
    """`"<" in key` raises `TypeError` on an int.

    That escaped `ModelCallFailed` entirely and reached the caller as a crash
    out of a module whose contract is that it raises one of three named errors.
    JSON cannot produce a non-string key, so the shape only ever arrives from a
    proxy or an SDK building its own objects — which is exactly the case this
    check exists for, and exactly the case where a `TypeError` says least.
    """
    from bot.model_client import CorruptToolCall

    client = _do_client(_reply(_tool_block({1: "a", "market_assessment": "x"})), monkeypatch)

    with pytest.raises(CorruptToolCall):
        client.propose("context")


# ----------------------------------------------------------- cost accounting


def test_a_usage_block_the_far_end_did_not_fill_in_does_not_cost_the_answer(monkeypatch):
    """The SDK does not validate this block, and that was measured.

    Responses are built with `construct_type`, which is unchecked construction:
    a `usage` of `{"input_tokens": 10}` alone yields `output_tokens=None` and
    `{"input_tokens": "10"}` yields the string. Reading them directly raised
    `AttributeError` on the first — outside this module's own three named
    errors — and threw away a decision that had already validated, over an
    accounting field.
    """
    reply = SimpleNamespace(
        content=[_tool_block(_QUIET_DECISION)],
        usage=SimpleNamespace(input_tokens="1200"),  # no output_tokens at all
    )
    client = _do_client(reply, monkeypatch)

    decision, usage = client.propose("context")

    assert decision.market_assessment == "quiet"
    assert usage.input_tokens == 1200
    assert usage.output_tokens == 0


def test_a_count_nobody_could_read_makes_the_COST_unknown_rather_than_small():
    """`None` already means "nobody has priced this model". "Nobody could read
    what it spent" is the same answer to the same question, and a cost computed
    from a count that could not be read would be a figure nobody measured
    printed beside figures that were.
    """
    from bot.config import CLAUDE_MODEL_SPECS, ClaudeTier, Env
    from bot.model_client import ModelClient

    env = Env(_env_file=None)  # type: ignore[call-arg]
    env.anthropic_api_key = "k"
    env.do_inference_key = ""
    client = ModelClient(
        env, "system", spec=CLAUDE_MODEL_SPECS[ClaudeTier.SONNET], cache_system=False
    )

    def response(**usage: Any) -> Any:
        return SimpleNamespace(usage=SimpleNamespace(**usage))

    priced = client._usage_from(response(input_tokens=1_000_000, output_tokens=0))
    unreadable = client._usage_from(response(input_tokens=[1], output_tokens=0))

    assert priced.estimated_cost_usd == 2.0
    assert unreadable.estimated_cost_usd is None


# ------------------------------------------- which model actually answered
#
# `ModelClient.model_id` records what was REQUESTED. DigitalOcean names the
# model it actually served in the response body, and nothing read it — so a
# request routed to something else (a deprecated id silently aliased, a
# catalogue entry repointed, a proxy substituting a cheaper model) left the
# audit log, the cost arithmetic and every surface naming a model nobody ran.
#
# These pin the read-back and, just as importantly, pin that it REPORTS. Whether
# a mismatch should fail the cycle is a behaviour change on the path that
# produces order quantities, and it is the operator's to make; `docs/DROPLET_AI.md`
# carries it as an open precondition.


def _served(model: Any) -> Any:
    """A reply that names the model that answered — or does not."""
    reply = _reply(_tool_block(_QUIET_DECISION))
    if model is not _ABSENT:
        reply.model = model
    return reply


_ABSENT = object()


def test_the_model_that_actually_answered_is_read_off_the_response(monkeypatch):
    """The whole of the read-back: the requested id and the served id, both kept.

    Two raw strings and one DERIVED comparison. A stored mismatch flag would be
    a third fact about the same thing that can disagree with the other two,
    which is the reasoning that keeps `Adoption.is_live` computed.
    """
    client = _do_client(_served("deepseek-v4-pro"), monkeypatch)

    _, usage = client.propose("context")

    assert usage.requested_model_id == "deepseek-v4-pro"
    assert usage.served_model_id == "deepseek-v4-pro"
    assert usage.served_as_requested is True


def test_a_substituted_model_is_reported_and_the_cycle_still_completes(monkeypatch):
    """**The finding this exists for, and the behaviour it deliberately does NOT
    change.**

    A request for one model answered by another is recorded as a mismatch — and
    the decision still comes back, the tokens are still counted, and nothing
    raises. Making it fail the cycle is a change to the order path and belongs
    to the operator, not to a reporting field.
    """
    client = _do_client(_served("llama3.3-70b-instruct"), monkeypatch)

    decision, usage = client.propose("context")

    assert decision.market_assessment == "quiet"
    assert usage.served_model_id == "llama3.3-70b-instruct"
    assert usage.served_as_requested is False
    assert usage.input_tokens == 1_000


def test_a_reply_that_names_no_model_reads_as_unknown_rather_than_a_match(monkeypatch):
    """`None` is "could not ask", never "yes".

    Same three-valued shape as `FinnhubCalendar.is_degraded` and `BrokerClock`:
    an absence that reads as agreement is how a check quietly stops checking.
    A `False` here would be equally wrong in the other direction — it would
    report a substitution nobody observed.
    """
    client = _do_client(_served(_ABSENT), monkeypatch)

    _, usage = client.propose("context")

    assert usage.served_model_id == ""
    assert usage.served_as_requested is None


def test_an_unreadable_model_field_is_absent_rather_than_coerced(monkeypatch):
    """Responses are built with `construct_type`, so `model` can be any type.

    `str()` on it would turn `None` into the string "None" — a model id that
    matches nothing and reads as a substitution, inventing a finding out of a
    transport fault. Same rule as the token counts one function above.
    """
    for value in (None, 12345, ["deepseek-v4-pro"]):
        client = _do_client(_served(value), monkeypatch)

        _, usage = client.propose("context")

        assert usage.served_model_id == ""
        assert usage.served_as_requested is None


def test_a_dated_snapshot_of_the_requested_alias_is_not_a_substitution(monkeypatch):
    """Anthropic resolves an alias to a dated snapshot on every ordinary call.

    Exact equality would report a mismatch every time, and a warning that fires
    on every call is a warning nobody reads — which would leave the real
    substitution exactly as invisible as it is now.

    The exemption is narrow on purpose: hyphen plus eight digits, nothing else.
    A plausible-looking family suffix is a different model and must read as one.
    """
    from bot.model_client import CallUsage

    def verdict(requested: str, served: str) -> bool | None:
        return CallUsage(
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            estimated_cost_usd=None,
            requested_model_id=requested,
            served_model_id=served,
        ).served_as_requested

    assert verdict("claude-sonnet-5", "claude-sonnet-5-20260401") is True
    # A size, a quantisation or a revision is a DIFFERENT model wearing a
    # familiar prefix. This is the case the narrow rule exists to keep.
    assert verdict("nemotron-3-ultra", "nemotron-3-ultra-550b") is False
    assert verdict("qwen3-coder", "qwen3-coder-flash") is False
    assert verdict("claude-sonnet-5", "claude-sonnet-5-2026040") is False


# ------------------------------------------------------ output_config on this path


def test_the_forced_tool_call_never_carries_output_config(monkeypatch):
    """The existing assertion was passing by finding nothing to check.

    It ran against a spec with both Anthropic-only flags False, so
    `output_config` could not have been set whatever the transport did. The
    three specs that DO set it are Claude ids and are refused at construction,
    so the property rested entirely on that coincidence rather than on the
    branch. This spec sets it, and the request must still not carry it: at an
    endpoint measured to accept `output_config` with a 200 and ignore it, a
    request holding both reads to anybody debugging as though the schema were
    being enforced server-side.
    """
    from bot.config import ModelSpec

    spec = ModelSpec(model_id="deepseek-v4-pro", sends_anthropic_effort=True)
    client = _do_client(_reply(_tool_block(_QUIET_DECISION)), monkeypatch, model_id="x")
    client._spec = spec

    client.propose("context")

    assert "output_config" not in client._client.captured
    assert client._client.captured["tool_choice"]["name"] == STRUCTURED_TOOL_NAME
