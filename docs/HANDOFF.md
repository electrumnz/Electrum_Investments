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
- there is no auth, no multi-tenancy, and no compliance surface beyond personal tax
- the dashboard and settings page bind to `127.0.0.1` with no login

If that ever stops being true, re-read `reference/STATUS.md` first — the licence
groupings there become real obligations rather than a record.

---

## What exists

**A deterministic risk gate** (`src/bot/risk.py`). Claude proposes orders; this
approves or rejects them against `config/rules.yaml`. It enforces: symbol
allowlist, session windows, news blackouts, per-trade risk, **combined risk
across all open positions**, position concentration, buying-power utilisation,
gross notional, stop/target placement sanity, limit-price sanity, trades per day
and per week, per-symbol cooldown, a sticky daily-loss kill switch, a
**consecutive-loss stand-down**, and a capped crypto sleeve. Every rule has a test
that proves it rejects.

**An Alpaca paper broker** (`src/bot/broker.py`) behind a `Broker` Protocol, with
a `MockBroker` for tests. Paper-only is enforced twice — at startup and again in
the broker constructor.

**An MCP server** (`src/bot/mcp_server.py`) exposing `check_order`, `place_order`,
`close_position`, `get_risk_status`, `get_positions`, `get_rules`,
`get_recent_decisions`, `reset_trading_session`. `place_order` re-runs the gate,
so the tool surface cannot be talked past.

**An audit log** (`audit/<date>.jsonl`) recording every proposal, verdict,
execution, and the token cost of every Claude call.

**A reference library** (`reference/`) tracking fourteen agent, backtesting and LLM-trading projects with pinned
commits and detected licences, so upstream drift shows up as a git diff.

**104 tests**, `ruff` clean, `mypy --strict` clean.

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

**A dashboard.** The audit log is JSONL. A Next.js + Supabase dashboard was
scoped and deliberately deferred — build it when you know which numbers you
actually look at, not before.

**Paid data feeds.** No news, sentiment, whale-tracking or economic calendar is
wired in. `src/bot/data/` has stub adapters with the interfaces already shaped.

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
   `claude_client.py` and measure whether it helps.

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

**Crypto sleeve.** `config/rules.yaml` has it wired and disabled. It trades 24/7
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
  `docs/BUZZ_SETUP.md` — the security section is not boilerplate.
- **Alpaca does not return a position open time.** `Position.opened_at` is
  populated with fetch time; the audit log is the real source of truth for when a
  position was entered.

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
