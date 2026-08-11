# DigitalOcean Agent Platform, assessed against Mudhorn

Researched **11 August 2026** against the live DigitalOcean documentation.
Everything below is cited or explicitly marked unverified. **No request has been
sent to `api.digitalocean.com` or to an agent endpoint from this repository or
from the droplet during this research** — the live-account facts quoted here
were measured by the operator and are labelled as theirs.

> ## The short version
>
> **Move Grogu. Leave Yoda and the Armorer where they are. Write no Python
> tonight.**
>
> A hosted agent cannot reach a stdio MCP server. `electrum-bot-mcp` speaks
> stdio and binds no port, and DigitalOcean's agent MCP support is for **remote
> HTTP** servers it dials from its own side. So moving Yoda to a hosted agent
> takes away `get_risk_status`, `get_positions`, `query_history` and eighteen
> others and leaves a teacher with no figures — which is the confident partial
> answer this repository exists to prevent, arriving through the transport
> layer. That is not a tuning problem; it is the whole of what Yoda does.
>
> Grogu is the opposite case and the reason this is worth doing at all. It has
> no tools **by design**, and its isolation is currently a deployment fact
> nobody can read: a `config.yaml` under `/home/hermes/dreamer` that the web
> process cannot even stat, which is why `/dreaming` carries a fallback banner
> instead of a check. As a hosted agent that isolation becomes four fields on
> one authenticated GET. **The guarantee stops being asserted and starts being
> observed** — and that is a genuine upgrade, not a lateral move.
>
> **The existing `DO_INFERENCE_*` seam is the wrong foundation for this.** It
> is right for what it was built for and it cannot carry an agent: different
> host, different path, different credential type, different API dialect. Four
> mismatches, and reusing the variable names would manufacture exactly the
> half-configuration `run-chat.sh` refuses.

---

## This is a different product from `docs/DROPLET_AI.md`

Both live under **DigitalOcean Gradient™ / Inference**, and the docs have since
been reorganised so that Agent Platform sits *inside* the Inference product tree
(`/products/gradient-ai-platform/…` now 301s to `/products/inference/…`). That
reorganisation is why the two are easy to conflate. They are not the same thing.

| | Serverless Inference (`DROPLET_AI.md`) | Agent Platform (this file) |
|---|---|---|
| What it is | A model endpoint | A hosted, configured agent |
| Host | `inference.do-ai.run` | `https://<id>.agents.do-ai.run`, one per agent |
| Path | `/v1/messages`, `/v1/chat/completions` | `/api/v1/chat/completions` |
| Dialect | Anthropic Messages **or** OpenAI | OpenAI chat-completions only |
| Credential | Model access key | **Agent endpoint access key** |
| Instructions | In your request, every call | **Stored server-side** |
| Tools | In your request | Attached server-side, persistently |
| Control plane | Independent of `api.digitalocean.com` | `api.digitalocean.com/v2/gen-ai/*` |
| Model named by | Slug (`anthropic-claude-5-sonnet`) | **UUID** (`model_uuid`) |

The last row is a real trap and worth stating on its own: agent creation takes
a **`model_uuid`**, listed from `GET /v2/gen-ai/models`, not the serving slug
the inference endpoint takes. `DO_INFERENCE_MODEL` holds a slug. The two
namespaces do not overlap and a slug pasted into `model_uuid` is a plausible
wrong value that fails somewhere other than where it was typed.

Sources: [Create agents](https://docs.digitalocean.com/products/inference/how-to/create-agents/),
[Use agents](https://docs.digitalocean.com/products/inference/how-to/use-agents/),
[Agent Inference API reference](https://docs.digitalocean.com/reference/api/reference/agent-inference/),
[GradientAI Platform API reference](https://docs.digitalocean.com/reference/api/reference/gradientai-platform/).

---

## What an agent actually is

### Creating one

`POST https://api.digitalocean.com/v2/gen-ai/agents`, authenticated with a
DigitalOcean **personal access token** (`dop_v1_…`) — the account-control-plane
credential, not a model access key.

The documented request body, in full, because the interesting fields are the
ones nobody mentions:

| Field | Notes |
|---|---|
| `name`, `description` | `description` is explicitly "not used in inference" |
| `instruction` | **This is where a soul would go.** One string, stored server-side |
| `model_uuid` | Foundation model, by UUID |
| `project_id`, `region`, `tags`, `workspace_uuid` | Placement |
| `knowledge_base_uuid[]` | RAG |
| **`mcp_servers[]`** | `server_url`, `server_label`, `authorization`, `headers`, `allowed_tools[]` |
| `anthropic_key_uuid` | **Bring your own Anthropic key** |
| `open_ai_key_uuid`, `model_provider_key_uuid` | Same, other providers |
| `model_router_uuid`, `router_preset_slug` | **The auto-fallback router, as a first-class field** |
| `reasoning_effort`, `thinking_token_budget` | Sampling |
| `web_search_enabled`, `web_fetch_enabled` | **Built-in tools, off unless set** |

Every field is documented `optional`, including `name` and `model_uuid`, which
is a documentation artefact rather than a real contract — the how-to says a
name, a model, instructions, a project id and a region are all required.
Treat the how-to as authoritative and the schema's `optional` markers as noise.

**Functions are a separate call**, not part of create:
`POST /v2/gen-ai/agents/{uuid}/functions`, taking `function_name`,
`description`, `input_schema`, `output_schema`, `faas_namespace`, `faas_name`.

**Guardrails are a separate call** too:
`POST /v2/gen-ai/agents/{uuid}/guardrails`.

**Agent routing is a separate call**:
`POST /v2/gen-ai/agents/{parent}/child_agents/{child}`. Remember this one — it
is the transitive hole in section 3.

### Invoking one

Each agent gets its own hostname, returned as `deployment.url`:

```
https://qdvqcnyeeqt7td46j26foyxx.agents.do-ai.run
```

```shell
curl -X POST "$AGENT_ENDPOINT/api/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $AGENT_ACCESS_KEY" \
  -d '{"messages":[{"role":"user","content":"…"}],
       "stream": false,
       "include_functions_info": true,
       "include_retrieval_info": true,
       "include_guardrails_info": true}'
```

Four things about that request are load-bearing here.

- **The credential is an agent endpoint access key**, made with
  `POST /v2/gen-ai/agents/{uuid}/api_keys` and shown once. The API reference
  says it plainly: it "is not interchangeable with DigitalOcean OAuth tokens
  (`dop_v1_*`, …), which are used with Serverless Inference and the
  control-plane API". So this is a **third** credential type on the box,
  alongside the Alpaca keys and the Anthropic key, and it is scoped to one
  agent — which is the good half.
- **`model` is vestigial.** The Python example in the how-to passes
  `model = "n/a"`. The agent's model is server-side. The API reference marks
  `model` required with example `llama3-8b-instruct`, which contradicts the
  how-to; the how-to is newer and is the one with a working example.
- **The three `include_*_info` flags are the observability.** They are what
  makes a response say which functions ran, which knowledge chunks were
  retrieved and which guardrails fired. **Set all three, always.** They are the
  only per-response record that exists — see the deprecation in section 3.
- **The path is ambiguous between two documents.** The how-to says
  `/api/v1/chat/completions`; the API reference says `/v1/chat/completions`
  with a required `?agent=true`. Both are current. **Unverified which one an
  agent actually answers on**, and it is a two-minute check once an endpoint
  exists.

Private is the default visibility, and private still requires the access key
for direct requests. Public exists only to enable the embeddable chatbot
widget.

---

## Bring your own Anthropic key — and why it matters more than it looks

The operator observed `/v2/gen-ai/anthropic_api_keys` returning 200. The
**documented** path is `/v2/gen-ai/anthropic/keys` (list, create, get, update,
delete, plus `GET …/keys/{uuid}/agents` to list agents using a key). Both
apparently exist; only one is documented. **Use the documented one** and treat
the other as an undocumented alias that can be withdrawn without notice.

```shell
curl -X POST -H "Authorization: Bearer $DIGITALOCEAN_TOKEN" \
  "https://api.digitalocean.com/v2/gen-ai/anthropic/keys" \
  -d '{"api_key":"sk-ant-…","name":"mudhorn-dreamer"}'
```

Then `anthropic_key_uuid` on agent create.

**What it does, verbatim from DigitalOcean:** "For commercial models like OpenAI
or Anthropic Claude models, you can bring your own API key. **We bill usage
directly to your model account.**" And on the pricing page: "When using
Anthropic commercial models with your own model API keys, billing is handled
directly by Anthropic at the provider's rates."

**Why this is probably the thing that unblocks tonight.** The limits page
states, flatly:

> All tiers, except Tier 1 and Tier 2, have access to all models. **Tier 1 and
> Tier 2 do not have access to any Anthropic models and OpenAI models** (except
> `gpt-oss-120b` and `gpt-oss-20b`).

That is almost certainly the source of the operator's
`"this model is not available for your subscription tier"`. The account is new,
so it is Tier 1, and Tier 1 has no Anthropic access **through DigitalOcean's
own billing relationship**. A registered Anthropic key changes whose commercial
relationship is being used.

**And that inference is exactly the kind of thing this repository refuses to
state as fact.** Two readings survive the documentation:

1. The tier gate is about *DigitalOcean-billed* access, and a BYO key routes
   around it. This is what "we bill usage directly to your model account"
   implies, and it is why the feature exists.
2. The tier gate is about *model availability* per se, and a BYO key changes
   only the invoice.

**They are distinguishable with one API call**, and that call is the first
thing to make tonight — see "Tonight, in order", step 2. Do not build anything
on reading 1 before it has been measured.

Two more facts about keys that bite:

- **The first use of any provider's model requires accepting that provider's
  terms in the Control Panel**, and the operator has already established there
  is no API for it. The `agreement` object is exposed on the model as metadata
  and is read-only. So there is a mandatory browser step and it cannot be
  scripted.
- **Deleting a key breaks every agent using it.** The docs warn: "Agents
  without a model cannot process requests." Change the agents first.

Sources: [Manage partner provider keys](https://docs.digitalocean.com/products/inference/how-to/manage-model-provider-keys/),
[Limits](https://docs.digitalocean.com/products/inference/details/limits/),
[Pricing](https://docs.digitalocean.com/products/inference/details/pricing/).

---

# 1. Which soul, if any, should become a hosted agent

**Grogu. Not Yoda. Not the Armorer. And the reasoning is different in each of
the three cases, which is why "move the souls to DO" is not one decision.**

### Yoda — no, and this one is not close

Yoda's job is to answer about the account and then say what the number *means*.
Every figure in that comes from the MCP server: `get_risk_status`,
`get_positions`, `get_trades`, `get_journal_stats`, `get_stand_down_status`,
`get_recent_news`, `get_recent_decisions`, `query_history`, `review_watches`,
`search_news`, `list_dreams`, `dream_vault_status` and the rest — twenty-odd
tools reached over **stdio**, from a process `run-mcp.sh` starts with the right
working directory.

A hosted agent cannot dial any of them. DigitalOcean's MCP support attaches
**remote HTTP** servers by `server_url`, which DigitalOcean's own infrastructure
connects out to. `electrum-bot-mcp` ends in `server.run(transport="stdio")`. It
listens on no port and has no URL.

So moving Yoda produces a fluent teacher that cannot read the account. That is
not a degraded Yoda; it is the exact failure already recorded in `CLAUDE.md`
under the `.gitignore` finding — *"it answered a question about account risk
with the limits and no live state: a confident partial answer, which is the
exact failure this project exists to prevent, arriving through the plumbing
rather than the model."* Doing it deliberately, having read that paragraph,
would be worse than doing it by accident.

The two ways to give the tools back are both larger than the move:

- **Publish the MCP server over HTTP.** That puts `place_order` on a URL
  DigitalOcean can reach. Section 2 says why not.
- **Reimplement the read-only tools as DigitalOcean Functions.** Twenty
  functions, each a web function in a FaaS namespace, each needing a route to
  the droplet's data, each a second implementation of a query that already
  exists. And function schemas may only use `string`, `boolean` or `number` —
  no nested objects — so `get_risk_status`'s payload would have to be
  flattened or stringified, which is a second place for the same figures to be
  wrong.

### The Armorer — no, and for a reason that is nearly the opposite

The Armorer would technically work. It needs no MCP tools: `render_briefing`
computes the limits and their consequences in **Python**, locally, and the
figures travel in the prompt — precisely so the model never derives them. A
hosted agent receives that same briefing as a user message and answers the same
way. And the applying half stays local regardless: `WrapperApplier` shells out
to `apply-settings.sh` on the box, so a hosted agent returning text changes
nothing about who writes `config/rules.yaml`.

The blocker is the rails. `souls/armorer.md` is root-owned on the droplet
(`bootstrap.sh` chowns `souls/` to `root:root`) specifically so the service
account cannot edit the character that restrains it. The Armorer is **the only
route to changing `config/rules.yaml` from the interface**. If any soul's rails
should stay hardest to move, it is that one — and moving them into an
`instruction` field moves them somewhere a DigitalOcean PAT can rewrite.

Not "never". Not tonight, and not first.

### Grogu — yes, and it is the case worth making

Everything that makes Yoda a bad candidate makes Grogu a good one:

- **It has no tools by design.** `run-dream.sh` exists precisely so the
  dreamer's registry does not contain the bot's MCP server. Nothing is lost by
  moving to a place where tools are hard to attach; that is the goal.
- **It quotes no figures.** `build_prompt` deliberately never shows it profit
  and loss; what it gets is events and headlines. There is no measured number
  whose provenance a transport change could damage.
- **Its verification story does not move.** `Hop.checked`, the `Verification`
  badge and `weakest_hop` are computed in `dreaming.py` over the returned
  object. Where the tokens came from does not touch any of it.
- **And its isolation gets *better*.** Today `/dreaming` cannot check whether
  the dreamer's Hermes really lacks the MCP server — the config is under
  `/home/hermes/dreamer`, mode 0700, and the web process runs as `mudhorn`. So
  the page reports which *binary* answered and carries a banner when the
  isolated one is absent. Honest, and weaker than anyone would like. A hosted
  agent replaces that with `GET /v2/gen-ai/agents/{uuid}` returning
  `functions: []`, `mcp_servers: []`, `child_agents: []`,
  `web_search_enabled: false`, `web_fetch_enabled: false`. Section 3.

One real cost, stated plainly: **`claude.dream` and the Hermes dreamer panel
are two different things and only one of them moves.** `electrum-bot dream` is
a Python Anthropic SDK call in `dreamer.py`; the panel on `/dreaming` is a
Hermes turn through `run-dream.sh`. A hosted agent replaces the *panel*. The
daily dream that actually fills the shelf is untouched, and pointing that at an
agent endpoint is a different and much larger change — it uses structured
output, which section "What agents cannot do" says is unavailable.

### What the "root-owned souls" property becomes

State it exactly, because it is the thing the operator is actually trading.

**Today:** `souls/*.md` are files on the droplet owned by `root:root`. The
`mudhorn` web process reads them and cannot write them. The `hermes` account
that runs the model cannot write them either. The character that restrains an
agent is not editable by the agent, by the process driving it, or by anything a
signed-in dashboard user can reach. Enforced by Unix permissions, which do not
fail silently.

**Hosted:** the character is a string in DigitalOcean's database, editable by
anyone holding a DigitalOcean PAT for this account, over the public internet,
with no shell on the droplet required.

That is genuinely weaker in one direction and genuinely stronger in another,
and both halves should be said:

- **Weaker: the blast radius of a leaked PAT now includes the agent's
  character.** A PAT already controls droplets, DNS and billing, so anyone
  holding one could already do worse — but "worse" and "differently" are not
  the same, and a rewritten instruction is a change nothing on the box would
  notice. The mitigation is the ordinary one and it is not new: the PAT does
  not go on the trading box. It is used from the operator's laptop to create
  and update the agent; the box holds only the agent **endpoint access key**,
  which can invoke the agent and cannot reconfigure it.
- **Stronger: every edit is versioned and attributable.**
  `GET /v2/gen-ai/agents/{uuid}/versions` returns, per version, the full
  `instruction` text, `created_by_email`, `trigger_action`, `version_hash` and
  `currently_applied`, plus the attached functions, guardrails and knowledge
  bases at that version. `PUT …/versions` rolls back. The droplet has no
  equivalent: `souls/grogu.md` edited by root at 3am leaves a mtime and
  nothing else.

**So the honest framing is: the rails move from "cannot be edited by the thing
they restrain" to "can be edited only by the account owner, and every edit is
recorded, attributed and reversible."** For a speculative-idea generator with no
tools and no order path, that is an acceptable trade. For the Armorer, which is
the only route to a limit change, it is not — which is the whole reason the
three souls get three different answers.

**And `souls/grogu.md` must stay in the repository as the source of truth.**
The agent's `instruction` is a *deployment* of that file, the same way
`config/rules.yaml` on the droplet is a deployment of the one in git. A drift
check is one GET and a string compare; see "Tonight, in order", step 6.

---

# 2. What must not move, and what stops an order path appearing

### `RiskGate.evaluate` must stay local, deterministic Python

Non-negotiable and unchanged by any of this. The reason is already written down
and applies verbatim: *a gate that can fail is a gate that can fail open.* A
network call inside the gate turns a rejection into a timeout, and a timeout
into whatever the caller does next.

Nothing in the Agent Platform tempts this except one thing, and it is worth
naming so nobody proposes it as a convenience: **guardrails are not the risk
gate and must never be described as one.** DigitalOcean's guardrails are three
fixed classifiers — sensitive data, jailbreak, content moderation — whose
detection rules "cannot be customised", billed per token, that overwrite the
agent's *output text* when triggered. They know nothing about `|entry − stop| ×
qty`. Attaching one is fine. Putting the word "guardrail" anywhere near the
operator's four rules in a commit message is how somebody later assumes a check
exists that does not.

### Could an order path end up reachable from a hosted agent?

**Yes, by three routes, and all three require somebody to build them
deliberately. None is a default and none is one flag away.**

**Route A — DigitalOcean Functions.** `POST /v2/gen-ai/agents/{uuid}/functions`
attaches a function the model may call. A function is a **DigitalOcean Functions
web function**, i.e. code in a FaaS namespace on this account. Nothing stops
someone writing one that HTTP-POSTs the droplet and calls `place_order`.

What stops it today: no such function exists, no such namespace is provisioned,
and the droplet exposes no HTTP order path to call. Note carefully what is
*not* the stopper — the risk gate would still run, because `place_order` in
`mcp_server.py` runs it first. The problem is not that the gate would be
bypassed; the problem is that **an order could be initiated from a public agent
endpoint by whoever holds the access key**, which is a different threat surface
from an operator at a dashboard behind a password.

**Route B — remote MCP.** `mcp_servers[]` on the agent takes a `server_url`
that DigitalOcean dials out to, with an `authorization` header and an optional
`allowed_tools` allowlist. If the repo's MCP server were ever published over
HTTP, this becomes one field. `place_order`, `close_position`, `tighten_stop`,
`adopt_dream` and `reset_trading_session` would all be one `allowed_tools`
omission from reachable.

What stops it today, and this is the strong one: **`electrum-bot-mcp` speaks
stdio.** `main()` is `server.run(transport="stdio")`. It binds no socket, it
has no URL, and Hermes starts it as a child process through
`sudo -u mudhorn run-mcp.sh`. There is nothing for `server_url` to point at.
That is a structural property, not a configuration — the same distinction
`CLAUDE.md` draws about Hermes toolsets, where *"a dropped toolset is a line in
a YAML file and it fails silently, whereas a binary that is not installed
cannot be reached by a bad merge."*

**So: never publish this repository's MCP server over HTTP.** Add it to the
list beside "do not install `alpacahq/cli` on the box". If a remote MCP surface
is ever genuinely wanted, it is a *separate*, read-only server with its own
process, its own port, its own auth and an explicit tool list that omits every
mutating tool — not a transport flag on the existing one.

**Route C — agent routing.** A child agent's tools are reachable through its
parent. An agent with nothing attached, routed to an agent that has the MCP
server, is not isolated. Section 3.

### The three rules to write down

1. **The MCP server stays stdio.** No HTTP transport, no reverse proxy in front
   of it, no tunnel.
2. **A hosted agent gets no functions and no MCP servers**, and that is checked
   rather than intended — see section 3.
3. **`web_search_enabled` and `web_fetch_enabled` stay `false`.** They are off
   by default and turning one on is a one-word edit. `docs/HANDOFF.md` already
   records the rule this would break: *the model reads rendered, attributable
   text, never raw pages.* DigitalOcean's built-in web fetch is raw pages, into
   a prompt, with no attribution layer, on a model whose product is speculation.
   A headline that could restructure a markdown document is already a known
   failure here; an arbitrary fetched page is that with no ceiling.

---

# 3. The dreamer's isolation, as an observable property

**It becomes four fields plus a transitive walk, and yes — it can be observed
rather than asserted. That is the single best thing about this migration.**

`GET https://api.digitalocean.com/v2/gen-ai/agents/{uuid}` returns, among much
else:

| Field | Isolated value |
|---|---|
| `functions[]` | `[]` |
| `mcp_servers[]` | `[]` |
| `child_agents[]` | `[]` |
| `web_search_enabled` | `false` |
| `web_fetch_enabled` | `false` |

**All five, and the fifth exists because of the fourth.** `child_agents` is why
this is a walk and not a lookup: an agent with no tools of its own, routed to an
agent that has the MCP server, reaches the MCP server. The check has to recurse,
and an agent it cannot resolve must count as **not isolated**, not as a leaf.
Fail closed, same as `grants.resolve_granted_symbols` answering `{}` on any
failure.

Compare that with today. `/dreaming` cannot inspect the dreamer's Hermes
registry at all — `/home/hermes/dreamer` is 0700 and the web process is
`mudhorn`. So the page reports *which binary answered* and shows a banner when
the isolated one is missing. It is honest about the gap, and the gap is real:
a `run-dream.sh` pointing at an instance whose `config.yaml` had gained an
`mcp_servers` entry would look identical from the page.

**So the property genuinely strengthens.** Today: *"we invoke a wrapper that is
supposed to run an instance with no broker tool."* Hosted: *"we asked, and it
reported no functions, no MCP servers, no child agents and no web tools."*

### Three things that are still asserted, and must be labelled as such

The point of writing this down is that "observable" is not the same as "fully
observable", and the difference is where a future overclaim would live. The
Dreaming page's banner has already been wrong once — it read "no route to the
broker", which was true of the dream records and false of the chat panel beside
them — so this is a repeat offence waiting to happen.

- **`allowed_tools` is a claim about DigitalOcean's client, not the server.**
  It restricts which tools DigitalOcean will call. It does not restrict what the
  MCP server exposes. Irrelevant while `mcp_servers` is empty; it stops being
  irrelevant the moment it is not.
- **The instruction is a rail with no enforcement.** `souls/grogu.md` asks the
  dreamer not to propose orders. That is prose, and it was prose before too —
  the structural guarantee has always been the absence of the tool, not the
  sentence. Unchanged, and worth restating so nobody reads "hosted agent" as
  "stronger prose".
- **Whether the response's tool-use annotations are complete is unverified.**
  `include_functions_info: true` yields `functions.called_functions[]`. Whether
  a *built-in* tool (web search, web fetch, knowledge retrieval, MCP) appears
  there or only in a separate annotation block is not stated. So a "no tools
  were used" reading off one response is weaker evidence than the configuration
  GET, and the configuration GET is the one to build a check on.

### And the observability that was taken away

**Insights, agent tracing and conversation logs were deprecated for all agents
on 30 June 2026.** What remains is Agent Metrics (tokens in/out, requests,
latency, throughput; up to 15 minutes stale) and Runtime Logs — both **Control
Panel only**, and the limits page says outright: *"You cannot view agent traces
or conversation logs."* DigitalOcean also states it does not store inputs or
outputs at all.

For this repository that is closer to good news than bad. It means the
transcript exists in exactly one place — `audit/*.jsonl` and the Dreaming
page — and there is no second copy to disagree with it. But it also means
**nothing recovers a dream turn that the local side failed to record**, so a
hosted dreamer must write its own record before it is trusted, exactly as the
current one does.

---

# 4. Cost and failure modes

### Money

Agent creation is free. Guardrails are cheap and priced per token:

| Guardrail | Price |
|---|---|
| Content Moderation | $0.20 per 1M tokens |
| Jailbreak Detection | $0.20 per 1M tokens |
| Sensitive Data Detection | $0.34 per 1M tokens |

Model usage is billed at the same rates as serverless — which
`docs/DROPLET_AI.md` already establishes are **Anthropic's exact list prices**,
with no reseller discount or markup. On a dreamer panel answering a handful of
messages a day, the whole thing is well inside the noise of the $2.60–$13.00 a
year `config.py` already records for the daily dream.

**With a BYO Anthropic key, DigitalOcean bills nothing for tokens** — they go
on the operator's existing Anthropic invoice. Guardrails, functions and
knowledge bases would still be DigitalOcean charges.

### The prepaid balance, and the failure it invents

This is the one to read twice, because it is a new way for things to go dark
and it is shaped exactly like the failure the tailnet banner exists for.

Serverless inference is **prepaid only**. And:

> Your prepayment balance applies to your **entire account** (your current
> team), not only Serverless Inference. Usage charges from all DigitalOcean
> products, such as Droplets or Managed Databases, draw from the same balance.
>
> Serverless Inference is currently the **only** product DigitalOcean suspends
> when your balance reaches $0. Other products continue running as usual, even
> if their charges are what depleted the balance.

**The droplet that runs the trading bot is on this account.** So the failure
mode is:

> The droplet's own monthly charges draw the shared balance to $0.
> DigitalOcean suspends inference. **The trading loop carries on perfectly
> normally** — it calls Anthropic directly, it is not affected — while every
> hosted agent surface stops answering. Service green, journal filling, orders
> still going to the broker, and the only symptom is an agent that returns an
> error.

That is the tailnet expiry described in `CLAUDE.md`, in a new place, **with the
ten-day warning removed.** There is no notice period. The balance crosses zero
and the surface stops.

Three mitigations, in order of how much they actually help:

1. **Use a BYO Anthropic key.** If reading 1 in the BYO section holds, the
   tokens are billed by Anthropic and this coupling does not exist for model
   usage. **Whether a $0 DigitalOcean balance still suspends an agent that
   bills its tokens elsewhere is UNVERIFIED**, and it is the single most
   valuable unverified item in this document. Ask support, or measure it.
2. **Enable auto-reload.** Thresholds of $5/$10/$25 or custom; reload amounts of
   $5/$25/$100 or custom. This is the documented answer and it converts a
   silent outage into a card charge.
3. **A separate DigitalOcean team** keeps a separate balance — but only for
   teams outside an organisation, and it means a second team to administer for
   a dreamer panel. Not worth it here.

**Do not build a balance check into anything that gates.** It is a network call
and the rules about those are already written. If a balance reading is ever
surfaced, it belongs beside the tailnet status on the dashboard — a fact
reported to the operator, on the surface that is about to disappear, with
`unknown` as a first-class answer when the check itself failed.

### Rate limits

| Tier | Agents | Knowledge Bases | RPM | TPM |
|---|---|---|---|---|
| 1 | 15 | 15 | 120 | 500K–750K |
| 2 | 60 | 60 | 120 | 500K–750K |
| 3–4 | 60 | 60 | 600 | 800K–2M |
| 5 | 120 | 120 | 4,500 | 3.5M–70M |

A dreamer panel answering a person is four orders of magnitude inside Tier 1.
Two further limits are worth knowing because they are not rate limits and will
not announce themselves: **"Teams have a daily limit on the number of agents
they can create"** (unquantified), and **"Teams have limited number of tokens
available for agents to use… each agent on your team draws tokens from that
model's amount"** — a per-model team allocation with no published figure.

### The router, which is worse here than in `DROPLET_AI.md`

`model_router_uuid` and `router_preset_slug` are **fields on agent create**.
That makes automatic fallback a property of the agent, set once, invisible
afterwards — where in the inference path it was at least a per-request choice a
wrapper could refuse.

`run-chat.sh` and `run-dream.sh` both already refuse a `DO_INFERENCE_MODEL`
containing `router`, with the right reasoning stated in place: *"which model
answered a Hermes turn is not visible from here… A downgrade nobody can observe
is worse than a failed call."* The same rule applies, harder:

- **Set neither `model_router_uuid` nor `router_preset_slug`.** Pin
  `model_uuid`.
- **Whether the served model is readable off an agent response is
  UNVERIFIED.** The OpenAI response shape carries a `model` field, but the
  request-side `model` is documented as ignored (`"n/a"`), so what the response
  echoes is unknown. Until somebody reads one back, assume it is not observable
  and treat the router as unusable here on that basis alone.
- **A configured router is at least visible on the configuration GET**, so the
  isolation check in section 3 should assert both fields are absent. Cheap, and
  it catches a control-panel edit.

---

# 5. The migration, and why the existing seam is the wrong foundation

### The `DO_INFERENCE_*` seam does not carry an agent

It is a good seam. It is a seam for a **model endpoint**, and an agent is not
one. Four mismatches, any one of which is fatal:

| | The seam sets | An agent needs |
|---|---|---|
| Host | `ANTHROPIC_BASE_URL=https://inference.do-ai.run` | `https://<id>.agents.do-ai.run`, per agent |
| Path | Client appends `/v1/messages` | `/api/v1/chat/completions` |
| Credential | Model access key, in `ANTHROPIC_API_KEY` | Agent endpoint access key, explicitly not interchangeable |
| Dialect | Anthropic Messages | OpenAI chat-completions |

The dialect row is the one that ends it. `run-chat.sh` and `run-dream.sh` work
by telling an Anthropic-speaking client to speak Anthropic somewhere else.
There is no arrangement of `ANTHROPIC_BASE_URL` that makes an Anthropic client
talk to an OpenAI chat-completions endpoint at a different path. **Whether
Hermes honours `OPENAI_BASE_URL` or any OpenAI-compatible provider setting is
UNVERIFIED** — it is not in this repository, it lives in `hermes model` and
`~/.hermes/config.yaml` on the box, and it is the first thing to check in step 4
below.

**So reusing `DO_INFERENCE_KEY` for an agent access key would be precisely the
failure those wrappers were written to prevent**: a half-configuration that
reads as a working one. If an agent variable is ever needed it is a new,
differently-named one — `DO_AGENT_ENDPOINT` and `DO_AGENT_ACCESS_KEY` — living
in the same per-instance `inference.env`, with the same rule that an empty key
means today's behaviour, and refusing rather than falling back on a partial
configuration.

`inference.env` itself — one file per Hermes instance, owned by `hermes`, mode
600, never in `/opt/mudhorn/.env` — **is** the right foundation and needs no
change. It is already the mechanism that makes per-agent routing real, and the
dreamer already has its own copy.

### What agents cannot do, which bounds the whole plan

- **No structured output.** A full read of the Agent Inference API reference
  returns no `response_format`, no `json_schema`, no `output_config`. This is
  the same blocker `DROPLET_AI.md` records for serverless, in a second place.
  It means `claude.propose`, `claude.dream` and `claude.confer` — all three of
  which use structured output — cannot move to an agent endpoint at all, today.
  Only the **Hermes conversational surfaces** are candidates, which is exactly
  the split `DROPLET_AI.md` already drew.
- **No account tools without publishing something.** Section 2.
- **No conversation history from DigitalOcean.** Deprecated 30 June 2026.
- **Region is TOR1, ATL1 or RIC1 only.** No European region, no data-residency
  statement. Fine for paper money; would not be for real money.

### Tonight, in order

Every step is reversible and no step is a prerequisite for the trading loop.
The loop is untouched by all of this from beginning to end.

**0. Nothing in `src/` changes tonight.** That is the recommendation, not an
omission. No agent exists yet; a Python module to audit agents that do not
exist is speculative machinery, and the useful shape of it is not knowable
until step 3 has produced a real response body to read. The work tonight is
account setup, one measurement, and one wrapper.

**1. Accept the Anthropic terms in the Control Panel.** Mandatory, browser
only, no API. Do this first because everything after it fails without it and
the failure will look like something else.

**2. Register the operator's Anthropic key, and settle the tier question.**

```shell
curl -sS -X POST -H "Authorization: Bearer $DIGITALOCEAN_TOKEN" \
  -H "Content-Type: application/json" \
  "https://api.digitalocean.com/v2/gen-ai/anthropic/keys" \
  -d '{"api_key":"'"$ANTHROPIC_API_KEY"'","name":"mudhorn-dreamer"}'
```

Keep the returned `api_key_info.uuid`. Then create a throwaway agent on an
Anthropic model *with* `anthropic_key_uuid` set, and one *without*. **If the
first succeeds and the second is refused for tier, reading 1 is confirmed and
BYO key is the route.** If both are refused, agents on Anthropic models are not
available on this account tonight and the honest answer is to stop here and say
so — a DigitalOcean-hosted open-source model is a different proposal and a
different conversation, not a fallback to slide into.

Destroy the throwaways. `DELETE /v2/gen-ai/agents/{uuid}`.

**3. Create the dreamer agent.** Instruction is `souls/grogu.md`, verbatim.
Nothing else attached.

```shell
curl -sS -X POST -H "Authorization: Bearer $DIGITALOCEAN_TOKEN" \
  -H "Content-Type: application/json" \
  "https://api.digitalocean.com/v2/gen-ai/agents" \
  -d @- <<JSON
{
  "name": "mudhorn-grogu",
  "instruction": $(python3 -c 'import json,sys; print(json.dumps(open("souls/grogu.md").read()))'),
  "model_uuid": "<uuid from GET /v2/gen-ai/models?usecases=MODEL_USECASE_AGENT>",
  "anthropic_key_uuid": "<from step 2>",
  "project_id": "<project>",
  "region": "tor1",
  "tags": ["mudhorn", "dreamer"]
}
JSON
```

No `knowledge_base_uuid`. No `mcp_servers`. No `model_router_uuid`. No
`router_preset_slug`. `web_search_enabled` and `web_fetch_enabled` left unset,
which is `false`.

Then read it straight back and confirm the five isolation fields and the two
router fields, by eye tonight and by code later:

```shell
curl -sS -H "Authorization: Bearer $DIGITALOCEAN_TOKEN" \
  "https://api.digitalocean.com/v2/gen-ai/agents/$AGENT_UUID" \
| python3 -c 'import json,sys; a=json.load(sys.stdin)["agent"]; print({k:a.get(k) for k in ("functions","mcp_servers","child_agents","web_search_enabled","web_fetch_enabled","model_router_uuid","router_preset_slug")})'
```

**4. Find out whether Hermes can talk to it at all.** This is the step that
decides whether tonight ends in a working dreamer panel or in a documented
finding, and it cannot be skipped or assumed. The agent speaks OpenAI
chat-completions at `/api/v1/chat/completions` with a bearer key; Hermes is
configured for Anthropic. Check `hermes model` and the `agent:` block in
`/home/hermes/dreamer/config.yaml` — **and change it with
`deploy/merge-hermes-config.py`, never by appending**, for the duplicate-key
reason already documented.

If Hermes cannot be pointed at an OpenAI-compatible endpoint, **that is a
legitimate place to stop.** The agent exists, is isolated, is verifiable, and
the panel keeps working exactly as it does today. Say so and leave it.

**5. If it can: create the endpoint access key and wire one wrapper.**

```shell
curl -sS -X POST -H "Authorization: Bearer $DIGITALOCEAN_TOKEN" \
  -H "Content-Type: application/json" \
  "https://api.digitalocean.com/v2/gen-ai/agents/$AGENT_UUID/api_keys" \
  -d '{"agent_uuid":"'"$AGENT_UUID"'","name":"dreamer-panel"}'
```

Shown once. It goes in `/home/hermes/dreamer/inference.env`, owned by `hermes`,
mode 600, as `DO_AGENT_ENDPOINT` and `DO_AGENT_ACCESS_KEY` — **new names, not
the `DO_INFERENCE_*` ones.** The PAT does not go on the box at all.

`run-dream.sh` gains a branch in the same shape as the existing one and with
the same discipline: refuse rather than fall back, refuse a non-`https://`
endpoint, refuse a key with no endpoint, and print which arrangement it
*requested* while saying plainly that it cannot confirm what answered.

**Do `run-dream.sh` only. `run-chat.sh` is not touched tonight** — Yoda and the
Armorer stay on Anthropic, for the reasons in section 1, and doing one wrapper
at a time is what makes a failure attributable.

**6. Verify behaviourally, not by reading config.** Open `/dreaming` and ask
it something. Then ask it to do something only a tooled agent could do and
confirm it cannot. Same rule as the dropped-toolset finding: the display path
and the effective path are different code, so *ask the agent*.

**7. A drift check, when there is something to check.** Once an agent exists,
one GET and a string compare answers "does the deployed instruction still match
`souls/grogu.md`?" That is worth having and it is worth writing **after** step 3
has produced a real response body, not before. It belongs beside `tailnet.py`
in shape: pure, offline where it can be, `unknown` as a first-class answer, and
gating nothing.

### Rollback

Complete, at every step, and none of it touches the trading loop:

- **Step 5:** blank `DO_AGENT_ACCESS_KEY` in
  `/home/hermes/dreamer/inference.env`. The next message picks it up — there is
  no Hermes daemon and the wrapper execs a fresh process per turn. Telling
  anyone to restart one sends them chasing a unit that does not exist.
- **Step 3:** `DELETE /v2/gen-ai/agents/{uuid}`, or roll the instruction back
  with `PUT /v2/gen-ai/agents/{uuid}/versions`.
- **Step 2:** `DELETE /v2/gen-ai/anthropic/keys/{uuid}` — but change any agent
  using it first, or that agent has no model and stops answering.
- **Everything:** `souls/grogu.md` is unchanged in git throughout, so the
  Hermes path is always exactly one blanked variable away.

---

## Things that look reasonable and are not

- **Do not put the chatbot embed script on `brand/`.** Setting an agent's
  endpoint public produces a `<script>` snippet with `data-agent-id` and
  `data-chatbot-id`, ready to paste into a static site. `brand/` is the shop
  window where **every figure is invented**, it has no server, and it recently
  had a decorative sign-in form removed for implying it held something. A live
  model on it would be the opposite problem: something real, on the surface
  built to hold nothing real. If it ever happens, allowed-domains are a
  prerequisite, not a follow-up — and the answer today is no.
- **Do not describe DigitalOcean guardrails as risk controls.** Three fixed
  content classifiers with uncustomisable rules. They do not know what a stop is.
- **Do not point the loop, the dreamer command or the conference at an agent
  endpoint.** All three use structured output; agents do not offer it.
- **Do not give a hosted agent `web_search_enabled` or `web_fetch_enabled`
  "just to see".** Off is the default and turning it on is a one-word edit that
  nothing downstream would notice.
- **Do not put a DigitalOcean PAT on the trading box.** It creates and
  reconfigures agents, and it controls droplets, DNS and billing. The box needs
  only a per-agent endpoint access key, which can invoke and cannot configure.
  This is the same reasoning `DROPLET_AI.md` gives for preferring a model
  access key, one step further along.
- **Do not reuse `DO_INFERENCE_*` for agent settings.** Four mismatches, and
  the seam's whole value is that a half-configuration refuses instead of
  quietly working.
- **Do not remove the Hermes path once an agent works.** The fallback worth
  having is the operator blanking one variable, not code that fails over. A
  provider that fails over automatically is the silent-downgrade problem with
  two vendors instead of one model.
- **Do not let a hosted agent become the record.** DigitalOcean stores no
  inputs or outputs and has deprecated conversation logs. The transcript exists
  where this repository puts it or it does not exist.

---

## What is unverified

Stated plainly, because a stated gap is worth more than a confident guess, and
because two of these decide whether tonight works at all.

1. **Whether a BYO Anthropic key gives a Tier 1 account access to Anthropic
   models on agents.** The strongest reading of the docs says yes; the limits
   page can be read the other way. **One API call settles it — step 2.** The
   most valuable unknown here.
2. **Whether a $0 DigitalOcean prepaid balance suspends an agent whose tokens
   are billed to the operator's own Anthropic account.** Documented for
   serverless; not addressed for BYO-key agents.
3. **Whether Hermes can be pointed at an OpenAI-compatible endpoint at all.**
   Not in this repository. Decides whether step 5 exists.
4. **The invocation path.** `/api/v1/chat/completions` (how-to, dated 13 Jul
   2026) versus `/v1/chat/completions?agent=true` (API reference, generated
   7 Aug 2026). Both current, only one presumably right.
5. **Whether the served model is readable off an agent response.** Determines
   whether a router could ever be made observable. Assume not.
6. **Whether a caller-supplied `role: "system"` message reaches the model
   alongside the agent's stored `instruction`, or replaces it, or is ignored.**
   The schema accepts the role. Matters if a briefing is ever passed the way
   `render_briefing` passes one today.
7. **Whether `include_functions_info` reports built-in tool use** (web search,
   web fetch, knowledge retrieval, MCP) or only attached function routes.
8. **Which of `/v2/gen-ai/anthropic/keys` and `/v2/gen-ai/anthropic_api_keys`
   is canonical.** The operator measured the second as live; only the first is
   documented.
9. **The daily agent-creation limit and the per-model team token allocation.**
   Both are stated as existing with no figure attached.
10. **Latency of an agent endpoint versus a direct Anthropic call.** Not
    published. Irrelevant to the loop, possibly noticeable on a chat panel.
11. **Everything above was read, not run.** No request has been made to
    `api.digitalocean.com` or to any agent endpoint from this repository or
    from the droplet.

---

## Sources

- [Create agents](https://docs.digitalocean.com/products/inference/how-to/create-agents/)
- [Use agents in your applications](https://docs.digitalocean.com/products/inference/how-to/use-agents/)
- [Route functions in agents](https://docs.digitalocean.com/products/inference/how-to/route-agent-functions/)
- [Route to multiple agents](https://docs.digitalocean.com/products/inference/how-to/route-agents/)
- [Manage agent guardrails](https://docs.digitalocean.com/products/inference/how-to/manage-agent-guardrails/)
- [Manage agent versions](https://docs.digitalocean.com/products/inference/how-to/manage-agent-versions/)
- [Manage partner provider keys](https://docs.digitalocean.com/products/inference/how-to/manage-model-provider-keys/)
- [Manage model access keys](https://docs.digitalocean.com/products/inference/how-to/manage-model-access-keys/)
- [Manage workspaces](https://docs.digitalocean.com/products/inference/how-to/manage-workspaces/)
- [View agent metrics and logs](https://docs.digitalocean.com/products/inference/how-to/view-agent-observability/)
- [Use server-side tools (MCP, web search, web fetch)](https://docs.digitalocean.com/products/inference/how-to/use-server-side-tools/)
- [Manage serverless inference prepayment](https://docs.digitalocean.com/products/inference/how-to/manage-serverless-inference-prepayment/)
- [Inference pricing](https://docs.digitalocean.com/products/inference/details/pricing/)
- [Inference limits](https://docs.digitalocean.com/products/inference/details/limits/)
- [Inference availability](https://docs.digitalocean.com/products/inference/details/availability/)
- [Inference features](https://docs.digitalocean.com/products/inference/details/features/)
- [AI data privacy](https://docs.digitalocean.com/products/inference/details/data-privacy/)
- [GradientAI Platform API reference](https://docs.digitalocean.com/reference/api/reference/gradientai-platform/)
- [Agent Inference API reference](https://docs.digitalocean.com/reference/api/reference/agent-inference/)
- [Recent release notes](https://docs.digitalocean.com/release-notes/recent/) — for the 30 June 2026 deprecation of tracing and conversation logs
- `docs/DROPLET_AI.md` — the serverless inference half, and the price-parity and structured-output findings this file leans on
