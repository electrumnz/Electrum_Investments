# Running costs

Prices verified against the [Claude pricing page](https://platform.claude.com/docs/en/about-claude/pricing)
on **9 August 2026**. Re-check before relying on the arithmetic; these move.

## The stack

| Service | Purpose | Cost |
|---|---|---|
| **Anthropic API** | The decision engine | ~$5–15/mo (see below) |
| **Alpaca** | Broker, paper trading | **$0** |
| **Alpaca market data** | Quotes and bars, free tier | **$0** |
| **GitHub** | Code hosting | **$0** |
| **Buzz** | Chat interface, self-hosted or Block's relay | **$0** |
| **Hermes Agent** | Agent runtime | **$0** (MIT; you pay only for inference) |
| **Claude Pro** | Only if you want cloud-scheduled Routines | $20/mo, optional |
| **Hosting** | Not needed — see below | **$0** |
| **Total to start** | | **~$5–15/mo** |
| **With scheduled runs** | | **~$25–35/mo** |

There is no VPS line. An earlier plan for this project assumed a Windows VPS,
because MetaTrader 5's Python package is Windows-only. Alpaca is a REST API, so
that constraint is gone: run it on any machine, or on Claude Code Routines with
nothing of your own running at all.

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

15-minute cadence, US equities session only (14:00–21:00 UTC), weekdays:

- 7 hours/day ÷ 15 min = **28 calls/day** → ~**600 calls/month**
- Each call: ~2,000 tokens of system prompt (cached) + ~1,500 of market context + ~400 out

On Haiku with the 1-hour cache:

| Component | Monthly tokens | Cost |
|---|---|---|
| Cached system prompt (reads) | 1.2M | $0.12 |
| Cache writes (~7/day) | 0.3M | $0.60 |
| Uncached input | 0.9M | $0.90 |
| Output | 0.24M | $1.20 |
| **Total** | | **~$3/month** |

Round up to **$5–15** for retries, longer contexts as you add news and sentiment,
and days when you experiment.

### Why the cache TTL matters

Prompt caching has both a 5-minute and a **1-hour** TTL. The 1-hour write costs 2×
base input; reads cost 0.1×.

At a 15-minute cadence a 5-minute cache expires between every call and never pays
for itself — you would pay the write premium and never get a read. The 1-hour
cache gets roughly **four reads per write**, which is why `claude_client.py` sets
`"ttl": "1h"` explicitly. An earlier version of this project's plan assumed only
the 5-minute TTL existed and concluded caching was useless here. That was wrong.

### If you increase the cadence

Cost scales roughly linearly with call frequency:

| Cadence | Calls/month | Haiku | Sonnet 5 |
|---|---|---|---|
| 15 min (default) | 600 | ~$3 | ~$6 |
| 5 min | 1,800 | ~$9 | ~$18 |
| 1 min | 9,000 | ~$45 | ~$90 |
| 1 min, 24/7 (crypto) | 43,200 | ~$215 | ~$430 |

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

At ~$10/month:

| Account size | Monthly return needed |
|---|---|
| $5,000 | 0.20% |
| $10,000 | 0.10% |
| $25,000 | 0.04% |
| $100,000 | 0.01% |

These look trivially achievable, and that is the trap: the hard part is not
covering $10 of API cost, it is not losing the capital. Judge the bot on
drawdown and consistency, not on whether it beat its own hosting bill.

Every Claude call's cost is recorded in `audit/<date>.jsonl` as
`estimated_cost_usd`, so you can check the real figure rather than trusting this
page.
