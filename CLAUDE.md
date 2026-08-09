# Electrum Bot — instructions for Claude Code sessions

This repo is an AI trading bot running against an **Alpaca paper-trading
account**. Read this before doing anything with orders, risk, or config.

## The one rule that matters

**`src/bot/risk.py` decides what may be traded. You do not.**

You propose orders; the risk gate approves or rejects them against
`config/rules.yaml`. It is deterministic Python. It cannot be persuaded,
and a rejection is final. If a rejection looks wrong, the fix is to change
`config/rules.yaml` and say so — never to route around the gate.

Concretely, when working in this repo:

- Never call Alpaca's order tools directly. Use `place_order` from this repo's
  MCP server, which runs the gate first.
- Never add an order path that skips `RiskGate.evaluate`.
- Never widen a limit in `config/rules.yaml` to make a specific trade fit. Limits
  change deliberately, in their own commit, with a reason.
- Never set `ALPACA_PAPER_TRADE=false`. The code refuses to start, twice, on
  purpose. Going live is out of scope for this build — see `docs/HANDOFF.md`.

## Why the guardrails are this strict

In the Alpha Arena competition (Nof1, Oct–Nov 2025), six frontier LLMs each
traded $10,000 of real money for two weeks under identical prompts. Every US
flagship model finished underwater — Claude Sonnet −$3,081, GPT −$6,267 — and
all six ran win rates of 25–30%. Fees dominated P&L: the model that made 238
trades lost 57% of its stake; the one that made 38 lost least.

The lesson is not "LLMs can't trade". It is that a confident, fluent, wrong
model does real damage when nothing sits between it and the account. That is
what the risk gate is for, and it is why trade *frequency* is treated as a risk
parameter rather than a performance one.

**Doing nothing is a valid, frequently correct output.** Do not propose marginal
trades to appear useful.

## Layout

```
config/rules.yaml       Trading limits. Enforced in code. The only place to change behaviour.
src/bot/risk.py         The risk gate. The load-bearing file in this repo.
src/bot/broker.py       Broker Protocol + AlpacaBroker + MockBroker.
src/bot/mcp_server.py   MCP tools: check_order, place_order, get_risk_status, ...
src/bot/models.py       Domain models. Quantities are shares/coin units, never "lots".
src/bot/claude_client.py  Anthropic SDK wrapper (1h prompt cache, structured output).
src/bot/main.py         CLI: `electrum-bot smoketest`, `electrum-bot loop`.
audit/                  Append-only JSONL decision log. Gitignored.
reference/              Third-party projects we borrow from. See reference/STATUS.md.
```

## Conventions

- Python 3.11+, `ruff` and `mypy --strict` must both pass.
- Everything runs in `.venv`: `.venv/bin/python -m pytest`, `.venv/bin/ruff check .`, `.venv/bin/mypy`.
- Tests use `MockBroker`; no test may touch the network or a real account.
- The risk gate collects *all* failure reasons rather than short-circuiting, so a
  rejected proposal explains everything wrong with it at once. Keep that property.
- New risk rules need a test that proves they reject, not merely that they exist.

## Running it

```sh
.venv/bin/python -m pytest              # full suite
electrum-bot smoketest --mock           # no credentials needed
electrum-bot smoketest                  # needs Alpaca paper keys
electrum-bot loop                       # proposes and vets; places nothing
electrum-bot loop --execute             # places approved orders on PAPER
```

`--execute` is off by default. Leave it off until you have watched the proposals
for a while and agree with them.

## What is deliberately not here

- Live trading. Paper only.
- A trading strategy. The foundation is broker + safety + interface; the strategy
  is the operator's to build and is the genuinely hard part.
- A backtesting harness, a web dashboard, and paid sentiment feeds. All are
  sketched as growth paths in `docs/HANDOFF.md`.
