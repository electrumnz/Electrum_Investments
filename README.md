# Electrum Investments — AI Trading Bot

An AI trading bot for **Alpaca paper trading**, where Claude proposes orders and
a deterministic risk gate decides whether they happen.

**Paper money only.** There is no live-trading path in this build, by design.

---

## The idea in one picture

```
        You, in plain English
                 │
   Claude Code  ·or·  a Buzz channel
                 │
         ┌───────┴────────┐
         │                │
   Alpaca MCP       electrum-bot MCP
  (market data,      (the risk gate)
   read-only)              │
                    ┌──────┴──────┐
                    │  risk.py    │  ← deterministic; the model cannot argue with it
                    └──────┬──────┘
                           │  only approved orders
                    Alpaca paper account
```

Claude proposes. `src/bot/risk.py` disposes. Every proposal and verdict lands in
an append-only audit log.

---

## Why it is built this way

In [Alpha Arena](https://protos.com/llm-crypto-trading-contest-finds-llms-cant-trade-crypto/)
(Nof1, Oct–Nov 2025), six frontier LLMs each traded $10,000 of real money for two
weeks under identical prompts. Every US flagship finished underwater — Claude
Sonnet −$3,081, ChatGPT −$6,267 — and all six ran 25–30% win rates. Fees dominated
P&L: the model that made 238 trades lost 57% of its stake, while the one that made
38 lost the least.

So: the model is treated as a fallible proposer, the limits live in tested Python,
and **trade frequency is a risk parameter**, not a performance one.

---

## Quick start

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest            # 135 tests, no credentials needed

cp .env.example .env                  # add Alpaca paper + Anthropic keys
.venv/bin/electrum-bot smoketest      # connects, asks Claude one question, places nothing
```

Full walkthrough, including which steps need a human and which are scriptable:
**[SETUP.md](SETUP.md)**.

## Commands

```sh
electrum-bot smoketest --mock    # offline sanity check, no credentials
electrum-bot smoketest           # connect to your paper account
electrum-bot loop                # propose and vet continuously; place nothing
electrum-bot loop --execute      # place approved orders on the PAPER account
electrum-bot-mcp                 # run the MCP server (usually launched by Claude Code)
```

`--execute` is off by default. Watch the proposals for a while first.

---

## What the risk gate enforces

All of it from [`config/rules.yaml`](config/rules.yaml), all of it tested:

| | |
|---|---|
| Symbol allowlist | Only listed symbols, ever |
| Session windows | UTC hours; crypto exempt (24/7) |
| News blackouts | No entries around high-impact events |
| Per-trade risk | Max % of equity lost if the stop fills |
| **Total risk** | Max combined loss if every open stop filled — the usual binding limit |
| Position size | Concentration backstop on any one position |
| Buying power | Max share of buying power one order may consume |
| Gross notional | Cap on total market exposure |
| Concurrent positions | Hard count limit |
| Trade frequency | Per day and per week |
| Per-symbol cooldown | Stops flip-flopping |
| Daily loss kill switch | Sticky for the session once tripped |
| Stand-down | Suspends live trading after consecutive losses; paper continues |
| Crypto sleeve | Disabled by default; capped when enabled |
| Option expiry | Refuses entries near expiry; warns loudly before auto-exercise |
| Order sanity | Limit orders only; stops and targets on the correct side |

A rejected proposal comes back with **every** rule it broke, not just the first.

---

## Documentation

| | |
|---|---|
| **[SETUP.md](SETUP.md)** | Step-by-step, with `[human]` / `[auto]` markers |
| **[CLAUDE.md](CLAUDE.md)** | Rules for Claude Code sessions in this repo |
| **[docs/HANDOFF.md](docs/HANDOFF.md)** | What exists, what's missing, what will bite you |
| **[docs/COSTS.md](docs/COSTS.md)** | Running costs — about $17–27/month |
| **[deploy/README.md](deploy/README.md)** | Running it on a server, as a service that survives a reboot |
| **[docs/TRANSFER.md](docs/TRANSFER.md)** | Moving accounts and credentials to a new owner |
| **[docs/HERMES_SETUP.md](docs/HERMES_SETUP.md)** | Hermes agent runtime, and the chat surface later (has a real security caveat) |
| **[reference/STATUS.md](reference/STATUS.md)** | Tracked third-party projects and their versions |

---

## Layout

```
config/rules.yaml         Trading limits. The only place to change behaviour.
src/bot/
  risk.py                 The risk gate — the load-bearing file
  broker.py               Broker Protocol + AlpacaBroker + MockBroker
  mcp_server.py           MCP tools exposing the gate
  models.py               Domain models (shares/coin units, never "lots")
  claude_client.py        Anthropic SDK wrapper, 1h prompt cache
  context.py              Renders market state for Claude
  audit.py                Append-only JSONL log
  main.py                 CLI entry point
  data/                   News and calendar adapters (stubs for now)
reference/                Tracked third-party projects (clones gitignored)
scripts/                  fetch_reference.py, check_reference_updates.py
tests/                    135 tests; the risk suite is the important one
```

---

## Status

Working foundation. **No trading strategy** — that part is deliberately left to
whoever runs it, and it is the genuinely hard part. Start with
[docs/HANDOFF.md](docs/HANDOFF.md).
