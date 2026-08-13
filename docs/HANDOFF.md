# Handoff

You are taking over a working foundation, not a finished product. This document
says what exists, what is deliberately missing, and what to be careful about.

---

## Reality check — read this before you invest much time

In October–November 2025, Nof1 ran [Alpha Arena](https://protos.com/llm-crypto-trading-contest-finds-llms-cant-trade-crypto/):
six frontier LLMs, $10,000 of **real** money each, identical prompts, two weeks
on Hyperliquid.

| Model | Result |
|---|---|
| Qwen3 Max | +$2,232 |
| DeepSeek | +$489 |
| Claude Sonnet | −$3,081 |
| Grok | −$4,531 |
| Gemini | −$5,671 |
| ChatGPT | −$6,267 |

Every US flagship finished underwater. All six ran **25–30% win rates**. Fees
dominated P&L — Gemini made 238 trades and lost 57%; Claude made 38 and lost
least of the losers.

Three things follow, and they shaped every design decision here:

1. **An LLM is not a strategy.** It is a flexible reasoner that is confidently
   wrong a lot of the time. Something deterministic has to sit between it and the
   account. That is `src/bot/risk.py`.
2. **Overtrading is the main way to lose.** Not bad picks — churn. Trade frequency
   is treated here as a risk parameter with hard caps.
3. **Paper first, for a long time.** Not as a formality. As the actual point.

None of this means the project is pointless. It means the interesting work is in
the strategy and the discipline, not in wiring an LLM to a broker — that part is
already done and it was the easy part.

---

## Scope

**Single operator, personal trading, paper account.** Not a product, not
multi-user, not exposed to anyone over a network.

That assumption is load-bearing and worth stating plainly, because it is why:

- non-permissive licences in `reference/` are not a constraint — the Commons
  Clause triggers on selling, AGPL's network clause on conveying to other users,
  and neither happens here
- there is no multi-tenancy and no compliance surface beyond personal tax
- the dashboard has ONE shared password rather than accounts (`src/bot/web/auth.py`).
  That is proportionate to a paper account and would not be proportionate to a
  live one; if this ever fronts real money, replace that file rather than
  extending it

If that ever stops being true, re-read `reference/STATUS.md` first — the licence
groupings there become real obligations rather than a record.

### Nothing is published

There used to be one exception: `brand/`, a static marketing site on Vercel
rendering a committed fixture of invented figures. It has been deleted. One
operator, no audience, and everything it cost — a second design system, a
fixture with its own correctness assertions, a public page that looked like a
trading dashboard — bought nothing.

**The dashboard renders account equity, open positions and realised P&L**, and
it binds to `127.0.0.1`. It now sits behind a shared password
(`src/bot/web/auth.py`), which is what made exposing it over a Tailscale Funnel
defensible — the prerequisite was met rather than waived. That gate is
proportionate to **paper** money: one password, no accounts, no rotation, no
record of who signed in. **If this ever fronts real money, `auth.py` is the file
to replace rather than extend.**

`tailscale serve` instead of `funnel` removes the public exposure entirely and
still reaches your own devices. With one operator that is the smaller
arrangement, and it is one command on the box.

---

## What exists

**A deterministic risk gate** (`src/bot/risk.py`). Claude proposes orders; this
approves or rejects them against `config/rules.yaml`. It enforces: symbol
allowlist, session windows, news blackouts, per-trade risk, **combined risk
across all open positions**, position concentration, buying-power utilisation,
gross notional, stop/target placement sanity, limit-price sanity, trades per day
and per week, per-symbol cooldown, a sticky daily-loss kill switch, a
**consecutive-loss stand-down**, per-instrument session windows, and an
optional per-class capital cap. Every rule has a test
that proves it rejects.

**An Alpaca paper broker** (`src/bot/broker.py`) behind a `Broker` Protocol, with
a `MockBroker` for tests. Paper-only is enforced twice — at startup and again in
the broker constructor.

**An MCP server** (`src/bot/mcp_server.py`) exposing `check_order`,
`place_order`, `close_position`, `get_risk_status`, `get_positions`,
`get_rules`, `get_option_expiries`, `get_journal_stats`, `get_trades`,
`get_stand_down_status`, `get_recent_decisions` and `reset_trading_session`.
`place_order` re-runs the gate, so the tool surface cannot be talked past.

**A trade journal and metrics engine** (`src/bot/journal.py`,
`src/bot/metrics.py`). SQLite trade store feeding win rate, profit factor,
expectancy, R-multiples, drawdown, and an MAE/MFE analysis that judges whether
the model's own stop and target placement was sane.

**An audit log** (`audit/<date>.jsonl`) recording every proposal, verdict,
execution, the feeds the model was shown, and the token cost of every Claude
call. It is the only record of a REJECTED proposal, it is backed up hourly
alongside the journal, and `src/bot/insight.py` indexes it into rebuildable
SQLite so the whole history is queryable rather than only a recent window.

**An operator command centre** (`src/bot/web/`) — Board, Decisions, Trades,
Analytics, Settings and Chat — rendering equity, open positions, realised P&L,
the metrics, the rules and every rejected proposal with the gate's reasoning.
Read-only apart from `POST /chat`. It binds `127.0.0.1` and is reached over
Tailscale, or published with a Funnel behind `DASHBOARD_PASSWORD`.

**Three data feeds** (`src/bot/data/`). Marketaux supplies headlines for
context. Finnhub supplies the earnings calendar the news blackout reads — which
means that rule fires now, where before it had no windows and never once did.
The X feed supplies posts from watched accounts, and is off unless both
`social.enabled` and `X_BEARER_TOKEN` are set. Only Finnhub gates anything.

**A reference library** (`reference/`) tracking fourteen agent, backtesting and LLM-trading projects with pinned
commits and detected licences, so upstream drift shows up as a git diff.

**569 tests**, `ruff` clean, `mypy --strict` clean.

---

## What is deliberately missing

**A trading strategy.** The bot will propose trades, and the gate will stop the
dangerous ones, but nothing here has an edge. This is the hard part and it is
yours. Start by watching `electrum-bot loop` without `--execute` and asking
whether you would have taken those trades.

**Live trading.** `ALPACA_PAPER_TRADE=false` is refused in two places. Going live
needs your own KYC (SSN, ID documents, bank link — Alpaca's live flow, in your
name), and it should need a track record you actually believe.

**A backtesting harness.** Right now you can only evaluate forward, which is slow.
This is probably the highest-value thing to build next.

**A messaging surface.** The dashboard now has a Chat page driving Hermes
through the MCP tools, so the CLI is no longer the only way in. Buzz and
Discord stay deferred because Telegram, WhatsApp and Signal all want a phone
number. See `docs/HERMES_SETUP.md`.

**Whale-tracking.** Headlines, the earnings calendar and posts from watched
accounts are all wired (`src/bot/data/`), but nothing tracks large-holder flow.
The adapter interfaces are shaped for it.

**A blackout window after a high-impact post.** `xfeed.py` reads posts from the
accounts in the `social:` block, and they are context only: they reach the
prompt and never the risk gate. The obvious next step is a blackout mirroring
`news_blackout_minutes_after` — refuse new positions for N minutes after a
watched account posts, on the grounds that the first move is spread and noise,
which is exactly the reasoning behind the earnings blackout.

It is not built, and the reasons are worth stating rather than rediscovering:

- It changes what the gate refuses, so it belongs in its own commit with a
  reason and a test that proves it rejects. That is the standing rule for every
  limit in this repo.
- An earnings blackout works because the event is *scheduled*, so a window can
  open before it. A post is unscheduled, so only the "after" half is
  implementable, and the "before" half is the part that protects you from
  trading into a coin flip.
- It needs a definition of "high impact" that is deterministic. "The model
  thought it sounded important" is not one, and a gate that can be talked into
  a verdict is the thing this project exists to avoid. A defensible version
  would key on the account and perhaps a keyword list, both written down in
  `config/rules.yaml` where they can be reviewed.

**Web access. Deferred, and the first job is deciding which kind.**

Nothing in this system can reach the open web. Hermes' `browser`,
`computer_use` and `x_search` toolsets are disabled in
`deploy/hermes-config.yaml`; the trading model sees only what `context.py`
renders; and the chat surface reads recordings rather than fetching. That will
not hold forever — three feeds is a narrow view of the world, and the day a
position moves on something none of them carried is the day this comes up.

**"Give the bot web access" is four different projects, and they are not
equally sensible.** Ranked from most defensible to least:

1. **More or better feeds** — a paid market-data tier, a real news API, a
   filings source. Low risk, because it arrives as structured data through an
   adapter in `src/bot/data/`, gets recorded in `MarketInputs`, and is
   reviewable afterwards. This is almost certainly what is actually wanted.
2. **A live news tool for chat** — needs a paid tier with **its own quota**,
   kept separate from the loop's. The reason is in CLAUDE.md: Marketaux's free
   allowance is already fully spent by the loop, so a second consumer starves
   the trading cycle on exactly the day somebody is asking about the news.
3. **Browser access for Hermes** — it is an agent that reaches a brokerage
   account through the MCP server, on a headless box. The toolsets are off as a
   security posture, not an oversight. Turning them on is a decision about what
   an agent with a prompt injection in its context window is allowed to do.
4. **Web search for the trading model** — the one to be most careful about.
   `indicators.py` exists because a model handed raw material produces a
   confident number nobody can check. Raw web pages are that failure with more
   surface: the gate checks size and stops, not whether the reasoning was
   invented, and a page can be written by anyone with an interest in what this
   account does next.

Three constraints hold across all four and should survive whoever picks this up:

- **Nothing web-derived may become a gating input.** `RiskGate` is
  deterministic Python precisely so it cannot be argued with, and a network
  call inside it could also fail open. The Finnhub calendar is the one feed
  that reaches the gate, and it carries `is_degraded` for exactly this reason.
- **The model reads answers, not sources.** Whatever arrives should be
  rendered into the context block as recorded, attributable text — the same
  contract as headlines today — never handed over as pages to reason from.
- **Every new feed needs its own rate budget and a degraded flag.** An empty
  result from an expired key looks identical to a quiet day, and only one of
  those should change a decision.

---

## Suggested order of work

1. **Watch it.** Run `loop` without `--execute` for a couple of weeks. Read the
   audit log. Form an opinion about whether the proposals are any good.
2. **Backtest.** Alpaca gives free historical bars. Being able to test an idea in
   minutes instead of weeks changes everything about how fast you can learn.
   Three options are already cloned under `reference/src/`:

   | | Use it for | Licence |
   |---|---|---|
   | `backtesting.py` | Simplest credible harness. **Start here.** | AGPL-3.0 |
   | `vectorbt` | Parameter sweeps: "does this have any edge at all?" | Apache-2.0 + Commons Clause |
   | `nautilus_trader` | Realistic fills, backtest/live parity | LGPL-3.0 |

   Suggested path: `backtesting.py` to get a working harness, `vectorbt`
   alongside it once you want to sweep parameters, and `nautilus_trader` only if
   you outgrow both.

   None is MIT, which is worth knowing, but none of them constrains this project
   — see **Scope** above. That would only change if this stopped being a personal
   tool.
3. **Pick one thesis and make it explicit.** "Buy oversold large-caps in an uptrend"
   is testable. "Trade well" is not. Put it in the system prompt in
   `model_client.py` and measure whether it helps.

   Before inventing an architecture, read the prior art in `reference/src/`.
   `TradingAgents` (Apache-2.0) models a real trading desk — analysts,
   researchers, trader, risk manager — and supports Claude directly.
   `ai-hedge-fund` (MIT) runs a panel of investor personas that argue a position
   before it is taken, which is a plausible structural answer to a lone model
   being confidently wrong at a 25–30% win rate. `FinRL` (MIT) takes the
   reinforcement-learning route and ships an Alpaca execution layer against the
   same broker this bot uses, so its plumbing is directly readable.
4. **Add one data source.** Economic calendar first (Finnhub free tier) — knowing
   when not to trade is worth more than another signal.
5. **Only then** consider the dashboard, sentiment feeds, or a faster loop.

---

## Growth paths, with the trade-offs

**Event-driven instead of polling.** Watch the market with cheap rule-based
triggers and wake Claude only when one fires. Same responsiveness, ~10× less API
cost than calling every minute. See `docs/COSTS.md`.

**Crypto.** `config/rules.yaml` has the class wired and disabled. It trades 24/7
and is driven by different things than equities, which is the case for it — and
also why the cap exists, because 24/7 means 24/7 opportunities to lose money.
Enable with a real capital cap, never a shared budget.

**Sentiment and whale tracking.** LunarCrush (~$30/mo) for aggregated crypto
social sentiment is the highest-value paid feed. Whale Alert API (~$50/mo) after
that. Both are noisy and both are gameable — pump-and-dump accounts exist
precisely to be followed. Discount sources by their historical reversal rate.

**Browser automation for sources without APIs.** `reference/src/hyperagent` is the
tool. Two cautions: it is **AGPL-3.0** (the only copyleft project in the
reference set — modifying it and exposing it over a network obliges you to publish
your changes), and scraping breaches many sites' terms of service. Check before
pointing it anywhere.

**Scheduled unattended runs.** Claude Code Routines (needs Claude Pro, $20/mo) run
in Anthropic's cloud with your machine off. Hermes' built-in cron is the
alternative if you are already running the gateway.

---

## Things that will bite you

- **`config/rules.yaml` is calibrated to a $100,000 paper balance.**
  `min_equity_floor_usd: 90000` is a 10% drawdown floor on that. If you reset the
  paper account to a different balance, change this or the bot will halt
  immediately.
- **Session windows are UTC** and assume US equities, 14:00–21:00, which skips the
  noisy first 30 minutes of the open. During EST rather than EDT this shifts by an
  hour. Crypto ignores the window entirely, by design.
- **The kill switch is sticky.** Once the daily loss limit trips, recovery within
  the same session does not re-enable trading. Use `reset_trading_session` — and
  notice that you are doing it.
- **The PDT rule is gone.** FINRA retired it on 2026-06-04 and Alpaca removed
  `daytrade_count` from its API on 2026-07-06. The old $25,000 threshold no
  longer applies — 4x intraday buying power now needs only $2,000 equity. What
  replaced it is Intraday Margin Deficit calls, and repeated non-compliance
  inside five business days still costs a 90-day restriction, which is what the
  `margin:` block in `rules.yaml` leaves headroom against.
- **Risk caps are measured in risk, not position size.** `max_total_risk_pct`
  sums what would be lost if every stop filled. That makes it leverage-neutral,
  but it also means a tight stop permits a large position: a 1%-risk trade with
  a 1% stop implies a position worth ~100% of equity. `max_position_pct` is the
  backstop for that, and it is deliberately generous at 50%.
- **Buzz's ACP mode bypasses approval gates.** Use the native gateway path. See
  `docs/HERMES_SETUP.md` — the security section is not boilerplate.
- **Alpaca does not return a position open time.** `Position.opened_at` is
  populated with fetch time; the audit log is the real source of truth for when a
  position was entered.
- **Option expiry is handled by Alpaca whether you are watching or not.** ITM by
  $0.01 is auto-exercised at 6pm ET; a short ITM position is auto-assigned after
  the close; and an ITM position the account cannot fund is **liquidated inside
  the final hour**. "Do Not Exercise" cannot be filed through the API — it needs
  a support ticket — so closing the position yourself is the only programmatic
  way to choose a different outcome. `src/bot/options.py` watches for this and
  the warnings lead the Claude context block; do not quietly demote them.

---

## Handing over credentials

Everything is in one Bitwarden vault (see `SETUP.md`). To transfer ownership:

1. Share the Bitwarden collection, or export and hand over securely.
2. **Rotate the Alpaca paper keys and the Anthropic API key** after transfer —
   whoever set them up has seen them.
3. Transfer or fork the GitHub repo.
4. Anthropic billing is per-account; the new owner should use their own key on
   their own card rather than inheriting one.

The two regulated things — a live Alpaca account and a bank link — cannot be
transferred and were never created. Whoever goes live does that themselves, in
their own name.

---

## Tax note

If you eventually trade real money in the US: Alpaca issues a 1099-B; crypto is
treated as property so every trade is a taxable event; wash-sale rules apply to
equities. The audit log records everything you would need, but it is not
accounting software. Talk to an accountant before your first live year ends, not
after.
