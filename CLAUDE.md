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

### The journal is backed up with `.backup`, and the busy timeout is the point

`data/journal.db` is the only irreplaceable file on the droplet.
`deploy/backup-journal.sh` snapshots it hourly under `mudhorn-backup.timer`.

**Never change it to `cp`.** The bot may be part-way through a write, and SQLite
keeps a WAL and an shm alongside the database. A copy taken between two of those
writes opens fine and reports corruption later, at whatever moment something
reads the wrong page.

Two things in that script look like defensive padding and are not:

- **The busy timeout on the `sqlite3` connection.** Without it `.backup` returns
  "database is locked" the instant the bot is mid-write, so the snapshots that
  fail are exactly the ones taken during the activity worth keeping. It fails
  closed, so the symptom is a missing backup rather than a bad one, and nothing
  notices. This was found by running the script against a journal under a
  continuous writer, not by reading it.
- **The integrity check on every snapshot before it is kept.** It is written to a
  temporary name, opened, checked, and discarded unless it comes back `ok`. A
  backup nobody has opened is a hope.

A backup on the same droplet survives a bad restore, not a dead droplet. Copying
`backups/daily` off the box is deliberately not automated, because it needs a
destination and a credential that should not live on the trading box.

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

### A bare `.gitignore` directory pattern matches at every depth

`.gitignore` once carried a bare `data/`, meant for the SQLite journal at the
repository root. It also silently excluded **`src/bot/data/`**, so three modules
were never committed while `main.py` imported them regardless.

Nothing local caught it. The files were on disk, so the suite passed, `mypy`
passed, and `git status` was clean, because ignored files are not reported. It
surfaced only on the deployed box as `No module named bot.data.finnhub` — and
the damage was not the crash. The agent could still read `config/rules.yaml`, so
it answered a question about account risk with the limits and no live state: a
confident partial answer, which is the exact failure this project exists to
prevent, arriving through the plumbing rather than the model.

Every runtime-artefact pattern is now anchored (`/data/`, not `data/`), and
`tests/test_packaging.py` fails the build if any file under `src/` is untracked,
matches an ignore rule, or is imported without being committed. **Keep those
patterns anchored, and do not delete that test.**

The general form is worth carrying: a green local suite says nothing about what
is actually in the repository.

### The indicators are computed in Python, and the model is handed the answers

`src/bot/strategy.py` defines mean reversion, trend break, news reaction and
momentum properly — thesis, entry conditions, invalidation, exit. None has a
demonstrated edge; they are scaffolding so the model has something falsifiable
to work against instead of "trade well".

**`Broker.get_daily_bars` now supplies history and `src/bot/indicators.py`
computes the figures.** The averages, the Wilder ATR, the volume average and the
confirmed swing levels are all arithmetic done in code, and `context.py` renders
the results.

**Do not replace that with bars in the prompt.** Handing the model a series and
asking it to work out a 200-day average reintroduces the exact failure this
guards against: it will produce a number, state it confidently, and the risk gate
will approve the trade, because the gate checks size and stops rather than
whether the reasoning was invented. That is the Alpha Arena failure arriving
through the data layer. The model reads figures; it does not derive them.

**Missing stays missing.** Every field is `None` when the bars cannot support it,
and `render` prints it as `unavailable` and names it. `sma_200` over 40 bars is a
40-day average wearing the wrong label, and it is worse than nothing because it
gets believed. `fetch_indicators` returns the symbols that produced nothing as a
second value for the same reason, and the loop logs them as
`symbols_without_history` in every `cycle_complete` line, alongside
`calendar_degraded`. A symbol dropped silently is one the model sees a live quote
for with no history and no warning.

**Two strategies are still not evaluable, and must keep saying so.** Trend break
needs intraday bars, because on a daily bar a close through a level and a wick
that closed back inside are the same row, and telling those apart is the whole
strategy. News reaction needs the same, plus a spread history. Both keep a
`requires` naming exactly what is absent. Trimming those to nothing because the
repo now has *some* history would remove the warning and leave the gap, which is
worse than never having added bars at all.

The remaining work is `get_intraday_bars` on the `Broker` protocol, which Alpaca
also supplies free, and a spread history.

### One directory is published. The rest must never be

`brand/` is deployed publicly at **https://mudhorn-capital.vercel.app** (Vercel,
Root Directory `brand`, so every push redeploys). It is static, and it reads no
journal, no broker and no credential.

It is now a six-page app — sign-in shell, overview, trades, analytics, rules,
about — rather than a single identity page, and **that made the rule matter more
rather than less.** Everything it renders comes from `brand/assets/demo-data.js`,
a committed fixture generated by `scripts/generate_demo_data.py`. There is no
`fetch` anywhere in it and no API to point one at.

Three details are load-bearing and should not be tidied away:

- **The demo banner is plain HTML in all six files**, not something `app.js`
  writes. A label saying the figures are invented must not depend on a script
  having run.
- **The sign-in page gates nothing.** It is prefilled, accepts anything, and
  every page is reachable without it, which the page itself says. A working gate
  would imply the site holds something worth protecting. Making it real is the
  prerequisite for showing live data, never a follow-up to it.
- **The generator asserts its own output** before writing: no trade over the 1%
  cap, open risk under 2%, a stop on every trade, and the limits echoed into the
  JSON still matching `config/rules.yaml`. A demo showing a 1.4% risk against a
  1% cap teaches the reader the wrong thing about what the gate does.

`config/rules.yaml` is the single exception, copied verbatim onto the Rules page.
It is limits rather than secrets and is already public.

If asked to point the public site at the real journal: that is a separate and
much larger project — real authentication, an API off the droplet, TLS, a threat
model — and not a matter of swapping the data source. Say so.

**That is not a precedent for `src/bot/web/`.** The dashboard renders account
equity, open positions and realised P&L, and it has no login *because* it binds
to `127.0.0.1`. The absence of auth is safe only while nothing is published, so
deploying it would put a live view of a brokerage account on the open internet.
Remote access is Tailscale, never a public URL.

If asked to "host the dashboard too because the brand page worked", the answer
is no, and building real authentication first is the prerequisite, not a
follow-up.

This got stronger, not weaker, when the chat panel landed. The dashboard used to
only *display* an account; `POST /chat` means it can now *drive an agent* that
reaches the broker. Exposure used to risk disclosure and now risks action. The
panel is off unless `DASHBOARD_CHAT_TOKEN` is set, which is deliberate: enabling
it should be a decision, never a side effect of deploying.

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
  broker.py             Broker Protocol + AlpacaBroker + MockBroker. Daily bars live here.
  indicators.py         Averages, ATR, volume ratio, swing levels. Pure functions over
                        bars. Computed in Python so the model never derives them.
  mcp_server.py         MCP tools: check_order, place_order, get_risk_status, ...
  models.py             Domain models. Quantities are shares/coin units, never "lots".
  config.py             Typed env + rules loader. Validators reject incoherent limits.
  claude_client.py      Anthropic SDK wrapper (1h prompt cache, structured output).
  context.py            Renders market state for Claude.
  strategy.py           Base strategies. Placeholders with a shape, not an edge.
                        `requires` names what each one still cannot see.
  data/                 External feeds. marketaux.py = headlines (context only);
                        finnhub.py = earnings calendar (feeds the blackout gate).
  audit.py              Append-only JSONL decision log.
  metrics.py            Win rate, profit factor, expectancy, R, MAE/MFE. Pure functions.
  web/                  Local dashboard + a Hermes chat panel. Binds 127.0.0.1.
                        Read-only apart from POST /chat, which is off unless
                        DASHBOARD_CHAT_TOKEN is set.
  main.py               CLI: `electrum-bot smoketest`, `electrum-bot loop`.
deploy/                 VPS provisioning: bootstrap.sh + systemd units. Runs the
                        loop WITHOUT --execute; src/ and config/ stay root-owned
                        so the service account cannot edit its own limits.
                        backup-journal.sh + mudhorn-backup.timer snapshot the
                        journal hourly with sqlite3 .backup, never cp.
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
.venv/bin/python -m pytest              # full suite (272 tests)
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
