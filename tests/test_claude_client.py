"""The transport, and the one property of it that no other test could see.

`electrum-bot dream` could not make its model call at all. `ClaudeClient.dream`
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

from bot.claude_client import (
    DREAM_MAX_RETRIES,
    DREAM_MAX_TOKENS,
    DREAM_TIMEOUT_SECONDS,
    ClaudeDecision,
)
from bot.confer import DreamerTurn, TraderTurn
from bot.dreamer import DreamHop, DreamStep, StepCondition

SRC = Path(__file__).resolve().parent.parent / "src" / "bot"

# Every Pydantic model this repository hands to `messages.parse` as an
# `output_format`. Not a hand-maintained list that can quietly go stale — the
# test below enumerates the call sites out of the source and fails if one names
# a schema that is not here. Same arrangement as `tests/test_auth.py`
# enumerating routes from the application rather than from a list somebody
# remembered to update.
STRUCTURED_OUTPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    "ClaudeDecision": ClaudeDecision,  # the decision loop, every 15 minutes
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
# `PositionPlan` is the current worst at five, which makes `ClaudeDecision` the
# slowest schema this repository sends: 14.7 seconds, against 3.1 for the fixed
# `DreamStep`. Measure the next optional field added there rather than assuming
# it fits.
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
    SDK — a `"output_format"` key in the kwargs dict `ClaudeClient` builds, and
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
    assert dynamic == ["bot.claude_client"], (
        "only ClaudeClient.confer may take its schema from its caller; a new "
        f"pass-through appeared in {sorted(set(dynamic))}. Add its schemas to "
        "STRUCTURED_OUTPUT_SCHEMAS and check them here."
    )


def test_confers_callers_pass_a_schema_this_file_checks():
    """The one pass-through resolves in `confer.py`, so read it there too.

    `ClaudeClient.confer` takes its schema as an argument, deliberately: an
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
            "them required with claude_client.EVERY_FIELD_REQUIRED, or split "
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
        "afford. Add claude_client.EVERY_FIELD_REQUIRED to the model."
    )


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
