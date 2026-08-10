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

**Unconfirmed: the stop leg's trigger price has never been read back.** See
item 6 — `WorkingOrder` carries no `stop_price`, so every check has shown
`limit_price=None` and said nothing about whether the trigger is actually 820.
The journal says 820; the broker has not been asked.

**Also unverified:** whether `mudhorn-bot-execute.conf` is installed on the
droplet, i.e. whether the loop can place orders of its own. Every observed cycle
had `proposals: 0`, so the logs do not settle it either way.

### What a session running in a fresh container CANNOT check

Recorded because two items below were blocked on it and the next session will
hit the same wall rather than a bug.

A cloud session clones the repo and nothing else. `data/` and `audit/` are
gitignored, so **there is no journal, no dreams store and no audit log**, and
`ALPACA_API_KEY` / `ALPACA_SECRET_KEY` are absent from the environment. So:

- the live account cannot be read,
- the resting orders cannot be listed or cancelled,
- the stop leg's trigger price cannot be fetched,
- journal row 1 cannot be inspected — only the code that writes it can.

Anything in this file phrased as "verify on the box" means exactly that: it
needs a session with the credentials, or the operator at a shell on the droplet.
Do not report these as done from a container that cannot see them.

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

## 2. The dream vault — commissioned, and the permission half is the risky half

The dreamer produces hypotheses and they currently reach nobody. The operator
wants them to become something the trading agent can take up, with the two
agents able to talk it through first, and with a dream able to widen what may
be traded.

**Read `CLAUDE.md` "The dreamer has no order path, and that is structural"
before touching any of this.** That section is the reason the dreamer is allowed
to exist beside a live order path at all, and this feature edges towards the one
thing it forbids. Everything below is shaped around not crossing it.

### The shape

Four places a dream can live, and it moves between them:

- **Workbench** — being dreamt about now. Today's flat store.
- **Prophecy vault** — fleshed out, with CONDITIONS attached, tracked for
  fulfilment. Grogu and the crystal ball live here.
- **Dream vault** — conditions fired, or offered directly. The only vault the
  trading agent can see, and where the two agents talk.
- **Adopted** — the trading agent has taken it. The dreamer keeps only a
  **wisp**: the Pensieve arrangement, the memory removed and a trace left.

Plus **archive**, for retired dreams kept as a record.

**Who may do what, enforced in the store rather than by convention:** the
dreamer moves dreams freely between workbench, prophecy, vault and archive, and
may delete. The trading agent may adopt from the vault, and may return an
adopted dream to the vault **with a stated reason**, and may do nothing else —
in particular it may not delete. A one-way door in one direction only.

### Expiry, and why it runs from the vault rather than from creation

Dreams expire. The operator was explicitly unsure between 90 days and a year, so
the TTLs are configuration: workbench 90, prophecy 365, dream vault 90, adopted
90. Prophecy gets the long one because a prophecy is a long-horizon claim by
nature — a condition that resolves inside a quarter was not much of a prophecy.

**Measured from entry into the CURRENT vault, never from `created_at`.** A dream
pulled back out of the vault for more work would otherwise inherit a nearly-dead
clock and expire mid-rework, which punishes exactly the behaviour the system
wants to encourage.

**Expiry marks; it never deletes.** For an adopted dream it does one thing more,
and that thing has teeth: it **withdraws the symbol permission**. See below.

### Caps

Workbench 24, prophecy 12, vault 12, adopted 3. Adopted matches
`max_concurrent_positions` on purpose — an adopted dream the account has no slot
to trade is a promise it cannot keep. A full vault refuses a move and says so;
it does not raise, because a full vault must not take a page down.

### The permission, which is the part to be careful about

The operator's rule: *the dreamer may look outside `allowed_symbols` to other
Alpaca instruments, as long as it does not go around the hard blocks on GROUPS
of instruments.* Crypto disabled means the dreamer cannot see, name or dream
crypto; enable crypto and it can, and vice versa. When a dream names an
unapproved-but-not-blocked instrument, **the trading agent may trade that
instrument for as long as the dream is alive in the vault.**

So what widens is `allowed_symbols`, and never the class. Five properties hold
it in place and none of them may be softened:

- **The class hard-block is derived from `Rules.enabled_instruments`**, not from
  `allowed_symbols`. That is what makes "crypto is off" mean off rather than
  "off unless a dream says otherwise".
- **The grant is resolved OUTSIDE the gate and passed in**, in the same shape as
  `news_windows`. `RiskGate` must stay deterministic: no SQLite read, no network,
  no clock beyond its own `_now()`. A gate that reads a database is a gate that
  can fail, and this one must not fail open.
- **A failed resolution yields an EMPTY set, so it fails CLOSED.** Same rule as
  `FinnhubCalendar.is_degraded` and `stops_unchecked`: an unknown is never
  treated as a permission.
- **The grant is time-boxed to the adoption.** Return-to-vault or expiry revokes
  it immediately, and a symbol traded under a dead dream is refused like any
  other unlisted symbol.
- **The symbol still faces every other gate**, under its own class's limits —
  risk, concentration, session, concurrency, cooldown. Adoption buys entry to the
  allowlist and nothing else. That is the whole reason this is tolerable: a
  permission is not an order.

`Dream` still carries **no qty, no entry, no stop and no side**, and
`tests/test_dreaming.py` still asserts the field overlap with `OrderProposal` is
empty. `Dream.symbols` is new and is deliberately separate from the existing
free-text `instruments` field: `instruments` names what a dream is ABOUT
precisely so it can never be read as a ticker, and `symbols` is the structured
claim that can become a permission. **Do not collapse them into one field.**

### A2A, stored so a human can read it

Messages between dreamer and trader hang off a dream, append-only, with a
speaker and a kind (question, answer, offer, accept, return, note). Rendered in
the vault and in the trading agent's adopted area.

**The exchange is explicitly invoked and turn-capped, not an unattended
negotiation loop.** Two agents talking to each other unwatched, where one of
them can widen what may be traded, is not a thing to leave running overnight;
the mechanism is built, the autonomy is not. Making it continuous is its own
decision, with its own commit.

### Conditions are written to be checkable

A prophecy's condition carries the sentence a person reads AND the structured
`field`/`op`/`value` that code can grade — the same split `SymbolAssessment` and
`AssessmentTrigger` already use, for the same reason. **The threshold is a
number and never the name of another figure**: "above the 20-day" re-checked
next month tests a level nobody ever saw, because the average moved.

A dream with NO conditions must not read as "all conditions met". That is
`can_grade_anything` and `has_cycles` again — an absence of conditions is a
different fact from conditions satisfied, and only one of them should promote a
dream into the vault.

### Still to decide

- Whether an adopted dream's reasoning should reach the trading model's
  **prompt** as context, or only its symbol permission. Feeding a speculative
  chain into the thing that sizes positions is the direction the whole repo
  leans away from; feeding it nothing makes the adoption invisible to the
  reasoner acting on it. Not resolved.
- Whether a dream should be gradeable after adoption — did the prophecy come
  true? — and if so, that it grades the PLAN and never the P&L, beside
  `triggers.py` and `DreamLedger` rather than beside `metrics.py`.

---

## 3. Crypto's own total-risk ceiling

The operator's rule, verbatim: *"if crypto is enabled. crypto shall not consume
more that 0.5% of equity in risk. shall not hold more than 0.5% of risk in
positions. current positions in profit do not count towards offsetting this.
position must be closed if another wants to be taken if outside of risk
profile."*

Per-trade was already 0.5%. What is new is a **class total**: crypto's combined
open risk may not exceed 0.5% of equity, which makes it effectively one
full-size crypto position at a time. That is intended rather than an accident of
the numbers.

Two clauses are the ones most likely to get "simplified" away later, so they are
pinned by their own tests:

- **Unrealised profit does not offset open risk.** Risk is
  `|entry − stop| × qty` — what is lost if the stop fills — and a position being
  up today does not change what its stop costs. Same leverage-neutral reasoning
  the rest of the file rests on.
- **An existing position must be CLOSED to open another**, and the rejection
  message has to say so. A bare number would not convey the consequence the
  operator actually asked for.

Third property, from the house rule about missing data: a held crypto position
with **no journal row** has an unknowable stop, so the class total cannot be
established — and that **rejects**, rather than counting the unknown as zero.
Fails closed.

---

## 4. The trading agent is in control of the live position, and its moves are recorded

Two halves, and only the second is new ground.

**It has to know the trade is its.** Row 1 was placed by hand, tagged `manual`,
and the model has never been told it now manages it. `position_plans` already
carry a read on every open position, so the channel exists; what is missing is
that the position arrives as something the agent OWNS rather than something it
is commenting on.

**When it makes a move, the reason is stored.** `record_exit` takes a price, a
time and a realised figure, so stop-hit, target-hit, closed-by-hand and expiry
are indistinguishable afterwards — see item 16, which this is the front half of.
Every intentional move on a position (adopt, tighten stop, close, hold with a
stated reason) should be a recorded act with an actor, a timestamp and a
reasoning string.

**What is deliberately NOT switched on: unattended execution of those moves.**
Closing a position and moving a stop sit outside the proposal path on purpose,
and `RiskGate.evaluate` never sees them — that is why a stand-down cannot strand
an open trade. Making the loop able to move a stop by itself is a new live
execution path and it is the operator's call, not a side effect of building the
record. Build the record, the reason and the surface; leave the trigger off.

**The P&L half stays out**, as ever. What is graded is whether the position
ended the way it was designed to, never what it earned.

---

## 5. Clear the resting SPY order — BLOCKED on credentials

*"Pending order on SPY still showing, clear this. we have forced the position."*

**Not done, and it could not be done from here.** This session had no
`ALPACA_API_KEY` (see CURRENT STATE), so the resting order could be neither
listed nor cancelled, and the codebase has no cancel path to run even with keys.

Two things follow:

- **There is no `cancel_order` on the `Broker` protocol at all.** Every order
  this repo has ever placed could only be abandoned, never withdrawn. That is a
  real gap and it is bigger than this one order.
- **A cancel does not go through `RiskGate`.** The gate vets proposals that OPEN
  exposure; cancelling a resting order reduces it, and gating it would be the
  same mistake as a stand-down that froze position management. Same class as
  closing a position and moving a stop.

Before cancelling anything, establish WHICH order is resting. The stop leg
`952237ac` is meant to be there — it is the thing rule 3 depends on, and
cancelling it would leave the short unprotected. If what is showing is the stop
leg, the answer is not to clear it. If a second, unfilled entry order survived
the forced fill, that is the one to withdraw. **Read the order back before
acting**, which needs item 6 to be worth reading at all.

---

## 6. `WorkingOrder` has no `stop_price`, so a resting stop cannot be read

Promoted out of CURRENT STATE because it now blocks item 5.

`WorkingOrder` carries `limit_price` and no `stop_price`. A stop leg resting at
the broker therefore renders as `limit_price=None` on every surface that shows
working orders, and **nothing in this repository can state what level it will
trigger at.**

That was survivable while nothing sent a stop to the broker. It is not now:
entries go out as brackets and OTOs, the stop leg IS the thing the operator's
third rule depends on, and the operator can see that a leg exists while having
no way to check it is at the price the journal says. The journal's
`planned_stop` and the broker's actual trigger are two different facts and only
one of them is visible.

A resting stop whose level nobody can read is most of the way to no stop.

---

## 7. The ticker tape — UI, and it is diagnosed rather than vague

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

Note the glow has to survive being read by someone who cannot separate green
from amber — it is a second channel, not decoration, which is why colour alone
was not used.

Related and already decided, recorded so it is not re-litigated: hover-pause on
the tape **stays**. Every cell now carries a tooltip naming its kind and whether
an order against it would rest, and pausing is the only way to read one without
waiting for the marquee to come round.

### RESOLVED — the dashboard showed "no quote" on all sixteen

Fixed by the restart that picked up the current code. Kept rather than deleted
because the earlier reading — sixteen "no quote" cells beside an account showing
exactly $100,000 — looked convincingly like `MockBroker` and was not. **Equity
of exactly $100,000 is the real Alpaca paper default**, so it is not evidence of
a mock broker, and the next person to see it should not chase that.

---

## 8. The Board's scrolling, and the rest of the UI audit

*"on board page we have this weird scrolling stuff where open positions and
pending orders are."*

`render.py` wraps tables in `<div class="scroll">` with `overflow-x:auto`, and
there is a `max-height:60vh; overflow-y:auto` rule in the same neighbourhood.
The suspicion is a nested scroller appearing where nothing needs to scroll, or
trapping the page scroll on a touch device.

Audited in a real browser with a position seeded to mirror row 1, because the
empty-page version of the Board hides exactly this: an area with no rows cannot
overflow. Findings and the CSS that causes them are folded in here rather than
guessed at.

**This is the standing rule, not a one-off.** Every visual defect this project
has found — the tape dissolving into the background, the login page 401ing on
`/live`, the `.gate` CSS collision, the badge guessing why it was empty — was
invisible to 860+ passing tests and visible in about a minute in a browser.

---

## 9. Open up `allowed_symbols`

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

**Interacts with item 2.** The dream vault widens this list per-adoption and
per-symbol, with an expiry. Widening the static list makes the dream route less
necessary for those names; keeping it narrow makes adoption the interesting
path. Decide which is the intended shape before doing both at once.

---

## 10. `record_fill` writes the PROPOSAL, not what the broker did

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

## 11. The X feed is configured and inert

`social:` in `config/rules.yaml` names three accounts whose posts move a price
before the wire story lands, `src/bot/data/xfeed.py` is written and tested, and
**none of it runs**: `social.enabled` is false and `X_BEARER_TOKEN` is unset.
Reading timelines needs a paid X tier, so off is the normal state and a
deployment without it is fully functional.

What is actually outstanding is a decision plus a subscription, not code:

- **Which X tier**, and what it costs against what it buys. The binding
  constraint is a MONTHLY cap on posts retrieved rather than a daily request
  count, which is why the cache TTL here is 10 minutes rather than Marketaux's
  30 — caching a market-moving post for half an hour would defeat the point of
  fetching it.
- **`is_degraded` is already wired and must stay wired.** An empty post list
  from an expired token looks exactly like a quiet morning, and only one of
  those should change how a price move is read. A degraded result is
  deliberately not cached, so one bad minute does not silence the feed for the
  whole TTL.
- **Do not make it gate anything.** A blackout window after a high-impact post
  would mirror `news_blackout_minutes_after` and is a genuinely reasonable idea,
  but it changes what the gate refuses: its own commit, with a reason and a test
  that proves it rejects. "The model thought this post sounded bearish" is the
  opposite of a deterministic input.

Posts render AHEAD of headlines in the prompt on purpose. By the time a headline
carries the story the gap has already opened.

---

## 12. yfinance for the tape, and ONLY the tape

Agreed in principle. The tape gates nothing, so a Yahoo price breaks no rule.

**Two conditions, both non-negotiable:** the source is labelled on the cell, and
it never reaches sizing, the risk gate, the Board's figures or the model's
context. A price from a venue you cannot trade at is real and is not your price.

Not installed; needs a `pyproject.toml` entry.

**It is NOT the answer to item 14**, despite being the obvious guess: yfinance
serves quotes, not calendars. Whether it is even needed is worth re-asking now
that the tape carries live Alpaca prices — the problem it was proposed for
turned out to be a stale deployment (item 7, RESOLVED). Do not add a dependency
to the box that runs the trading loop for a problem that has already gone away.

---

## 13. "Machine on loop should be storing it as a job"

Raised and never addressed. The loop re-proposes from scratch every cycle, so
there is no queue that survives a rejection, a shut session or a restart. A
proposal the gate refused at 09:15 is simply gone.

Design question rather than a task: what is a job, when does it expire, and what
stops a stale one firing into a market that has moved? Answer that before
writing anything.

Note the shape is now half-built elsewhere: an adopted dream (item 2) is exactly
a durable, expiring, agent-held intention that survives a restart. Whatever a
job turns out to be, it should not be a second mechanism for the same idea.

---

## 14. Holiday calendars for TSE, ASX and NZX

The tape's exchange badges are weekday-shaped for those three, so Boxing Day
renders the ASX as open. `ClockFace.tracks_holidays` is False for them and the
badge's own tooltip says so, which is why this is deferred rather than wrong.
New York is covered, via `session_calendar` and Alpaca.

**yfinance is NOT the tool.** `exchange_calendars` is the right library — XTKS,
XASX, XNZE with real holiday rules, offline — and it is a dependency on the box
that runs the trading loop, added to colour a badge for three markets the bot
does not trade.

**Do not hardcode three holiday lists instead.** They go stale in silence, and a
stale list still looks answered.

---

## 15. Let the agent choose its exit type

`OrderProposal.take_profit_price` is a single fixed price. Alpaca supports
trailing stops natively, so this is a model and adapter change rather than a
strategy one. The exit is the agent's decision and it should be able to carry
the one it actually made.

More urgent than it was: since entries became GTC brackets, an arbitrary target
is no longer a journal note — **it is a live order resting at the broker.**

---

## 16. An exit review, grading the PLAN and never the profit

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

Item 4 is the front half of this — recording the reason at the moment of the
move rather than reconstructing it afterwards.

---

## 17. A settings agent

The only route to changing `config/rules.yaml` from the interface. Deliberately
conservative, stubborn, and **asymmetric**: it makes the operator argue for a
limit getting looser and encourages one getting tighter.

**It pushes back; it does not deny.** That distinction is why the per-class
limit validator was removed — a hard refusal at config load is the same intent
implemented as a wall, at the moment it helps least.

Settings has no edit control today and `tests/test_web.py` enforces that, so
this is a deliberate change to that rule rather than an addition beside it.

---

## 18. Smaller, and noted so they are not rediscovered

- **`sessions_utc: [[8, 24]]` is the SUMMER shape** and runs an hour early all
  winter, opening 03:00 New York instead of 04:00. Harmless in the direction it
  errs — the extra hour is the overnight session, which Alpaca will also take —
  but nothing in the code can detect it. Diary entry, twice a year.
- **The `mudhorn-capital` Vercel project could not be read** through the Vercel
  connector: `prj_LpUzEDhsQz5duCKFzhL3FxKgbMSA` returns 404 and does not appear
  in `list_projects` for the team the bot's own comments are posted under. Six
  other projects list fine. Probably connector token scope rather than anything
  real, but it was never confirmed, so the deploy cannot be inspected from here.
- **Multi-agent dreaming.** Several dreamers working a topic independently and
  debating it out before a verdict. `Thought.by` already carries the
  attribution, and the A2A message store from item 2 is most of the transcript
  machinery.
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
  `journal.SCHEMA` — or to `dreaming.SCHEMA` — needs a migration beside it and a
  test that starts from the old shape.
- **Look at the UI in a browser.** See item 8.
- Prefer reporting missing data over inventing a plausible value.
