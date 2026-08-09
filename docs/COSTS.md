# Running costs

Prices verified against the [Claude pricing page](https://platform.claude.com/docs/en/about-claude/pricing)
on **9 August 2026**. Re-check before relying on the arithmetic; these move.

## The stack

| Service | Purpose | Cost |
|---|---|---|
| **Anthropic API** | The decision engine | ~$5–15/mo (see below) |
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
cache gets roughly **four reads per write**, which is why `claude_client.py` sets
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
