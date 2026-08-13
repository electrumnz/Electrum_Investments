# Running costs

Prices verified against the [Claude pricing page](https://platform.claude.com/docs/en/about-claude/pricing)
on **9 August 2026**. Re-check before relying on the arithmetic; these move.

> **Every figure below is for a CLAUDE model, and the Python model path now
> follows `DO_INFERENCE_KEY`.** The arithmetic is still the right shape — token
> counts, cadence and the cache multipliers do not change with the destination
> — but the per-million rates do: DigitalOcean's open models run roughly
> $0.18–$0.99 per million against Sonnet's $2/$10. No DigitalOcean model has
> prices on file in `MODEL_SPECS`, deliberately: a price reaches that table in a
> commit with a reason, exactly as a limit reaches `config/rules.yaml`, and
> until one does its calls report an **unknown** cost rather than a zero. An
> invented price is the same class of error as an invented indicator.
>
> Two figures here are also **not yet measured at that endpoint**. Prompt
> caching is undocumented for its `/v1/messages`, and a dropped `cache_control`
> does not raise — it bills 10x on the system block. And a model that reasons
> before answering spends output tokens the Claude arithmetic below does not
> account for. `cached_tokens` on the `cycle_complete` line is what settles the
> first; the second wants a week of real cycles.

## The stack

| Service | Purpose | Cost |
|---|---|---|
| **The model API** | The decision engine | ~$5–15/mo on Claude (see below) |
| **Alpaca** | Broker, paper trading | **$0** |
| **Alpaca market data** | Quotes and bars, free tier | **$0** |
| **Finnhub** | Earnings calendar for the news blackout | **$0** (60 req/min) |
| **Marketaux** | Headlines for Claude's context | **$0** (100 req/day) |
| **GitHub** | Code hosting | **$0** |
| **Buzz** | Chat interface, self-hosted or Block's relay | **$0** |
| **Hermes Agent** | Agent runtime | **$0** (MIT; you pay only for inference) |
| **Claude Pro** | Only if you want cloud-scheduled Routines | $20/mo, optional |
| **VPS** | Always-on host for the Hermes gateway | **$12/mo** |
| **Total to start** | | **~$17–27/mo** |
| **With scheduled runs** | | **~$37–47/mo** |

### Why there is a VPS line

Only one component forces an always-on machine: the **Hermes gateway**. A chat
bot that is offline when you message it is not a chat bot. Everything else here
runs on demand and costs nothing while idle.

That box then also carries the bot loop, the dashboard and the SQLite journal,
so it is one provider rather than two. A DigitalOcean 2 GB droplet at $12/mo is
the pick; `docs/HERMES_SETUP.md` has the comparison and the sizing reasoning.
Briefly: the 1 GB tier at $6 runs five processes plus an OS and wants a swap
file, and CPU is irrelevant at a 15-minute cadence, so the money buys RAM.

Note what this is **not**. An earlier plan assumed a *Windows* VPS, because
MetaTrader 5's Python package is Windows-only, and Windows licensing roughly
doubles the price. Alpaca is a plain REST API, so any cheap Linux box does.

## Claude API arithmetic

Per million tokens:

| Model | Input | Output | Cache read | 1h cache write |
|---|---|---|---|---|
| **Haiku 4.5** (default) | $1 | $5 | $0.10 | $2 |
| Sonnet 5 | $2\* | $10\* | $0.20 | $4 |
| Opus 5 | $5 | $25 | $0.50 | $10 |

\* Sonnet 5 introductory pricing, through **31 August 2026**. It becomes $3/$15
after that — a 50% rise. If you are on Sonnet, note the date.

### Default configuration

15-minute cadence, **running continuously** — not only during the equities
session. `cmd_loop` calls Claude every cycle and the session window is enforced
afterwards, by `RiskGate.evaluate`, on whatever gets proposed.

That is deliberate rather than an oversight. `config/rules.yaml` gives crypto
`sessions_utc: [[0, 24]]`, so the moment that sleeve is enabled the overnight
cycles stop being idle and start being the point. Gating the model call on the
equities window would be work to undo later.

- 24 hours ÷ 15 min = **96 calls/day** → ~**2,900 calls/month**
- Measured on a real cycle: 2,072 input tokens, 129 output, **$0.0027 per call**

| | Calls/month | Cost |
|---|---|---|
| **Continuous (what it does)** | ~2,900 | **~$8/month** |
| Equities session only, for comparison | ~590 | ~$1.60 |

Round up to **$8–15** for retries, longer contexts as news and sentiment are
added, and days when you experiment.

Roughly four in five of those calls happen when no equity trade could be
approved anyway. That is the price of leaving the door open for crypto, and at
this scale it is a rounding error — but it is worth knowing it is a choice.

### Why the cache TTL matters

Prompt caching has both a 5-minute and a **1-hour** TTL. The 1-hour write costs 2×
base input; reads cost 0.1×.

At a 15-minute cadence a 5-minute cache expires between every call and never pays
for itself — you would pay the write premium and never get a read. The 1-hour
cache gets roughly **four reads per write**, which is why `model_client.py` sets
`"ttl": "1h"` explicitly. An earlier version of this project's plan assumed only
the 5-minute TTL existed and concluded caching was useless here. That was wrong.

### If you increase the cadence

Cost scales roughly linearly with call frequency:

All continuous, since that is how the loop runs:

| Cadence | Calls/month | Haiku | Sonnet 5 |
|---|---|---|---|
| 15 min (default) | ~2,900 | ~$8 | ~$16 |
| 5 min | ~8,800 | ~$24 | ~$48 |
| 1 min | ~43,800 | ~$118 | ~$236 |

**Frequency is not free, and it is not just a cost problem.** In the Alpha Arena
competition the heaviest trader made 238 trades and lost 57%; the lightest made 38
and lost least, with fees explicitly cited as dominating P&L. `config/rules.yaml`
caps trades per day and per week for that reason. Raising the *thinking* cadence
without raising the *trading* caps is fine — that is just paying to think more
often. Raising both is how accounts get churned to death.

### The cheaper pattern, when you need it

If you genuinely want minute-level responsiveness, do not call Claude every
minute. Watch the market with cheap rule-based triggers — indicator crosses,
volatility spikes, level breaks — and wake Claude only when something fires.
Typical result is 50–200 calls/day instead of 1,440, for the same reaction speed.
Not built here; noted in `docs/HANDOFF.md` as a growth path.

## Break-even

This is a paper account, so nothing needs to break even yet. The number worth
tracking is **whether the bot's paper P&L would have covered its own running
cost** at the size you would actually trade.

At ~$22/month, API plus the VPS:

| Account size | Monthly return needed |
|---|---|
| $5,000 | 0.44% |
| $10,000 | 0.22% |
| $25,000 | 0.09% |
| $100,000 | 0.02% |

These look trivially achievable, and that is the trap: the hard part is not
covering $22 of running cost, it is not losing the capital. Judge the bot on
drawdown and consistency, not on whether it beat its own hosting bill.

Note the shape of the two costs, because it changes as the account grows. The
API bill scales with how often you think; the VPS is flat whatever you do. On a
small account the VPS dominates and the percentages above are mostly rent. Get
much past $25,000 and both become rounding errors, which is the point at which
this page stops being worth reading.

Every Claude call's cost is recorded in `audit/<date>.jsonl` as
`estimated_cost_usd`, so the API side can be checked rather than trusted. The
VPS is not in there — it is a fixed monthly line on someone's card.


---

## The dreamer

`electrum-bot dream` is a different shape of call from the decision loop and is
priced the opposite way round: bought **deep** rather than cheap, and run rarely
enough that the depth costs almost nothing.

Measured on the real prompt with four open chains and a news day: about 2,360
system tokens, 1,270 user tokens, and a few hundred output tokens on top of the
thinking pass. Thinking bills as output.

| Tier | Thinking | Per run | Daily | Every 6h |
| --- | --- | --- | --- | --- |
| Haiku | none | $0.0071 | $2.60/yr | $10.40/yr |
| **Sonnet** | ~4,000 | **$0.054** | **$19.80/yr** | $79.21/yr |
| Opus | ~4,000 | $0.136 | $49.50/yr | $198.02/yr |

Sonnet daily is the shipped default. Haiku is listed for completeness and is the
wrong choice here: it has no extended thinking at all, and thinking is the entire
mechanism by which a dream gets past its first hop.

### The 1-hour cache is a PENALTY here, not an optimisation

This is the counter-intuitive part and it is worth stating plainly.

A 1-hour cache write bills at **2x base input** and a read at **0.1x**. The
decision loop wakes every fifteen minutes, so it gets roughly four reads per
write and the cache pays for itself several times over. The dreamer runs once a
day, so the cache has **always expired** by the time it is next asked: every
single call pays the 2x write and never once collects a read.

On the dreamer's system block that is $0.0095 a run against $0.0071 uncached — a
third more, for a feature sold as a saving. So `ModelClient` takes
`cache_system`, and the dreamer sets it to False.

The general rule: **cache only when the call interval is shorter than the TTL.**
Anything slower is strictly worse off caching than not.

### Why daily rather than more often

Cost is not the constraint at any plausible cadence; even Opus every six hours is
under $200 a year. What daily buys is a page worth opening. A chain needs four to
six steps to move through seed, explore, iterate and verdict, so at this rate one
resolves inside a week and an operator checking in on Sunday finds two or three
that actually moved. Every six hours would produce twenty-eight steps a week,
which is more than anyone reads, and the model is told to prefer advancing an
existing chain, so most of the extra runs would re-litigate the same four.
