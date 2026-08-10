# TODO

Work that is decided but not built, and the reasoning that would otherwise be
lost. `CLAUDE.md` holds how the system behaves *now*; this holds what is next
and why it is not done yet.

Ordered by what is actually blocking, not by size.

---

## CURRENT STATE — there is a live position

Not a task. Recorded because it outlives the session that opened it and nothing
else names it.

    SHORT 21 SPY @ 773.324285      opened 2026-08-10 13:37:40 UTC
    stop 820.00, no take-profit    risk $980.19 = 0.98% of equity
    journal row 1, strategy "manual"
    entry order  a76f0545-5db0-445e-9b31-c538b371b7a6
    stop leg     952237ac-d7ec-426e-bb5f-5c6ce7294260  (BUY 21, resting)

Placed by hand as an operator test of the out-of-hours order path, not proposed
by the model. Tagged `manual` so it cannot corrupt `mean_reversion`'s record.

**Unconfirmed: the stop leg's trigger price has never been read back.**
`WorkingOrder` carries no `stop_price`, so every check has shown
`limit_price=None` and said nothing about whether the trigger is actually 820.
The journal says 820; the broker has not been asked. **Add `stop_price` to
`WorkingOrder`** — a resting stop whose level nobody can read is most of the way
to no stop, and every surface that shows working orders has the same blind spot.

**Also unverified:** whether `mudhorn-bot-execute.conf` is installed on the
droplet, i.e. whether the loop can place orders of its own. Every observed cycle
had `proposals: 0`, so the logs do not settle it either way. Worth knowing
before assuming the bot is or is not able to act.

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

## 2. ~~The dashboard shows "no quote" on all sixteen~~ — RESOLVED

Fixed by the restart that picked up the current code. The tape now carries live
prices and moves: GLD 397.80 +2.09%, QQQ 721.70 +0.99%, ETH/USD 1,898.72 -0.53%,
USO 124.28 +4.55%. Kept here rather than deleted because the earlier reading —
sixteen "no quote" cells beside an account showing exactly $100,000 — looked
convincingly like MockBroker and was not. **Equity of exactly $100,000 is the
real Alpaca paper default**, so it is not evidence of a mock broker, and the
next person to see it should not chase that.

---

## 3. The ticker tape — UI, and it is diagnosed rather than vague

Four separate problems. (b) is the one that makes the strip look wrong.

**(a) The clocks do not stand out.** `.tape .clk` is `background: var(--ink)` —
darker than the graphite band — with `border-left`/`border-right` in
`var(--slate)`. But the ordinary cells are divided by
`border-right: 1px solid rgba(42,52,65,.5)`, which is the *same slate*. So a
clock is framed in exactly the same line as the gap between two instruments, and
reads as one more cell rather than as a different kind of object. Contrast alone
will not fix it while the frame is shared — the clocks need their own treatment,
not a darker version of everyone else's.

**(b) The break bars make no sense, and that is literally true.** Each cell
carries TWO vertical marks doing unrelated jobs:

- `.tape .cell::before` — a 2px rail on the LEFT edge, coloured by direction
  (gain/loss) and scaled in height by `--mag`, so it means something.
- `.tape .cell` `border-right` — a flat grey line on the RIGHT edge, meaning
  nothing.

Adjacent cells therefore render a grey line immediately followed by a coloured
one, a few pixels apart, and a reader cannot tell which of the two carries
information. Worse on the current data: with no quote `--mag` is 0, so the rail
collapses to a short pewter stub at 28% height that looks like a stray tick.

Pick one. The rail is the meaningful one and should stay; the decorative border
is what to remove, or replace with spacing.

**(c) The `TRADING` badge on the far left earns nothing.** Flagged twice by the
operator. It renders the gate verdict — `trading` / `armed · orders rest until
open` / `idle` — pinned outside the scroller.

The case for removing it: the clocks now carry each exchange's own state, so a
reader already knows whether New York is open, and "TRADING" adds a word that
looks like a status light while telling them nothing they can act on. Its
predecessor, "gate open, session shut", was worse and was removed for naming two
internal mechanisms; this is the same problem with friendlier words.

The case against, stated so it is a decision rather than a drift: **it is the
only account-wide fact on the strip.** `RiskGate` is account-wide, so whether
the bot itself will act appears nowhere else on the tape, and the middle state
is the one genuinely worth knowing — the bot will propose, the gate will
approve, and the order will rest until the open.

If it goes, that fact needs a home. The Board is the obvious candidate, and the
tape is not the place for it.

**(d) The exchange glow has never actually been seen.** `.mkt-live` carries a
green `text-shadow` and no screenshot taken during development ever had an
exchange open — NYSE rendered `ooh` (amber) and TSE/ASX/NZX `closed` in every
capture. So the open state and its glow are **unverified in a browser**, not
merely unpolished.

It is observable now: the US session is open, so the NYSE badge is rendering
`mkt-live` with the glow live on the strip. Look at it before tuning it, and
note the glow has to survive being read by someone who cannot separate green
from amber — it is a second channel, not decoration, which is why colour alone
was not used.

Related and already decided, recorded so it is not re-litigated: hover-pause on
the tape **stays**. Every cell now carries a tooltip naming its kind and whether
an order against it would rest, and pausing is the only way to read one without
waiting for the marquee to come round.

---

## 4. Open up `allowed_symbols`

The sixteen on the tape are the **watchlist**, display only. The bot may
actually trade six: SPY, QQQ, AAPL, MSFT, JNJ, KO.

The operator's words: *"16 symbols was just for the ticker tape, 16 favourite
symbols, we can open the approved instruments up way more than that."*

`allowed_symbols` is a permission and lives under `instruments:` in
`config/rules.yaml`, so widening it is a deliberate edit in its own commit —
never a side effect of a display change. Worth deciding whether the model's
context can carry indicators for a much larger list before expanding it; the
loop fetches bars and intraday per symbol per cycle, so the cost is linear in
whatever this becomes.

---

## 5. `record_fill` writes the PROPOSAL, not what the broker did

Row 1's entry price has been corrected by hand and the journal now reads
$980.19, matching the account. The underlying behaviour has not changed.

`record_fill` runs immediately after `broker.place_order` and records the
proposal's quantity and the proposal's limit price. Neither is what happened:

- the limit was 772.84, the fill averaged **773.324285**
- a fill is **not atomic**. A poll during this one returned `FILLED 3.0` and was
  briefly written down as a partial fill. It was a reading mid-fill, and the
  order completed to 21.

So the journal records an intention and calls it an outcome. On this trade the
gap was $10.17 of overstated risk — the safe direction, and still a number that
does not describe the account. On a genuinely partial fill it would be worse,
and `Trade` has no way to express one at all: one `qty`, one `entry_price`, no
concept of 3 filled now and 18 later, or of 3 filled and the rest cancelled.

**The decision to make:** should `record_fill` wait for a terminal order state
rather than recording at submission? Not free — waiting leaves a live position
unjournalled, which is the `14b88c8` hole. `reconcile` already squares journal
against broker every cycle and is the likely right home for the correction.

---

## 6. yfinance for the tape, and ONLY the tape

Agreed in principle. The tape gates nothing, so a Yahoo price breaks no rule.

**Two conditions, both non-negotiable:** the source is labelled on the cell, and
it never reaches sizing, the risk gate, the Board's figures or the model's
context. A price from a venue you cannot trade at is real and is not your price.

Not installed; needs a `pyproject.toml` entry. Do item 2 first.

---

## 7. "Machine on loop should be storing it as a job"

Raised and never addressed. The loop re-proposes from scratch every cycle, so
there is no queue that survives a rejection, a shut session or a restart. A
proposal the gate refused at 09:15 is simply gone.

Design question rather than a task: what is a job, when does it expire, and what
stops a stale one firing into a market that has moved? Answer that before
writing anything.

---

## 8. Holiday calendars for TSE, ASX and NZX

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

## 9. Let the agent choose its exit type

`OrderProposal.take_profit_price` is a single fixed price. Alpaca supports
trailing stops natively, so this is a model and adapter change rather than a
strategy one. The exit is the agent's decision and it should be able to carry
the one it actually made.

More urgent than it was: since entries became GTC brackets, an arbitrary target
is no longer a journal note — **it is a live order resting at the broker.**

---

## 10. An exit review, grading the PLAN and never the profit

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

## 11. A settings agent

The only route to changing `config/rules.yaml` from the interface. Deliberately
conservative, stubborn, and **asymmetric**: it makes the operator argue for a
limit getting looser and encourages one getting tighter.

**It pushes back; it does not deny.** That distinction is why the per-class
limit validator was removed — a hard refusal at config load is the same intent
implemented as a wall, at the moment it helps least.

Settings has no edit control today and `tests/test_web.py` enforces that, so
this is a deliberate change to that rule rather than an addition beside it.

---

## 12. Smaller, and noted so they are not rediscovered

- **`sessions_utc: [[8, 24]]` is the SUMMER shape** and runs an hour early all
  winter, opening 03:00 New York instead of 04:00. Harmless in the direction it
  errs — the extra hour is the overnight session, which Alpaca will also take —
  but nothing in the code can detect it. Diary entry, twice a year.
- **The `mudhorn-capital` Vercel project could not be read** through the Vercel
  connector: `prj_LpUzEDhsQz5duCKFzhL3FxKgbMSA` returns 404 and does not appear
  in `list_projects` for the team the bot's own comments are posted under. Six
  other projects list fine. Probably connector token scope rather than anything
  real, but it was never confirmed, so the deploy cannot be inspected from here.
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
  `.gitignore` pattern, a 401 on `/live`, a `.gate` CSS collision, and a journal
  schema that could not store what the models allowed. Any change to
  `journal.SCHEMA` needs a migration beside it and a test that starts from the
  old shape.
- **Look at the UI in a browser.** Every visual defect this session — the tape
  dissolving into the background, the login page 401ing on `/live`, the badge
  guessing why it was empty — was invisible to 860+ passing tests.
- Prefer reporting missing data over inventing a plausible value.
