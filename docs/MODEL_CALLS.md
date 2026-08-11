# Every model call in this repository, and which provider each one survives

Audited **11 August 2026**, against the working tree at `82cb7fb`; re-checked at
`bfc8e04`, which added `docs/DO_AGENTS.md` and touched no source file, so every
measurement below still holds.

This is the per-call-site companion to two sibling documents, and the three do
not overlap:

- **`docs/DROPLET_AI.md`** researched DigitalOcean's *serverless inference* API —
  the provider question.
- **`docs/DO_AGENTS.md`** assesses DigitalOcean's *Agent Platform* — hosted
  agents, which is the Hermes/souls half and a different product.
- **This one** enumerates the *callers* — every prompt and every model call in
  the repository — adds **Vercel AI Gateway** as a second inference candidate,
  and puts a verdict on each call site **per provider**.

Everything below is marked **MEASURED** (something was run and the output read)
or **DOCUMENTED** (read from a vendor page or a schema, not run). The split is
the point. `docs/DROPLET_AI.md` closes by saying "everything above was read, not
run"; most of what follows has now been run, and three of the things it inferred
turn out to be wrong in the direction that matters.

> ## The short version
>
> **The blocker that made every structured call unmovable is a DigitalOcean
> blocker, not a proxy blocker. Vercel AI Gateway does not have it.**
>
> 1. **`output_format=` is not a wire field.** MEASURED. The SDK folds it into
>    `output_config.format`, alongside `effort`, as **one object**. So the
>    schema and the effort setting stand or fall together, and the two
>    "mismatches" `docs/DROPLET_AI.md` lists are one mismatch with two
>    consequences.
>
> 2. **Vercel's request validator models that exact object, in depth, and
>    requires it.** MEASURED live: `output_config.format.type` must be
>    `"json_schema"`, `output_config.format.schema` is required, and
>    `output_config.effort` accepts Anthropic's exact five levels. **The repo's
>    real `ClaudeDecision` and `DreamStep` schemas both pass it.** DigitalOcean
>    authenticates before parsing the body, so the same question **cannot be
>    asked there at all** without a key.
>
> 3. **A dropped schema breaks loudly in every case that produces a number, and
>    silently in exactly one: the quiet cycle.** MEASURED. Prose, truncation, a
>    missing stop, a null stop, an unknown enum — all raise `ValidationError`
>    and become a failed cycle. But `{"market_assessment": "..."}` alone parses
>    as a completed cycle that considered nothing, which is the exact failure
>    `assessments` exists to prevent.
>
> 4. **The prompt cache the loop is designed around does not engage today, on
>    Anthropic, on the default tier.** MEASURED, and the biggest surprise here.
>    System block 3,676 tokens; Haiku 4.5's minimum cacheable prefix 4,096. Two
>    identical calls returned `cache_creation=0, cache_read=0`. So "a proxy might
>    silently break caching" costs **$0/month as shipped** — and ~$12/month on
>    Sonnet 5, ~$30/month on Opus 5, where caching demonstrably works.
>
> 5. **`ANTHROPIC_BASE_URL` already routes the Python path and nothing says so.**
>    MEASURED. `ClaudeClient` passes no `base_url`; the SDK reads the
>    environment; `mudhorn-dream.service` and `mudhorn-confer.service` both carry
>    `EnvironmentFile=/opt/mudhorn/.env`. The repo's switch is
>    `DO_INFERENCE_KEY`; the SDK's switch is `ANTHROPIC_BASE_URL`; only the first
>    is reported by the startup banner.
>
> 6. **The souls are not a Hermes-only concern.** MEASURED by call site. Grogu is
>    compiled into `claude.dream`'s and `claude.confer`'s system prompts, Yoda
>    into `claude.confer`'s. Phase 1 as shipped already puts Grogu on two
>    different models on the same page.
>
> **The honest conclusion the operator did not ask for:** DigitalOcean's real
> value in this system is Hermes, where there is no structured output at all —
> and that half is already built and correct. For the *structured* Python calls,
> **Vercel AI Gateway is the better target**, and it is the only one of the two
> where `messages.parse` works unchanged. `claude.propose` should still not move
> anywhere, but the reason narrows from two to one.

---

## How this was established

- **Call sites enumerated by AST**, not from memory, over `src/`, `scripts/` and
  `tests/`, matching `messages.parse`, `messages.create`, `messages.stream` and
  `anthropic.Anthropic(`. `tests/test_claude_client.py::output_format_sites`
  already does the narrower version and is the right thing to extend. Subprocess
  model paths were found separately by grepping `subprocess.run` and `exec`
  across `src/` and `deploy/`.
- **A local stub server** speaking the Anthropic wire shape, standing in for "a
  normalising layer that models a subset of the upstream schema and drops what it
  has never heard of". It records the exact request body and returns a reply of
  our choosing. The repository's own `ClaudeClient.propose` and
  `ClaudeClient.dream` were driven against it, so what is reported is the shipped
  path and not a reconstruction of it.
- **Live unauthenticated probes** against `https://inference.do-ai.run` and
  `https://ai-gateway.vercel.sh`.
- **Live probes against Anthropic**, using the opt-in key
  `ANTHROPIC_API_KEY_ELECTRUM` that `tests/test_claude_client.py` already uses:
  `count_tokens` (free) for the system-block size, and four `max_tokens=8` calls
  to settle the caching question. Total spend under two cents.

### The one difference that decided how much could be established

> **DigitalOcean authenticates before it parses the body. Vercel parses the body
> first.** MEASURED, and it is why this audit can say far more about one than the
> other.
>
> DigitalOcean returned the same `401 {"id": "Unauthorized", ...}` for a request
> with no `model` field, a request carrying a junk top-level field, a request
> missing `anthropic-version`, and `GET /v1/models`. **No unauthenticated probe
> of any kind can reveal how it handles an unknown field.**
>
> Vercel returned `400 {"type":"error","error":{"type":"invalid_request_error",
> "message":"model: Invalid input: expected string, received undefined"}}` for
> the same missing-`model` request, with a bad key. Its validator runs first, so
> its request schema can be mapped from outside.

No authenticated request was sent to either. Every provider claim below is
labelled accordingly, and "what to run first" at the end names the single call
that settles each open one.

---

## The call sites

Eight places reach a model. Five are production; three are audit harnesses under
`scripts/`, listed because they are what would *prove* a migration and would
themselves have to be repointed.

| # | Call site | Model / how chosen | Structured output | Caching | Params sent | Cadence |
|---|---|---|---|---|---|---|
| 1 | `claude.propose` — `main.py:212` (loop), `main.py:149` (smoketest) | `CLAUDE_MODEL_IDS[env.claude_tier]`, default **Haiku 4.5** | **Yes** — `output_format=ClaudeDecision` | **1h `cache_control`**, and it does not engage | `max_tokens=4096`; Sonnet/Opus add `thinking={"type":"adaptive"}` and `output_config={"effort":"medium"}`; **SDK default timeout (600 s) and `max_retries=2`** | ~96/day, ~2,900/month |
| 2 | `claude.dream` — `dreamer.py:1331` | `CLAUDE_MODEL_IDS[env.dream_tier]`, default **Sonnet 5** | **Yes** — `output_format=DreamStep` | `cache_system=False`, deliberately | `max_tokens=16000`; `thinking={"type":"adaptive"}`; `output_config={"effort":"high"}`; `timeout=240 s`, `max_retries=1` | 1/day |
| 3 | `claude.confer` — `confer.py:2036`, two clients (Grogu, Yoda) | `env.dream_tier` | **Yes** — schema passed in by the caller | `cache_system=False`, deliberately | `max_tokens=4096`; `thinking={"type":"adaptive"}`; `output_config={"effort":"medium"}`; `timeout=240 s`, `max_retries=1` | ≤12/day |
| 4 | `deploy/run-chat.sh` → `hermes -z` | `ANTHROPIC_MODEL` / `~/.hermes/config.yaml`. **Already DO-capable** | No | Hermes' business | Nothing this repo sets; 180 s subprocess timeout in `chat.py` | Per chat message |
| 5 | `deploy/run-dream.sh` → `hermes -z` | Same, from the dreamer instance's own `inference.env`. **Already DO-capable** | No | Hermes' business | Same | Per message on `/dreaming` |
| 6 | `scripts/agent_behaviour_live.py` | Hardcoded `claude-sonnet-5` (agent), `claude-opus-5` (judge) | No — plain `messages.create`, prose graded by a judge | No | `model`, `max_tokens`, `messages` only | Manual |
| 7 | `scripts/dream_cycle_live.py` | `CLAUDE_MODEL_IDS[env.dream_tier]`; judge `claude-opus-5` | Probes the shipped `parse`, then falls back to schema-in-the-message + `model_validate` | No | `thinking`, `output_config={"effort":"high"}`, `max_tokens=16000`, 900 s / 90 s probe | Manual |
| 8 | `scripts/confer_live.py` | `CLAUDE_MODEL_IDS[env.dream_tier]` | Via the real `confer` | No | As `confer` | Manual |

Two things that are **not** model paths, checked rather than assumed:
`src/bot/souls.py` only reads files off disk, and `settings_agent.py`'s
subprocess is a root-owned config-file mover, not an agent.

### What the SDK actually puts on the wire

MEASURED, against the stub, for the shipped `ClaudeClient`:

```
POST /v1/messages          anthropic-version: 2023-06-01     (NO beta header)

tier=haiku    keys = [max_tokens, messages, model, output_config, system]
              output_config = {format: {type: "json_schema", schema: {...}}}
              thinking      = absent

tier=sonnet   keys = [max_tokens, messages, model, output_config, system, thinking]
              output_config = {effort: "medium", format: {...}}
              thinking      = {"type": "adaptive"}

system[0]     = {type, text, cache_control: {type: "ephemeral", ttl: "1h"}}
```

Four details, none of them obvious from reading `claude_client.py`:

- **`output_format=` is not a wire field.** The SDK folds it into
  `output_config.format`. So `output_format` and `output_config` — which read in
  the source like two independent settings — are **one object on the wire**.
  `docs/DROPLET_AI.md` treats the missing `output_config` and the
  `reasoning_effort` spelling as two separate mismatches. They are one mismatch
  with two consequences, and the second one is that a dropped object also
  silently downgrades `dream` from `effort: high` and `confer` from `medium` to
  the provider's default, on the two calls bought for depth.
- **No `anthropic-beta` header is sent.** MEASURED. Whether a gateway passes
  beta headers through is therefore **moot for the shipped path** — it matters
  only to the older `extra_body={'output_format': ...}` +
  `anthropic-beta: structured-outputs-2025-11-13` mechanism, which this
  repository does not use and should not adopt.
- **The Haiku path sends `output_config` too**, carrying only `format`. The
  default tier is not exempt.
- **`ClaudeDecision`'s schema declares `required: ["market_assessment"]` and
  `additionalProperties: false`.** One required field. That single fact is what
  makes the quiet-cycle hole below possible.

---

## Finding 1 — a dropped schema, and exactly what breaks

The mechanism, read out of the installed SDK (`anthropic` 0.121.0):

```python
def parse_text(text, output_format):
    if is_given(output_format):
        return TypeAdapter(output_format).validate_json(text)   # no try/except
    return None
```

`messages.parse` runs that over every `text` block. No error handling, so a
response the schema cannot accept raises `pydantic_core.ValidationError` **out of
the SDK call**, not into a `None`. The `if decision is None: raise RuntimeError`
branch in `claude_client.py` is near-unreachable whenever a schema is given: it
fires only when the response carries no text block at all.

MEASURED, driving `ClaudeClient.propose` against a stub that ignores
`output_config` and returns text of our choosing:

| Response the model returns without a grammar constraining it | Result |
|---|---|
| Ordinary prose | **RAISES** `ValidationError` |
| Prose wrapping a fenced JSON object | **RAISES** `ValidationError` |
| Truncated JSON (hit `max_tokens` mid-object) | **RAISES** `ValidationError` |
| `stop_loss_price` missing | **RAISES** `ValidationError` |
| `stop_loss_price: null` | **RAISES** `ValidationError` |
| `direction: "short"` (not in the enum) | **RAISES** `ValidationError` |
| `qty: "1,021"` (thousands separator) | **RAISES** `ValidationError` |
| `qty/limit_price/stop_loss_price` as numeric **strings** | **PARSES** — coerced silently to floats |
| Extra hallucinated fields (`use_margin`, `order_type: "market"`, `leverage`) | **PARSES** — extras ignored |
| `{"market_assessment": "..."}` and nothing else | **PARSES** — 0 proposals, 0 assessments, 0 position_plans |

**The operator's rule holds where it was written to hold.** "Free-form prose
truncates and numbers reject" survives a dropped schema, because the rejection
was always Pydantic's and Pydantic still runs. Rule 3 in particular is intact: a
proposal with no stop, or a null stop, cannot be constructed. All of those raise
into `cmd_loop`'s `except Exception`, log `model_call_failed`, write an audit
event and skip the cycle without emitting `cycle_complete`. Loud, and already
designed for.

**Two things do not hold, and one of them is large.**

The small one: **numeric strings are coerced.** A server-side grammar would
refuse `"qty": "21"` where the schema says number; Pydantic's lax mode yields
`21.0`. The value is unchanged, so this is a widened input surface rather than a
wrong figure — but it is the guarantee getting softer, and it is Pydantic's
leniency, not the API's.

The large one:

> **A well-formed JSON object carrying only `market_assessment` parses as a
> completed quiet cycle.** Zero proposals, zero assessments, zero position plans,
> no exception, `cycle_complete` emitted normally, a decision written to the
> audit log that looks exactly like every other decision.

`CLAUDE.md` is explicit about why `assessments` costs output tokens on a quiet
cycle: *"'nothing met the conditions' reads identically to 'the loop never looked
at QQQ'"*. The server-side schema is what forces those fields to be filled.
Remove it and a model that answers the first required field and stops produces a
record saying the loop considered nothing, with nothing anywhere saying the
schema was not applied. It is worse for a held position than for a watch: with no
`position_plans`, an open position is silently un-reviewed, and the operator's
surface says the model had no plan for it when in fact it was never made to write
one.

**If a provider rejects the field instead of dropping it**, every `propose`,
`dream` and `confer` call returns `400`, the SDK raises `BadRequestError`, and
all three callers catch it and degrade. Loud, immediate and safe. **That is the
good outcome** — which is why drop-versus-reject is the question worth a key: the
two branches are "nothing works, obviously" and "everything works, and one cycle
shape lies".

---

## Finding 2 — the required-on-the-wire fix, per provider

MEASURED, from the wire body of `ClaudeClient.dream`:

```
output_config = {effort: "high", format: {type: "json_schema", schema: <9,833 bytes>}}
DreamStep       properties=15  required=15   (all)
  StepCondition properties=6   required=6    (all)
  DreamHop      properties=3   required=3    (all)
```

The fix is real and lives entirely inside `output_config.format.schema`.

| Provider branch | Does the fix help, hurt, or mean nothing? |
|---|---|
| **Anthropic direct** | Works exactly as measured on 10 August. The 12-optional timeout and the 15-required-nullable 10.5 s compile are properties of Anthropic's grammar compiler. |
| **Vercel → an `anthropic/*` model** | **Unchanged.** The gateway routes to Anthropic, so the same compiler runs on the same bytes. This is the branch where the fix keeps every bit of its value. |
| **DigitalOcean → an Anthropic model, if `output_config` is forwarded** | Unchanged, same reason — a passthrough to the same compiler. |
| **DigitalOcean → an Anthropic model, if `output_config` is dropped** | **Inert.** It changes bytes nobody reads. It does not hurt: `EVERY_FIELD_REQUIRED` is `json_schema_extra` and touches only the emitted schema, never Python validation, which `test_required_on_the_wire_does_not_make_anything_required_in_python` already pins. Nothing regresses; the fix stops buying anything. |
| **Either provider → a non-Anthropic model** | **Untested, and its premise does not transfer.** The cliff at ~10 optional properties is a property of *Anthropic's* compiler. Another engine has its own characteristics and the 10.5-second figure predicts nothing there, in either direction. |

**The client-side floor, measured.** With the schema dropped, `DreamStep` still
rejects an empty object and rejects prose, because `title`, `seed`, `stage` and
`thought` have no Python defaults. Everything else defaults silently:

| Reply | Result |
|---|---|
| `{}` | RAISES (4 errors) |
| Prose | RAISES |
| `stage: "daydream"` | RAISES (unknown enum) |
| The four required fields, every optional field absent | **PARSES** — `chain=[]`, `symbols=[]`, `conditions=[]`, `weakest_hop=''`, `advance_id=None` |
| `symbols: ["Cargill", "BTC/USD"]` | **PARSES** — a non-routable name survives validation |

Neither of the last two is a new risk — `grants.py` applies the enabled-class
hard block and derives a symbol's true class from the broker's own routing rule,
and a chain-less dream is caught by `promotion_for`. But note the shape: **the
fields that go quiet are exactly the ones `EVERY_FIELD_REQUIRED` was added to
force the model to state.** The wire contract and the Python floor were
deliberately different; dropping the wire half leaves only the floor.

---

## Finding 3 — prompt caching, and a surprise on the way

The question was going to be "does `cache_control` survive a proxy, and what does
it cost if not". Measuring the input answered a different one.

**MEASURED.** `build_system_prompt(load_rules())` is 14,264 characters. Token
counts via `messages.count_tokens`: **3,670 on Haiku 4.5**, **4,826 on Sonnet 5**
(different tokenizers; both include a one-character user message).

Then two identical requests seconds apart, with the shipped
`{"type": "ephemeral", "ttl": "1h"}` block:

| Model | Call | `input_tokens` | `cache_creation` | `cache_read` |
|---|---|---|---|---|
| `claude-haiku-4-5-20251001` | 1 | 3,676 | **0** | **0** |
| `claude-haiku-4-5-20251001` | 2 | 3,676 | **0** | **0** |
| `claude-sonnet-5` | 1 | 13 | **4,822** | 0 |
| `claude-sonnet-5` | 2 | 13 | 0 | **4,822** |

> **On the default tier the cache never engages, and it never has.** Haiku 4.5's
> minimum cacheable prefix is 4,096 tokens (DOCUMENTED); the system block is
> 3,676 (MEASURED). A prefix under the minimum is not an error — it silently
> produces no cache entry. So `claude_client.py`'s `ttl: "1h"`, the reasoning in
> its module docstring, and the "four reads per write" arithmetic in
> `docs/COSTS.md` are all correct in principle and all describing something that
> is not happening.

The repository's own recurring lesson in a new place: a lenient mechanism plus a
silent coercion is a mechanism that can fail completely while looking healthy.
Nothing reads `cache_creation_input_tokens` back to check — `_usage_from`
collects it, `main.py` logs the cost, and a permanent zero looks exactly like a
cheap cycle.

### What losing caching actually costs

At ~2,900 calls/month, one 1-hour write per four calls at the 15-minute cadence:

| Tier | Cached block | Blended cached | Uncached | Δ if caching breaks |
|---|---|---|---|---|
| **Haiku 4.5** (default) | 3,676 tok | — *(never engages)* | $0.003676/call | **$0.00/month** |
| Sonnet 5 | 4,822 tok | $0.005545/call | $0.009644/call | **+$11.89/month** |
| Opus 5 | ~4,822 tok | $0.013863/call | $0.024110/call | **+$29.72/month** |

**So the caching risk in `docs/DROPLET_AI.md` is materially overstated for the
shipped configuration and understated for the tiers anyone would actually move
to.** That document estimates "roughly a doubling, ~$8 to ~$16/month" on Haiku;
the measured answer on Haiku is zero, because the loop already pays the uncached
price. It also says the exact figure needs a `count_tokens` call against
`build_system_prompt(rules)` before anyone quotes it — that call has now been
made, and the numbers above are it.

Two consequences that have nothing to do with any provider and matter more than
the migration question:

- **The system block alone costs $10.66/month at full input price on Haiku**
  (3,676 × 2,900 × $1/Mtok), against `docs/COSTS.md`'s whole-loop estimate of
  ~$8/month. That estimate records 2,072 input tokens per call, measured before
  the prompt grew its sessions, position-plan and grant sections; the system
  block alone is now larger than the whole figure.
- **Raising `CLAUDE_TIER` to Sonnet turns the cache on**, which is a
  counter-intuitive place for a cost cliff: the same 1h block bills at 1× forever
  on Haiku and 0.1× from the second call on Sonnet.

Provider status: **Vercel documents `cache_control` support** (DOCUMENTED) and
its validator rejects a malformed `cache_control` on a system block (MEASURED —
`400 system: Invalid input`), which a permissive passthrough would not do.
**DigitalOcean documents caching for Chat Completions and omits `cache_control`
from the published `/v1/messages` schema** (DOCUMENTED). Either way the
minimum-prefix rule is a *model* property and carries through a passthrough, so
on a Haiku slug the answer stays zero on every provider.

---

## Finding 4 — Vercel AI Gateway, measured from outside

Because Vercel validates before authenticating, its request schema can be mapped
without a key. Every row below is **MEASURED**, live, with a deliberately invalid
API key:

| Request | Response |
|---|---|
| missing `model` | `400 model: Invalid input: expected string, received undefined` |
| **bogus top-level field** | **`401`** — reached auth, i.e. **unknown top-level keys are stripped, not rejected** |
| `output_config: "hello"` | `400 output_config: Invalid input: expected object` |
| `output_config.format.type: "bogus_fmt"` | `400 output_config.format.type: Invalid input: expected "json_schema"` |
| `output_config.format` with **no** `schema` | `400 output_config.format.schema: Invalid input: expected record, received undefined` |
| `output_config.effort: "nonsense"` | `400 output_config.effort: Invalid option: expected one of "low"\|"medium"\|"high"\|"max"\|"xhigh"` |
| `output_config.effort: "medium"` | `401` — accepted |
| `thinking.type: "nonsense"` | `400 thinking.type: Invalid option: expected one of "adaptive"\|"enabled"\|"disabled"` |
| `thinking: {"type":"adaptive"}` | `401` — accepted |
| system block with malformed `cache_control` | `400 system: Invalid input` |
| system block with `cache_control: {"type":"ephemeral","ttl":"1h"}` | `401` — accepted |
| **the repo's real `ClaudeDecision` body** (9,440 bytes) | `401` — **accepted by the validator** |
| **the repo's real `DreamStep` body** (9,998 bytes) | `401` — **accepted by the validator** |
| old shape: top-level `output_format` | `401` — i.e. an unknown key, stripped |

**This resolves the coordinator's question about the two documented shapes.** The
API reference's `output_config` is the one the gateway models; the Python
streaming example's `extra_body={'output_format': ...}` plus
`anthropic-beta: structured-outputs-2025-11-13` is the older mechanism, and the
gateway treats a top-level `output_format` as an unknown key. **The shipped
`messages.parse(output_format=...)` sends `output_config`** — MEASURED at the top
of this document — **so it sends the modelled shape, with no beta header
required.** Nothing needs `extra_body`, and the beta-header passthrough question
does not arise for this repository.

**Three caveats, and the first is the important one.**

1. **Validation is not enforcement.** The validator modelling, typing and
   *requiring* `output_config.format.schema` is far stronger evidence than
   DigitalOcean's silence — a gateway that intended to drop the field would have
   no reason to require it — but it does not prove the gateway forwards the
   schema to Anthropic and returns a grammar-constrained response. **One
   authenticated call settles it**, and it is the first thing in "what to run
   first".
2. **Unknown top-level keys are stripped, not rejected.** MEASURED. So the
   normalising-layer risk `docs/DROPLET_AI.md` identifies is real here too — it
   simply does not apply to `output_config`, which is modelled. It would apply to
   any future Anthropic parameter the gateway has not caught up with, and that is
   a standing hazard rather than a one-off.
3. **Model ids are provider-prefixed** (`anthropic/claude-opus-5`), so a swap
   touches `CLAUDE_MODEL_IDS`, exactly as a DigitalOcean swap would — with the
   pricing-table coupling in the next section applying identically. Vercel's
   pricing relative to Anthropic list is **not verified here**, and it is the
   figure that decides whether moving buys anything at all.

`provider_metadata.gateway.routing` on the response (DOCUMENTED) is the direct
answer to the silent-downgrade problem that forced a blanket ban on DigitalOcean
router slugs — **but nothing in this repository reads it.** MEASURED: nothing
anywhere reads `response.model` either. The observability exists at the API and
is unconsumed, which makes it a precondition to implement rather than a property
to rely on.

---

## Model ids, tiers, and the cost figures nobody would notice going wrong

`CLAUDE_MODEL_IDS` and `CLAUDE_PRICING_USD_PER_MTOK` are both keyed by
`ClaudeTier`, an enum of exactly three members. Three consequences.

**A wrong model id is loud.** MEASURED: sending DigitalOcean's slug
`anthropic-claude-5-sonnet` to Anthropic returns `NotFoundError` (404) with the
slug named in the message, as does an ordinary typo. Both callers catch it,
neither proceeds. "Swapped the base URL, forgot the model ids" is a dead loop
with an explicit error, not a wrong trade. On Vercel the id is a plain string to
the validator, so a bad one surfaces after auth and could not be measured here.

**Error envelopes differ, and the SDK copes.** MEASURED: DigitalOcean returns
`{"id": "Unauthorized", "message": ...}` where Anthropic and Vercel return
`{"type": "error", "error": {...}}`. The SDK still raises the right class —
chosen by status code — but against DigitalOcean `err.type` is `None`. Nothing in
`src/` branches on `err.type`; the loop catches `Exception` and the other two
catch `(anthropic.APIError, ValueError, RuntimeError)`. MEASURED against the
installed SDK: in 0.121.0 `APIConnectionError` and `APITimeoutError` are both
subclasses of `APIError`, and `pydantic.ValidationError` is a `ValueError`, so
those tuples cover every failure a new hop can invent. **No change needed.**

**The cost tracker is keyed by tier, not by model.** Harmless today, because both
providers charge Anthropic's list price for Anthropic models. It stops being
harmless the moment a tier is pointed at a cheaper model — which is *the only
reason the Python path would move at all*, since a like-for-like swap saves
nothing. Then `CallUsage.estimated_cost_usd` reports Anthropic prices for a model
that costs a fraction, silently, on the `cycle_complete` line, in the audit
event, on the Dreaming page's per-run cost and on the Settings page's per-year
estimate. **A figure that looks like a measurement and is not is the exact thing
this repository refuses.** If a tier is ever repointed, the pricing table moves in
the same commit.

**Per-call model selection is not expressible.** `ClaudeClient` reads
`CLAUDE_MODEL_IDS[self._tier]`; no field can hold "this run, use something else".
The operator's "allocate tasks to better models" is already real on Hermes —
`run-chat.sh` and `run-dream.sh` each read their own instance's
`DO_INFERENCE_MODEL`, deliberately separate so Grogu can differ from Yoda. It is
**not** real on the Python side and needs a new field, not a new provider.

**`env.claude_tier` is recorded, the served model is not.** MEASURED:
`main.py:277` writes `"tier": env.claude_tier.value`; nothing reads
`response.model`. So `docs/DROPLET_AI.md`'s precondition — record the served model
and treat a mismatch as a failed cycle — is **not met today** on any provider.
The Hermes wrappers already say this out loud (*"requesting, not confirming —
this wrapper cannot see which model answered"*); the Python path makes the same
claim silently.

---

## `ANTHROPIC_BASE_URL` — honoured by the Python path, and unverified for Hermes

**MEASURED.** `anthropic/_client.py` resolves `base_url` as *kwarg >
`ANTHROPIC_BASE_URL` > profile config*. `ClaudeClient` constructs
`anthropic.Anthropic(api_key=env.anthropic_api_key)` and passes no `base_url`, so
the environment variable wins. Every stub measurement in this audit proved it:
the only thing pointing the shipped `ClaudeClient` at `127.0.0.1` was that
variable.

That is convenient, and it is the gap between two switches:

| | Switch | Read by |
|---|---|---|
| This repository | `DO_INFERENCE_KEY` (+ `DO_INFERENCE_BASE_URL`) | `Env.inference_provider`, `run-chat.sh`, `run-dream.sh` |
| The Anthropic SDK | `ANTHROPIC_BASE_URL` | Every `anthropic.Anthropic()` in the process |

`Env.inference_provider` inspects only the first pair, cannot see the second, and
prints a startup banner asserting which provider is in force.

**There is a deployment shape where they disagree.** MEASURED:
`deploy/systemd/mudhorn-dream.service` and `mudhorn-confer.service` both carry
`EnvironmentFile=/opt/mudhorn/.env`, so every line in that file is in `os.environ`
for those processes. `mudhorn-bot.service` does not — the loop reads `.env` only
through pydantic-settings, which does not export to the environment. So an
`ANTHROPIC_BASE_URL=` line in `/opt/mudhorn/.env` — the documented pattern for
both gateways, and what `docs/DROPLET_AI.md` step 6 shows being exported — would
reroute **the dreamer and the conference and not the trading loop**, while the
banner reported "Anthropic direct" for all three.

Loud in the likely case: an Anthropic key at a gateway base URL is a `401` and
`dream_call_failed` follows. Quiet in the unlikely one: both variables set, two
processes on one provider and one on another, nothing on any surface
distinguishing them. The Hermes wrappers already solve exactly this by
`unset ANTHROPIC_BASE_URL` on the Anthropic branch, so the rollback is complete
rather than half-applied. **The Python path has no equivalent.** The smallest
honest fix is for `Env.inference_provider` to read
`os.environ.get("ANTHROPIC_BASE_URL")` and report a disagreement, in the same
shape as `market_clock` reporting the broker's clock beside the computed one:
name the discrepancy, do not resolve it.

**Whether Hermes honours `ANTHROPIC_BASE_URL` is still unverified**, and phase 1
rests on it. `run-chat.sh` is honest about this in place and mitigates it
correctly by exporting the gateway key as `ANTHROPIC_API_KEY`, so a client that
ignores the base URL gets a 401 rather than quietly serving the turn from
Anthropic. That is the right failure direction and nothing here improves on it.
**Do not verify it by reading `/tools` or a config file** — the dropped-toolset
finding in `CLAUDE.md` applies unchanged.

---

## The souls

`souls.py` makes no model call. It reads Markdown off disk and returns a prompt
prefix, so a provider swap changes nothing about the mechanism: the file
permissions (`souls/` root-owned, so the service account cannot edit its own
rails), the read-at-call-time behaviour, the degrade-to-voiceless path and the
1,600-word cap are all upstream of the transport and survive intact.

**What is not true is that the souls are a Hermes-only path.** MEASURED, by call
site:

| Soul | Where it is read | Which model answers |
|---|---|---|
| Yoda | `web/app.py:975` → `run-chat.sh` | Hermes — **already DO-capable** |
| Yoda | `confer.py:1147` — the trading agent's system prompt | Python `claude.confer` — Anthropic |
| Grogu | `web/app.py:705` → `run-dream.sh` | Hermes — **already DO-capable** |
| Grogu | `dreamer.py:1279` — prepended to `SYSTEM_PROMPT` | Python `claude.dream` — Anthropic |
| Grogu | `confer.py:1146` — the dreamer's system prompt | Python `claude.confer` — Anthropic |
| Armorer | `web/app.py:767` → `run-chat.sh` | Hermes — **already DO-capable** |

So `docs/DROPLET_AI.md`'s clean split — Path A is Python, Path B is Hermes, the
souls are Path B — is true of the Armorer and half true of the other two. Phase 1
as executed puts Grogu on DigitalOcean when it answers the chat panel on
`/dreaming`, and leaves it on Anthropic when it writes the dream records rendered
on the same page. Same character, one screen, and `CLAUDE.md` already records
that this page has been the site of one overclaim about exactly which half of it
a statement covers.

That is not an argument against phase 1. It is an argument for saying so on the
Dreaming page, in the same style as the isolation banner: name which half moved.

**What the souls would lose if the Python halves moved to a weaker model is
instruction-following on prose, and nothing structural.** A soul is a character
prefix and a set of prohibitions — *"a soul is a reason to say something
SHORTER"*, *"a soul shapes the framing and never touches a figure"* — enforced
entirely by the model choosing to comply. A smaller model complies less. No gate
anywhere catches a soul being ignored, because there was never meant to be: the
guarantees that matter are structural (`Dream` carries no order fields,
`TraderPowers` imports no broker) and are unaffected by which model reads the
prose.

---

## Failure behaviour, per call site

MEASURED by reading the handlers:

| Call site | Catches | On failure | Loud? |
|---|---|---|---|
| `claude.propose` (loop) | `except Exception` | `log.error("model_call_failed")` + audit event + **no `cycle_complete`** + skip | **Yes** — a visible gap on the Decisions page |
| `claude.propose` (smoketest) | nothing | Propagates; the command fails | **Yes** |
| `claude.dream` | `(APIError, ValueError, RuntimeError)` then `except Exception` | `log.warning("dream_call_failed")`, returns `None`, marks nothing seen | **Weakest of the three** — a log line, no audit event |
| `claude.confer` | same tuple, then `except Exception` | `_TurnFailed` → recorded as `NO_DECISION` with `CALL_FAILED` in the transcript | **Yes** — a stored verdict, deliberately not `DEFER` |
| `run-chat.sh` / `run-dream.sh` | wrapper exits 78 on misconfiguration | `ChatReply.failed`, rendered on the page | **Yes** — refuses rather than falling back |

- **`propose` is the only call with an unbounded budget.** It takes the SDK
  defaults — a 600-second read timeout and `max_retries=2` — so one cycle's model
  call can occupy **30 minutes** before admitting failure, against a 900-second
  cadence. `dream` and `confer` both set `timeout=240` and `max_retries=1`
  explicitly, with a comment saying an SDK default nobody wrote down is not a
  bound. The same reasoning applies to `propose` and has not been applied to it.
  Any gateway adds latency and a new class of connection failure, so this gets
  worse under any provider change and is worth fixing regardless.
- **The dreamer's failure is the quietest thing here** — a `log.warning` and a
  `None`, with no audit event and nothing on the Dreaming page separating "the
  model failed" from "no dream today". If `dream` is the first Python path to
  move, the surface that would show the migration failing is the one that shows
  it least.

---

## Verdicts, per call site and per provider

**STAY** = leave it where it is. **MOVE** / **MOVE WITH CHANGES** / **DO NOT
MOVE** as instructed.

| Call site | Anthropic direct | DigitalOcean | Vercel AI Gateway |
|---|---|---|---|
| **`claude.propose`** | **STAY** | **DO NOT MOVE** | **DO NOT MOVE** — for one reason now, not two |
| **`claude.dream`** | STAY (fine) | **MOVE WITH CHANGES** — and only after the drop-versus-reject test | **MOVE WITH CHANGES** — the cleanest target |
| **`claude.confer`** | STAY (fine) | **MOVE WITH CHANGES**, after `dream` holds | **MOVE WITH CHANGES**, after `dream` holds |
| **`run-chat.sh`** (Yoda, Armorer) | — | **MOVE — already done, correctly** | MOVE, but no reason to churn |
| **`run-dream.sh`** (Grogu) | — | **MOVE — already done** | MOVE, but no reason to churn |
| **`scripts/agent_behaviour_live.py`** | Judge **STAY** | Agent MOVE FREELY | Agent MOVE FREELY |
| **`scripts/dream_cycle_live.py`** | Judge **STAY** | MOVE WITH CHANGES | MOVE WITH CHANGES |
| **`scripts/confer_live.py`** | — | MOVE FREELY with `confer` | MOVE FREELY with `confer` |

### `claude.propose` — DO NOT MOVE, and the reason has narrowed

It had two independent reasons. **One is now gone on Vercel and one is not.**

- ~~*It costs a server-enforced schema on the path that produces order
  quantities and stop prices.*~~ On Vercel this looks resolved: the validator
  models, types and requires `output_config.format.schema`, and the repo's real
  `ClaudeDecision` body passes it. Pending one authenticated call.
- **It still buys nothing.** Price parity means a like-for-like swap saves
  $0.00 and adds a hop, a second credential and a normalising layer between this
  code and the API it was written against. The only version that saves money is
  the version that changes the model proposing trades — and `CLAUDE.md` is
  unambiguous that model identity there is a risk parameter, not a tuning knob:
  *"Changing this is a strategy change wearing a config change's clothes."*
  Gateway failover, the other thing a gateway sells, is the single worst
  available behaviour on the order path.

So the verdict is unchanged and the argument is now a single sentence: **the
schema is no longer the reason; the absence of any benefit is.** What would
change it is a *measured* saving against Anthropic list, on a model the operator
has separately decided should propose trades — two decisions, in two commits,
with the second one having nothing to do with providers. If it ever moves, every
precondition in `docs/DROPLET_AI.md`'s "If the loop moves anyway" still applies,
plus the two this audit adds: **read the served model off the response and treat
a mismatch as a failed cycle** (nothing does today), and **fix the quiet-cycle
hole** — a cycle with zero assessments should be refused as a malformed answer,
not recorded as a considered decision.

### `claude.dream` — MOVE WITH CHANGES, and to Vercel first

Right first candidate on any provider: nothing waits on it, a failure costs one
dream, and `DreamStep`'s Python floor rejects an empty object and prose. Changes,
in order:

1. **Raise `dream_call_failed` from a log line to an audit event before moving.**
   Otherwise the migration's own failure mode is the quietest surface in the
   repository.
2. **Prove the schema is enforced, not merely accepted** — the single call in
   "what to run first". On Vercel that is the whole test. On DigitalOcean it is
   the drop-versus-reject test, and a "dropped" answer means building the
   forced-tool-call substitute `docs/DROPLET_AI.md` describes before anything
   moves.
3. **If the tier is repointed at a cheaper model, move
   `CLAUDE_PRICING_USD_PER_MTOK` in the same commit.**
4. **DigitalOcean-specific:** its Haiku slug documents an 8,192-token output cap
   against `DREAM_MAX_TOKENS = 16000`. That combination is a `400`, which is
   loud — but it means the Haiku slug is unavailable to the dreamer entirely.

### `claude.confer` — after `dream` has held

Same schema question; its verdict shape is already the loudest of the three. The
additional change is the one `docs/DROPLET_AI.md` names: a model change here
changes what gets adopted, and an adopted dream is a live symbol grant. A
`TODO.md` line, not a free swap.

### Where this agrees and disagrees with `docs/DROPLET_AI.md`

**Agrees**, now with measurement behind it: souls yes, dreamer probably,
`claude.propose` never; a router must not be pointed at anything that trades; the
served model is not recorded today.

**Disagrees on four points.**

1. **"Structured output is the blocker" is a DigitalOcean finding, not a general
   one.** Vercel models the exact field the SDK sends, requires its `schema`, and
   accepts both of this repository's real schemas. The conclusion "assume not"
   was correct for the provider researched and does not generalise.
2. **The caching risk is the wrong size.** Estimated as a doubling from ~$8 to
   ~$16/month on Haiku; measured, it is **$0**, because the cache does not engage
   on Haiku at all — 3,676 tokens against a 4,096-token minimum. The real cost
   lands on Sonnet (+$11.89/month) and Opus (+$29.72/month), the tiers a
   migration would target. Not an unreasonable estimate — arithmetic over a
   figure nobody had read back, and that document says as much.
3. **`output_config` and `reasoning_effort` are one mismatch, not two.**
   `output_format=` is folded into `output_config.format`, so a single dropped
   object takes the schema *and* the effort setting — on the two calls (`dream`
   at `high`, `confer` at `medium`) bought for depth.
4. **The Path A / Path B split is not clean**, because Grogu and Yoda live in
   Path A as well. "Route the souls freely, per soul" is true for the Armorer and
   half-true for the other two.

**Adds two things that document could not have known**, because it was reading
rather than running: **`ANTHROPIC_BASE_URL` already routes the Python path** and
two of three systemd units put `/opt/mudhorn/.env` into the process environment;
and **DigitalOcean authenticates before parsing**, so its most important open
question is not answerable from outside at all, while Vercel's is.

---

## What to run first

In this order. Each is one command, each answers something nothing else can.

1. **Vercel: is the schema *enforced*, not merely accepted?** With a gateway key,
   ask for something the schema forbids and see who refuses.
   Use a confirmed id — `anthropic/claude-sonnet-5`. The exact spelling of a
   Haiku slug on this gateway is unverified, and the validator treats `model` as
   a plain string, so a wrong one fails after auth rather than in the probe.
   ```sh
   curl -sS https://ai-gateway.vercel.sh/v1/messages \
     -H "x-api-key: $AI_GATEWAY_API_KEY" -H "anthropic-version: 2023-06-01" \
     -H "content-type: application/json" -d '{
       "model":"anthropic/claude-sonnet-5","max_tokens":128,
       "messages":[{"role":"user","content":"Reply with a friendly paragraph of prose. Do not use JSON."}],
       "output_config":{"format":{"type":"json_schema","schema":{
         "type":"object","properties":{"colours":{"type":"array","items":{"type":"string"}}},
         "required":["colours"],"additionalProperties":false}}}}'
   ```
   JSON matching the schema despite the instruction ⇒ **enforced**, and `dream`
   can move. Prose ⇒ accepted-and-dropped, which is the worst outcome and puts
   Vercel exactly where DigitalOcean is.

2. **DigitalOcean: drop versus reject.** The same body against
   `https://inference.do-ai.run/v1/messages` with `anthropic-claude-haiku-4.5`
   and a model access key. A `400` naming `output_config` is the **good** answer.
   A `200` of prose proves the field is dropped.

3. **Caching, on a Sonnet-class slug and not on Haiku.** Two identical requests a
   minute apart carrying a 1h `cache_control` block of at least ~5,000 tokens;
   assert `cache_read_input_tokens > 0` on the second. Running it on a Haiku slug
   returns zero for the minimum-prefix reason and proves nothing about the
   provider.

4. **Ask the agent, on the Chat page, whether Hermes moved.** Not `/tools`, not a
   config file. The behavioural check is the authoritative one.

Then, and only then, the phase-2 sequence in `docs/DROPLET_AI.md`.

---

## What is still unverified

1. Whether Vercel **enforces** `output_config` or merely validates it. Test 1
   above. This is now the single most valuable call in the whole plan.
2. Whether DigitalOcean drops or rejects `output_config`. **Unanswerable without
   a key** — it authenticates before parsing the body, measured.
3. Vercel's pricing relative to Anthropic list. Decides whether moving buys
   anything at all, and is the only thing that could reopen `claude.propose`.
4. Whether `cache_control` is honoured end to end on either gateway's
   `/v1/messages` (both accept it at the request layer; neither has been read
   back from a response).
5. Whether Hermes honours `ANTHROPIC_BASE_URL`.
6. Latency added by a proxy hop — which interacts with `propose` taking the SDK's
   600-second default timeout and two retries.
7. Whether any non-Anthropic model on either provider compiles a 15-property
   schema with nested models, and at what cost. The Anthropic grammar
   measurements do not transfer.
