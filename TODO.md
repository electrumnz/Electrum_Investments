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

**Operator's decision: it runs on the DREAM TIMER, once a day, capped.** Not on
the loop's fifteen-minute pulse — that would be ninety-six unattended
negotiations a day on the same process that proposes orders, which is the Alpha
Arena failure shape with two models instead of one. A day is far slower than a
price moves, which is the right speed for deciding whether a second-order
hypothesis is worth acting on.

**Five caps, and the last one is the one that actually stops them talking
forever.** A turn limit alone does not: it bounds one conversation and says
nothing about having the same conversation again tomorrow.

- **6 turns per exchange** (3 each). Enough to ask, answer and reach a verdict.
  Hard stop, no extension.
- **2 dreams conferred per daily run.** So a run costs at most 12 model calls
  on top of the dream step itself, and the bill is predictable.
- **3 exchanges per dream, lifetime.** If they cannot agree in three
  conferences the dream parks. A fourth would be the same argument again.
- **`TEXT_MAX_CHARS` per message**, reusing the existing prose cap.
- **A dream may only be conferred again if SOMETHING CHANGED since the last
  exchange** — a condition fired, a hop was added or checked, the operator
  posted a note, the vault moved. Otherwise it is skipped and recorded as
  nothing new to discuss. Without this rule two agents re-litigate an unchanged
  dream every single day forever, politely, at cost, and every individual cap
  above still holds while they do it.

An exchange also ends early on accept, return or park. The transcript is stored
either way — including the exchanges that ended in nothing, because a dream the
trader kept declining is a fact about the dreamer worth having.

### What the trading model is told about an adopted dream

**Operator's decision: the FULL CHAIN reaches the prompt.** Every hop, its
source, and the verification badge, for ADOPTED dreams only — not for
everything sitting in the vault, which bounds the volume to at most three.

This is deliberately against the grain of the rest of the repo, so the reasons
it is safe here need to hold rather than be assumed:

- **It is not a track record.** The thing `metrics.py` is kept away from the
  model for is an OUTCOME sample — win rate, P&L, three losses in a row — which
  a model will confidently overfit to. A causal chain about cicadas and sesame
  is not an outcome sample and there is nothing in it to overfit to.
- **It must arrive LABELLED.** The chain is speculative by construction, so it
  is rendered with its `Verification` badge and its `weakest_hop` adjacent and
  never separated from them. An unqualified chain in a prompt reads as
  established fact, and the whole reason `Hop.checked` exists is that some of
  those sentences were invented.
- **It changes no gate.** The chain is context, exactly like headlines and
  posts. `RiskGate` never sees it and cannot be argued with by it.
- **It does not make the trade.** The prompt must say so plainly: an adopted
  dream permits a symbol, it does not propose a position, and the agent still
  has to justify direction, entry, stop and size on its own evidence. A dream
  is a reason the symbol is available, never a reason to be in it.

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

### What reaches the Board, and what must not

The Board is live account state — journal, broker, audit — and the thing it is
for is answering "is anything wrong, and is there anything I have to do". So
the dream work reaches it as a small, fixed vocabulary of TAGS, each with a
stated rule for when it appears. The Board must not become a second Dreaming
page.

**A dream tag on a position is PROVENANCE, never endorsement.** It says where
the permission to hold that symbol came from. It must not read as "a prophecy
backs this trade" — the chain that produced it is speculative by construction,
and a badge that implies otherwise would put the dreamer's confidence next to a
real figure, which is the one thing `brand/` is kept separate to avoid.

**Every tag is derived from STORED state, never recomputed by a model.** The
source is the adoption record and a `dream_id` on the trade. A tag a model
decides to apply is a tag that can be argued into existence.

The vocabulary:

- **`dream`** — on a position or trade row: opened under an adopted dream, with
  the dream named on hover. This is the one that carries the visual treatment
  the operator asked for — a wisp, orbs drifting round the row. Decoration, and
  therefore subject to the projection layer's rule: it must FAIL TO VISIBLE.
  Script off, script threw, `prefers-reduced-motion` — the row still renders
  with its tag as plain text. Never hide the row and reveal it in JS.
- **`dream-expired-holding`** — the edge case that matters most, and the one to
  build first. A position is open under a dream whose permission has EXPIRED.
  Expiry withdraws the right to open, and it must **never force-close**:
  closing is deliberately outside the gated path, and an unattended auto-close
  at 3am is a new execution path nobody chose. So the position stands and the
  Board says exactly that — permission gone, no new entries in this symbol,
  existing position untouched and still yours to manage. Silence here would be
  a position sitting under a permission that no longer exists with nothing on
  screen to say so.
- **`dream-expiring`** — an adoption inside its final stretch. Warn early, for
  the same reason the Tailscale banner warns at ten days: the failure is notice
  followed by a loss of capability, and the notice period is the only time it
  can be acted on.
- **`prophecy-fired`** — conditions met, moved to the vault, waiting for the
  trading agent. A count and a link, not the reasoning.
- **`awaiting-trader`** — dreams sitting in the vault unactioned, WITH AN AGE.
  An offer nobody answered is a different fact from no offer, and the age is
  what separates them.
- **`a2a`** — unread agent-to-agent messages since the operator's last sitting.
  `seen.py` already answers "what changed since I last looked" and must be the
  source; do not build a second marker.
- **`adopted N/3`** — slots used against the cap.
- **`unexplained-move`** — the inverse tag, and the one that makes item 4 real:
  a position whose stop or quantity changed at the broker with **no recorded
  reason**. The whole point of storing the reasoning is that its absence is
  visible; a feature that only shows the moves it managed to capture would hide
  its own failures.

Deliberately NOT on the Board: dream chains and reasoning (that is the Dreaming
page), and `DreamLedger` rates (reasoning-quality statistics, which belong
beside `metrics.py` on Analytics and reach the operator, never the model).

### Running it end to end found two gaps no test could show

Three real dreams were generated against the live model, then the vault and a
conference were driven. Both worked exactly as written, and the pipeline still
cannot function, for two reasons neither the suite nor either audit caught.

**1. Nothing promotes a dream out of the workbench.** `Dream.is_offerable` is
defined and **never called**; `dreamer.py` never moves a dream. The dreamer
writes to `WORKBENCH`, the conference reads only `Vault.VAULT`, and no code
joins the two. Observed: three dreams, two of them at `verdict` stage, all
three still on the workbench; `vault 0/12`; and `confer` completing honestly
with `considered: 0, calls: 0, cost 0`. **The vault is always empty and the
conference always no-ops.**

The missing piece is the promotion rule, and it is a decision rather than
plumbing: a dream reaching a `keep` verdict is not automatically worth
offering, and the whole point of the prophecy shelf is that a dream with
*checkable conditions* is a different thing from one with a conclusion. The
honest shape is probably `verdict=keep` **plus** at least one condition with a
number in it → `PROPHECY`, and conditions all met → `VAULT`. That is what
`all_conditions_met` and `has_conditions` were built for.

**2. The dreamer never names a symbol.** All three dreams came back with
`symbols: []` and `symbols_dropped: 0`, so nothing was filtered — the model
simply did not fill the field. `DreamStore.adopt` refuses an empty symbol
list, so even a dream sitting in the vault could grant nothing. This is a
prompt gap rather than a code one, and it is a **second, independent** reason
the permission path is inert, on top of the prompt-and-feeds gap below.

Worth keeping in mind while fixing it: the field must not be coaxed into being
filled for its own sake. A dream about French reactor cooling water genuinely
may have no tradeable symbol, and `instruments` free text is the right home for
"what this is about". An empty `symbols` is a valid answer; what is wrong is
that it is currently the *only* answer.

### The feature is wired and INERT, and that is the next thing to fix

`RiskGate` honours a grant. Nothing else does. The system prompt lists
`rules.allowed_symbols` (`claude_client.py:270`) and the tick, indicator,
intraday and news fetches all run over that same list
(`main.py:94, 131-132, 288-290, 428`). So the model is never told a granted
symbol exists, and a proposal in one would be dropped for want of a quote
before it ever reached the gate that would have allowed it.

**Until that is closed, adoption grants a permission nothing can use.** The
gate half is the half that had to be right first — a permission path that
worked before it was safe would be the wrong order — but the feature is not
real until the symbol reaches the prompt and the feeds.

**A second, narrower gap found in the same pass and deliberately NOT
half-fixed:** `FinnhubCalendar` is constructed once at loop start with
`symbols=list(rules.allowed_symbols)`, so a granted symbol is never in the set
the earnings calendar fetches windows for, and `_news_blackout` **can never
fire for one.** The gate's logic is fine; its input is narrowed. Mutating
`.symbols` after construction would leave the feed's cache holding
pre-filtered windows, which looks fixed and behaves inconsistently — worse
than the open gap. It closes with the prompt-side work, not before.

### An adversarial audit broke it: seven holes, three serious — ALL CLOSED

Run against the permission path with working reproductions, not by reading.
**166 tests were green over every one of them.** The probes live in the
session scratchpad; every one is now fixed, with a test that was verified to
FAIL when the fix is reverted.

The list below is kept as written, because the shape of each hole is worth
more than a tick. What changed, in one line each: the class block is derived
from `Rules.true_class_key` and checked against the SYMBOL in both `grants.py`
and `RiskGate._resolve_class` (1); `DreamStore.adopt` refuses an override that
widens the dream's own claim, with `SYMBOLS_NOT_OFFERED` and
`CLASS_NOT_OFFERED` (2); `_class_symbols` counts an open position by its true
class rather than by the grants in force now (3); every stamp is written UTC
and `Adoption.is_live` is the only definition of live (4); the grant join
requires `d.vault = 'adopted'` and `adopt` does all four writes in one
transaction (5); `check_order` is handed `news_windows` from a calendar the MCP
session builds once (6); a NULL `expires_at` reads as expired and the
provenance guard covers the arithmetic as well as the query (7).

1. **CRITICAL — the class hard-block tested the CLAIMED key, never the
   symbol.** An adoption saying `BTC/USD` under `us_equity` was a live
   permission to trade crypto under the equity book's limits: 0.5% risk cap,
   15% concentration and one-position rules all bypassed. And because
   `AlpacaBroker` routes on `"/" in symbol`, the order reaching Alpaca *is* a
   crypto order — **unbracketed, so no broker-side stop**, which is rule 3
   gone. `CLAUDE.md` asserted the opposite in prose; `tests/test_grants.py`
   only ever tried `{"BTC/USD": "crypto"}`, the case that already worked.
2. **CRITICAL — `adopt_dream`'s `symbols`/`asset_class` overrides are a blank
   cheque.** A dream carrying no symbols and no class can be adopted with
   invented ones, so any dream in the vault becomes a token for granting
   yourself anything. The fix belongs in `DreamStore.adopt` — the store must
   validate that a grant matches the dream's own claim — rather than in the
   caller, so it holds whatever any future tool does.
3. **HIGH — handing a dream back drops a still-open position out of three
   class caps**, and the trading agent picks the moment. `_class_symbols` reads
   grants in force *now*, so a position stops counting the instant the grant
   ends. Membership for an OPEN position must come from what it was opened
   under; `Trade.dream_id` already records that.
4. **HIGH — expiry compares ISO timestamps as STRINGS in SQL and fails open.**
   With a `+13:00` stamp — Pacific/Auckland, which the dream timer uses **by
   design** — a grant six hours past expiry reads as live from
   `granted_symbols` while `Adoption.is_live` correctly says expired. Two
   paths, two answers, and the permission one is the wrong one to be lenient.
5. **MEDIUM-HIGH — a live grant can sit on a non-ADOPTED dream.** The join
   checks the dream exists, not that it is adopted; and `adopt()` writes across
   three connections, so an interruption strands a live grant on a dream still
   in the vault, which `return_to_vault` then refuses to close.
6. **MEDIUM — the MCP order path passes no `news_windows` at all**, so the
   earnings blackout has never applied to an order placed through chat or by
   the operator. Pre-existing, and nothing to do with dreams.
7. **LOW — `expires_at IS NULL` reads as permanent** (it should read as
   expired, since `adopt` always writes one), and a naive `now` raises
   `TypeError` out of `grants.py` into the decision cycle.

**And one that is live right now because the Funnel is up:** the sign-in rate
limiter keys on `request.client.host`, so behind a Funnel or any reverse proxy
every visitor shares one bucket. A remote guesser can therefore lock the
**operator** out indefinitely, because the throttle also blocks the correct
password. Availability rather than disclosure, and worth fixing before this is
relied on.

Clean on audit, recorded so it is not re-checked: `max_granted_symbols` has no
bypass; `grants.py` returned `{}` for all eighteen malformed inputs tried;
`evaluate` reads no file, network or clock; both migrations are additive,
idempotent and preserve rows; and the auth surface refuses every route
including `/live` and `/openapi.json`, with forged cookies rejected and the
rate limit unmovable by `X-Forwarded-For`.

### A second audit found six more, and they are closed too

Independent of the first, over the same path. Two of the seven above were
confirmed a second time, which is the useful part: the same holes found twice
by different routes.

- **HIGH — an expired adoption bricked an ADOPTED slot permanently.**
  `_is_full(ADOPTED)` counted rows on the shelf rather than LIVE grants, so
  three expiries filled it forever: `delete` refuses an adopted dream, `move`
  refuses every actor, and `expired()` only marks. It counts live adoptions
  now, and `DreamStore.has_room` exposes the same arithmetic to a caller that
  wants to ask before spending a model call. Deleting is still refused, and the
  refusal now names `return_to_vault` and `electrum-bot vault-expire` — the
  adoption rows are the only record that a permission existed, and deleting the
  dream takes them with it.
- **MEDIUM — the `dreaming:` block in `config/rules.yaml` was read by
  nothing.** `vault_caps()` and `vault_ttls()` had no production caller on the
  conference path, so `caps.vault: 1` still admitted three and every
  conference-made grant took the dataclass's 90 days. `Conference._caps` fills
  from the file, `TraderPowers.adopt` gained `ttl_days`, and the anti-drift
  test loads the actual file instead of comparing two sets of code defaults —
  which agreed with each other by construction and said nothing about the file.
- **MEDIUM — "longest-waiting offer first" was ordered by the wrong clock.**
  `in_vault` sorted on `updated_at`, so a dream shelved 150 days ago and edited
  this morning was answered LAST. It sorts on `vault_entered_at` now, and the
  test that covered it could not tell the clocks apart because its fixture set
  both to the same moment — it now sets them deliberately at odds.
- **MEDIUM — an adoption the store refused was never retried.** The change gate
  measures the DREAM and the blocker was the SHELF, so a dream both agents
  agreed on was skipped `nothing_new` forever, including after a slot freed.
  The refusal note carries its own message kind and `_consider` re-tests it
  when — and only when — the shelf actually has room.
- **LOW-MEDIUM — a failed grant resolution looked exactly like "nothing
  adopted".** `resolve_grants` returns a `GrantResolution` naming which of the
  five states produced the empty mapping, and the heartbeat carries
  `grants_degraded` and `grant_state` beside `calendar_degraded`.
- **LOW — the runtime-directory guard in `tests/conftest.py` went blind after
  the first offence.** It diffed a file LISTING, so once `data/dreams.db`
  existed a test writing rows into it was invisible. It fingerprints size and
  mtime now, so it catches a file that grew as well as one that arrived.

### One bypass was found and CLOSED, recorded so it is not reopened

Three gates measure "how much of this class am I already carrying" by symbol
membership of `allowed_symbols`: `_concurrent_positions`, `_class_total_risk`
and `_instrument_capital_cap`. A granted symbol is in no such list, so a
position held under a grant was **invisible to its own class's concurrency
cap, class total-risk cap and capital cap.** The grant would have bought entry
to the allowlist *and* a silent exemption from three limits — including the
crypto 0.5% total the operator wrote in their own words.

`RiskGate._class_symbols` now unions `allowed_symbols` with the symbols
currently granted under that class **and with every open position whose true
class is this one**, and hands the set to all three. The tests covering it were
verified to FAIL when each union is reverted, which is the only way to know a
test is doing its job.

That last clause is a correction to what this section used to say. It read
"a position still held under a **lapsed** grant drops back out of those counts.
Not a regression — it was never in them", and a second audit measured the
opposite: before adoption existed a position in an unlisted symbol could not
exist at all, and the trading agent picks the moment because `return_to_vault`
is one of its two powers. Handing a dream back moved $1,200 of live class risk
out of a $1,500 class cap and turned a rejection into an approval with nothing
closed and nothing about the exposure changed.

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

## 4b. Three different SPY prices in ONE render, none of them wrong

Found by cross-checking the live Board against itself, 2026-08-10 15:59 UTC.
A single page render showed:

    ticker tape        SPY 774.12
    positions "Now"    774.0900
    orders   "Market"  774.0800

Traced, and every one is a different **measurement**, correctly obtained:

- **"Now" is `Position.current_price`** — Alpaca's own mark on the position.
- **"Market" is `broker.get_tick(symbol).mid`** — the midpoint the poller
  computes from bid and ask. A cent away from the broker's mark is exactly
  what you would expect.
- **The tape is a third reading on its own clock** — `refresh_seconds: 60.0`,
  deliberately slower than the five-second account poll, because a tape is
  orientation and a minute-old price there is fine while a rate-limit stall on
  the account read would not be.

So nothing is broken and the page still misleads, because **three different
facts are presented in three columns that all read as "the current price of
SPY"** with nothing saying they are measured differently. An operator
comparing them has no way to tell a stale tape from a bid-ask midpoint from a
broker mark — and the natural reading of a disagreement is that something is
wrong.

This is the `market_clock` rule arriving in a new place: *"the venue's phase
and the gate's window, stated separately and never merged into one green
light"*. Same principle, same failure — two claims that are not the same claim.

**The fix is labelling, not unification.** Do not collapse them onto one source:
the position mark is what the broker will settle against, the midpoint is what
an order's distance should be measured from, and the tape's whole point is that
it is cheap and slightly behind. Name each in its column header or its caption,
and give the tape its read time the way the Board's tiles already carry theirs.

Worth noting the timestamp machinery *did* work correctly throughout: the page
said `read 15:56` while `Rendered 15:59`, so the three-minute gap was disclosed
rather than hidden. That is the reading-versus-render rule doing its job, and
it is the reason this was checkable at all.

## 5. RESOLVED — the "pending order" IS the stop leg. Do not clear it.

Read off the live Board through the Funnel, 2026-08-10 15:56 UTC. There is
exactly **one** pending order on the account:

    pending   SPY  buy   21    submitted 10 Aug 13:35 UTC
    position  SPY  sell  21    filled    10 Aug 13:37:40 UTC

The position is SHORT 21 SPY. A BUY 21 is precisely what closes it, and it was
submitted at 13:35 **with the bracket**, two minutes before the entry filled.
It is stop leg `952237ac-d7ec-426e-bb5f-5c6ce7294260`, and it is the only thing
between the short and an unbounded loss.

**Clearing it would leave a $16,240 short position naked.** That is the whole
of the operator's third rule gone, on the one live position, to tidy a row.

**Why it looked like junk, which is the real defect:** the deployed renderer
prints `market` in the Limit column and `n/a` in Needs, because `render.py`
only ever knew about `limit_price`. So the most important resting order on the
account displays as an unexplained market order with no level and no distance.
Fixed on this branch — it reads `820.0000 stop` with the gap computed — and
worth remembering as the lesson: **a badly rendered safety mechanism gets
mistaken for debris and asked to be removed.**

Cross-checked against the journal and against arithmetic while the Board was
open, and all four figures reconcile exactly:

| | dashboard | computed |
|---|---|---|
| Unrealised | −$16.08 | `(773.324285 − 774.09) × 21` = −16.08 |
| At risk | $980.19 | `\|773.324285 − 820\| × 21` = 980.19 |
| % of equity | 0.98% | 0.98% |
| Stop | 820.0000 | journal `planned_stop` 820 |

**`cancel_order` still does not exist on the `Broker` protocol**, and that
remains a real gap — an order this repo places can be abandoned but never
withdrawn. It is left open deliberately rather than built in a hurry, because
the first thing anyone would reach for it to do is the thing above.

## 5b. Clear the resting SPY order — SUPERSEDED, kept for the reasoning

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
- **`LivePoller` fills `open_risk_usd` and not the two new per-symbol fields.**
  `apply_journal_state` now derives three figures from one `open_trades()` read
  — the total, `open_risk_by_symbol` and `symbols_with_unknown_risk` — and
  `src/bot/web/live.py` still populates only the total, leaving the other two
  at their defaults. Nothing there gates and no cap is affected, so this is not
  a live fault. It becomes one the moment a surface renders per-class risk: an
  empty breakdown reads as "this class risks nothing", which is the missing-vs-
  zero rule again. Any Board tag showing a class's risk needs the poller moved
  onto the same three-figure read first.
- **The `mudhorn-capital` Vercel project is real and deploying — the CONNECTOR
  is what could not see it.** Previously recorded as a 404 with the cause
  unconfirmed. Settled from the other direction: the Vercel bot posted a Ready
  deployment for `prj_LpUzEDhsQz5duCKFzhL3FxKgbMSA` on this branch's PR, with a
  working preview URL. So the project exists, `brand/` still redeploys on every
  push, and the 404 through `list_projects` is connector token scope and
  nothing about the deployment. The practical limit stands: the deploy cannot
  be inspected from a session, only from the PR comment or the Vercel UI.
- **Multi-agent dreaming.** Several dreamers working a topic independently and
  debating it out before a verdict. `Thought.by` already carries the
  attribution, and the A2A message store from item 2 is most of the transcript
  machinery.
- **Vercel AI Gateway.** `https://ai-gateway.vercel.sh` speaks the Anthropic
  Messages API, so it is a base-URL swap rather than a rewrite.

---

## Measured accessibility failures in the shipped palette

Computed WCAG 2.1 relative-luminance ratios, not eyeballed. Three fail.

- **`--rust #B3524A` on graphite is 3.48:1 and fails AA for text** — and it is
  the colour of `.banner.crit b`, at 11px uppercase mono with wide tracking.
  **The most severe state on the deck has the least readable heading**, which
  is a warning that did not happen. Split the token: keep `--rust` for borders
  and rails, add `--rust-text #CF7A70` (5.50:1) for the label.
- **An inline link is invisible as a link.** `a{color:var(--bone)}` is the body
  colour and `text-decoration-color:var(--slate)` is 1.47:1. That is WCAG 1.4.1
  failing in the direction where there is no colour channel either. Use
  `--pewter` for the underline (5.37:1) with `text-underline-offset`.
- **`--pewter` at 10px is thin.** 4.81:1 clears WCAG 2, but WCAG 2 is known to
  overstate contrast in dark mode and APCA would score a 10px weight-400 face
  well under its body minimum. Lift the token to `#8B96A4` (5.76:1) rather than
  auditing the forty rules that use it.

**And `color-scheme: dark` is absent entirely**, so every native control —
the chat textarea, the password field, scrollbars, and the paint that happens
before the stylesheet applies — renders in light chrome inside a graphite deck.
One declaration fixes all of it. `theme-color` and `viewport-fit=cover` belong
in the same edit, and `overscroll-behavior: contain` on `.scroll` and
`.chat .log` is very likely a **second, separate cause** of the reported
scrolling trouble: a horizontal table scroll chaining into a page scroll on
touch is a different fault from the 1px bracket overflow.

Full research pass with sources, ranked and costed, is in the session
scratchpad as `inspiration.md` — including cross-document view transitions for
the hyperspace jump (viable, but the Cmd+K palette navigates programmatically
so it would get no transition, which is worse than not having it), `@property`
on `--mag` so the tape rail eases rather than snaps, and a **static render
harness** so the deck can be looked at in a browser without credentials. That
last one is how TODO item 7(d) — the exchange glow nobody has ever seen — ever
gets closed.

## Working with several agents in one tree

Learned the hard way, twice in one session, and it costs real work each time.

**Never `git stash` while another agent is writing to the tree.** `git stash
push --keep-index` reverts every unstaged change, which is exactly the
in-flight work of anything running beside you. Observed: an agent's
`render.py` went from 5,057 lines back to 4,925 mid-edit, and a second agent
lost two untracked files it had to recover from `stash@{0}^3`. The agent
carries on editing a file that has silently moved under it.

The safe pattern is to **stage and commit by explicit path**, never `git add
-A`, and to verify the committed slice by checking out a clean copy elsewhere
rather than by emptying the working tree.

**A RED suite is not evidence either, if an agent is writing.** Observed: 30
failures across the suite, re-run immediately, 1101 passing — same tree, and
the first reading was simply taken part-way through a file being written. That
is the `.backup`-not-`cp` rule in a new place: a snapshot taken between two
writes is internally inconsistent and looks exactly like corruption. Verify
twice before believing a red tree, and never start debugging one on a single
reading.

**Do not run a repo-wide autofix while agents are writing either.** `ruff
check --fix .` rewrites their files underneath them, which is the same hazard
as the stash with a friendlier name. Fix only the paths you own.

**And do not trust a green suite run against a tree that holds another agent's
half-finished work.** A commit assembled from a subset of it has never been
tested as a unit — that is how `grants.py` reached the repository while the
`config.py` it depends on did not, and CI caught it with `"Rules" has no
attribute "dreaming"` on a suite that had just passed locally. That is the
"green local suite says nothing about the repository" rule arriving through
concurrency rather than through `.gitignore`.

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
