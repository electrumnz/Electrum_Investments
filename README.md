# Electrum Investments — AI Trading Bot

Phase A scaffold: Python service that proposes orders via Claude and gates them
against `config/rules.yaml` before they ever reach BlackBull Markets. **No live
trading yet** — demo only until break-even is demonstrated (see `Plan` section
below).

## Stack

- Python 3.11+
- [`anthropic`](https://docs.claude.com/en/api/getting-started) — Claude API (Haiku 4.5 default for the lean profile)
- [`MetaTrader5`](https://pypi.org/project/MetaTrader5/) — official Python package, **Windows-only**
- `pydantic` for typed config and structured outputs
- `structlog` for JSON logs
- `pytest` for the risk-module test suite

A `MockBroker` ships alongside `MT5Broker` so unit tests and local development
work on Linux/Mac without MT5 installed.

## What you (the operator) need to do once

1. **BlackBull Markets demo account** — sign up at https://blackbull.com, choose
   ECN Prime, request demo credentials. You'll receive a login number, password,
   and server name (e.g. `BlackBull-Demo`).
2. **Windows VPS** — any cheap forex VPS in London (FxSVPS, Contabo, AWS EC2
   `t3.small` Windows). The `MetaTrader5` package only runs on Windows.
3. **Install MetaTrader 5** on the VPS — download from BlackBull's site so the
   build matches their server.
4. **Anthropic API key** — create one at https://console.anthropic.com.
5. **Clone this repo** onto the VPS and `pip install -e .` (or use `uv`).
6. **Copy `.env.example` → `.env`** and fill in the four required values:
   - `ANTHROPIC_API_KEY`
   - `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`

## Run it

Smoketest (connects to the broker, asks Claude one trivial question, no orders):

```sh
electrum-bot smoketest
```

Decision loop (continuous; still proposes only — no orders placed in Phase A):

```sh
electrum-bot loop
```

Stop with Ctrl-C. Decisions are written to `audit/<UTC-date>.jsonl`.

## Running the tests

```sh
pip install -e '.[dev]'
pytest
```

The risk-gate suite is the load-bearing one — it proves Claude's proposals are
properly vetted against `config/rules.yaml` before anything reaches the broker.

## Layout

```
config/rules.yaml          # Trading rules — bot may NOT violate these
src/bot/
  config.py                # Typed env + rules loader
  models.py                # OrderProposal, Position, Decision, etc.
  broker.py                # Broker Protocol + MT5Broker + MockBroker
  risk.py                  # The risk gate
  claude_client.py         # Anthropic SDK wrapper (caching, structured outputs)
  context.py               # Renders market state for Claude
  data/                    # News + calendar adapters (stubs in Phase A)
  audit.py                 # JSONL decision/trade log
  main.py                  # CLI entry point
tests/                     # pytest suite
```

## Phase plan

- **A — foundation (now)**: scaffolding, mock broker, smoketest, risk-gate tests ✅
- **B — data plumbing**: real prices via MT5 ticks; news + calendar feeds
- **C — decision loop**: capture rules, wire Claude, vet on demo
- **D — demo run**: 4+ weeks live on demo, track Claude $ vs. P&L
- **E — go live**: smallest size on the same low-volatility universe; add the
  capped crypto sleeve only after the core proves itself

See `/root/.claude/plans/i-am-currently-with-synchronous-hamster.md` for the
full plan and break-even math.
