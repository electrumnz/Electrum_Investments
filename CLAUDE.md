# Mudhorn Capital — trading bot. Instructions for Claude Code sessions.

An AI trading bot running against an **Alpaca paper-trading account**. Read this
before touching orders, risk, or config.

**Scope:** single operator, personal trading, paper money. Not a product, not
multi-user, not exposed to anyone over a network. That assumption is why there is
no auth, why the dashboard binds to `127.0.0.1`, and why the non-permissive
licences in `reference/` are not a constraint. If it ever stops being true,
re-read `reference/STATUS.md` first.

---

## The one rule that matters

**`src/bot/risk.py` decides what may be traded. You do not.**

You propose orders; the risk gate approves or rejects them against
`config/rules.yaml`. It is deterministic Python. It cannot be persuaded, and a
rejection is final. If a rejection looks wrong, the fix is to change
`config/rules.yaml` and say so, never to route around the gate.

- Never call Alpaca's order tools directly. Use `place_order` from this repo's
  MCP server, which runs the gate first.
- Never add an order path that skips `RiskGate.evaluate`.
- **Do not install [`alpacahq/cli`](https://github.com/alpacahq/cli)** on the box
  that runs the bot, however useful `alpaca account` looks. It submits orders,
  and Hermes cannot drop its `terminal` toolset, so a shell-reachable order
  binary is a live bypass of all four of the operator's rules sitting one command
  away from the agent. Everything it offers read-only is already covered by the
  dashboard, `get_risk_status` and `electrum-bot smoketest`.
- Never widen a limit in `config/rules.yaml` to make a specific trade fit. Limits
  change deliberately, in their own commit, with a reason.
- Never set `ALPACA_PAPER_TRADE=false`. The code refuses to start, twice, on
  purpose. Going live is out of scope — see `docs/HANDOFF.md`.

---

## The operator's four rules

These are the point of the project. Everything else is scaffolding. Do not
"simplify" one away.

1. **Max 1% of equity at risk per trade** (`max_risk_per_trade_pct`)
2. **Max 2% of equity at risk across all open positions** (`max_total_risk_pct`)
3. **Hard stops on every trade** — `stop_loss_price` is required and validated
4. **Consecutive-loss stand-down** — three qualifying losses suspends live
   trading

---

## Things that are counter-intuitive and easy to break

### Risk is measured in RISK, not position value

`max_total_risk_pct` sums `|entry − stop| × qty`, not notional. This is
deliberate and should not be "corrected" back to a notional cap.

Measuring risk makes the rule **leverage-neutral**: 2% means the same thing on
cash equities, on margin, on options, or on futures if a second broker is ever
added. A notional cap does not have that property, and an earlier version of this
repo had one — on a $100k account it capped positions at about three shares of
SPY, because the number came from a CFD context where "2% invested" means 2% of
*margin posted*.

Consequence worth knowing: a 1%-risk trade with a 2% stop is a position worth
about 48% of equity, so holding two of them needs margin. On a cash account the
buying-power guard binds first. That is correct, not a bug.

`max_position_pct` is a concentration backstop at 50% and is deliberately
generous. It is not meant to be the binding constraint.

### The Pattern Day Trader rule no longer exists

**FINRA retired it on 2026-06-04** (Regulatory Notice 26-10) and Alpaca removed
`daytrade_count` and `pattern_day_trader` from its API on 2026-07-06. The $25,000
threshold is gone; 4x intraday buying power now needs only $2,000 equity.

There is no PDT gate in `risk.py` because there is no PDT rule. **Do not restore
one.** What replaced it is Intraday Margin Deficit calls, guarded by the
`margin:` block in `config/rules.yaml`. Repeated non-compliance inside five
business days still costs a 90-day restriction, so the guard leaves headroom.

### Option expiry is resolved by the broker whether anyone is watching

Alpaca **auto-exercises** anything in the money by $0.01 at 6pm ET on expiry day,
**auto-assigns** short in-the-money positions after the close, and **liquidates**
an in-the-money position the account cannot fund **inside the final hour**.

**"Do Not Exercise" cannot be filed through the API** — it needs a support
ticket. So closing the position early is the only programmatic way to choose a
different outcome. That is why `src/bot/options.py` exists, why its warnings lead
the Claude context block ahead of market data, and why they must not be quietly
demoted to a footnote. The bot does not propose option trades; this layer is
purely protective.

### The stand-down suspends money, not trading

During a stand-down, **paper trading continues exactly as normal** — the rule is
"can't trade money", not "stop trading". Only live execution is withheld.

**Closing a position and moving a stop are never gated.** `RiskGate.evaluate`
only sees proposals that *open* exposure. A stand-down that froze position
management would strand open trades with no way out, which is worse than the
losing streak that caused it.

State lives in SQLite, not in memory, so restarting the process does not clear
it. That is the point.

### Each instrument class carries its own rules

`config/rules.yaml` has an `instruments:` block keyed by asset class. Session
windows, symbol lists and strategy live there; the portfolio limits (1% per
trade, 2% total risk, stand-down, daily loss, margin) stay global.

This exists because a single global `sessions_utc` is wrong the moment there is
more than one class. Equities trade a fixed window, crypto trades continuously,
so a shared window meant enabling crypto silently forbade trading it for three
quarters of the day. Do not collapse it back to one list.

`Rules.allowed_symbols` is **derived**, unioning enabled classes, so disabling a
class removes its symbols everywhere at once.

### The journal must be wired in or the caps count nothing

`AccountSnapshot.open_risk_usd` is what the total-risk cap counts against, and it
can only come from the journal — Alpaca holds stop-losses as separate orders, so
the broker cannot report it.

Every path that hands an account snapshot to the risk gate must populate it:
- CLI: `reconcile()` then `apply_journal_state()` in `src/bot/main.py`
- MCP: `_Session.account()` in `src/bot/mcp_server.py`

Miss it and the cap silently has nothing to count. This was a real bug, fixed in
`14b88c8`. A held position with no journal entry has an unknowable stop, so its
risk is reported as **missing** rather than guessed at.

### The two data feeds are not equally important

`src/bot/data/` holds two adapters and they fail in different ways.

**Marketaux** supplies headlines for Claude's context. It gates nothing. If it
is down or unconfigured the model reasons with less information and that is all.

**Finnhub** supplies the earnings calendar that `RiskGate._news_blackout` reads.
Before it existed the only calendar was `EmptyCalendar`, so that rule shipped
and never once fired. It is live now, which means the failure mode is new: an
outage produces zero windows, and **zero windows is indistinguishable from "no
announcements this week"** to the gate.

So `FinnhubCalendar.is_degraded` exists and the loop reports it as
`calendar_degraded` in every `cycle_complete` line. Same principle as
`reconcile`'s `risk_is_understated`: say the number is unknown rather than imply
it is zero. Do not "simplify" the flag away, and do not make a failed fetch
return an empty list without setting it.

**The caches are rate-limit requirements, not optimisations.** Marketaux's free
tier allows 100 requests a day against a loop that wakes 96 times, so the
30-minute TTL is what keeps the quota intact. Lowering it exhausts the day's
allowance before the session ends.

### One directory is published. The rest must never be

`brand/` is deployed publicly at **https://mudhorn-capital.vercel.app** (Vercel,
Root Directory `brand`, so every push redeploys). It is static, and it reads no
journal, no broker and no credential.

**That is not a precedent for `src/bot/web/`.** The dashboard renders account
equity, open positions and realised P&L, and it has no login *because* it binds
to `127.0.0.1`. The absence of auth is safe only while nothing is published, so
deploying it would put a live view of a brokerage account on the open internet.
Remote access is Tailscale, never a public URL.

If asked to "host the dashboard too because the brand page worked", the answer
is no, and building real authentication first is the prerequisite, not a
follow-up.

---

## Why the guardrails are this strict

In the Alpha Arena competition (Nof1, Oct–Nov 2025), six frontier LLMs each
traded $10,000 of real money for two weeks under identical prompts. Every US
flagship finished underwater — Claude Sonnet −$3,081, GPT −$6,267 — and all six
ran win rates of 25–30%. Fees dominated P&L: the model that made 238 trades lost
57% of its stake; the one that made 38 lost least.

The lesson is not "LLMs can't trade". It is that a confident, fluent, wrong model
does real damage when nothing sits between it and the account. That is what the
risk gate is for, and it is why trade *frequency* is treated as a risk parameter
rather than a performance one.

**Doing nothing is a valid, frequently correct output.** Do not propose marginal
trades to appear useful.

---

## Layout

```
config/rules.yaml       Trading limits. Enforced in code. The only place to change behaviour.
src/bot/
  risk.py               The risk gate. The load-bearing file in this repo.
  reconcile.py          Squares journal against broker each cycle. Populates open risk.
  journal.py            SQLite trade store + persistent stand-down state.
  stand_down.py         Consecutive-loss breaker: when to trigger, when to escalate.
  options.py            OCC parsing and expiry safety. Protective only.
  broker.py             Broker Protocol + AlpacaBroker + MockBroker.
  mcp_server.py         MCP tools: check_order, place_order, get_risk_status, ...
  models.py             Domain models. Quantities are shares/coin units, never "lots".
  config.py             Typed env + rules loader. Validators reject incoherent limits.
  claude_client.py      Anthropic SDK wrapper (1h prompt cache, structured output).
  context.py            Renders market state for Claude.
  data/                 External feeds. marketaux.py = headlines (context only);
                        finnhub.py = earnings calendar (feeds the blackout gate).
  audit.py              Append-only JSONL decision log.
  metrics.py            Win rate, profit factor, expectancy, R, MAE/MFE. Pure functions.
  web/                  Local read-only dashboard. Binds 127.0.0.1; no auth by design.
  main.py               CLI: `electrum-bot smoketest`, `electrum-bot loop`.
deploy/                 VPS provisioning: bootstrap.sh + systemd units. Runs the
                        loop WITHOUT --execute; src/ and config/ stay root-owned
                        so the service account cannot edit its own limits.
audit/                  Append-only JSONL. Gitignored.
data/journal.db         SQLite journal. Gitignored.
reference/              Third-party projects we borrow from. See reference/STATUS.md.
```

---

## Conventions

- Python 3.11+, `ruff` and `mypy --strict` must both pass.
- Everything runs in `.venv`: `.venv/bin/python -m pytest`, `.venv/bin/ruff check .`, `.venv/bin/mypy`.
- Tests use `MockBroker`; no test may touch the network or a real account.
- Tests must not write to `data/` or `audit/` — pass a `tmp_path` journal.
- The risk gate collects *all* failure reasons rather than short-circuiting, so a
  rejected proposal explains everything wrong with it at once. Keep that property.
- New risk rules need a test that proves they **reject**, not merely that they exist.
- Prefer reporting missing data over inventing plausible values. An estimated
  exit price is recorded as estimated; unknown risk is reported as unknown.

## Running it

```sh
.venv/bin/python -m pytest              # full suite (195 tests)
electrum-bot smoketest --mock           # no credentials needed
electrum-bot smoketest                  # needs Alpaca paper keys
electrum-bot loop                       # proposes and vets; places nothing
electrum-bot loop --execute             # places approved orders on PAPER
electrum-bot-mcp                        # MCP server, usually launched by Claude Code
electrum-bot-web                        # dashboard on http://127.0.0.1:8787
```

`--execute` is off by default. Leave it off until you have watched the proposals
for a while and agree with them.

## What is deliberately not here

- Live trading. Paper only.
- A trading strategy. The foundation is broker + safety + interface; the strategy
  is the operator's to build and is the genuinely hard part.
- Option *trading*. Greeks, spreads and assignment are deferred; only expiry
  safety exists.
- A backtesting harness and a dashboard. Both sketched in `docs/HANDOFF.md`.
