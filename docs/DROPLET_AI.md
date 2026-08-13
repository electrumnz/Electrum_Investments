# DigitalOcean inference for Mudhorn's agents

Researched **10 August 2026**, against the live DigitalOcean docs. Everything
below is either cited or explicitly marked unverified. Re-check the pricing and
the model list before acting on the arithmetic; both move.

> ## The short version
>
> **Do the chat and dreaming halves. Do not do the trading loop.**
>
> DigitalOcean resells Anthropic's models at **exactly Anthropic's list price**,
> so routing `model_client.py` through it saves nothing and costs one more hop,
> one more credential and one more prepaid balance to keep topped up. Its
> Anthropic-compatible endpoint also **does not document the structured-output
> parameter that `claude.propose` depends on** — the field is absent from the
> published request schema. That is the whole decision.
>
> Where DigitalOcean genuinely pays is the *other* model path: Hermes, which
> drives the three souls on `/chat`, `/dreaming` and `/settings`. That is where
> "allocate tasks to better models if needed" is a real feature rather than a
> risk, because none of those agents propose an order, and it is a
> configuration change on the box rather than a code change in this repository.

---

## What it is actually called now

The product is **DigitalOcean Gradient™ AI Platform**, and the part this is
about is documented under **Inference**. Three deployment shapes sit under one
control plane — **Serverless Inference** (pay per token, no infrastructure),
**Dedicated Inference** (GPU-hour), and **Batch Inference** (asynchronous, 24h
window). Only serverless is relevant here.

- Serverless base URL: **`https://inference.do-ai.run`**
- It is **independent of the main DigitalOcean control-plane API**
  (`api.digitalocean.com`), and uses its own credential type.

Sources: [Inference product docs](https://docs.digitalocean.com/products/inference/),
[Serverless Inference API reference](https://docs.digitalocean.com/reference/api/reference/serverless-inference/).

---

## The API surface

**It is both OpenAI-compatible and Anthropic-compatible, on two different
endpoints.** That is the single most useful fact here, and it is easy to miss.

| Endpoint | Shape | Notes |
|---|---|---|
| `POST /v1/chat/completions` | **OpenAI-compatible** | The documented default. Works with the OpenAI SDK by changing the base URL. |
| `POST /v1/messages` | **Anthropic-compatible** | Documented as "the interface for Claude Code and other agentic workflows". Setting `ANTHROPIC_BASE_URL` to the DigitalOcean endpoint is the documented pattern. |
| `POST /v1/responses` | OpenAI Responses API | For newer models and multi-step tool use. |
| `POST /v1/embeddings`, `GET /v1/models`, `POST /v1/images/generations`, `POST /v1/async-invoke` | — | Not relevant here. |

Source: [Serverless Inference API endpoints](https://docs.digitalocean.com/products/inference/how-to/si-endpoints/).

### Authentication differs per endpoint, and that is a trap

- `/v1/messages` — `x-api-key: $MODEL_ACCESS_KEY` **plus** `anthropic-version: 2023-06-01`.
  Exactly Anthropic's own header shape.
- `/v1/chat/completions` — `Authorization: Bearer $DIGITALOCEAN_TOKEN`.

The API reference also lists DigitalOcean OAuth tokens (`dop_v1_`, `doo_v1_`,
`dor_v1_`) as acceptable bearer credentials for serverless inference. **Prefer a
model access key over a personal access token** for the reason in
"Credentials" below.

Sources: [Messages API how-to](https://docs.digitalocean.com/products/inference/how-to/use-messages-api/),
[Chat Completions how-to](https://docs.digitalocean.com/products/inference/how-to/use-chat-completions-api/).

### The architecture is a proxy, and the proxy normalises

DigitalOcean's own engineering write-up describes a *Model Executor Service*
that "handles the provider translation and forwards to the provider's API. The
response is normalized back through the same pipeline", and says
provider-specific quirks are "absorbed here to prevent leaks to your
application."

**That sentence is the risk, stated by the vendor.** For commercial Anthropic
models this is a passthrough to Anthropic with a translating layer in the
middle. A translating layer that models a subset of the upstream schema does not
usually *error* on a field it has never heard of — it drops it. Which failure
you get for an undocumented parameter is precisely the thing that cannot be
established from documentation, and it is the thing this repository cares most
about. See "Structured output" below.

Source: [Serverless Inference: a deep dive](https://www.digitalocean.com/blog/serverless-inference-deep-dive).

---

## Models and price

Serverless carries Anthropic and OpenAI commercial models alongside ~35
DigitalOcean-hosted open-source ones. The Anthropic rows, which are the ones
that matter for a like-for-like swap:

| DigitalOcean model ID | Context | Max output | Tools | Caching | Reasoning |
|---|---|---|---|---|---|
| `anthropic-claude-opus-5` | 1,000,000 | 128,000 | ✔ | ✔ | ✔ |
| `anthropic-claude-5-sonnet` | 1,000,000 | 128,000 | ✔ | ✔ | ✔ |
| `anthropic-claude-haiku-4.5` | 200,000 | 8,192 | ✔ | ✔ | — |
| `anthropic-claude-fable-5` | 1,000,000 | 128,000 | ✔ | ✔ | ✔ |

Note the **naming is DigitalOcean's, not Anthropic's** — `anthropic-claude-5-sonnet`,
not `claude-sonnet-5`. A swap therefore touches `CLAUDE_MODEL_IDS` in
`config.py`, not only a base URL.

### Price, against Anthropic's own list

| Model | DO input / output | DO cache read | DO 1h cache write | Anthropic list |
|---|---|---|---|---|
| Claude Opus 5 | $5.00 / $25.00 | $0.50 | $10.00 | $5.00 / $25.00 |
| Claude Sonnet 5 | $2.00 / $10.00 | $0.20 | $4.00 | $3.00 / $15.00 ($2/$10 intro to 31 Aug 2026) |
| Claude Haiku 4.5 | $1.00 / $5.00 | $0.10 | $2.00 | $1.00 / $5.00 |

**They are the same numbers.** Cache read is 0.1×, the 5-minute write 1.25× and
the 1-hour write 2.0× — Anthropic's own multipliers, reproduced exactly. There
is no reseller discount and no reseller markup.

Open-source models are where the money is: roughly **$0.18–$0.99 per million
tokens**, e.g. `llama3.3-70b-instruct` ~$0.65/M, `ministral-3-8b-instruct` ~$0.20/M,
`deepseek-r1-distill-llama-70b` ~$0.99/M. `glm-5.2`, `mimo-v2.5-pro` and
`kimi-k3` carry both tool calling and caching, and GLM/MiMo are the only models
in the catalogue whose usage notes mention structured outputs at all.

Sources: [Supported models](https://docs.digitalocean.com/products/inference/details/models/),
[Inference pricing](https://docs.digitalocean.com/products/inference/details/pricing/).

---

## The three things this repository depends on

### 1. Prompt caching — supported, with one documented gap

`cache_control` with `{"type": "ephemeral", "ttl": "1h"}` is documented for
Anthropic models, with the 5m/1h choice and the exact multipliers above. Cache
hits report as `cache_read_input_tokens`, the same field
`ModelClient._usage_from` already reads. Open-source models cache
automatically and need no `cache_control` at all.

**The gap:** the caching how-to is titled and scoped to the **Chat Completions
and Responses APIs**, and states caching for Anthropic models works "in the
chat completions API". It does not mention `/v1/messages`. Separately, the
`/v1/messages` request schema in the API reference **does not list
`cache_control` anywhere** — though it does list `system` as `string | array`,
which is the shape a cache-controlled system block requires, and the chat
completion *response* schema carries `cache_created_input_tokens`,
`cache_read_input_tokens` and separate 1h/5m ephemeral creation buckets.

So caching almost certainly works on `/v1/messages`; it is not *documented* to.
That distinction matters here because the failure mode is silent — a dropped
`cache_control` does not raise, it just bills 10× on the system block forever.

**If caching silently failed**, the loop's system prompt (a static block in the
low thousands of tokens) would move from 0.1× to 1× on every one of ~2,900
calls a month. On Haiku that is roughly a doubling of the loop's model bill,
from ~$8/month to ~$16/month. That estimate is arithmetic, not a measurement —
`docs/COSTS.md` records the uncached remainder (2,072 input tokens) but not the
cached system-block size, so the exact figure needs a `count_tokens` call
against `build_system_prompt(rules)` before anyone quotes it.

**Verification is one assertion, and it must be run before trusting anything:**
send two identical requests a minute apart and assert
`cache_read_input_tokens > 0` on the second. Same check `docs/COSTS.md` already
implies and the same rule as everywhere else here — a figure nobody has read
back is a hope.

Sources: [Prompt caching how-to](https://docs.digitalocean.com/products/inference/how-to/use-prompt-caching/),
[Inference features](https://docs.digitalocean.com/products/inference/details/features/).

### 2. Structured output — MEASURED 12 Aug 2026, and it is worse than "not documented"

> **Everything below this box was written from the documentation. It has now
> been run against the live endpoint, and the finding is the dangerous one.**
>
> **`output_config` is accepted with HTTP 200 and silently ignored.** Not
> refused, not 400 — accepted, and the reply comes back as prose. Measured on
> `llama3.3-70b-instruct`, `glm-5.2`, `openai-gpt-oss-120b` and `deepseek-3.2`:
> every one returned `stop_reason: max_tokens` and a paragraph beginning *"The
> ticker SPY refers to the SPDR S&P 500 ETF Trust…"*. A caller that only checked
> for an error would believe the schema was in force.
>
> **Forced tool calling DOES work, on 16 of 27 text models.** Verified end to
> end: a `tool_choice` of `{"type":"tool","name":"record"}` with the schema as
> `input_schema` returns a proper `tool_use` block with conforming values. That
> is the route, and it is the one this document already predicted.
>
> **Three traps found by running it that reading would have missed:**
>
> - **`glm-5.2` looks like it works and does not.** It returns a `tool_use`
>   block whose keys are corrupted — `{'<tool_call>record  <arg_key>symbol':
>   'SPY', …}` — the model emitting its own tool-call markup as text with the
>   proxy half-parsing it. A naive check that looked for one valid field passes
>   it. This document previously named `glm-5.2` and `mimo-v2.5-pro` as the two
>   models whose notes mention structured output; `mimo-v2.5-pro` returns
>   HTTP 500 on a tool call. **The documentation pointed at exactly the two
>   worst candidates.**
> - **Six models return HTTP 500** on a forced tool call: `glm-5`, `glm-5.1`,
>   `kimi-k2.6`, `mimo-v2.5-pro`, `minimax-m2.5` (*"Failed to parse tool call
>   arguments"*), `qwen3.5-397b-a17b`.
> - **Two run out of budget before emitting the call** — `kimi-k3` and
>   `openai-gpt-oss-120b` both hit `max_tokens` reasoning first.
>
> **Anthropic models are tier-gated, and the catalogue does not say so.**
> `/v1/models` lists all ten, and calling `anthropic-claude-5-sonnet` returns
> **403 `"this model is not available for your subscription tier"`**. Listing is
> not entitlement. This also caught a real defect in
> `scripts/do_inference_probe.py`, which reported that 403 as *"`output_config`
> is refused outright"* — a confident claim about schema support derived from an
> authorisation error. Fixed: 401/403 is now `NOT AVAILABLE`, kept apart from a
> 400.
>
> **Clean on a forced tool call** (toy schema, one sample each):
> `alibaba-qwen3-32b`, `deepseek-3.2`, `deepseek-4-flash`,
> `deepseek-v4-flash-0731`, `deepseek-v4-pro`, `gemma-4-31B-it`, `kimi-k2.5`,
> `llama-4-maverick`, `llama3.3-70b-instruct`, `mistral-3-14B`,
> `nemotron-3-nano-omni`, `nemotron-3-ultra-550b`, `openai-gpt-oss-20b`,
> `qwen3-coder-flash`, `qwen3.8-max`.
>
> **Now measured, 13 Aug 2026, by `scripts/do_schema_fidelity.py`** — the real
> `ModelDecision` (11,234 bytes, 10 nested `$defs`) and the real `DreamStep`
> (11,655 bytes, 6), as a forced tool call, validated client-side by Pydantic.
> See "Which models hold the real schema" below. **The headline is that three
> samples chose a broken model and ten samples caught it.**

### Which models hold the real schema — MEASURED 13 Aug 2026

`ModelDecision`, 10 samples each. `DreamStep`, 6.

| Model | ModelDecision | median | DreamStep | median |
|---|---|---|---|---|
| `deepseek-v4-pro` | **10/10** | 26.1s | **6/6** | 21.2s |
| `nemotron-3-ultra-550b` | **10/10** | 30.7s | **6/6** | 19.7s |
| `mistral-3-14B` | **10/10** | 12.6s | **6/6** | 7.0s |
| `qwen3-coder-flash` | **10/10** | 14.1s | **6/6** | 4.2s |
| `deepseek-4-flash` | 10/10 | 70.1s | 4/6 | 102.5s |
| `kimi-k2.5` | 8/10 | 81.7s | 3/6 | 233.4s |
| `llama3.3-70b-instruct` | **4/10** | 2.2s | — | — |

**`llama3.3-70b-instruct` is the finding, and it is a lesson about sampling
rather than about that model.** It scored **3/3** on the first pass and **4/10**
on the second. Its six failures are all `empty_arrays` — `assessments=0`,
`position_plans=0` — after being shown four symbols and an open position. Its
2.2-second median is the tell: it returns an almost-empty decision instantly.

That output is structurally valid, passes Pydantic, and **reintroduces exactly
the gap `assessments` exists to close**: a cycle that considered nothing becomes
indistinguishable from a loop that never looked. Three samples would have
shipped it. `propose` runs 96 times a day, and 3 samples cannot separate 0% from
10%.

**Two failure classes are kept apart on purpose.** `kimi-k2.5` and
`deepseek-4-flash` fail on read TIMEOUTS, which is transport and is recoverable
by retry — genuinely different from returning a wrong shape. They are still
unsuitable for `propose` at 82s and 233s medians, but for a reason about latency
rather than about correctness, and conflating the two would misattribute the
defect.

**Disqualified outright**, from the first pass:

- **`qwen3.8-max` returned PROSE on 2 of 3** — no `tool_use` block at all. That
  is the silent quiet-cycle failure `docs/MODEL_CALLS.md` names, and it is
  disqualifying for `propose` at any price.
- **`openai-gpt-oss-20b` and `gemma-4-31B-it`, 0/3 each.** Both invent their own
  vocabulary against the schema — `{'action': 'BUY', 'shares': '30', 'side':
  ..., 'ticker': 'AAPL'}` where the schema demands `direction`, `qty`, `symbol`.
  Client-side Pydantic catches every one, which is precisely why the validation
  has to stay when the server-side guarantee goes away.

**What this does NOT measure, and nothing should read it as measuring:
reasoning quality.** It grades whether a model can hold the shape, not whether
its numbers are any good. A 14B model can comply perfectly and propose nonsense,
and `RiskGate` checks arithmetic rather than judgement. `scripts/agent_behaviour_live.py`
is the harness for that half and it has not been run on these candidates.
**Do not pin `propose` on the strength of this table alone.**

### 2a. What the documentation said, before it was run

`claude.propose` calls `messages.parse(output_format=ModelDecision)`, which the
Anthropic SDK sends as `output_config.format` with a JSON schema. The API
enforces the schema server-side; the SDK validates the result into a Pydantic
model. That is a **correctness mechanism**, not ergonomics: a `qty` or a
`limit_price` that comes back malformed is rejected rather than coerced.

DigitalOcean's published `/v1/messages` request body is:

```
max_tokens*   messages*   metadata   model*   reasoning_effort   speed
stop_sequences   stream   system   temperature   thinking
tool_choice   tools   top_k   top_p
```

**There is no `output_config`.** There is no `response_format` on
`/v1/chat/completions` either — the how-to documents only `model`, `messages`,
`temperature`, `max_completion_tokens` and the deprecated `max_tokens`. A
full-text search of the Serverless Inference API reference returns no match for
`response_format`, `json_schema`, `output_config` or `structured`.

Two further mismatches in that same field list are worth naming, because they
show the endpoint is Anthropic-*shaped* rather than Anthropic-*identical*:

- **`reasoning_effort`, not `output_config.effort`.** `model_client.py` sends
  `output_config={"effort": "medium"}` on Sonnet and Opus. That key does not
  exist in DigitalOcean's schema.
- **`temperature`, `top_k` and `top_p` are listed as accepted**, where
  Anthropic's Opus 5 and Sonnet 5 reject all three with a 400. So the schema is
  a generic superset and per-model validation happens somewhere downstream —
  which tells you nothing about what happens to a field the superset omits.

**What would have to be built to replace it.** The documented substitute is tool
calling, which DigitalOcean states plainly: "All commercial models from
Anthropic and OpenAI available on DigitalOcean support tool (function)
calling", and the Messages how-to claims "full compatibility with Anthropic's
tool-use schema". `tool_choice` is in the schema, so a single forced tool whose
`input_schema` is `ModelDecision`'s JSON schema, with the result validated
client-side by Pydantic, reproduces most of the guarantee.

**Most of it, not all of it.** The differences are exactly the ones this
repository is built around:

- **The schema guarantee becomes client-side.** `strict: true` on a tool
  definition is not documented at DigitalOcean, so the model is *asked* for the
  shape rather than *constrained* to it. Pydantic still rejects a bad object —
  the numbers still reject — but the rejection rate goes up and every rejection
  is a failed cycle.
- **It is a second order path's worth of new code** in the one module that
  feeds the risk gate, for a component whose entire monthly bill is $8.
- **It must fail closed.** A malformed tool payload has to raise into the
  existing `except Exception` in `cmd_loop`, log `model_call_failed` and skip
  the cycle. It must never fall back to parsing prose, and it must never retry
  onto a different model — see rule 4 below.

**Is it worth it? No.** Not for the trading loop. The thing being bought is
access to cheaper models on the one call in the system where model choice is a
risk parameter, and the thing being spent is a server-enforced schema on the
path that produces order quantities and stop prices. That trade is the wrong way
round at any price, and at $8/month there is no price.

### 3. A failed model call must degrade the cycle, never end the loop

`cmd_loop` already wraps `claude.propose` in a broad `except Exception`, logs
`model_call_failed`, records an audit event and skips to the next cycle without
emitting `cycle_complete`. **Any new provider inherits that requirement
unchanged**, and it is the easiest thing in this whole plan to get right,
because the wrapper is already there and catches broadly on purpose.

What is *not* already handled is the new failure mode a provider swap invents.

**DigitalOcean's Inference Router automatically falls back to another model on
rate limit or capacity constraint** — "requests fall back automatically with no
dropped calls". For a chat surface that is a feature. **For the trading loop it
is the single worst behaviour available**, and it must not be enabled there.

A silent downgrade to a weaker model on a trading decision is worse than a
failed cycle, and the reason is visibility. A failed cycle is loud: it logs, it
writes an audit event, it does not emit `cycle_complete`, and the Decisions page
shows the gap. A silent downgrade emits `cycle_complete` normally, records a
decision that looks exactly like every other decision, and the only trace that
a different model produced it is a field nobody is reading. Every rule in this
repository about missing data — `calendar_degraded`, `is_degraded`,
`stops_unchecked`, `open_risk_usd` reporting *unknown* rather than zero — is the
same rule: report the weaker fact rather than imply the stronger one. A router
implies the stronger one by construction.

So: **if a router is ever used for the loop, the model that served the request
must be read off the response and recorded in the audit event, and a served
model other than the configured one must be treated as a failed cycle.**
Simpler and better: do not point the loop at a router at all.

---

## Model choice is a risk parameter, not a performance one

This is the part of the operator's request that needs pushing back on rather
than implementing.

"Allocate tasks to better models if needed" is a good instinct and it is right
for two of the three agent paths. It is wrong for the third, and the reason is
the whole reason this repository exists: in the Alpha Arena competition six
frontier models traded real money under identical prompts and all six finished
underwater. **Model identity is not a tuning knob on the thing that proposes
orders — it changes what gets proposed.**

| Path | Recommendation | Why |
|---|---|---|
| **`claude.propose`** — the trading loop | **Pin.** One model, set in `config/rules.yaml`-adjacent config, changed deliberately in its own commit with a reason. No router, no fallback, no per-request selection. | A model swap changes the distribution of proposals reaching the gate. The gate checks arithmetic, not judgement — it will approve a differently-shaped bad trade just as readily. Changing this is a strategy change wearing a config change's clothes. |
| **`claude.dream`** — the dreamer | **Route freely.** Per-run model selection is fine and genuinely useful. | It carries no order fields by construction (`tests/test_dreaming.py::test_a_dream_cannot_describe_an_order`), and depth is the entire product. A cheaper thinking model that follows a causal chain two hops out is a straight win. |
| **`claude.confer`** — the conference | **Route, cautiously.** | `TraderPowers` reaches no broker and its module-level AST test proves it. But an adopted dream is a live symbol grant, so a model change here changes what gets adopted. Treat a change as worth a line in `TODO.md`, not as free. |
| **Hermes** — the three souls | **Route freely, per soul.** This is the best fit in the whole system. | Yoda teaches, Grogu wonders, the Armorer argues. Three different jobs that genuinely want different models, and none of them proposes an order. `RiskGate.evaluate` still runs on every order path regardless. |

The honest summary: **the operator's feature is real, and its home is Hermes.**

---

## The two model paths, assessed separately

They are different jobs with different risk, and "all agent use" reads as one
instruction only until you look at them.

### Path A — `src/bot/model_client.py` (the trading loop, the dreamer, the conference)

The Anthropic SDK, called from Python, in-process, three methods:
`propose` (96×/day, structured, 1h-cached), `dream` (1×/day, structured,
uncached, 16k tokens, 900s timeout) and `confer` (up to 12 calls/day,
structured, uncached).

**All three use structured output.** `propose` returns `ModelDecision`,
`dream` returns `DreamStep`, `confer` takes its schema as an argument. So the
blocker in section 2 applies to the whole module, not only to the loop — which
is why the recommendation below moves the dreamer *last* and only after the
tool-calling substitute has been proven on something that cannot lose money.

**Cost of swapping: negative.** DigitalOcean charges Anthropic's list price for
Anthropic models, so a like-for-like swap saves exactly $0.00 and adds a proxy
hop, a second credential, a prepaid balance that can run dry, and a
normalisation layer between this code and the API it was written against. The
only saving available is moving to open-source models, which is a *model*
decision, not a *provider* decision — and on the loop it is the decision the
table above says to pin.

### Path B — Hermes (the Chat page, the Dreaming page, the Settings page)

A separate `hermes -z` process per message, launched through
`deploy/run-chat.sh` (and `run-dream.sh` for the isolated dreamer instance),
running as the `hermes` user. `HermesBridge.ask` prepends the selected soul on
stdin and reads the answer off stdout. **This repository does not configure
Hermes' model at all** — that lives in `~/.hermes/config.yaml` on the box, under
the `agent:` key that Hermes writes itself and that
`deploy/merge-hermes-config.py` is careful to deep-merge rather than clobber.

**This is the easy half, and the valuable half.** DigitalOcean explicitly
supports pointing an Anthropic-compatible client at `/v1/messages` via
`ANTHROPIC_BASE_URL`, which is the documented Claude Code pattern. If Hermes
honours `ANTHROPIC_BASE_URL` and `ANTHROPIC_API_KEY` — **unverified, and the
first thing to check** — the swap is two exported variables in `run-chat.sh` and
`run-dream.sh`, or a `hermes model` invocation, and no Python changes at all.

Two properties survive untouched, and both matter:

- The **user split** is unaffected. `hermes` still holds no broker credentials
  and still reaches the broker only through the MCP server, where
  `RiskGate.evaluate` runs on every order path.
- The **dreamer's isolation** is unaffected. `run-dream.sh` still runs from its
  own `HERMES_HOME` with a registry that must not contain the bot's MCP server,
  and the Dreaming page still says so in a banner when it does not.

---

## Recommendation, in order

**Phase 1 — Hermes only. Do this.**

Point the three souls at DigitalOcean. It is a box configuration change, it
touches no file in this repository, it is trivially reversible, and it delivers
the thing the operator actually asked for: the ability to hand a task to a
better or cheaper model. Chat and dreaming are exactly where per-task routing
belongs.

**Phase 2 — measure, then decide about `dream` and `confer`. Probably do this.**

Once Hermes has run on DigitalOcean for a fortnight without incident, prove the
forced-tool-call substitute for structured output on `claude.dream` — the one
structured call in the system that cannot lose money and that nothing waits on.
If it holds, `confer` follows.

**Phase 3 — `claude.propose`. Do not do this.**

Not "not yet" — not on this evidence. It buys nothing (price parity), it costs a
server-enforced schema on the order path, and the only version of it that saves
money is the version that changes the model that proposes trades. If the
operator wants it anyway, the prerequisites are in "If the loop moves anyway"
below, and it should be its own commit with its own reason, like a limit change.

---

## Migration sequence

### Phase 0 — account setup (no code, ~15 minutes)

1. Ensure the DigitalOcean account has a **positive prepaid balance**.
   Serverless inference is prepaid only: "You must maintain a positive prepaid
   account balance to send serverless inference requests." **A zero balance is a
   new way for every agent surface to go dark at once**, and nothing in this
   repository can see it. Whoever does this should set a billing alert in the
   same sitting.
2. Create a **model access key**: console → AI Platform → Serverless Inference →
   *Create model access key*, or
   `POST https://api.digitalocean.com/v2/gen-ai/models/api_keys` with a
   DigitalOcean PAT.
3. **Scope it.** Model access keys can be scoped to specific foundation models
   and inference routers, restricted to a VPC network, and have batch inference
   enabled or not. Scope it to the models Hermes is meant to use and nothing
   else. Do **not** use a personal access token here — a PAT is an
   account-control-plane credential sitting on a box that also runs an agent
   with a shell.
4. Smoke-test it from a laptop, not the droplet:

   ```sh
   curl -sS https://inference.do-ai.run/v1/messages \
     -H "x-api-key: $DO_MODEL_ACCESS_KEY" \
     -H "anthropic-version: 2023-06-01" \
     -H "content-type: application/json" \
     -d '{"model":"anthropic-claude-haiku-4.5","max_tokens":64,
          "messages":[{"role":"user","content":"Reply with the single word OK."}]}'
   ```

### Phase 1 — Hermes (reversible in one line)

5. Check whether Hermes honours `ANTHROPIC_BASE_URL`. If it does not, the model
   is configured through `hermes model` and `~/.hermes/config.yaml`'s `agent:`
   block instead — **and that block must be changed with
   `deploy/merge-hermes-config.py`, never by hand-appending**, for the duplicate-key
   reason already documented at the top of `deploy/hermes-config.yaml`.
6. Add the two exports to `deploy/run-chat.sh` beside the existing `HERMES_HOME`
   export, reading from the environment so no secret enters the repository:

   ```sh
   export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-https://inference.do-ai.run}"
   ```

   The key itself comes from the `hermes` user's own environment, not from
   `/opt/mudhorn/.env` — that file is owned by `mudhorn` and mode 600, and
   `hermes` cannot read it. **That is the user split working, not a problem to
   route around.**
7. Do `run-dream.sh` separately and second, so a failure is attributable.
8. Verify by *asking the agent*, on the Chat page, not by reading a config
   file. Same rule as the dropped-toolset finding in `CLAUDE.md`: the
   authoritative check is behavioural.
9. Watch for a fortnight. The Chat page and the Dreaming page are the surfaces
   that show it.

**Rollback:** remove the two exports (or re-run `merge-hermes-config.py` against
the backup it took). No process to restart — `run-chat.sh` execs a fresh
`hermes` per message, so the next message picks up the change. There is no
Hermes daemon; telling anyone to restart one sends them chasing a unit that does
not exist.

### Phase 2 — `dream` and `confer`, if Phase 1 held

10. Prove the structured-output substitute on `dream` first. `DreamStep` is the
    right schema to try it on: nothing waits on the call, a failure costs one
    dream, and `dreamer.py` already treats a failed step as a failed step.
11. Only then `confer`, and note in `TODO.md` that a model change here changes
    what gets adopted.

**Rollback:** revert the commit. The dreamer and the conference are separate
commands (`electrum-bot dream`, `electrum-bot confer`) on separate timers, so
neither can take the trading loop down with it.

### If the loop moves anyway

The operator may decide otherwise, and this section exists so the decision is
made with the consequences visible rather than discovered afterwards. Every one
of these is a precondition, not a nice-to-have:

- **Prove caching first.** Two identical `/v1/messages` calls a minute apart;
  assert `cache_read_input_tokens > 0` on the second. If it is zero, stop — the
  swap costs money instead of saving it.
- **Prove the structured substitute** against `ModelDecision` over at least a
  week of real cycles, comparing rejection rates against the current path.
- **Pin the model. No router.** `model` is a fixed string, never
  `router:something`.
- **Record the served model** in the `cycle_complete` line and in the audit
  event, and treat a mismatch as a failed cycle. A silent downgrade must not be
  able to look like a normal decision.
- **Keep `model_call_failed` fail-closed.** No prose fallback, no cross-provider
  retry, no second attempt on a different model.
- **`take_profit_price` is optional and `stop_loss_price` is required.** Whatever
  produces the object, `OrderProposal`'s validation is what still has to reject.
- **A test that proves it rejects**, per the repository's own convention for new
  risk-adjacent behaviour.

---

## Credentials

The box already holds Alpaca and Anthropic keys in a root-created, `mudhorn`-owned,
mode-600 `/opt/mudhorn/.env` (`bootstrap.sh` lines 75–83). A DigitalOcean
inference key is **one more secret with identical handling** — no new mechanism,
no new file, no new permission.

**What changes, exactly:**

- **`.env.example`** — one new commented-out line beside `ANTHROPIC_API_KEY`,
  with no value:

  ```
  # DigitalOcean Gradient serverless inference. Optional.
  # A MODEL ACCESS KEY, not a personal access token — scope it to the models
  # you actually use. Unset, nothing changes and the Anthropic path is used.
  DO_INFERENCE_KEY=
  DO_INFERENCE_BASE_URL=https://inference.do-ai.run
  ```

- **`src/bot/config.py`** — two optional fields on `Env`, defaulting to empty,
  in the same shape as `finnhub_api_key` and `x_bearer_token`:

  ```python
  do_inference_key: str = Field(default="", alias="DO_INFERENCE_KEY")
  do_inference_base_url: str = Field(default="", alias="DO_INFERENCE_BASE_URL")
  ```

  **Empty must mean "use Anthropic directly", and that is load-bearing.** The
  provider is chosen by whether a key is present, exactly as chat is off unless
  `DASHBOARD_CHAT_TOKEN` is set and the social feed is off unless
  `X_BEARER_TOKEN` is. A deployment that has not set it is a supported
  configuration, and the rollback for every phase above is "unset the variable".

- **`deploy/bootstrap.sh`** — nothing structural. The `.env` copy, `chown` and
  `chmod 600` already cover any new key. The one honest change is the operator
  instruction near line 158, which currently says the dreamer "needs
  `ANTHROPIC_API_KEY` in .env" and would need to name the alternative.

- **Hermes' key is separate and must stay separate.** It belongs to the `hermes`
  user's own environment, not to `/opt/mudhorn/.env`, which `hermes` cannot read
  and should not be able to. Two keys is the correct answer here, not a
  duplication to tidy away — it is the same split that keeps the broker
  credentials away from the agent's shell.

- **Settings page.** Credentials are reported as configured or not configured
  and never rendered. A DigitalOcean key inherits that unchanged. Do not add a
  row that prints a prefix, a suffix or a length.

---

## Do not do this

Things the research turned up that look reasonable and are not.

- **Do not point the trading loop at an inference router.** Automatic fallback
  to another model is the feature; on the order path it is a silent downgrade
  that still emits `cycle_complete`. See rule 4 above.

- **Do not put the DigitalOcean key in any static page.** This was written when
  a public marketing site existed under `brand/`, and it is the same reason the
  dashboard password could not go there: static files in a public GitHub repo
  make the key readable in the repository and in the page source, with no
  server anywhere to check it against. That site is gone; the rule applies to
  whatever replaces it.

- **Do not use a DigitalOcean personal access token as the inference
  credential.** A PAT controls the account — droplets, DNS, billing. A model
  access key is scopable to specific models and routers and restrictable to a
  VPC. The box that would hold it also runs an agent with a shell.

- **Do not swap the loop's provider and the loop's model in the same change.**
  Two variables, one commit, and no way to attribute a change in proposal
  quality to either. Provider first if at all, model never without a reason in
  its own commit.

- **Do not assume `output_config` passes through undocumented.** The vendor
  describes a normalising layer that "absorbs" provider quirks. An absorbed
  field does not raise. If someone wants to try it anyway, the test is not "did
  the call succeed" — it is "does an intentionally schema-violating response get
  rejected", which means constructing a prompt that would violate the schema and
  confirming the API refuses rather than the SDK.

- **Do not make DigitalOcean a second consumer of the same feed quotas.** This
  is a model provider, not a data provider; nothing here changes the Marketaux
  100/day or the Finnhub budget, and nothing about this plan is a reason to add
  a live-fetch news tool.

- **Do not remove the Anthropic path once DigitalOcean works.** The fallback
  worth having is the operator flipping one environment variable, not code that
  fails over automatically. Automatic provider failover on a trading loop is the
  silent-downgrade problem again, with two vendors instead of one model.

- **Do not read `/tools` or a config file to confirm Hermes moved.** Ask the
  agent. The `CLAUDE.md` finding about dropped toolsets applies unchanged: the
  display path and the effective path are different code.

---

## Rate limits, regions and other operational facts

- **Rate limits** are per tier: Tier 1 and 2 at **120 RPM** and 500K–750K TPM;
  Tier 3 and 4 at **600 RPM** and 800K–2M TPM; Tier 5 at **4,500 RPM**. The
  loop's 96 calls/day is four orders of magnitude inside Tier 1. Inference
  routers are separately capped at **1,000 RPM**. How an account is assigned a
  tier is not documented.
- **Prepaid balance is mandatory** for serverless inference, as above.
- **Regions:** dedicated inference lists NYC2, TOR1, ATL1, RIC1; the Agent
  Platform lists TOR1, ATL1, RIC1. **The docs do not say which region serves
  `inference.do-ai.run`**, and there is no data-residency statement. EU regions
  are described as planned. For paper trading this is a non-issue; it would not
  be for real money.
- **Batch inference** exists (10B enqueued tokens per model per account, 50,000
  requests per file, 24-hour completion window) and is irrelevant here — nothing
  in this system tolerates a 24-hour answer.
- **Per-request model selection** is trivially supported: it is the `model`
  field, on every endpoint. This part of the operator's ask needs no mechanism
  beyond what already exists.

Sources: [Inference limits](https://docs.digitalocean.com/products/inference/details/limits/),
[Inference availability](https://docs.digitalocean.com/products/inference/details/availability/),
[Inference router how-to](https://docs.digitalocean.com/products/inference/how-to/use-inference-router/).

---

## What is unverified

Stated plainly, because half of this plan's risk lives here and a confident
partial answer is the failure this repository exists to prevent.

1. **Whether `cache_control` works on `/v1/messages`.** Strongly implied by the
   response schema and the pricing table; documented only for chat completions.
   Fails silently if wrong. **Test before trusting.**
2. **Whether `output_config` / structured output works at all.** Absent from
   every published schema and how-to. Assume not.
3. **Whether `thinking: {"type": "adaptive"}` is accepted.** `thinking` is in
   the schema as an opaque object; the `adaptive` value is not documented, and
   DigitalOcean uses `reasoning_effort` where Anthropic uses
   `output_config.effort`.
4. **Whether Hermes honours `ANTHROPIC_BASE_URL`.** The first thing to check in
   Phase 1, and the difference between a two-line change and a config merge.
5. **Latency and overhead added by the proxy.** Not published. Irrelevant at a
   15-minute cadence; possibly noticeable on the Chat page.
6. **Which region serves the serverless endpoint.**
7. **Everything above was read, not run.** No request has been sent to
   `inference.do-ai.run` from this repository or from the droplet. Nothing here
   is measured.

---

## Sources

- [Inference product docs](https://docs.digitalocean.com/products/inference/)
- [Serverless Inference API reference](https://docs.digitalocean.com/reference/api/reference/serverless-inference/)
- [Serverless Inference endpoints](https://docs.digitalocean.com/products/inference/how-to/si-endpoints/)
- [Messages API / agentic workflows how-to](https://docs.digitalocean.com/products/inference/how-to/use-messages-api/)
- [Chat Completions how-to](https://docs.digitalocean.com/products/inference/how-to/use-chat-completions-api/)
- [Prompt caching how-to](https://docs.digitalocean.com/products/inference/how-to/use-prompt-caching/)
- [Supported models](https://docs.digitalocean.com/products/inference/details/models/)
- [Inference pricing](https://docs.digitalocean.com/products/inference/details/pricing/)
- [Inference features](https://docs.digitalocean.com/products/inference/details/features/)
- [Inference limits](https://docs.digitalocean.com/products/inference/details/limits/)
- [Inference availability](https://docs.digitalocean.com/products/inference/details/availability/)
- [Inference router how-to](https://docs.digitalocean.com/products/inference/how-to/use-inference-router/)
- [Model access keys how-to](https://docs.digitalocean.com/products/inference/how-to/manage-model-access-keys/)
- [Serverless Inference: a deep dive](https://www.digitalocean.com/blog/serverless-inference-deep-dive)
- [Gradient AI SDK — serverless inference](https://gradientai-sdk.digitalocean.com/getting-started/serverless-inference/)
- Anthropic list pricing, for the comparison: `docs/COSTS.md` (verified 9 Aug 2026)
