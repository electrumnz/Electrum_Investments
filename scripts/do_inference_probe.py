#!/usr/bin/env python
"""Does DigitalOcean's Anthropic-compatible endpoint enforce a schema?

The one measurement `TODO.md` item 20 turns on, and it exists because reading
the documentation cannot answer it.

## Why this cannot be settled by reading

`claude_client.py` calls `messages.parse(output_format=...)`, which the Anthropic
SDK sends as `output_config.format`. **DigitalOcean's published `/v1/messages`
request schema does not list `output_config`.** That looks conclusive and is
not: the same published list accepts `temperature`, `top_k` and `top_p`, all
three of which Opus 5 and Sonnet 5 reject with a 400. So the field list
describes DigitalOcean's proxy rather than the model behind it, and it is not
evidence either way about a field it omits.

## The three outcomes, and only one of them is dangerous

1. **HONOURED** — the schema is enforced server-side, exactly as at Anthropic.
   The swap becomes an `ANTHROPIC_BASE_URL` line plus a key, no code change.
2. **REJECTED** — a 400. Loud, easy to detect, easy to fall back from.
3. **IGNORED** — accepted, and the schema quietly stops being enforced. The call
   looks like it worked. This is the outcome worth making the trip for, and the
   reason the probe asks for something the model would not volunteer.

**How outcome 3 is detected.** A schema-shaped answer is not proof of
enforcement: a capable model asked politely will often return the right shape
anyway, so "it parsed" would report ENFORCED for an endpoint that dropped the
field entirely. The probe therefore uses a schema with a **constraint the prose
actively pushes against** — a field the instructions invite the model to fill
with a sentence, declared as an integer with a hard range. Server-side
enforcement makes that structurally impossible; an unenforced call is free to
comply with the prose.

That is a one-way test and the script says so rather than overclaiming: a
violation proves the schema was NOT enforced, while compliance is consistent
with enforcement and merely fails to disprove it. Repeated over several
attempts, since one compliant sample is weak evidence.

## What it never does

No order path, no journal, no `data/`, no `audit/`. It imports **nothing from
`bot` at all** — not the models, not the client — so it cannot reach a broker
even by accident, and it runs on a stdlib Python with no virtualenv. The schema
below is written out by hand for that reason rather than derived from
`ClaudeDecision`: the question is whether the endpoint enforces *a* schema, and
a hand-written one with a deliberate trap in it answers that better than the
real one would.

## Running it

    export DO_MODEL_ACCESS_KEY=...        # console-created; NOT a dop_v1_ PAT
    .venv/bin/python scripts/do_inference_probe.py --model <do-model-id>

`--list` prints the catalogue instead, which is the first thing to check: if no
Claude model is offered on this account's tier, everything below is moot.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "https://inference.do-ai.run"

# The trap field. `verbosity` is declared as a bounded integer and the prompt
# below asks for it in words, so the two are in direct conflict. A server that
# enforces the schema cannot return "high"; one that ignored `output_config` is
# free to.
PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string"},
        "verbosity": {"type": "integer", "minimum": 1, "maximum": 5},
    },
    "required": ["symbol", "verbosity"],
    "additionalProperties": False,
}

# Deliberately pushes against the schema. An unconstrained model tends to obey
# the sentence; a constrained one cannot.
PROBE_PROMPT = (
    "Reply about the ticker SPY. For the 'verbosity' field, describe how "
    "detailed your answer is using a word such as 'high', 'medium' or 'low' — "
    "write it as a word, not as a number."
)


def _post(url: str, key: str, payload: dict[str, Any], timeout: float) -> tuple[int, str]:
    """One request. Returns the status and the raw body, and never raises.

    A transport failure is reported as status 0 rather than as an exception,
    because "could not ask" is a distinct answer from any of the three outcomes
    and must never be presented as one of them.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", "replace")
    except Exception as exc:
        # Broad on purpose: a transport failure is a RESULT here, not a crash.
        return 0, f"{type(exc).__name__}: {exc}"


def _get(url: str, key: str, timeout: float) -> tuple[int, str]:
    request = urllib.request.Request(
        url, headers={"x-api-key": key, "anthropic-version": "2023-06-01"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def _extract_json(body: str) -> dict[str, Any] | None:
    """Pull the model's object out of an Anthropic-shaped response.

    Tolerant on purpose: the whole question is whether this endpoint returns
    Anthropic's shape, so a parser that assumed it would beg the question.
    """
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    for block in parsed.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
            return dict(block["input"])
        text = block.get("text")
        if isinstance(text, str):
            try:
                candidate = json.loads(text)
            except ValueError:
                continue
            if isinstance(candidate, dict):
                return candidate
    return None


def probe_structured(base: str, key: str, model: str, attempts: int, timeout: float) -> None:
    print("=" * 72)
    print("1. output_config — is the schema enforced server-side?")
    print("=" * 72)

    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": 256,
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
        "output_config": {"format": {"type": "json_schema", "schema": PROBE_SCHEMA}},
    }

    violations = 0
    complied = 0
    unparsed = 0

    for attempt in range(1, attempts + 1):
        status, body = _post(f"{base}/v1/messages", key, payload, timeout)

        if status == 0:
            print(f"  attempt {attempt}: COULD NOT ASK — {body[:160]}")
            print("\n  VERDICT: unknown. The endpoint was not reached, which is")
            print("  not one of the three outcomes and must not be read as one.")
            return

        if status >= 400:
            print(f"  attempt {attempt}: HTTP {status}")
            print(f"    {body[:400]}")
            print("\n  VERDICT: REJECTED. `output_config` is refused outright.")
            print("  That is the loud, safe failure — detectable, and the")
            print("  tool-calling fallback in TODO item 20 is the route.")
            return

        obj = _extract_json(body)
        if obj is None:
            unparsed += 1
            print(f"  attempt {attempt}: HTTP {status}, no JSON object in the reply")
            continue

        verbosity = obj.get("verbosity")
        if isinstance(verbosity, bool) or not isinstance(verbosity, int):
            violations += 1
            print(f"  attempt {attempt}: VIOLATION — verbosity={verbosity!r} (not an integer)")
        elif not 1 <= verbosity <= 5:
            violations += 1
            print(f"  attempt {attempt}: VIOLATION — verbosity={verbosity} out of range")
        else:
            complied += 1
            print(f"  attempt {attempt}: complied — verbosity={verbosity}")

    print()
    print(f"  {complied} complied, {violations} violated, {unparsed} unparsed")
    print()
    if violations:
        print("  VERDICT: IGNORED — the dangerous outcome.")
        print("  The call succeeded and the schema was NOT enforced: the model")
        print("  returned a value the declared type forbids. Do NOT point")
        print("  claude_client.py here without the tool-calling substitute.")
    elif complied:
        print("  VERDICT: consistent with ENFORCED, and this is one-way evidence.")
        print("  Every attempt obeyed a constraint the prompt argued against,")
        print("  which is what enforcement looks like — but a capable model may")
        print("  simply have complied. Treat as encouraging, not as proof.")
        print("  Raise --attempts before relying on it.")
    else:
        print("  VERDICT: unknown. Nothing parsed, so neither claim is supported.")


def probe_tools(base: str, key: str, model: str, timeout: float) -> None:
    """The documented fallback, measured rather than assumed available."""
    print()
    print("=" * 72)
    print("2. Forced tool call — is the fallback actually there?")
    print("=" * 72)

    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": 256,
        "messages": [{"role": "user", "content": "Reply about the ticker SPY."}],
        "tools": [
            {
                "name": "record",
                "description": "Record the answer.",
                "input_schema": PROBE_SCHEMA,
            }
        ],
        "tool_choice": {"type": "tool", "name": "record"},
    }

    status, body = _post(f"{base}/v1/messages", key, payload, timeout)
    if status == 0:
        print(f"  COULD NOT ASK — {body[:160]}")
        return
    if status >= 400:
        print(f"  HTTP {status}: {body[:400]}")
        print("\n  The documented fallback is NOT available either. That would")
        print("  leave no route to structured output on this endpoint at all.")
        return

    obj = _extract_json(body)
    print(f"  HTTP {status}, tool input: {obj!r}")
    print("\n  The fallback is reachable." if obj else "\n  No tool_use block came back.")


def list_models(base: str, key: str, timeout: float) -> None:
    status, body = _get(f"{base}/v1/models", key, timeout)
    if status != 200:
        print(f"HTTP {status}: {body[:400]}")
        return
    try:
        data = json.loads(body).get("data", [])
    except ValueError:
        print(body[:2000])
        return
    ids = sorted(str(m.get("id", "")) for m in data if isinstance(m, dict))
    print(f"{len(ids)} model(s) offered to this key:\n")
    for model_id in ids:
        print(f"  {model_id}")
    claude = [m for m in ids if "claude" in m.lower() or "anthropic" in m.lower()]
    print()
    # The tier question, answered from the catalogue rather than from the
    # pricing page. No Claude model here makes every other question moot.
    print(
        f"  {len(claude)} look like Anthropic models."
        if claude
        else "  NONE look like Anthropic models — check the account's tier before"
        "\n  going further; consolidation is not possible without them."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("DO_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--model", default="anthropic-claude-sonnet-4.5")
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--list", action="store_true", help="print the model catalogue and stop")
    args = parser.parse_args()

    key = os.environ.get("DO_MODEL_ACCESS_KEY", "").strip()
    if not key:
        print("Set DO_MODEL_ACCESS_KEY first.")
        print()
        print("  Console -> Gradient AI Platform -> Serverless Inference")
        print("           -> Create model access key")
        print()
        print("  It cannot be created through the API; that route was retired.")
        print("  Do NOT use a dop_v1_ personal access token: a PAT controls the")
        print("  whole account, including droplets, DNS and billing.")
        return 2

    base = args.base_url.rstrip("/")
    print(f"base: {base}")
    print(f"model: {args.model}\n")

    if args.list:
        list_models(base, key, args.timeout)
        return 0

    probe_structured(base, key, args.model, args.attempts, args.timeout)
    probe_tools(base, key, args.model, args.timeout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
