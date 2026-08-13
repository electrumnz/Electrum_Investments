#!/usr/bin/env python
"""Can a DigitalOcean model hold the REAL `ModelDecision` schema, repeatedly?

`scripts/do_inference_probe.py` answered the prior question — does the endpoint
enforce `output_config` (no, it accepts it and silently ignores it) and does a
forced tool call work (yes, on 16 of 27 text models). It answered that with a
deliberately hand-written two-field schema carrying one trap, which was right
for the question it asked and is **not evidence for this one**.

`docs/DROPLET_AI.md` says so in as many words and names this script's job:

> **Not yet measured:** whether any of them holds a `ModelDecision`-shaped
> schema — nested array, enum, several numeric order fields — reliably across
> repeated calls. A toy two-field schema is not evidence for the real payload
> [...] Do that before pinning anything to `dream`, let alone `propose`.

`ModelDecision` is the schema that produces order quantities and stop prices.
Choosing a model for it off a two-field toy test would be the confident wrong
figure this repository exists to refuse, arriving through the procurement
decision rather than through a model.

## What is graded, and why each grade is separate

Five outcomes, kept apart for the usual reason: collapsing them would let the
dangerous one hide inside a milder one.

- **`no_tool`** — the reply came back as TEXT with no `tool_use` block. This is
  the one that matters most and it is why the whole exercise is not just
  "did it parse". `docs/MODEL_CALLS.md` finding 3: a dropped schema breaks
  loudly in every case that produces a number and **silently in exactly one**,
  the quiet cycle, because `{"market_assessment": "..."}` alone parses as a
  completed cycle that considered nothing. A model that sometimes answers in
  prose cannot serve `propose` at any price.
- **`corrupt`** — a `tool_use` block whose keys are the model's own tool-call
  markup, `{'<tool_call>record  <arg_key>symbol': 'SPY'}`. `glm-5.2` does this
  and a naive check that looked for one valid field would pass it.
- **`invalid`** — a real tool call whose arguments Pydantic rejects. Loud and
  safe, but a model that does it often is a model that costs cycles.
- **`empty_arrays`** — valid, and it filled `assessments` or `position_plans`
  with `[]` when the prompt gave it symbols and an open position. Structurally
  legal and useless: those fields exist precisely so a quiet cycle can be told
  apart from a loop that never looked, and a model that always empties them has
  reintroduced the gap while passing validation.
- **`ok`** — a valid decision that also said something about what it was shown.

**A pass rate is the output, never a single sample.** One good answer from a
model that fails a third of the time is worse than a model that fails always,
because the first one ships.

## What it never does

No order path, no broker, no journal, no `data/`, no `audit/`. It imports the
schemas and nothing else from `bot`, builds no `OrderProposal` and calls no
gate — a proposal that comes back is counted and discarded. It is a measurement
harness in the shape of `scripts/agent_behaviour_live.py`, and like that one it
is opt-in and costs the operator's prepaid balance rather than running in CI.

    DO_INFERENCE_KEY=<model access key> \
      .venv/bin/python scripts/do_schema_fidelity.py --samples 3

Add `--models a,b,c` to narrow it, `--dream` to grade `DreamStep` instead.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

# The real schemas. Importing them is the entire point of this script existing
# separately from `do_inference_probe.py`, which deliberately imports nothing
# from `bot` so it can run on a stdlib Python with no virtualenv.
#
# No `sys.path` insert: the project is installed editable, so this runs under
# `.venv/bin/python` like the rest of the harnesses and a path hack would only
# mask a broken install.
from bot.dreamer import DreamStep
from bot.model_client import ModelDecision, _missing_required

DEFAULT_BASE_URL = "https://inference.do-ai.run"

# The models that came back clean on a forced tool call with the toy schema, per
# `docs/DROPLET_AI.md`. This is the CANDIDATE list, not a pass list — clearing a
# two-field schema is what earns a model a place in this run, and nothing more.
CANDIDATES = [
    "deepseek-v4-pro",
    "deepseek-3.2",
    "deepseek-4-flash",
    "deepseek-v4-flash-0731",
    "qwen3.8-max",
    "kimi-k2.5",
    "nemotron-3-ultra-550b",
    "llama-4-maverick",
    "llama3.3-70b-instruct",
    "alibaba-qwen3-32b",
    "mistral-3-14B",
    "openai-gpt-oss-20b",
    "gemma-4-31B-it",
    "qwen3-coder-flash",
    "nemotron-3-nano-omni",
]

TOOL_NAME = "record_decision"

# A compact but REAL-SHAPED cycle. It has to invite every part of the schema:
# several symbols so `assessments` has something to hold, an open position so
# `position_plans` does, and one setup obvious enough that a proposal with
# actual numbers is a reasonable answer — without instructing the model to
# propose, because "always proposes" is not the behaviour being measured.
PROMPT = """\
You are the trading agent for a paper account. Equity $100,000, cash $100,000.

Tradeable symbols and today's readings:

- SPY   close 641.20, sma20 648.90, sma50 655.10, atr14 6.41, vol_ratio 1.08
- QQQ   close 566.30, sma20 561.40, sma50 553.80, atr14 7.02, vol_ratio 0.94
- AAPL  close 241.85, sma20 238.10, sma50 232.40, atr14 4.15, vol_ratio 1.31
- NVDA  close 182.40, sma20 190.60, sma50 188.25, atr14 6.88, vol_ratio 1.77

Open positions:

- SPY  short 21 @ 773.32, stop 820.00, unrealised +$2,140

Limits in force: max 1% of equity at risk per trade ($1,000), max 2% across all
open positions, a hard stop is required on every proposal.

Record your decision for this cycle. Doing nothing is a valid and frequently
correct answer.
"""

DREAM_PROMPT = """\
Produce one lateral, second-order hypothesis about a supply chain, and record it.

Recent headlines you were shown:

- Cicada brood XIV emerges across the US Midwest, earliest on record
- Sesame prices firm in Gujarat on weak monsoon carryover
- Freight rates out of Santos ease for a third week

Record one step: either a new chain, or a condition on one.
"""


def _schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """The JSON schema, as the tool's `input_schema`.

    `model_json_schema()` emits `$defs` and `$ref` for nested models, which is
    what `OrderProposal`, `SymbolAssessment` and `PositionPlan` are. Sent as-is
    rather than flattened: whether a given endpoint resolves internal refs is
    part of what is being measured, and flattening here would hide a model that
    cannot.
    """
    return model.model_json_schema()


@dataclass
class Sample:
    grade: str
    seconds: float
    detail: str = ""
    proposals: int = 0
    assessments: int = 0
    position_plans: int = 0


@dataclass
class Result:
    model: str
    samples: list[Sample] = field(default_factory=list)

    @property
    def ok(self) -> int:
        return sum(1 for s in self.samples if s.grade == "ok")

    @property
    def rate(self) -> float:
        return self.ok / len(self.samples) if self.samples else 0.0

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.samples:
            out[s.grade] = out.get(s.grade, 0) + 1
        return out


def _post(base: str, key: str, payload: dict[str, Any], timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/messages",
        data=json.dumps(payload).encode(),
        headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # a harness must report, not crash
        return 0, f"{type(exc).__name__}: {exc}"


def _looks_corrupt(args: dict[str, Any]) -> bool:
    """The `glm-5.2` failure: tool-call markup leaking into the argument keys.

    Checked on the KEYS rather than on a missing field, because the giveaway is
    a key nobody named — a check that merely looked for one field it recognised
    would pass a payload that is nine-tenths markup.
    """
    return any(
        not isinstance(k, str) or "<" in k or ">" in k or k.strip() != k for k in args
    )


def _grade(body_text: str, status: int, schema_model: type[BaseModel], seconds: float) -> Sample:
    if status != 200:
        return Sample(f"http_{status}", seconds, body_text[:160])
    try:
        body = json.loads(body_text)
    except json.JSONDecodeError:
        return Sample("unparseable", seconds, body_text[:160])

    blocks = body.get("content") or []
    tool_blocks = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
    if not tool_blocks:
        # The dangerous one. Prose where a schema was forced.
        text = " ".join(
            b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
        )
        return Sample("no_tool", seconds, text[:160])

    args = tool_blocks[0].get("input")
    if not isinstance(args, dict):
        return Sample("corrupt", seconds, repr(args)[:160])
    if _looks_corrupt(args):
        return Sample("corrupt", seconds, repr(sorted(args)[:3])[:160])

    # **Graded under the rule the TRANSPORT actually enforces, which is not
    # `model_validate` alone.** `EVERY_FIELD_REQUIRED` puts every property into
    # the sent schema's `required` list, and `model_validate` honours the Python
    # defaults instead — so an OMITTED key was not a rejection here, it became a
    # value invented on this side and counted as a pass. Twenty-four properties
    # across the five shapes could go missing that way, and a model that omits
    # `action` on a `PositionPlan` was scoring 10/10 while the loop would have
    # recorded `hold` as though the model had said it.
    #
    # A procurement decision has to be made under the rule the code enforces,
    # or the table is measuring a laxer system than the one that will run.
    schema_json = schema_model.model_json_schema()
    gaps = _missing_required(args, schema_json, schema_json.get("$defs", {}))
    if gaps:
        return Sample("omitted", seconds, ", ".join(sorted(gaps))[:200])

    try:
        parsed = schema_model.model_validate(args)
    except ValidationError as exc:
        return Sample("invalid", seconds, str(exc).replace("\n", " ")[:200])

    proposals = len(getattr(parsed, "proposals", []) or [])
    assessments = len(getattr(parsed, "assessments", []) or [])
    plans = len(getattr(parsed, "position_plans", []) or [])

    if schema_model is ModelDecision and (assessments == 0 or plans == 0):
        # Valid and useless. The prompt named four symbols and one open
        # position; empty lists here are the gap `assessments` exists to close,
        # surviving validation.
        return Sample(
            "empty_arrays",
            seconds,
            f"assessments={assessments} position_plans={plans}",
            proposals,
            assessments,
            plans,
        )

    return Sample("ok", seconds, "", proposals, assessments, plans)


def run_one(
    base: str, key: str, model: str, schema_model: type[BaseModel], prompt: str, timeout: float
) -> Sample:
    payload = {
        "model": model,
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [
            {
                "name": TOOL_NAME,
                "description": "Record the decision. You MUST call this tool.",
                "input_schema": _schema_for(schema_model),
            }
        ],
        "tool_choice": {"type": "tool", "name": TOOL_NAME},
    }
    started = time.monotonic()
    status, text = _post(base, key, payload, timeout)
    return _grade(text, status, schema_model, time.monotonic() - started)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("DO_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--models", default="", help="comma-separated; default is the candidates")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--dream", action="store_true", help="grade DreamStep instead")
    args = parser.parse_args()

    key = os.environ.get("DO_INFERENCE_KEY", "").strip()
    if not key:
        print("DO_INFERENCE_KEY is not set. This costs the operator's balance,", file=sys.stderr)
        print("so it is opt-in and there is no default.", file=sys.stderr)
        return 2

    schema_model: type[BaseModel] = DreamStep if args.dream else ModelDecision
    prompt = DREAM_PROMPT if args.dream else PROMPT
    models = [m.strip() for m in args.models.split(",") if m.strip()] or CANDIDATES

    schema = _schema_for(schema_model)
    print(f"schema: {schema_model.__name__}, {len(json.dumps(schema))} bytes, "
          f"{len(schema.get('$defs', {}))} nested defs")
    print(f"{len(models)} models x {args.samples} samples\n")

    results = {m: Result(m) for m in models}
    jobs = [(m, i) for m in models for i in range(args.samples)]

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(run_one, args.base_url, key, m, schema_model, prompt, args.timeout): m
            for m, _ in jobs
        }
        for fut in concurrent.futures.as_completed(futures):
            model = futures[fut]
            try:
                results[model].samples.append(fut.result())
            except Exception as exc:
                results[model].samples.append(Sample("harness_error", 0.0, str(exc)[:160]))

    print(f"{'model':<26} {'ok':>6}  {'median s':>9}  grades")
    print("-" * 78)
    for model in models:
        res = results[model]
        times = [s.seconds for s in res.samples if s.seconds > 0]
        median = statistics.median(times) if times else 0.0
        counts = ", ".join(f"{g}={n}" for g, n in sorted(res.counts().items()))
        print(f"{model:<26} {res.ok}/{len(res.samples):<4} {median:>9.1f}  {counts}")

    print("\nfailures in detail")
    print("-" * 78)
    quiet = False
    for model in models:
        for s in results[model].samples:
            if s.grade != "ok":
                quiet = True
                print(f"  {model:<24} {s.grade:<14} {s.detail}")
    if not quiet:
        print("  none")

    print("\nA pass rate is the finding. One good sample from a model that fails")
    print("a third of the time is worse than one that fails always, because the")
    print("first one ships. `no_tool` is disqualifying for `propose` at any price.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
