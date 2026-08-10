# TODO

Work that is decided but not built, and the reasoning that would otherwise be
lost. `CLAUDE.md` holds how the system behaves *now*; this holds what is next
and why it is not done yet.

Ordered by what is actually blocking, not by size.

---

## 1. Fill an entry OUT OF HOURS — the blocking one

**The operator asked for a position ON in the pre-market and got a resting
order instead, which then filled after the 09:30 open.** That is not a bug in
the code; it is a fork nobody was offered. See `CLAUDE.md`, "A broker-side stop
OR an out-of-hours fill. Never both."

`place_order` attaches the stop, which makes every entry a bracket or an OTO,
and **Alpaca's docs say `extended_hours` is not accepted on either**. That half
is documented rather than tested here — no extended-hours bracket has been sent
from this codebase to watch it be refused. **The consequence is measured:** 21
SPY went in at 09:23:47 New York and sat at `filled_qty=0.0` through the
pre-market, then filled in the regular session.

First thing to do when building this is verify the documented half: send one
extended-hours bracket and confirm Alpaca rejects it rather than silently
dropping the flag. If it downgrades instead of rejecting, everything above is
still true and the failure mode is worse, because a stop would go missing with
no error.

**What to build:** a second execution path. Plain `LimitOrderRequest` with
`extended_hours=True` and **no bracket**, gated by a per-instrument
`allow_extended_hours_fills` flag in `config/rules.yaml`, default off.

**What it costs, stated plainly so the decision is deliberate:** the position
sits at the broker with **nothing resting behind it**. The stop is a journal
figure and `stop_watch` reports a breach on the loop's fifteen-minute pulse.

**Why that is less alarming than it sounds:** crypto already lives exactly here
— Alpaca accepts no bracket on it either — so this is a new trigger for an
existing arrangement, not a new concept. And a resting stop could not have fired
out of hours anyway: a stop becomes a MARKET order and extended-hours venues
take limit orders only. What is genuinely surrendered is the leg being there
when the regular session reopens.

**Do not let the model choose this.** It is an execution-layer decision from
config plus the clock, not a property of a proposal. `OrderProposal` should gain
no field for it.

Windows to test in: after hours 16:00–20:00 New York, pre-market 04:00–09:30.

---

## 2. Journal row 1 is wrong, and the journal cannot express why

**The order PARTIALLY filled: 3 shares of 21, at 773.43.** The journal says
21 @ 772.84 — wrong on both quantity and price, because it was written by hand
from the proposal before anything filled.

    journal claims   21 x (820 - 772.84) = $990.36
    actually filled   3 x (820 - 773.43) = $139.71
    still working    18 shares, which is not risk yet

`open_risk_usd` is what the 2% total-risk cap counts against, so the cap is
currently measuring against a figure seven times the real exposure. Erring
towards overstatement is the safe direction and it is still a number that does
not describe the account.

**The deeper gap: a partial fill has no representation here.** `record_fill`
records the proposal's quantity, and `Trade` carries one `qty` and one
`entry_price`. An order that fills 3 now and 18 later — or 3 and never the rest
— cannot be written down accurately at all. Reconciliation against the broker
is the only thing that could correct it, and the hand-written row means
`reconcile` has a journal entry it will treat as authoritative.

Three things to do, in order:

1. Find out what the one resting SPY order actually is. If it is the remaining
   18 shares of the entry, **there is no stop at the broker** and the 3 filled
   shares are unprotected. If it is the stop leg, the balance was cancelled.
2. Correct row 1 to the real filled quantity and price.
3. Decide whether `record_fill` should record only what filled, and what
   happens when the rest fills later. That is a design question about `Trade`,
   not a patch.

## 3. The dashboard shows "no quote" on all sixteen

The broker serves quotes fine — `get_tick('SPY')` returned 772.84 from the same
box — so this is the **poller**, not the broker or the credentials. The tape
renders every cell as "no quote" while the account reads correctly.

Diagnose before adding yfinance (below), or the second source will paper over
the first problem.

---

## 4. yfinance for the tape, and ONLY the tape

Agreed in principle. The tape gates nothing, so a Yahoo price breaks no rule.

**Two conditions, both non-negotiable:** the source is labelled on the cell, and
it never reaches sizing, the risk gate, the Board's figures or the model's
context. A price from a venue you cannot trade at is real and is not your price.

Not installed; needs a `pyproject.toml` entry. Do item 3 first.

---

## 5. "Machine on loop should be storing it as a job"

Raised and never addressed. The loop re-proposes from scratch every cycle, so
there is no queue that survives a rejection, a shut session or a restart. A
proposal the gate refused at 09:15 is simply gone.

Design question rather than a task: what is a job, when does it expire, and what
stops a stale one firing into a market that has moved? Answer that before
writing anything.

---

## 6. Holiday calendars for TSE, ASX and NZX

The tape's exchange badges are weekday-shaped for those three, so Boxing Day
renders the ASX as open. `ClockFace.tracks_holidays` is False for them and the
badge's own tooltip says so, which is why this is deferred rather than wrong.
New York is covered, via `session_calendar` and Alpaca.

**yfinance is NOT the tool**, despite being the obvious guess: it serves quotes,
not calendars. `exchange_calendars` is the right library — XTKS, XASX, XNZE with
real holiday rules, offline — and it is a dependency on the box that runs the
trading loop, added to colour a badge for three markets the bot does not trade.

**Do not hardcode three holiday lists instead.** They go stale in silence, and a
stale list still looks answered.

---

## 7. Let the agent choose its exit type

`OrderProposal.take_profit_price` is a single fixed price. Alpaca supports
trailing stops natively, so this is a model and adapter change rather than a
strategy one. The exit is the agent's decision and it should be able to carry
the one it actually made.

More urgent than it was: since entries became GTC brackets, an arbitrary target
is no longer a journal note — **it is a live order resting at the broker.**

---

## 8. An exit review, grading the PLAN and never the profit

Nothing records *why* a position closed. `record_exit` takes a price, a time and
a realised figure, so stop-hit, target-hit, closed-by-hand and expiry are
indistinguishable afterwards.

The interesting bucket is **closed by hand before either level** — the plan being
abandoned, which is discipline rather than luck.

**The P&L half stays out.** "Review the trade so it can learn" is the
reasonable-sounding request this repository exists to refuse: forty trades is
noise, a model shown three losses will confidently change approach, and that is
the Alpha Arena failure arriving as a feature request. Belongs beside
`triggers.py` and `DreamLedger`, not beside `metrics.py`.

---

## 9. A settings agent

The only route to changing `config/rules.yaml` from the interface. Deliberately
conservative, stubborn, and **asymmetric**: it makes the operator argue for a
limit getting looser and encourages one getting tighter.

**It pushes back; it does not deny.** That distinction is why the per-class
limit validator was removed — a hard refusal at config load is the same intent
implemented as a wall, at the moment it helps least.

Settings has no edit control today and `tests/test_web.py` enforces that, so
this is a deliberate change to that rule rather than an addition beside it.

---

## 10. Smaller, and noted so they are not rediscovered

- **`sessions_utc: [[8, 24]]` is the SUMMER shape** and runs an hour early all
  winter, opening 03:00 New York instead of 04:00. Harmless in the direction it
  errs — the extra hour is the overnight session, which Alpaca will also take —
  but nothing in the code can detect it. Diary entry, twice a year.
- **How a dream reaches the trading agent.** Currently it does not, deliberately:
  `Dream` carries no symbol, side, qty or stop. Whether one should reach the
  *prompt* as context is open.
- **Multi-agent dreaming.** Several dreamers working a topic independently and
  debating it out before a verdict. `Thought.by` already carries the attribution.
- **Vercel AI Gateway.** `https://ai-gateway.vercel.sh` speaks the Anthropic
  Messages API, so it is a base-URL swap rather than a rewrite.

---

## Rules that apply to everything above

- `src/bot/risk.py` decides what may be traded. Never route around
  `RiskGate.evaluate`.
- A new risk rule needs a test proving it **rejects**, not merely that it exists.
- **A green suite says nothing about the deployed thing.** It has now missed a
  `.gitignore` pattern, a 401 on `/live`, and a journal schema that could not
  store what the models allowed. Any change to `journal.SCHEMA` needs a
  migration beside it and a test that starts from the old shape.
- Prefer reporting missing data over inventing a plausible value.
