# Mudhorn Capital — trading bot. Instructions for Claude Code sessions.

An AI trading bot running against an **Alpaca paper-trading account**. Read this
before touching orders, risk, or config.

> ## Start here
>
> **`TODO.md` is the current work list.** It is ordered by what is actually
> blocking, it carries the reasoning behind each item, and it opens with the
> live account state. Read it before picking anything up — this file describes
> how the system behaves, that one describes what is unfinished and why.
>
> **There is a live position right now:** short 21 SPY at 773.324285, stop 820,
> $980.19 of open risk. Placed by hand as an operator test, journalled as row 1,
> tagged `manual`. Its stop leg is resting at the broker and its trigger price
> has never been read back — `WorkingOrder` carries no `stop_price`. Details in
> `TODO.md` under CURRENT STATE.

**Scope:** single operator, personal trading, paper money. Not a product, not
multi-user. That assumption is why the dashboard has one shared password rather
than accounts, why it binds to `127.0.0.1` and is reached over Tailscale or a
Funnel, and why the non-permissive licences in `reference/` are not a
constraint. The dashboard **may** now be exposed publicly, behind
`DASHBOARD_PASSWORD` — see `src/bot/web/auth.py` for exactly what that gate is
and is not. If the paper-money assumption ever stops being true, that file is
the first thing to replace, and re-read `reference/STATUS.md` too.

---

## Handing the operator a command: ONE step, only when it is ready

The operator runs everything on the droplet by hand, so every command in a reply
is a paste into a live root shell. Three rules, and the first one cost a deploy.

- **One command per message. Never a block of several.** On 13 Aug 2026 the
  update runbook was four lines pasted together. The `git pull` ABORTED, the
  three after it ran against the old checkout, `bootstrap.sh` printed
  "Provisioned.", and the verification step reproduced the exact bug the pull
  was meant to fix. Every line of output was true and the deploy had not
  happened, because the abort scrolled past under a wall of successful-looking
  output. **Three of those four commands could not tell whether the first one
  worked.** `deploy/update.sh` now asserts each step, and the interaction rule
  is the other half of the same fix.
- **Wait for the output before giving the next one.** Read what came back and
  say whether it worked. A step whose result nobody checked is a step that may
  not have happened, which is this repository's founding failure wearing a
  terminal's clothes.
- **A code block means RUN THIS NOW.** Never paste a command as illustration, a
  preview of a later step, or "here is what you will do after". The operator
  asked for this directly — *"only give me code blocks when you are ready"* —
  because a speculative block is indistinguishable from an instruction.

Say plainly what should happen and what to send back. If a step is not ready
yet, say so in words and give no block at all.

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
  that runs the bot, however useful `alpaca account` looks. It submits orders, so
  a shell-reachable order binary is a live bypass of all four of the operator's
  rules sitting one command away from anything with a prompt. Everything it
  offers read-only is already covered by the dashboard, `get_risk_status` and
  `electrum-bot smoketest`.
  Hermes' `terminal` toolset **is** dropped now (see below), which weakens the
  original argument for this rule without retiring it: a dropped toolset is a
  line in a YAML file and it fails silently, whereas a binary that is not
  installed cannot be reached by a bad merge.
- Never widen a limit in `config/rules.yaml` to make a specific trade fit. Limits
  change deliberately, in their own commit, with a reason.
- Never set `ALPACA_PAPER_TRADE=false`. The code refuses to start, twice, on
  purpose. Going live is out of scope — see `docs/HANDOFF.md`.

---

## Who decides what

**The agent has full control of the trade. The gate controls only the
consequence.**

Direction, symbol, entry, where the stop goes, whether the exit is a hard
target or a trail — all the agent's. Nothing in `risk.py` second-guesses any of
it. `_stops_on_correct_side` checks only which SIDE of entry each level sits
on — the stop on the losing side, and the take-profit on the winning side when
one was given at all — because a stop below entry on a short is not a stop, it
is a target. That is a correctness check, not a view on placement.

What the gate measures is what the choice COSTS: `|entry − stop| × qty` against
the per-trade cap, the portfolio total, that class's own total where one is
set, the concentration limit and buying power. Put the
stop wherever the thesis says; the size follows from it, and a wider stop buys
a smaller position rather than more risk.

So the honest answer to "is this stop any good" is **not the gate's to give and
not this file's either**. The useful answer is arithmetic: this stop implies
this size, and it fits or it does not. Offering an opinion on placement dressed
up as a limit is how a rule nobody agreed to gets added.

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
windows, symbol lists, strategy and **that class's own risk limits** live there;
the account-wide rules (2% total risk, stand-down, daily loss, margin) stay
global.

**A class can also cap its own TOTAL open risk**, via
`max_class_total_risk_pct`, which is separate from the per-trade cap and from
the portfolio-wide 2%. Crypto is set to 0.5%, which makes it effectively one
full-size crypto position at a time — intended, rather than an accident of the
two numbers meeting. Three properties, each pinned by a test that proves it
REJECTS:

- **Unrealised profit does not offset open risk.** Risk is
  `|entry - stop| * qty`, what the position loses if the stop fills, and being
  up today does not change what the stop costs. Netting a paper gain against a
  real stop distance would make the cap loosest exactly when the class had
  already run.
- **At the cap, an existing position must be CLOSED to open another.** The gate
  does not size the new trade down to fit, and the rejection message says so —
  a bare number would not carry the consequence.
- **An unknown REFUSES.** A held position in the class with no journal row has
  an unknowable planned stop, so the class total cannot be established, and the
  gate rejects rather than counting the unknown as zero. **This is the first
  gate in the repo that fails closed on missing data**, and it is a deliberate
  departure from the usual "report the gap, do not refuse" — worth knowing
  before assuming the older rule still holds everywhere.

**A per-class limit OVERRIDES the portfolio one, in either direction.**
`account:` is the default, not a ceiling. Set a class to 3% and that class gets
3%, and nothing refuses it.

There was briefly a validator that rejected a looser class limit at config
load. **Do not reintroduce it.** Refusing to start is a denial, and it denies at
the least useful moment — boot, with no explanation of the trade-off and no way
for the operator to say "yes, I mean it". The friction belongs in the *settings
agent* (see Deferred), which argues the case, makes the operator think about the
consequence, and then does what they say. Pushing back is not the same as
refusing, and only one of the two is this codebase's job.

What still matters is that the file and the gate agree. An override is
deliberately **not floored back with a `min`**: a config saying 3% while the
gate quietly applied 1% would be a limit nobody could read off the file, which
is worse than either number on its own. The Settings page names which figure is
in force per class, and says out loud when one is looser than the default —
information for the operator, not a warning at them.

Worth knowing rather than guarded against: `max_total_risk_pct` is still
portfolio-wide, so a per-trade override above it can never fill — the total-risk
gate refuses the trade that would breach it. Raising a class limit past the
total does nothing until the total moves too.

`max_concurrent_positions` on a class counts **within** that class, so both caps
apply and they measure different things: a class that gets loud cannot fill
every slot the portfolio has. Membership is by the class's own
`allowed_symbols`, not the position's asset-class enum, so one source answers
the question.

**Crypto is configured while disabled, on purpose.** The moment to enable it is
a moment when nothing else is open, which is the worst possible moment to be
deciding what a crypto position should be allowed to risk. Its limits are set
(half the per-trade risk, a third of the concentration, one position) with the
market shut and nothing riding on the decision, so enabling is a one-word edit.

### Every hour Alpaca will take is permitted, and the model is told what it buys

**The operator's decision, reversing an earlier one: pre-market and after hours
are both acceptable now.** *"If the machine wants to trade it, let's let it."*
`config/rules.yaml` carries `sessions_utc: [[8, 24]]` and
`refuse_premarket: false`, so the window covers 04:00–20:00 New York in summer —
Alpaca's whole tradeable day bar the overnight venue.

`RiskGate._premarket` **stays**, switched off rather than deleted. It reads the
phase from `market_clock`, which computes in New York time and so follows
daylight saving with no diary entry; a per-class `refuse_premarket: true` still
rejects, and a test pins that it does. Deleting the mechanism would make turning
it back on a code change rather than a config one.

The window is fixed UTC hours and the session is defined in New York, so
`[[8, 24]]` is the *summer* shape and is an hour out all winter — opening at
03:00 ET instead of 04:00. That is now harmless in the direction that used to
hurt: the hour it wrongly admits is the overnight session, which Alpaca will
also take.

**What replaced the refusal is telling the model what an out-of-hours order
actually becomes**, because the gate approving it does not make it the trade the
model thinks it is:

- The entry is a **bracket or an OTO** — the stop has to reach the broker with
  it — and Alpaca **refuses `extended_hours` on both**. So the order does not
  trade in the pre-market. It **rests** and fills at the next regular open, at a
  price that appears nowhere in the context it was proposed from.
- An out-of-hours quote on the free IEX feed is thin and frequently one-sided.
  That is the halved-mid bug's own habitat, and a limit read off it is a level
  that may not exist by the time the order is live.
- The stop rests at the broker but **cannot fire out of hours**, because a stop
  becomes a market order and extended-hours venues take limits only.

Those three live in `market_clock.OUT_OF_HOURS_MECHANICS`, rendered into the
market context by `render_sessions`, and again in the cached system prompt —
deliberately both. The context block is conditional on a session being shut; the
prompt half is a permanent property of the order path and must be true on every
cycle.

The prompt also says **do not widen the stop to compensate**. Size is computed
from stop distance, so loosening it to feel safer buys a bigger loss at the same
1% — and the gate approves it, because the gate checks arithmetic and not intent.

**"Out of hours" is a property of the moment AND the instrument**, so
`render_sessions` answers per class. Crypto renders as continuous and gets none
of the above: it trades through Sunday, and Alpaca accepts no bracket on it, so
both halves of the warning would be false there.

**`market_clock.py` states the venue's phase and the gate's window separately
and never merges them into one green light.** "The market is open" and "this bot
will trade" are different claims, and confusing them is what produced the
question this module was written after: *the market should be open right now*,
at 04:49 New York on a Monday. Alpaca was open — pre-market — four and a half
hours before the session this bot trades. `is_tradeable_by_bot` requires both,
which is what makes the discrepancy visible rather than hidden.

**Holidays and early closes are what the arithmetic cannot see, and there are
two independent answers to them on purpose.** Thanksgiving is a Thursday and Labor Day is a Monday;
`_phase_at` reads both as ordinary trading days, and no amount of New York time
zone care changes that. Alpaca's `GET /v2/clock` knows the calendar, so its
reading is carried into the context beside the computed one.

Four things about that, and they are the usual rules in a new place:

- **It answers a coarser question, so it does not replace `market_clock`.**
  `is_open` is the REGULAR session only — it cannot tell pre-market from
  after-hours from overnight, which is the distinction the module exists for.
- **A disagreement is named, not resolved.** Computed-open against
  broker-closed is reported as a probable holiday with the broker called
  authoritative; the reverse is reported too.
- **It must never gate anything.** It is a network call, and `RiskGate` has to
  stay deterministic and must not fail open. `get_clock` catches its own errors
  and returns `None`, which means *could not ask* — and the renderer says so in
  a sentence rather than letting silence read as "no holiday". Same rule as
  `FinnhubCalendar.is_degraded`.
- **The holiday case reads as OPEN to the phase computation**, so the
  out-of-hours mechanics were withheld at exactly the moment they mattered most
  the first time this was written. A broker `is_open: False` now forces them.
  `tests/test_market_clock.py` pins it on Labor Day 2026.

**The second answer is `session_calendar.py`, and it exists for the EARLY CLOSE
rather than the holiday.** The two are not equally dangerous:

- A holiday reads as a suspicious silence. Nothing fills, no bars arrive, and
  something eventually looks wrong.
- **An early close does not.** On the Friday after Thanksgiving, Christmas Eve
  and 3rd July the market shuts at 13:00 New York. `_phase_at` says 16:00 for
  three more hours, the session was genuinely open, and every figure on screen
  stays plausible. `get_clock` cannot help — it reports the state *now*, not
  that today ends early — and nothing else in the repository can see it at all.

So the calendar is fetched once a day and everything else is a pure lookup. Five
properties are load-bearing and every one is a rule already used elsewhere:

- **Keyed to the instrument class, never to a symbol.** Every US equity on
  Alpaca shares one session, so a per-symbol table is N identical rows with N
  chances to drift apart — and it gets worse as `allowed_symbols` grows, not
  better. The class is the real key and `config/rules.yaml` already holds it.
- **A failed refresh KEEPS the days it already had.** These dates were published
  months ago and do not stop being true because one fetch timed out. Clearing
  them would turn a transient failure into "every day is unknown", at the moment
  the answer is most wanted. It is flagged as stale instead.
- **A failed fetch backs off, on its own clock.** `RETRY_AFTER` is separate from
  `REFRESH_AFTER` because a failure leaves `_fetched_at` unset, so "have we ever
  fetched" stays false and every caller re-attempts. Observed live against
  `MockBroker`: the web poller reads every five seconds, so it made five failed
  attempts in ten and would have run all day. Against real Alpaca that is a hot
  retry loop aimed at an endpoint that has just shown it is unhappy.
- **Out of range is `None`, not "holiday".** Absence from the dict means "does
  not trade" only inside the window actually fetched, which is what `covers`
  exists for. Same rule as `has_cycles` and `can_grade_anything`.
- **It is derived, so it is never backed up and never gates.** `RiskGate` stays
  deterministic and must not fail open on a network call. The gate's own session
  check is still weekday-shaped and still does not know about holidays; this
  makes the gap visible to the operator and to the model rather than closing it
  inside the gate.

**The Settings card must not guess WHY it is empty**, and it did. "The Board has
not been opened" is a plausible cause and is wrong whenever the poller has read
and the broker had no calendar to give — which is every mock deployment. The two
states are now reported apart. Found by loading the page with the suite green,
which is the third time that has been the only way to find something.

**Alpaca's calendar speaks for New York and for nothing else, so the tape's
other three badges get `exchange_hours.py`.** Tokyo, Sydney and Auckland were
weekday-shaped, which rendered the ASX as trading on Boxing Day — a plausible
wrong figure on the one strip whose entire job is orientation.
`exchange_calendars` supplies XTKS, XASX and XNZE with real holiday rules,
computed offline, behind five pure functions and the same three-valued answer
as `SessionCalendar`.

**The dependency is OPTIONAL, and that is what resolved a trade-off rather than
waving it away.** It is a package on the box that runs the trading loop, added
to colour a badge for three markets the bot does not trade. It is imported
lazily inside a function, every failure answers `None`, and uninstalling it
reproduces the old behaviour EXACTLY — `ClockFace.tracks_holidays` goes back to
False, Boxing Day opens again, and the suite stays green. That was measured by
uninstalling it, not asserted. Nothing raises, and nothing keeps claiming to
know.

**There is no network call, and that is measured too.**
`tests/test_market_clock.py` blocks `socket.socket`, `socket.create_connection`
and `socket.getaddrinfo` before touching the library, so a release that started
fetching fails the suite rather than putting a network call on a render path.
It gates nothing, for the same reason `session_calendar` does not.

**New York is deliberately NOT answered here.** Alpaca's calendar already does,
and it is the broker this bot actually trades through, so its reading is the one
that matters when two sources disagree. `ClockFace.calendar_code` is empty for
New York on purpose — a second source for the same fact is a second fact that
can disagree with the first, which is the reasoning that keeps
`Adoption.is_live` computed and `Dream`'s back-reference derived.

**Do not replace it with three hardcoded holiday lists.** They go stale in
silence, and a stale list still looks answered, which is strictly worse than a
stated limit. **yfinance is not the tool either** — it serves quotes, not
calendars.

This exists because a single global `sessions_utc` is wrong the moment there is
more than one class. Equities trade a fixed window, crypto trades continuously,
so a shared window meant enabling crypto silently forbade trading it for three
quarters of the day. Do not collapse it back to one list.

`Rules.allowed_symbols` is **derived**, unioning enabled classes, so disabling a
class removes its symbols everywhere at once.

**A session is a day and an hour, and `session_days_utc` has no default on
purpose.** `sessions_utc` was hours only, so 15:00 on a Saturday sat inside the
equity window `[14, 21)` and the gate approved. That was free while the loop
placed nothing and stopped being free the moment `--execute` went on: Alpaca
does **not** refuse an out-of-hours equity order, it queues it to the next
session, so a weekend proposal fills at Monday's open — inside the half-hour
the window exists to skip.

Both plausible defaults for the new field are wrong for one of the two classes
already configured. Monday-to-Friday silently shuts crypto for two days a week,
which is the per-instrument bug above wearing a new costume; all seven days
leaves the equity hole open. So an enabled class must declare it and the
config validator refuses one that does not.

**`sessions_utc` takes two shapes, and the second one is for Globex.**

```yaml
sessions_utc: [[14, 21]]                              # every trading day
sessions_utc: {0: [[0, 21], [22, 24]], 6: [[22, 24]]} # per weekday
```

The flat form covers a fixed-window market and a 24/7 one, which is everything
Alpaca offers. The mapping form exists because CME Globex — futures, crude,
gold — runs Sunday 17:00 CT to Friday 16:00 CT with a 60-minute maintenance
break each day, and **that cannot be written in the flat form at all**: Sunday
opens only in the evening, so `session_days_utc: [0,1,2,3,4,6]` would hand
Sunday Monday's hours and call the market open all Sunday morning. Multiple
windows in a day are what express the daily break.

Do not approximate a Globex session with a wider flat window. The gate would
approve into a shut market and the broker would queue the fill into the next
open, which is the weekend bug again with more steps.

With the mapping, `session_days_utc` is **derived from the keys** — two places
naming the trading days is two places to disagree — and supplying both is
allowed only when they match exactly. Everything downstream reads
`windows_by_day`, which normalises the two shapes into one.

**A Globex config is wrong for half the year unless someone revisits it.** The
windows are UTC and Globex is defined in Central Time, so every boundary moves
an hour when US daylight saving starts and ends. Nothing in the code can detect
that. It is a diary entry, twice a year, and it is called out in
`config/rules.yaml` beside the worked template.

None of it is reachable yet: **Alpaca offers equities, options and crypto, and
nothing on Globex.** A second broker behind the same `Broker` protocol is the
prerequisite, which is the case `max_total_risk_pct` was already made
leverage-neutral for. The session shape is ready so that adding one is an
adapter rather than a redesign.

**Market holidays are still not covered, and the rejection message says so.**
Thanksgiving is a Thursday and passes this gate. Closing that needs Alpaca's
calendar endpoint, which is a network call, and a network call does not belong
inside a gate that has to stay deterministic and must not fail open. The guard
is weekday-shaped, not market-open-shaped, and should not be described as more
than that.

### A broker-side stop OR an out-of-hours fill. Never both.

**This is the one to read before touching the order path.** It cost four hours
of a session to learn, and the shape of the mistake was not technical: every
individual statement was correct and the CONSEQUENCE was never put to the
operator as a choice.

`AlpacaBroker.place_order` attaches the stop, which makes every entry a bracket
or an OTO. **Alpaca's documentation says `extended_hours` is not accepted on
either** — and note that this half is DOCUMENTED, not observed: no
extended-hours bracket has ever been sent from here to watch it be rejected.
What was observed is the consequence, below. So:

- An entry that carries a stop **cannot fill outside the regular session.** It
  rests and becomes eligible at the next open.
- An entry that **can** fill outside the regular session is a plain limit order
  with `extended_hours=True` and **no stop at the broker at all**.

There is no third option, at Alpaca or anywhere else.

**So "place the trade now" and "get a position on now" are different
instructions out of hours, and the code can only satisfy one of them.** An
operator who asks for a position during the pre-market and gets a resting order
has been given the other answer to the other question. Ask which they want
BEFORE submitting — after the order is at the broker the choice has been made
for them.

**Observed live, and this half is measured rather than read:** 21 SPY submitted
09:23:47 New York — inside the pre-market — came back `filled_qty=0.0` and sat
resting. It filled after 09:30, in the regular session. So an entry carrying a
stop demonstrably does not trade in the pre-market and does become eligible at
the open, which is exactly what the model is told.

Correct behaviour, correct explanation, and **not what was asked for.** The
operator wanted the position on during the pre-market; there is no arrangement
of a stopped order that does that.

**The unbracketed path is not new ground.** Crypto already takes it — Alpaca
accepts no bracket there either — so the stop is a journal figure and
`stop_watch` reports the breach on the loop's pulse. Extending that to
out-of-hours equities is a new trigger for an existing arrangement rather than
a new concept, and it is in `TODO.md` rather than built, because it trades away
rule 3's broker-side guarantee and that is the operator's call to make
deliberately.

Worth knowing before deciding: on a short horizon it gives up less than it
looks like. A resting stop could not have fired out of hours anyway — a stop
becomes a MARKET order and extended-hours venues take limit orders only. What
is actually surrendered is the leg being there when the regular session
reopens.

### A schema change does NOT reach a database that already exists

`CREATE TABLE IF NOT EXISTS` is a no-op on a table that is already there. So
editing `journal.SCHEMA` changes what a FRESH journal gets and nothing whatever
about the one on the droplet.

**The test suite is structurally blind to this.** Every test builds its journal
from scratch in a `tmp_path`, so every test always gets the new shape. 866 tests
were green over a database that could not store the row the models had just
been changed to allow.

Found by placing a real order. `take_profit_price` became optional; the model
and `SCHEMA` were both updated; the first no-target order reached Alpaca, rested
correctly — and then the journal write failed on `NOT NULL constraint failed:
trades.planned_target`. **The broker call happens BEFORE the journal write**, so
the result was a live order the journal had never heard of: `14b88c8` through a
new door, with `open_risk_usd` unable to count a position it has no record of
and the 2% cap blind to it.

`_drop_planned_target_not_null` rebuilds the table, because SQLite cannot drop a
NOT NULL with `ALTER`. Three properties are load-bearing:

- **One transaction.** Either the new table replaces the old with every row
  intact or nothing changes. A half-migrated journal is worse than an
  unmigrated one.
- **Idempotent and cheap.** It runs on every open; `PRAGMA table_info` is a
  lookup, so a correct database pays one query and stops.
- **The test builds the OLD schema deliberately**, which is the only way to
  exercise a migration at all, and asserts the existing rows survive. A
  migration that dropped history to fix a constraint would be a far worse trade
  than the constraint.

**Any future change to `SCHEMA` needs a migration beside it and a test that
starts from the old shape.** The suite cannot tell you that you have forgotten.

### The stop is a real order now, and it still cannot fire out of hours

`place_order` used to submit the entry limit order and nothing else.
`stop_loss_price` was validated by the gate, used to size the position, written
to the journal — and **never sent to Alpaca**. The operator's third rule is
"hard stops on every trade", and it was true at sizing time and false at the
broker: nothing was resting there that would have closed a losing position.

It is a **GTC bracket** now, and GTC rather than DAY is the whole point: a DAY
bracket's legs expire with the session, so a position held overnight would sit
unprotected from 16:00 until somebody noticed. GTC legs stay active across days,
surviving the close, the overnight session, the pre-market and the weekend.

**What no order type from any broker buys is an out-of-hours exit.** A stop is a
trigger that becomes a MARKET order, and extended-hours venues accept limit
orders only, so the leg rests through the night and is eligible again when the
regular session reopens. A gap through the stop fills at the open rather than at
the stop price. That is how every retail stop behaves; it is not an Alpaca
limitation and it cannot be configured away.

Three things the bracket structurally cannot cover, which is why
`stop_watch.py` runs on the loop's fifteen-minute pulse:

- out-of-hours, for the reason above
- **crypto, which Alpaca does not accept brackets on at all**
- a position adopted from the broker, or one whose bracket was cancelled by
  hand: a journalled stop with no order behind it

### The exit is the agent's, and a trail is ONE number

`OrderProposal.take_profit_price` is optional — `None` sends an OTO (entry plus
stop) rather than a bracket, so nobody invents a level to satisfy a validator,
and the journal migration `_drop_planned_target_not_null` exists because of that
change. `trail_percent` is the other half: `None` is a fixed stop, a figure IS
the trail.

**One field, not a field plus an `exit_style` enum.** A second fact about one
decision is a second fact that can disagree with the first, the same reason
`Adoption.is_live` is computed rather than stored. Percent rather than an
absolute distance because it means the same thing on SPY at 773 and BTC/USD at
65,000, and Alpaca's own trailing order takes it unchanged.

**`trailing_stop_level()` can only ever TIGHTEN.** It is floored on a long and
capped on a short by the stop already in force, so a trail is never a route to
widening what the position was sized against. A non-positive high-water mark is
refused rather than absorbed, because two sides of one function must not fail
differently.

**"A broker-side stop OR an out-of-hours fill" has a second instance here, and
this one was verified against the installed SDK.** `StopLossRequest` — the only
stop a bracket or an OTO can carry — has `stop_price` and `limit_price` and
nothing else, while `TrailingStopOrderRequest` is a standalone order type. **So
an entry cannot carry a trail.** `place_order` therefore rests the FIXED initial
stop exactly as before, which keeps rule 3 intact from the first instant at the
level the position was sized against, and `OrderResult.stop_at_broker` reports
which kind is actually resting rather than leaving a caller to assume.

**`stop_watch` reports and never closes.** Closing out of hours needs a
marketable limit order, which is a new execution path, and one that fires
unattended at 3am is a different proposition from one an operator watches.
Making the breach loud — the log line, an audit event, the cycle summary — is
the honest intermediate, and automating it is its own decision.

A symbol with no quote is **skipped rather than assumed safe** and named in
`stops_unchecked`. `fetch_market_ticks` drops a symbol whose fetch failed, so an
absent tick means "not checked", never "fine" — the `calendar_degraded` lesson
again. The breach count is on the `cycle_complete` line so that a zero is a
stated fact each cycle rather than the absence of a warning, which is also what
an outage looks like.

Compared against the **mid**, not the touch: a wide out-of-hours spread would
otherwise trip a long on the bid and a short on the ask, reporting a breach the
traded price never reached.

### The stop's WIDTH is reported, and the gate still holds no opinion on it

Size is the risk ceiling divided by the stop distance, so **the denominator is
what decides how large a position gets**. Measured live: `qwen3-coder-flash`
proposed KO with a $0.05 stop against a $1.32 ATR — 0.04 ATR, inside the
spread — which at the same stated dollar risk buys a position roughly
twenty-six times larger than a 1-ATR stop would. Every figure the sizing block
prints stays correct while that happens and the division is performed
honestly; the exposure arrives through the denominator, where nothing was
looking.

`src/bot/stop_width.py` measures it and states it, and **refuses nothing**.
That is the load-bearing half. `_stops_on_correct_side` checks only which SIDE
of entry each level sits on, deliberately and permanently, and a minimum stop
distance would be an opinion on placement dressed up as a limit — a rule
nobody agreed to, arriving through the back door. Do not "improve" this into a
rejection.

Four properties:

- **Two counters, never one.** A symbol with no ATR is `stops_unmeasured` and
  is never counted as not-tight. `stops_unchecked` one column across: counting
  it as fine would make the reassuring answer the one an outage produces.
- **A zero ATR is unmeasurable, not infinitely tight.** A flat instrument has
  no denominator, and a very large number there would be a plausible wrong
  figure on the surface built to refuse them.
- **The Decisions page DERIVES it** from `MarketInputs.readings` — the figures
  that cycle recorded — rather than reading a stored field, so there is no
  third fact to disagree with the other two. One `CarriesATR` protocol serves
  both `Indicators` and `IndicatorSnapshot`, because two implementations of one
  piece of arithmetic are two answers and the one on the page is the one nobody
  re-checks. A record predating `readings` renders NOT MEASURED, which is
  honest: nothing recovers a number never written down.
- **It renders AFTER the gate's verdict**, so nothing can read it as an input
  to a decision it had no part in. The tight case states the CONSEQUENCE
  ("26x") rather than only the ratio, because 0.04 reads as a small number and
  the position it buys is a large one.

### The request names a model; the response says what answered

Two endpoints serve this repository's model calls, and they can disagree
without anything raising. A catalogue aliasing a retired id, a proxy named in
`ANTHROPIC_BASE_URL` routing elsewhere, an endpoint serving a familiar name
from unfamiliar weights — every one succeeds, validates against the schema, is
priced from the REQUESTED model's price sheet, and produces a completely
ordinary-looking cycle whose orders were sized by weights nobody named. This
is the endpoint-versus-configuration mistake recorded above, one level in.

`CallUsage.served_as_requested` and `Decision.served_as_requested` are both
three-valued, and the third value is the point: `None` is "no model id came
back", which must not collapse into agreement or into a substitution nobody
performed. The response's `model` field is read defensively for the reason the
token counts are — the SDK builds responses with unchecked construction — and
a non-string reads as unknown rather than being coerced with `str()`, which
would manufacture a mismatch out of an object that is not a name.

**It REPORTS and never refuses.** An alias is an ordinary thing for a
catalogue to do, and throwing away a validated decision with its proposals,
its assessments and its spent cost over a naming difference would cost far
more than it protects. It reaches `claude_responded`, `cycle_complete`, the
`Decision` audit record, and the Decisions page — where it is named beside the
COST, because that is the figure a substitution makes wrong.

### A fourth agent that can reach the web, and nothing else

`souls/kuiil.md` and `deploy/run-research.sh`. The dreamer reasons about
cicada broods with no way to look anything up, so every hop it writes is
reference knowledge it already had or an invention. The researcher goes and
looks.

**It returns quotes and URLs and never conclusions, because a summary launders
provenance.** The distilled sentence has no author and no date; the paragraph
it came from had both, and after the distillation a reader cannot tell which
half was published and which half the model supplied. So `research.Citation`
has nowhere to put one — no summary, no implication, no significance — and the
field overlap with `OrderProposal` AND with `Dream` is pinned empty. The second
is the subtle half: `Dream` carries `symbols` and `asset_class_key`, which are
a live permission once adopted, and a route from a fetched page to a
tradeable-symbol claim is the connection this must never make.

**Nothing web-derived may become a gating input**, which is `docs/HANDOFF.md`'s
own rule. The module imports none of risk, broker, journal, reconcile,
mcp_server, grants, models, dreaming or position_actions, proved by parsing
its AST — the shape of the test pinning `TraderPowers` away from the broker —
and it makes no network call itself.

**A third Hermes home, because the quarantine has to be the process.** No
`mcp_servers` block at all, and the wrapper greps its own config and refuses
if one appears — and refuses equally when the config cannot be READ, because
"I could not establish that this instance is isolated" and "this instance is
isolated" are different answers. Its config is an ALLOWLIST where the other
two are denylists: a denylist admits whatever the next release adds, and here
that is a web-reading process gaining a capability nobody chose, invisibly.

`KNOWN_SOULS` is narrower than `ALL_SOULS` now. Kuiil ships and no chat
request may select it, because the Chat page's instance is the one holding
`place_order`. The registry test enumerates the DIRECTORY rather than a
hand-written tuple, and every shipped soul is held to the shared rails — a
fourth character must not arrive carrying none of them.

**`RESEARCHER` is a third A2A speaker and costs a constant**, because
`DreamMessage.speaker` was already open for exactly this. It is not an
`AGENT_SPEAKER`, so it does not move `confer.last_agent_turn_at` — a citation
is not a negotiation, and moving the marker would silence the change it just
created. It IS a new voice to `has_something_changed`, which is right: a
published source under the weakest hop changes what adopting the dream means.

### The journal records the PROPOSAL, and a fill is not atomic

`record_fill` runs immediately after `broker.place_order` and writes the
proposal's quantity and the proposal's limit price. Neither is necessarily what
the broker did.

Observed on the first real order: the limit was 772.84 and the fill averaged
773.324285, so `open_risk_usd` read $990.36 against a real $980.19 until it was
corrected by hand. Overstated is the safe direction and it is still a figure
that does not describe the account.

**A fill is not atomic, and one poll during it is a reading rather than an
outcome.** A check mid-fill on that order returned `FILLED 3.0` of 21 and was
briefly recorded as a partial fill; the order completed moments later. Anything
that samples an in-flight order has to treat the answer as a snapshot.

**`Trade` cannot express a partial fill at all** — one `qty`, one
`entry_price`, no concept of 3 filled now and 18 later, or of 3 filled and the
rest cancelled. That is a gap in the model, not a bug in a caller.

Recording at submission is not simply wrong, and it stayed: the alternative,
waiting for a terminal order state, leaves a live position unjournalled in the
meantime, which is the `14b88c8` hole. Recording early overstates risk for at
most one cycle; recording late understates it to ZERO for as long as the order
takes. **`reconcile` corrects it instead**, and `submitted_qty` /
`submitted_price` keep what was actually ordered, so the correction does not
erase the intention.

**It REFUSES to correct in four states, each named on `ReconcileResult` rather
than silently skipped:** two open trades in one symbol or a direction mismatch
(Alpaca aggregates per symbol, so its average entry is neither row's fill); a
position larger than `submitted_qty`; no `entry_order_id`; and a degraded order
read, because `get_open_orders` returns `[]` on its own failure and that means
"could not ask", never "nothing resting".

**The bug that fell out of building it:** step 1 closed any journal-open trade
the broker did not hold — so an out-of-hours entry, which rests until the next
regular open, was written off as closed on the very next cycle and then filled
into a position with no journal row. The close is deferred while the entry
order is still live.

### An exit says why it ended, and the P&L half stays out

`record_exit` used to take a price, a time and a realised figure, so stop-hit,
target-hit, closed-by-hand and expiry were indistinguishable afterwards.
`ExitReason` classifies it, which answers the one question worth asking on a
close: **did this end the way it was designed to?**

The interesting bucket is **closed by hand before either level** — the plan
being abandoned, which is discipline rather than luck.

**`None` is not `ExitReason.UNKNOWN`.** Nothing was recorded, versus the question
was asked and could not be answered. Same rule as `has_cycles`,
`can_grade_anything` and first-visit.

This belongs beside `triggers.py` and `DreamLedger`, never beside `metrics.py`:
those grade plan-following and reasoning quality, which are true regardless of
how a trade went and have no outcome sample to overfit to. **"Review the trade
so it can learn" is the reasonable-sounding request this repository exists to
refuse** — forty trades is noise, a model shown three losses will confidently
change approach, and that is the Alpha Arena failure arriving as a feature
request. The operator learns from the track record; the model learns from
nothing.

### The journal must be wired in or the caps count nothing

`AccountSnapshot.open_risk_usd` is what the total-risk cap counts against, and it
can only come from the journal — Alpaca holds stop-losses as separate orders, so
the broker cannot report it.

Every path that hands an account snapshot to the risk gate must populate it:
- CLI: `reconcile()` then `apply_journal_state()` in `src/bot/main.py`
- MCP: `_Session.account()` in `src/bot/mcp_server.py`
- Web: `LivePoller._read()` in `src/bot/web/live.py`

**All three go through `apply_journal_state` and hand it the whole snapshot,
which is deliberate rather than uniform for its own sake.** That function
derives FOUR figures from one journal read — the total, `open_risk_by_symbol`,
`planned_stop_by_symbol` and `symbols_with_unknown_risk` — and the poller used
to call `journal.open_risk_usd` and assign the total by hand, leaving the other
three at their empty defaults. Nothing in `web/` read them, so it was not a live
fault; it becomes one the first time a surface renders a class's risk, because
an empty breakdown reads as *this class risks nothing*. Passing the snapshot in
rather than taking one number out means a caller structurally cannot fill one
figure and forget the rest, and it keeps the total and the breakdown describing
the same set of open trades.

Miss it and the cap silently has nothing to count. This was a real bug, fixed in
`14b88c8`. A held position with no journal entry has an unknowable stop, so its
risk is reported as **missing** rather than guessed at.

**TWO gates now go further and REFUSE on that missing figure**, and the second
one is the account-wide 2%. The per-class total-risk cap was the first, where
an unknown in the class means the cap cannot be enforced at all.
`_total_risk` joined it because the same argument turned out to apply to the
portfolio rule and nothing was making it — measured against the shipped
config, an unknown position let a proposal through onto a book already at
2.79% of a 2% cap, because `max_class_total_risk_pct` is configured on crypto
alone and crypto is disabled. So in the shipped configuration NO gate refused
on missing risk, while this file said one did.

**The live consequence is real and is the right way round.** While a held
position has no journal row, nothing new opens. That is recoverable by closing
the position or journalling it, and it is better than a 2% rule that binds
only when the paperwork happens to be complete.

Reporting is still the rule everywhere else — `reconcile`'s
`risk_is_understated`, `stops_unchecked`, `calendar_degraded`. What separates
these two is that they are the gates whose ARITHMETIC is the missing number:
a cap cannot be applied to a total it cannot compute, so there is no partial
answer to report.

### A resting stop whose level nobody can read is most of the way to no stop

`WorkingOrder` carries `limit_price` and no `stop_price`. So a stop leg resting
at the broker renders as `limit_price=None` on every surface that shows working
orders, and nothing in this repository can state what level it will trigger at.

That was survivable while nothing sent a stop to the broker. It is not now:
entries go out as brackets and OTOs, the stop leg IS the thing rule 3 depends
on, and the operator can see that a leg exists while having no way to check it
is at the price the journal says. The journal's `planned_stop` and the broker's
actual trigger are two different facts and only one of them is visible.

Add `stop_price` to `WorkingOrder` before trusting a displayed stop.

### The command centre is the only surface now

There used to be two — this one and a public marketing site under `brand/` —
and confusing them was the mistake to avoid. `brand/` is deleted, so there is
one, and the thing to avoid now is building a second. See "Nothing is published
any more".

`src/bot/web/` renders **live** journal, broker and audit state, on seven pages:
Board, Decisions, Trades, Analytics, Chat, Dreaming, Settings — that is the nav
order, and Chat sits ahead of Dreaming because Chat is the dominant page. It
binds to `127.0.0.1` and is usually reached over Tailscale, and it **now sits
behind a shared password** (`src/bot/web/auth.py`, enforced as middleware), so
it may be exposed publicly. See "Nothing is published any more" for what that
gate is and is not.

That middleware is the reason a new route is refused by default: opening one
takes a deliberate edit to `OPEN_PATHS` *and* a classification in
`tests/test_auth.py`, which enumerates the routes from the application rather
than from a hand-maintained list.

**A cycle records what it considered, not only what it proposed.** `Decision`
carries `assessments` (one per symbol, with a stance of take/watch/pass/blocked
and, for a watch, the observable condition that would trigger it),
`position_plans` (why each open position is still on and what would close it)
and `inputs` (the headlines, blackout windows and indicator readings the model
was actually shown). Every one is optional with a default, because the audit log
is append-only and never migrated: a reader that rejected yesterday's format
would throw away the history it exists to preserve.

Those fields cost output tokens on a quiet cycle, which is most cycles, and that
is the point. Without them a quiet cycle records "no proposals" and nothing
else, so "nothing met the conditions" reads identically to "the loop never
looked at QQQ".

`position_plans` are **advisory and never executed**. Closing a position and
moving a stop sit outside the proposal path deliberately, so the page says so
plainly rather than leaving an operator to assume a "close" recommendation was
acted on.

**The Decisions page is the only surface on which a rejected proposal exists.**
A proposal the gate refuses never becomes a trade, so it reaches neither the
journal nor the broker: the reasoning lives in `audit/*.jsonl` and nowhere else.
That is why `audit.py` reads as well as writes, why the reader is tolerant of a
torn final line rather than raising, and why it counts what it could not parse
instead of silently dropping it.

Three properties there are load-bearing:

- **`RiskGate` collects every failure reason rather than short-circuiting.** The
  page renders all of them. Showing only the first would quietly discard the
  property the gate is built around.
- **A broker refusal is not an execution.** `DecisionEntry.acted` checks
  `accepted`, not merely that an `OrderResult` exists, or the one cycle where
  money did not move gets labelled as the cycle where it did.
- **"Held" is a normal outcome, styled as one.** Doing nothing is frequently
  correct and the page must not read it as a failure.
- **A watch with no named trigger is called out as empty.** "Waiting for more
  confirmation" is not a plan, and rendering it as one would let the model look
  like it has a view when it does not.

**Hermes' own memory DOES survive across chat turns, and that is measured
rather than assumed.** `run-chat.sh` ends in `exec hermes -z`, so every message
is a fresh process — which made it reasonable to think each turn started blank,
and the page note only ever claimed the *dashboard* keeps nothing. Tested
directly: told it a fact, reloaded the page to clear the browser-held replay,
asked again, and it answered correctly.

Two things follow.

- **There is no Hermes daemon to restart.** No `hermes.service` exists, and
  `deploy/systemd/` installs none. Tools are re-enumerated on every message, so
  a new MCP tool is live the moment the server changes — no restart step, and
  telling an operator to run one sends them chasing a unit that is not there.
- **Chat content is persisted on disk under `/home/hermes/`.** Account
  questions and their answers therefore outlive the browser tab. That directory
  is not backed up, which is right — it is not authoritative — but it is also
  not pruned, and nothing in this repo manages it. Worth knowing before
  treating the Chat page as ephemeral.

The dashboard's own continuity is separate and deliberately small: the browser
holds the turns, POSTs them, and `chat.py` replays only the last
`HISTORY_TURNS` (6). Refresh the page and that replay is empty — which is
exactly what makes the test above valid.

**A textarea inserts nothing on Ctrl/Cmd+Enter.** Plain Enter is the key that
inserts a newline by default, so making Enter *send* means the newline chord
has to insert one **by hand** at the caret and move the caret past it.
Rebinding Enter and assuming the browser still handles the other chord leaves a
key that silently does nothing — the worst kind of broken, because it looks
like the app ignored you. Shift+Enter is left alone precisely because the
browser does handle it. `e.isComposing` is checked first, or an Enter
confirming an IME candidate sends the message mid-word.

**Never put a backslash in `render.STYLES`.** It is an ordinary Python string,
so a CSS hex escape is read by *Python* first, as an octal escape: the
stylesheet receives a control character and the browser draws a tofu box beside
the leftover digits. Nothing warns, `ruff` does not care, and it is invisible
unless somebody looks at the rendered page. Use the literal character.
`tests/test_web.py` fails the build if a control character appears in there.

**Settings CAN change a limit now, and only through the Armorer.** It used to
offer no edit control at all, on the argument that a screen which could widen a
cap would be used to widen one during a losing run. The operator overruled that
and was right: *"Settings agent can't edit settings?? That's broken. That's what
setting agent is for, to give Josh an educated experience into why settings are
important."* A wall teaches nothing, and recording a chore for somebody to apply
at a shell is the same wall with an extra step.

So the route is `src/bot/settings_agent.py`, and **the asymmetry is the part
that must not be simplified away.** Tightening is recorded as asked. Loosening
states the arithmetic, raises the Armorer's objection, and waits for a second
explicit agreement after it has been read. `config/` stays root-owned — the
service account still cannot edit its own limits — and the change is applied
through a root-owned wrapper with the request id on stdin, the same shape as
`run-chat.sh`.

`LimitFact` answers **four** separate questions per limit — what it is, why it
sits there, the goal it serves, and what loosening costs. Four rather than one
paragraph, because collapsing them is how "it is for safety" ends up being the
whole justification for a number.

`tests/test_web.py` used to assert Settings had no `<input>` at all. **That
assertion was widened three times by editing it, never by deleting it**, so what
the page may contain is still enumerated rather than unconstrained.

Each limit still names the file that owns it. Credentials are still reported as
configured or not configured, never rendered: loopback-bound is not the same as
private, and a screenshot travels.

**The forge window (`web/forge_window.py`) shows both values at once**, because
"raise it to 2.0" is a number with nothing to compare it against and `1.0 → 2.0`
is a change. The old side is **the exact text on the line**, never a
re-rendering of the parsed number — `90000` and `90000.0` are one limit and two
different diffs.

### Two transports, and which one runs is a property of the ENDPOINT

Every Python model call — `propose`, `dream`, `confer` — goes to DigitalOcean
when `DO_INFERENCE_KEY` is set. Empty means Anthropic, which is still a fully
supported configuration, so the rollback is unsetting a variable.

**It is not a base-URL swap, and the reason was measured rather than reasoned
about.** DigitalOcean's `/v1/messages` accepts `output_config` with **HTTP 200
and silently ignores it**, returning prose. Not a 400. That is the worst of the
three possible answers: a caller checking only for an error would believe the
schema was in force while it was not.

So `ModelClient` has two transports. `messages.parse` where the schema is
enforced server-side, and a **forced tool call** where it is not — one tool
whose `input_schema` is the model's JSON schema, `tool_choice` pinned to it, and
the arguments validated **here** by Pydantic.

**It keys on `Env.inference_provider.is_digitalocean` and never on the model
id.** Whether a schema is enforced is a property of the thing SERVING the
request, not of the weights behind it; the same model is served with enforcement
at one endpoint and without it at another. A check on the model name gets that
exactly backwards the first time a familiar name appears in an unfamiliar
catalogue.

**That sentence was written before it was true, and an audit proved it false.**
`Env.inference_provider` describes a CONFIGURATION; it did not describe the
endpoint. The SDK resolves the base URL as `kwarg > ANTHROPIC_BASE_URL > a
credentials profile on disk`, and `ModelClient` passed no `base_url` on the
Anthropic branch — so a box with `ANTHROPIC_BASE_URL` aimed anywhere got the
banner saying "Anthropic direct", the server-enforced transport with
`output_config` attached to a proxy that may ignore it, and **the Anthropic key
in the header, sent to that third party**. Not hypothetical: both timer units
carry `EnvironmentFile=/opt/mudhorn/.env`, and `docs/DROPLET_AI.md` tells the
operator to export exactly that variable.

`base_url` is now passed explicitly on BOTH branches, which also closes the
profile-file route — the SDK consults no other source once a kwarg arrives — and
`Env.anthropic_base_url` is a declared field so the property can report the
endpoint that will actually be called. A deliberate proxy is NAMED rather than
refused, with the sentence saying this process cannot verify that endpoint
enforces the schema.

**The lesson is the one this file already records about the class hard-block: a
guarantee written here is not a guarantee, and prose asserting one is how it
stops being checked.** Where this file claims a structural property, there must
be a test that fails when it is removed.

**A reply with no tool call is a HARD FAILURE, and that is the one that
matters.** A dropped schema breaks loudly in every case that produces a number
and silently in exactly one: the quiet cycle. `proposals`, `assessments` and
`position_plans` all default to `[]` in Python — correctly, an empty list is a
real answer — so a bare `{"market_assessment": "..."}` parses as a completed
cycle that considered nothing, which is indistinguishable afterwards from a loop
that never looked. `qwen3.8-max` returned prose on 2 of 3 attempts against the
real schema *with `tool_choice` forcing the call*, so this is live rather than
hypothetical.

**The same hole has a second entrance, and it is CLOSED too.** A tool call that
IS made and comes back with `assessments: []` after being shown symbols arrives
at the identical place — `llama3.3-70b-instruct` does that on 6 of 10 samples,
in 2.2 seconds, and passes validation. `refuse_a_decision_that_considered_nothing`
raises `ConsideredNothing` into the existing `model_call_failed` path.

It lives in `main.py` rather than in the schema or the transport **because the
fault is a RATIO and only `cmd_loop` knows the denominator** — and which
denominator is the whole decision. It is `indicators`, not the symbols the loop
intended to look at (a cycle whose bars all failed would be refused as a model
fault when it was a feed fault) and not the ones carrying a quote (a symbol with
no history is one the context tells the model to propose nothing on, so
demanding an assessment for it would fail the cycle for obeying an instruction).
`indicators` is what the output contract is written against and is the same
object handed to `build_market_context`, so the check cannot drift from what was
rendered.

**Zero is the trip, never a shortfall**, and `position_plans` is reported rather
than refused. Same shape, different stake: an unassessed symbol is
unrecoverable, because the audit log is the only place a considered-and-passed
symbol is ever written down, while an unplanned position is in the journal, on
the Board, in `reconcile`, in `stop_watch` and behind a resting stop leg.

Three refusals rather than one, kept apart because they are three different
findings about the far end: no tool call at all, a tool call whose argument KEYS
are the model's own markup (`glm-5.2` half-parsed by the proxy), and arguments
Pydantic rejects (`openai-gpt-oss-20b` inventing `ticker`/`action`/`shares`
against a schema demanding `symbol`/`direction`/`qty`).

**What is lost is narrower than it sounds; what is kept is the part that
matters.** Pydantic still rejects a malformed object, so a `qty` or a
`stop_loss_price` that comes back wrong is refused rather than coerced,
`model_call_failed` is logged and `RiskGate` never sees it. What degrades is
RELIABILITY: the model is asked for the shape rather than constrained to it, so
the rejection rate rises and every rejection costs a cycle.

**An OMITTED key is a third case, and it used to be the worst of the three.**
`EVERY_FIELD_REQUIRED` puts every property into the schema's `required` list, so
the API's grammar makes an absence impossible on the server-enforced path.
`model_validate` does not: it honours the Python defaults, which every one of
these fields has. Measured across the five shapes actually sent, **24 properties
could be left out** — and an omission was not rejected, it became a value
invented HERE and attributed to the model. A `PositionPlan` carrying only
`symbol` and `reasoning` was recorded as `action=hold, thesis_intact=True`: an
opinion about an open position that nothing on the far end expressed, rendered
to the operator as one that was.

`_missing_required` walks the emitted schema and refuses an ABSENT key while
never refusing an empty one — so `assessments: []` still reaches `cmd_loop`'s
ratio check rather than being swallowed here. The two failures are different and
are caught in different places on purpose.

**`scripts/do_schema_fidelity.py` graded with the loose rule**, so every score
in `docs/DROPLET_AI.md` recorded before 13 Aug was measured against a laxer
system than the one that runs. It uses the transport's own check now. Re-run
under it, the four candidates came back 10/10 with zero omissions — the hole was
real and these models did not walk into it.

**Nothing falls back, in either sense.** No prose parsing after a failed tool
call — that hands back exactly the freedom the schema exists to remove. No retry
onto a second model. And **no falling back to the other PROVIDER**: a
`DO_INFERENCE_KEY` that is set but unusable REFUSES rather than quietly
answering from Anthropic, which would run, bill the wrong account and leave
nothing on any surface saying which model produced the orders.

**A Claude tier default named against DigitalOcean refuses at construction.**
That endpoint calls Anthropic models by different ids (`anthropic-claude-5-sonnet`,
not `claude-sonnet-5`) and 403s the ones it lists on this account's tier, so it
is a guaranteed failure — caught once, rather than 96 times a day inside the
loop's broad `except` as one more skipped cycle. `DECISION_MODEL_ID` and
`DREAM_MODEL_ID` are how a model is named.

**The guards moved because they were asking the wrong question.** `smoketest`,
`dream` and `confer` each tested `ANTHROPIC_API_KEY`. With two endpoints that
passes over a half-finished swap — endpoint set, model access key not, Anthropic
key present — which is a configuration the operator believes is live.
`model_calls_are_impossible` asks about the CONFIGURED provider's own
credential. The loop asks the narrower `provider_is_unusable`, and that
asymmetry is deliberate: a cycle with no model call still reconciles the journal
and runs `stop_watch`, and that safety work is worth more than the proposal.

**Three groups of vendor names are deliberately NOT renamed**, and each would
break something. `import anthropic` and `ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY`
are read BY the SDK — DigitalOcean speaks the Anthropic wire format, so renaming
them breaks the transport. `sends_anthropic_thinking` and
`sends_anthropic_effort` are named after the wire fields they control, so a
generically-named flag would send the wrong field in the wrong shape at an
endpoint that wants a flat `reasoning_effort`. And the `claude_*_tokens` fields
are written into `audit/*.jsonl`, which is append-only and never migrated — a
rename would read every historical cycle back as `0 in / 0 out`. The
model-facing half is already clean and `tests/test_model_client.py` pins it: no
schema, prompt, soul, journal or audit value sent to a model carries a vendor
name, because Pydantic turns a docstring into the schema's `description`.

**Caching is unverified at that endpoint and fails silently in the money
direction.** `cache_control` is not in DigitalOcean's published request schema,
and a dropped one does not raise — it bills 10x on the system block forever.
**MEASURED 13 Aug 2026: it does not engage.** The real system block (4,791
tokens) sent twice eight seconds apart with `cache_control` came back HTTP 200
both times with every cache counter at zero and an identical `input_tokens` on
the repeat. Accepted and ignored, exactly like `output_config`. "Reported zero"
cannot distinguish *not cached* from *cached but unreported* — only the billing
page settles that — so the planning assumption is that the system prompt is
billed in full on all 96 cycles a day.

**`cached_tokens` is NOT on the `cycle_complete` line**, which this file
previously said it was. It reaches `audit/*.jsonl` — on the `Decision` record as
`claude_cached_tokens`, and on the `model_cost_unknown` event — so it is the
Decisions page and a query that answer it, not the heartbeat. Checked against a
live cycle, because the claim had been repeated into three documents without
anybody reading a log line to confirm it.

### The sizing ceilings are two UNITS, and the model compares numbers

The caps reach the model as percentages and the inputs in dollars, so it was
doing five steps across two documents and skipping the one requiring a
subtraction — 185 shares proposed where 91 was permitted. `context.sizing_ceilings`
renders the ceilings in dollars, which fixed that.

**It moved the error rather than removing it.** Measured over 320 live calls:
the subtraction is now handled, and every material over-size is CROSS-UNIT — the
model does the RISK division and never checks the POSITION VALUE ceiling.
`deepseek-v4-pro` was 7.1x over on buying power five times, once 14.4x.

**The block already printed both units and already said "take the smaller".**
That sentence was in the document for every one of the sixteen over-sized
proposals, and it is not even well posed: the units differ, so the smaller
NUMBER is not the tighter constraint, and a model comparing them as numbers
picks the risk ceiling every time.

So the two are made COMPARABLE instead. With risk ceiling `R`, value ceiling
`V`, entry `p` and stop distance `d`, value binds when `V/p < R/d`, i.e. when
`d/p < R/V` — and **`R/V` carries no price and no stop**, so it is computed here
and rendered as one dimensionless figure. The model makes one comparison against
its own stop and one division. That is the shape that worked for the
subtraction, rather than a second figure beside the first.

**The finding that reframes it: on the shipped rules the crossover for
`us_equity` is 2.000% of entry.** One ATR on a $500 name is well under that, so
POSITION VALUE is the binding unit for essentially every realistic equity stop —
the block was presenting the ceiling that binds MOST of the time as the
afterthought.

**A crossover must never be phrased as "your stop must be at least X%".** That
is an opinion on placement arriving through the renderer — a minimum stop
distance nobody agreed to, and one obeyed by WIDENING the stop, which buys a
bigger loss at the same percentage. It is stated as a property of the
arithmetic, with the disclaimer on the same line, and a test pins it.

Still no worked maximum quantity, for the reason that has not changed: both
branches divide by something only the agent knows, so neither resolves to a
share count, and a worked example at the current price would read as a
recommendation to trade at that size.

### A stop tightened to nothing is REPORTED, never refused

Size is the ceiling divided by stop distance, and `RiskGate` deliberately holds
no opinion on placement — `_stops_on_correct_side` checks only which side.
Measured: `qwen3-coder-flash` proposed a $0.05 stop against a $1.32 ATR, 0.04
ATR, inside the spread, which at the same stated risk buys a position about
twenty-six times larger than a 1-ATR stop would.

`context.stop_width` measures every proposal against the ATR the loop already
recorded and states the multiple on ALL of them, so a clean cycle is a stated
fact rather than the absence of a warning. A symbol with no ATR is **UNMEASURED,
never fine**.

**Do not close this by making the gate reject a tight stop.** That is the
placement opinion this repository refuses to put in the gate, and
`tests/test_context.py` drives the real gate at a 0.008-ATR stop and asserts
APPROVED — so the tempting repair is a red build. The real protection is the
VALUE ceilings, which is why the crossover above had to land first.

The prompt says a stop is a claim about where the thesis is wrong rather than a
lever on size, and states **no numeric threshold** — a limit in the prompt is a
limit nobody can read off `config/rules.yaml`.

### The served model is read back, and reported rather than enforced

`CallUsage` carries `requested_model_id`, `served_model_id` and a three-valued
`served_as_requested`. Read with the same suspicion as the token counts: the SDK
builds responses with `construct_type`, which does not validate, so a non-string
is treated as **absent** rather than `str()`-coerced — a coerced `None` becomes
the literal `"None"`, an id matching nothing, inventing a substitution out of a
transport fault. Empty is *could not ask*, never agreement.

A dated snapshot of the requested alias counts as the same model, and the
exemption is deliberately narrow — hyphen plus exactly eight digits — because a
bare prefix rule would mask `nemotron-3-ultra` → `nemotron-3-ultra-550b`, which
is exactly the substitution this exists to catch.

**It does not fail the cycle, and that is a decision rather than an omission.**
Nothing had ever recorded which model answered, so there is no evidence of how
often the two differ at that endpoint, and a hard failure keyed on an unmeasured
comparison could kill 96 cycles a day. Report it, read the audit log for a week,
and only then decide. Note `served_as_requested is None` must never be a failure
even then: a proxy omitting the field is a transport gap, not a substitution.

### The model call is a feed too, and prose truncates while numbers reject

The same rule as the feeds below, learned the hard way. `claude.propose` was
the one network call in the cycle with nothing around it, and a live cycle died
because a rationale came back 34 characters over a 500-character cap. The
`ValidationError` propagated out of the SDK, killed the process, and systemd
restarted it straight into the same failure.

That was survivable while the loop placed nothing. With `--execute` on it means
real orders resting at the broker, the journal no longer reconciled and open
positions no longer watched — with nothing on screen to say the bot has gone.
The call is now wrapped, and a failed cycle logs `model_call_failed` and
records an audit event rather than emitting `cycle_complete`: a cycle that
could not get a decision must not be recorded as one that decided to do
nothing.

**Free-form prose truncates. Numbers reject.** `rationale`, `reasoning` and
`waiting_for` trim to `RATIONALE_MAX_CHARS` with an ellipsis, because nothing
downstream parses them — losing the tail of a sentence costs a reader some
context on the Decisions page, where a rejected response costs the whole cycle.
**Do not extend that leniency to a price or a quantity.** A truncated number is
a different number, and a plausible wrong figure that passes validation is the
exact failure this repository exists to prevent.

### The model has no memory, so the watch list is handed back to it

Every cycle is a fresh API call. The 1-hour prompt cache is a cost optimisation
on a static system prompt, not memory, and weights never change. So the model
was writing `waiting_for` triggers — "SPY closing below 641.20, roughly 1 ATR
under the 20-day" — and never seeing them again. A watch was a sentence written
to nobody, and the stance meant nothing.

`build_market_context` now carries **the previous cycle's assessments only**,
under "What you said last cycle", and the prompt tells the model to report on
each: fired, not yet, or stale.

- **One cycle back, never a history.** A running transcript costs output tokens
  every cycle for diminishing value, and a model handed its own narrative starts
  defending it. One cycle answers "did the thing I said I was waiting for
  happen", which is the whole point.
- **The section sits AFTER the indicators and intraday blocks**, so a trigger is
  checked against figures just read rather than from memory of the last cycle.
- **The age is stated, not implied.** Cycles are skipped while the market is
  shut, so "last cycle" on a Monday morning is Friday afternoon. Without the age
  a three-day-old trigger reads like a fifteen-minute-old one.
- **Seeded from the audit log at loop start**, because a restart is exactly when
  this is worth most: a mid-session deploy would otherwise discard every open
  watch. A failure there costs the recall and nothing else.
- **A skipped or failed cycle does not blank it.** The carry-forward happens
  only after a real decision, so a weekend leaves Friday's watches standing.

The prompt is explicit that a trigger the model wrote is **not evidence**.
Restating one it can no longer justify is worse than passing, because it reads
as conviction.

**The gate's verdicts go back too, and they are a different kind of fact.** The
previous cycle's proposals render with what the gate did and why. This is safe
in a way a P&L history is not: "risk 1,131.00 exceeds the per-trade cap
1,000.00" is deterministic, true regardless of how the trade would have gone,
and true again next cycle for the same proposal. There is no sample to overfit
to. Observed live and the reason it exists — the model proposed 87 AAPL at 13%
over the cap, which is the dangerous kind of wrong, and with nothing fed back it
would size that way every cycle forever. The prompt says the gate is code and
cannot be argued with: fix the named defect or drop the trade.

**What is deliberately NOT fed back is the track record.** Win rate, profit
factor, expectancy and R exist in `metrics.py` and reach the operator through
the Analytics page. They do not reach the model, and the reason is sample size:
forty trades is noise, a model shown three losses will confidently change
approach, and that is overfitting to randomness — the Alpha Arena failure
exactly. `PerformanceSummary.sample_is_thin` (20 trades) already encodes the
threshold for a human reader. Revisit when a *per-strategy* group clears it
several times over, and even then hand over a bounded summary with the sample
count attached, never a narrative.

**So the system learns and the model does not, on purpose.** The loop is
journal → `metrics.py` → Analytics → the operator changes `config/rules.yaml`
or `strategy.py` in a commit. That is memory held in SQLite and in git, moving
at human speed, with an audit trail. For money that is the right place for it.

### A feed failure must degrade the cycle, never end the loop

`fetch_market_ticks` and `fetch_indicators` in `context.py` both catch
`Exception`, deliberately. They run a network call per symbol per cycle, and the
Alpaca SDK raises `APIError`, `httpx` timeouts and JSON decode failures, none of
which is a `KeyError` or a `RuntimeError`. A narrow catch lets those through and
**ends the decision loop**: the journal stops being reconciled and open
positions stop being watched, with nothing on screen to say so.

A failed symbol comes back as missing history, which is already the honest
description of what the model has for it, and gets named in the prompt and in
`symbols_without_history`. Same reasoning as `data/_http.fetch_json`: there is
no exception from an HTTP client worth crashing a trading loop over, and an
unanticipated one is exactly the case where crashing would be worst.

**There is no cache on the bars, on purpose.** The Marketaux and Finnhub caches
are rate-limit requirements: those free tiers allow 100 requests a day against a
loop that wakes 96 times. Alpaca's market-data limit is per minute, not per day,
and six symbols every fifteen minutes is nowhere near it. A TTL here would be an
optimisation dressed as a rate-limit control, and it would make the indicators
lag the quote inside the same context block for no benefit.

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

**The audit log is snapshotted by the same script, and `cp` is correct there.**
That inverts the rule above, so it is worth being explicit rather than letting
someone pattern-match. The `.backup` reasoning is about SQLite specifically: a
WAL and an shm mean state is spread across three files, so a copy taken between
two writes is internally inconsistent. An audit file is append-only UTF-8 text,
one JSON object per line. There is no second file, nothing lands half-written
except a truncated final line, and `audit._parse` already tolerates exactly
that. `gzip -t` is the counterpart to the integrity check — a backup nobody has
opened is still a hope.

Three properties there:

- **The audit block runs BEFORE the journal's preconditions.** Everything in
  the journal half can `die` — `sqlite3` missing, the database gone — and a
  `die` above the audit snapshot would mean the record of every rejection
  quietly stopped being backed up because of a problem with a different file.
  The audit half needs only `gzip`, so it must not be able to fail for the
  other half's reasons. The non-zero exit for a failed audit file sits at the
  very end for the mirror-image reason.
- **Only files that changed are re-compressed.** A dated file is finished when
  the UTC day rolls over and never changes again, so after the first pass this
  touches today's file alone.
- **Audit snapshots are never pruned, unlike the hourly journal copies.** Those
  are successive copies of one database and the newest supersedes the rest;
  these are each a different day, and that day exists nowhere else. Measured at
  roughly 500 KiB/day uncompressed — well under 100 MB a year gzipped — so
  keeping everything is cheap, and a retention rule on the only record of a
  rejection is how that record quietly stops reaching as far back as anyone
  assumes.

**Disk growth and read speed were both measured and are not problems.** ~180
MiB/year raw for the audit log; `AuditLog.read()` is ~27 ms for the 24-hour
news window and ~126 ms across seven days of a 14-day corpus. Neither justifies
pruning history or moving the log into SQLite — and moving it would reintroduce
a schema to migrate, which append-only JSONL exists to avoid.

### The warning about losing the dashboard is shown on the dashboard

That reads backwards and is correct. Tailscale node keys expire — this tailnet
caps the lifetime at 90 days and forbids disabling it — and when the key lapses
the Funnel stops serving, the private address stops answering, **and the bot
carries on trading perfectly normally**. Service green, journal filling, orders
still going to the broker, and the only symptom is a URL that no longer
responds.

`src/bot/tailnet.py` plus `mudhorn-tailnet.timer` check it every six hours and
raise a banner at ten days remaining, around day 80.

The banner is on the surface that is about to disappear because **the failure is
notice followed by an outage, not a sudden event**. During the notice period the
dashboard is up and being looked at, which makes it the one channel guaranteed
to reach the operator while it can still be acted on. Afterwards, nothing on
this box can reach anyone — which is why the non-zero exit and
`systemctl --failed` exist as a backstop rather than as the primary route.

Three properties are load-bearing, and all three are the same principle:

- **A stale reading is `unknown`, not healthy.** `checked_at` travels with the
  status and `is_stale` is computed from it, so a check that quietly stopped
  reports that it stopped. A file describing a healthy link is not evidence of
  one.
- **A missing expiry date means expiry is disabled**, not zero days left. The
  good outcome must not read as the worst one.
- **Never having run reports nothing at all.** A box without the timer installed
  is not told its link is fine.

**The banner names the command that clears it** (`RECHECK_COMMAND`), because a
warning that outlives the fix by six hours teaches an operator to ignore the
next one.

**A key that has not expired is not the same as a URL that answers, and that
gap was live.** Measured 13 Aug 2026: every dashboard path on
`https://mudhorn.tailc04415.ts.net` returned 404 from `server: uvicorn` while
`POST /mcp` returned 401 — the Funnel was proxying to the MCP server rather
than to `mudhorn-web`. The unit was `active (running)` and healthy on
loopback, the checkout was clean, and this module reported the link in perfect
health, because it only ever looked at the key.

That is this module's OWN failure shape — service green, journal filling, and
the only symptom a URL that does not answer — arriving through a cause it
structurally could not see. `serves_the_dashboard` reads what the Funnel is
actually proxied to out of `tailscale serve status --json` and compares it
against `DASHBOARD_PORT`.

Three things about it:

- **Three-valued, and `None` is "could not ask".** No serve output, unreadable
  serve output, or no host with the Funnel on at all — none of those may read
  as True, and none of them escalates either. A check that raised an alarm on
  its own inability to read would fire on every box that does not collect the
  serve output, which is how a real warning gets trained out of a reader.
- **A host served on the tailnet but NOT funnelled is ignored.** It says
  nothing about the public URL, and counting it would let a correct private
  mapping vouch for a broken public one.
- **`funnel_hostnames` was already there and answered the wrong question.** It
  said which hosts had the Funnel switched ON — which a Funnel pointed at the
  wrong port satisfies perfectly — and it was parsed, stored and read by
  nothing. `Dream.is_offerable` in a second place: defined, never called, and
  the feature inert behind it.

**Two things about the tailscale CLI, both measured while fixing this and both
now named in the banner.** They matter because the remedy for this warning is
typed at a root shell by somebody who is already dealing with an outage.

- **`tailscale serve` REMOVES the Funnel; `tailscale funnel` keeps it.** Adding
  a handler with `serve` printed "Removing Funnel for
  mudhorn.tailc04415.ts.net:443" and took the whole public hostname offline —
  including the ops MCP endpoint the deploy tooling reaches the box through.
  The handler it added was correct; public reachability went as a side effect.
  So `REPOINT_COMMAND` is the full command rather than "re-point it at 8787": a
  remedy stated as an intention is one the reader translates, and the obvious
  translation causes a second outage during the first.
- **`--set-path` STRIPS the prefix.** `/mcp` went from `401` to `404` when an
  explicit handler took precedence, because the backend was receiving `/`. The
  target has to carry the path: `--set-path=/mcp http://127.0.0.1:8788/mcp`.

The public hostname serves ONE `/` handler, so the dashboard and the ops MCP
server cannot both have it. They are split by path, and the dashboard owns `/`
because its internal links are absolute and it cannot live under a prefix.

### A feed writes into the model's document, so `.strip()` is not enough

`context.py` renders each headline as `f"- {h}"` into a **markdown document**
the model reads. `marketaux._parse` cleaned titles with `.strip()`, which
removes leading and trailing whitespace and **leaves an embedded newline
intact**. So a headline carrying a newline could close its bullet and open its
own `##` section — and the section most worth forging is *"Gate verdicts
(previous cycle)"*, which the prompt tells the model is deterministic and not to
be argued with.

**This is not a gate bypass and must not be described as one.** `RiskGate`
reads no prompt, and no headline widens a cap. What it reaches is the half the
gate deliberately does not second-guess: direction, symbol, entry, and where
the stop goes. That half belongs to the agent by design, which is precisely why
an outside party writing into it matters — the guardrail below it is intact and
the *choice* above it is being steered.

The fix is `" ".join(text.split())`, which collapses every kind of whitespace
including newlines, rather than trimming the ends.

Three things worth carrying:

- **`xfeed` was already safe, and only by ACCIDENT.** It normalised whitespace
  for formatting reasons, with nothing saying that was load-bearing and no test
  holding it there. A later tidy-up to `.strip()` "for consistency with the news
  adapter" would have opened the same channel silently. Posts render **ahead of**
  headlines, so that one sits closer to the top of the document. Both are pinned
  now.
- **It was found by MEASURING both parsers against a newline-bearing string**,
  not by reading them. Reading would have shown two plausible one-line
  expressions; only running them showed one emitting two lines. The accidental
  half was invisible any other way.
- **The general rule: anything rendered into a prompt is a document, not a
  string.** A value that can carry the document's own structural characters can
  restructure it. That applies to every feed added later, and to any surface
  that renders operator or agent text into a model's context.

### The three data feeds are not equally important

`src/bot/data/` holds three adapters and they fail in different ways.

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

**X** (`xfeed.py`) supplies posts from the accounts in the `social:` block of
`config/rules.yaml` — the ones whose words move a price before the wire story
exists. It gates nothing, exactly like Marketaux, and it is off unless both
`social.enabled` and `X_BEARER_TOKEN` are set. Reading timelines needs a paid
X tier, so off is the normal state and a deployment without it is fully
functional.

It carries `is_degraded` anyway, unlike Marketaux, and the reason is the Finnhub
lesson in a different costume: an empty post list from an expired token looks
exactly like a quiet morning, and only one of those should change how a price
move is read. A degraded result is deliberately **not cached**, so one bad
minute does not silence the feed for the whole TTL.

**Do not make it gate anything.** A blackout window after a high-impact post
would mirror `news_blackout_minutes_after` and is a genuinely reasonable idea,
but it changes what the gate refuses. That is its own commit, with a reason and
a test that proves it rejects. The gate is deterministic Python precisely so it
cannot be persuaded, and "the model thought this post sounded bearish" is the
opposite of a deterministic input.

Posts are rendered **ahead of** the headlines in the prompt, on purpose. By the
time a headline carries the story the gap has already opened, so reading them
in the other order would invert the thing that makes the feed worth having.

**The caches are rate-limit requirements, not optimisations.** Marketaux's free
tier allows 100 requests a day against a loop that wakes 96 times, so the
30-minute TTL is what keeps the quota intact. Lowering it exhausts the day's
allowance before the session ends. The X cache is shorter (10 minutes) because
its binding constraint is a monthly cap on posts retrieved rather than a daily
request count, and because caching a market-moving post for half an hour would
defeat the point of fetching it.

### The chat surface reads the news back; it must never fetch it

Asked "what's the latest news", the Chat page's agent answered that it had no
news feed connected, only a trading bot. That was **true and correctly
refused** — it declined to fabricate, which is the behaviour this project wants
— but it was a plumbing gap rather than a limitation: the loop reads three
feeds every fifteen minutes, and nothing exposed any of them.

Two things were missing and both are fixed:

- The MCP server had twelve tools and not one touched news. `get_recent_news`
  is now the thirteenth.
- `get_recent_decisions` read **only today's dated file** and returned `[]`
  when it did not exist. So the agent's recall of every rejection reset at UTC
  midnight, was empty every Monday morning, and was empty after any restart,
  with months of history on disk unread. It goes through `AuditLog.read()` now,
  which walks dated files and reports what it could not parse.

**The fix is `src/bot/news_history.py`, and the reason it reads rather than
fetches is a quota, not a preference.** Marketaux's free tier is 100 requests a
day against a loop that wakes 96 times — the allowance is already spent. A live
news tool on the chat surface would spend the *loop's* budget, and would fail in
the worst available direction: the trading cycle reasoning with no headlines, on
the exact day somebody was asking about the news. The X feed's monthly post cap
fails the same way. So the store is the source, `MarketInputs` in the audit log
is the store, and everything in that module is a pure function over an
`AuditView` with no network in it.

Three properties there are load-bearing:

- **A recording is not a search, and the age has to travel with the item.** A
  headline first seen six hours ago presented as "the latest news" is the
  confident partial answer this repository exists to prevent, arriving through
  the chat surface instead of the model. Every item carries `first_seen`,
  `last_seen` and an age; the tool description tells the agent to quote them.
- **No cycles recorded is not "no news".** The `FinnhubCalendar.is_degraded`
  lesson in a third costume. An empty window means the loop was stopped,
  restarting, or the market was shut — never that the world was quiet.
  `has_cycles` is deliberately a separate question from "are the lists empty",
  the MCP payload names it as `loop_recorded_nothing_in_window`, and the
  readout says it in a sentence, because a caller handed only empty lists
  reaches for the wrong reading every time.
- **Cycles written before `MarketInputs` existed are counted and named.** The
  audit log is append-only and never migrated, so a window can hold decisions
  with no `inputs` at all. Those are cycles whose feeds are not on file, which
  is not the same as cycles that saw nothing.

Items are ordered by when they were **first** seen, not last. A story sitting
in the 30-minute cache since this morning is not newer than one that broke ten
minutes ago, and sorting on `last_seen` would claim it was.

The degraded flags are `any` across the window rather than the newest cycle's,
because the claim being made is that the list is complete over the whole span,
and one failed fetch anywhere in it makes that false.

**Do not add a live-fetch news tool to make this feel fresher.** If the chat
surface genuinely needs live news, the prerequisite is a paid tier with its own
quota, kept separate from the loop's — not a second consumer of the same 100
requests.

Web access more broadly is a deferred decision rather than a settled no, and
`docs/HANDOFF.md` records the four separate things it could mean and how their
risk differs. Two rules hold whichever is chosen: **nothing web-derived may
become a gating input**, because `RiskGate` has to stay deterministic and must
not fail open on a network call; and **the model reads rendered, attributable
text, never raw pages**, which is the `indicators.py` rule arriving from a new
direction.

### The query index is derived, and that is what makes open SQL safe

`news_history.py` answers "what has it seen lately" by scanning dated files.
That is right for a window and wrong for a question: "what did we decide about
AAPL in March", "which rejection reason fires most often", "how many watches
named no trigger" all mean walking every file and parsing every line, which the
chat surface cannot do in a turn.

`src/bot/insight.py` builds a SQLite index so those are one query, and
`query_history` hands the agent read-only SQL over it.

**Nothing in the trading path reads it and nothing in it is authoritative.**
The JSONL is the record; `insight.db` is a cache of it that can be deleted at
any moment and rebuilt with `electrum-bot reindex`. Three things follow, and
they are the design rather than side effects:

- **The schema can change freely.** `SCHEMA_VERSION` is checked on open and a
  mismatch drops every table and replays the log. This is how you get an
  evolvable schema without ever migrating the append-only log — which must
  never be migrated, because a reader that rejected yesterday's format would
  throw away the history it exists to preserve.
- **A bug in the index cannot cost a trade.** The risk gate, the loop and the
  journal neither read nor write it.
- **It must never be backed up.** `deploy/backup-journal.sh` covers the journal
  and the audit log because those are irreplaceable. Restoring a stale derived
  index over a current one makes queries quietly answer from last week, and the
  script says so where somebody would add it.

**Open SQL is safe here for reasons that would not hold one directory across.**
The database is derived, rebuildable, holds no credentials and is read by
nothing that trades, so the worst a bad query does is return a bad answer. The
connection is opened `mode=ro`, which is the actual guarantee — SQLite refuses
the write at the file layer. The statement guard on top of it is the second
lock, and the token it exists for is `ATTACH`: read-only on *this* database
says nothing about a second one a query brings along. A progress handler bounds
runtime so a cartesian join returns an error rather than hanging a chat turn.

**Indexing is incremental, and idempotent so the increment cannot corrupt it.**

- **The offset is taken after the last COMPLETE line, never after the last
  byte.** The log is appended to by a running process, so its final line can be
  a partial write. Recording the file size would skip the rest of that line
  forever once the append completed it, and the record would be silently short
  a cycle with nothing to say so.
- **Every row uses a natural primary key with `INSERT OR REPLACE`**, so
  indexing the same line twice changes nothing. That makes the offset an
  optimisation rather than a correctness requirement. Correctness that depends
  on bookkeeping being right is correctness waiting to break.
- **News is stored as raw per-cycle sightings, not as a deduped table with a
  running count.** A count incremented during indexing would double on a
  re-read; the `news` view's `GROUP BY` cannot.

Measured on 14 days of 96-cycle days: cold build 169 ms, a no-op refresh 0.9
ms, a delta 2.5 ms, queries 1–3 ms. That is why the tools refresh on every call
instead of depending on a timer somebody has to remember to install.

**`readings.summary` is the rendered line, and is deliberately not parsed back
into numbers.** `MarketInputs` stores what the loop recorded — `"close 580.12,
sma20 574.30, atr 6.41"` — so it is indexed as searchable text and no figure is
extracted from it. Reversing prose into numbers would produce a value nobody
can check, which is the failure `indicators.py` exists to prevent arriving from
the other direction. If numeric history is wanted, the fix is to record numbers
in `MarketInputs` — which has since been done, so `IndicatorSnapshot` records
the figures as numbers beside the rendered line and `insight.py` indexes them.
The rendered line is still never parsed back; the numbers come from the
recording, not from the prose. That change only pays from the day it shipped,
so cycles recorded before it have prose about figures and no figures, and
nothing recovers them.

**None of this goes into the prompt.** It is an operator surface, reached
through the dashboard and through chat. Feeding a queryable track record back
to the model is the thing `metrics.py` is already kept away from, and a
counterfactual one — "the trigger fired and we did not act" — is noisier still.

### A watch is graded now, so it has to be written in a checkable form

`waiting_for` is prose — "SPY closing below 641.20, roughly 1 ATR under the
20-day" — and prose cannot be scored. So a watch was an opinion with no
consequence and the stance meant nothing.

`SymbolAssessment.trigger` carries the same condition as `field`, `op` and
`value`; `src/bot/triggers.py` grades it against later cycles. Both halves are
kept: the sentence is what a person reads, the trigger is what code checks.

**The threshold is a number and never the name of another figure.** "Above the
20-day" re-checked next week tests a level the model never saw, because the
average moved. A number pins the claim to the moment it was made, which is the
entire point of pre-registering one.

**This is the piece that could not be backfilled**, and the same is true of
`MarketInputs.readings` — the daily figures as numbers rather than as the
rendered line. A cycle recorded before those shipped has prose about figures
and no figures, and nothing recovers them later. That is why they went in ahead
of the index, which is fully retroactive.

**What is measured is plan-following, not profit.** No counterfactual P&L is
computed and none should be added: "you missed $2,400" assumes the fill, the
size and the intraday path. What makes a trigger worth scoring is that it was
written down first.

Four verdicts, and the distinctions are the whole value:

- **`unknown` is not `not_fired`.** The figure named was unavailable every time
  it was checked. `IndicatorSnapshot` keeps `None` rather than a zero for
  exactly this reason.
- **`pending` is not `not_fired` either.** The horizon has not elapsed, so
  nothing is settled. Precedence matters: an unelapsed window is `pending`
  whether or not a reading has been seen, or "we have not looked yet" becomes
  "we looked and found nothing".
- **`followed_through` is three-valued.** A watch that never fired is not a
  follow-through failure, and reporting it as one would count a correct wait
  against the bot.
- **`can_grade_anything` is separate from an empty list**, the same as
  `has_cycles` in `news_history`. No graded watches because nothing was
  recorded and no graded watches because every watch was honoured are opposite
  findings.

A watch with prose but no trigger and a watch naming nothing at all are counted
apart, and the counter deliberately **does not** claim to know why a trigger is
missing: a record predating this feature is indistinguishable from a model that
skipped the field, so it is named for what can be observed.

**Never put an unescaped `{` in `SYSTEM_PROMPT_TEMPLATE`.** It is rendered with
`.format(rules_summary=...)`, so an example written as `{field: "close"}` is
read as a placeholder and raises `KeyError: 'field'` — taking down the model
call, the smoketest and the loop together. Double the braces. Same shape as the
`render.STYLES` backslash trap, and `tests/test_strategy.py` guards it.

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

**Intraday bars now exist, and `src/bot/intraday.py` answers the one question
they were needed for.** `Broker.get_intraday_bars` fetches five-minute bars and
that module counts, against the prior session's high and low, how many bars
**closed** beyond the level and how many only **wicked** through it — plus the
break bar's volume as a multiple of its own recent average, and whether the
level has been **reclaimed**. Trend break is evaluable on that and carries no
`requires` any more.

Three things there are load-bearing:

- **The wick/close counts are computed, never inferred.** Handing over bars and
  asking which was a break is the same failure as asking for a 200-day average:
  a confident answer nobody can check. Same rule as `indicators.py`.
- **`reclaimed` is its own field because the failed break is the way this
  strategy loses money.** A level gives way, everyone piles in, price closes
  back inside within the hour. `is_clean_break` deliberately returns False for
  it: broke-and-failed is not a weaker version of broke.
- **Volume is only ever a ratio.** Alpaca's free feed is an IEX sample rather
  than consolidated tape, so an absolute figure would mislead. Both sides of the
  ratio come from the same partial sample, which makes the comparison fair.

**News reaction is still not fully evaluable and must keep saying so.** Intraday
bars closed one of its two gaps; the other is a **spread history**, and bars do
not carry a spread. The context shows the current spread with nothing to compare
it against, so "the spread has normalised" cannot be checked. Its `requires` now
names only that. Deleting it alongside the gap that *was* fixed would remove the
warning and leave the hole — `tests/test_strategy.py` fails the build if it goes.

A session is grouped by **UTC date**, which is exactly right for US equities
(14:00-21:00 UTC, never crossing midnight) and conventional for crypto. It would
be wrong for a market whose session spans midnight UTC, which is CME Globex if a
second broker ever arrives.

### The deck is live, and one poller owns the broker conversation

`src/bot/web/live.py` streams the account to the browser over Server-Sent
Events. No new dependency: `StreamingResponse` with `text/event-stream` on this
side, the browser's native `EventSource` on the other, and it is plain HTTP so
a Funnel or a reverse proxy passes it through untouched. It also reconnects by
itself, which matters on a phone that has been in a pocket.

**The Board no longer talks to the broker during a render.** It used to open a
session inline, so a slow Alpaca held the page with nothing on screen — and
after a hyperspace jump that promised speed, which made the wait read as a
broken deck rather than a slow one. `_account_orders_prices` now reads whatever
the poller has and returns immediately.

**A cold start renders unknown, never zero.** `latest()` is `None` until the
first read comes back, and `render.board` answers that with its own shape: four
tiles at the width the figures will occupy, a banner saying this is a cold start
rather than an empty account, and the shimmer carrying the wait. `0.00` equity
would be a plausible wrong figure, which is the thing this repository exists to
refuse. The route returns before the banners in that case, because every one of
them is a statement about a broker reading.

**Do not call `ensure_running()` from a render path.** It schedules an asyncio
task and FastAPI runs `def` routes in a threadpool, where there is no loop to
schedule onto. The stream starts the poller when a browser subscribes.

**Only a page with `data-live` targets opens the stream.** Without that guard
the sign-in page opened it too — it inlines `SCRIPT` like every other page — and
took a 401, because `/live` sits behind the same password as the pages that
render an account. One pointless request and one console error per view of the
login form.

**That guard used to mean a session which never opened the Board never talked
to Alpaca at all, and it does not any more.** The ticker tape carries
`data-live` targets and the tape is on every page, so any page now keeps the
poller warm. That was a deliberate trade rather than a regression: the old
property was about an unattended box, and the poller's own idle stop is what
protects the overnight case. A render still costs no network.

**The client may only UPDATE a figure the server already rendered**, never be
what reveals one. Same rule as the projection layer. Every value is in the
markup before the script runs, so a blocked script shows the reading it was
served — one page-load old and honest about it — rather than an empty box.

**That bug was invisible to the test suite.** 731 tests, `ruff` and `mypy
--strict` were all green while the login page 401ed on every view. It took
driving the thing in a browser. Same shape as the `.gitignore` finding above: a
green local suite says nothing about what actually happens.

### A graceful shutdown that waits for a stream waits forever

`/live` is Server-Sent Events, and an SSE connection is long-lived BY DESIGN —
it never closes on its own. Uvicorn's default graceful shutdown waits for open
connections, so `systemctl restart mudhorn-web` sat in `deactivating
(stop-sigterm)` for the full systemd stop timeout with one browser on the deck.
Measured 14 Aug 2026: a deploy timed out on it, and `enable-dream.sh` reported
a false WARNING because it curled the port two seconds after asking for a
restart that had not finished.

`timeout_graceful_shutdown=SHUTDOWN_GRACE_SECONDS` bounds the wait. Dropping
the stream costs nothing: the browser's native `EventSource` reconnects by
itself, which is why SSE was chosen over a bespoke socket in the first place.
Bounded rather than zero, so an ordinary in-flight render still completes, and
systemd's own `TimeoutStopSec` stays the backstop underneath it.

**Anything that restarts this unit must wait for the PORT, not sleep.** A fixed
`sleep 2` before a verification curl is how a script comes to report a failure
about work that succeeded — and a check that cries wolf about its own
impatience is worse than no check, because it teaches the reader to disregard
the one time it is right.

### A visit is a sitting, and the marker never advances to now

`src/bot/web/seen.py` answers "what changed since I last looked?", which is the
question an operator opening this two or three times a day actually arrives
with. A cookie carries three stamps: the marker, the previous request, and when
the sitting began.

**The marker advances to the PREVIOUS request's time, never to `now`.**
Stamping `now` marks as seen anything recorded between the render and the next
click — which was never on screen. `last` is the latest moment the operator
provably *was* shown the state of the world.

**A sitting ends two ways and both are needed.** A thirty-minute gap ends one
that stopped; a six-hour ceiling ends one that never stops. Without the second,
anything requesting more often than the gap holds a sitting open forever: a
refresh timer, a phone on the Board, or an `EventSource` reconnecting on a flaky
link. On a first sitting that is worse than a frozen marker, because none is
ever established and the delta stays empty for the life of the tab.

**The cookie is stamped on HTML page routes only.** Not `/live`, for the
reconnect reason above. Not `/login`, which would advance the marker to a moment
the operator was shown a password form. Not `/healthz`, where a probe every
thirty seconds does the same thing as a reconnect.

**First visit is a third state, not "nothing happened"**, and every count is
`int | None` rather than `0` when there is no marker — so a renderer that
ignores the state prints something visibly broken rather than plausibly wrong.
Same rule as `has_cycles` in `news_history` and `can_grade_anything` in
`triggers`. That rule was missing once already: the caveat "the loop may have
stopped" fired for a caller that simply never opened the audit log, and the
module's own test pinned the wrong behaviour.

**`reaches_past_marker` covers the audit reader and NOT
`Journal.closed_trades(limit=...)`.** That query sorts `exit_time` ascending
before its LIMIT, so a limited call drops the NEWEST closes while leaving the
earlier bucket full — trades that closed since the marker would be reported as
nothing, with no caveat. No caller passes a limit; do not add one without
sorting descending first.

### The projection layer must fail to VISIBLE, and it is built that way

The deck carries a starfield, a hyperspace jump on navigation, HUD bracket
corners and panels that materialise: `render.STYLES` plus `render.SCRIPT`.

This used to be **the same engine written twice** — the public site had its own
copy in `brand/assets/` — and keeping the two in step was a standing obligation
this file recorded. That site is gone, so there is one copy and nothing to
drift. Everything below still holds, and holds harder now that this is the only
surface an operator ever sees.

It is decoration, and the whole design is about making sure it stays that way.

**Nothing is hidden unless the script said so.** Hiding a panel takes BOTH
`html.fx-ready` and a per-element `fx-panel` class, and they are added together
in one synchronous block. JavaScript off, a script that threw, a blocked file:
all render the plain page with every figure visible. The obvious arrangement —
hide in CSS, reveal in JS — fails to a **blank page**, on the one surface whose
job is making problems visible. Never invert this.

**The settle timer is armed before anything can throw, and re-armed rather than
cancelled.** Two durations doing different jobs: 2.6s catches a throw between
hiding and playing, and 20s is the last resort for panels the IntersectionObserver
owns. Settling those at 2.6s would force everything below the fold visible before
anyone scrolled to it, so the reveal could never happen at all. `settleAll`
queries the DOM rather than a list built earlier, because it must not depend on
any of the code between it and the failure it is catching.

**`prefers-reduced-motion` switches the layer off, not down.** Both the
stylesheet and the script check it; the script never starts the canvas, never
adds the class and never builds the boot overlay, so the work is not done rather
than done invisibly. A full-screen radial starfield accelerating to lightspeed
is close to a worked example of a vestibular trigger.

**The boot readout names parts of the INTERFACE, never the account.** "Risk gate
armed" would be stating a fact it has not read, on a dashboard whose whole
argument is that figures are measured rather than plausible. The execution mode
is the one live value on it and the server renders it into a data attribute.

**On the public site the demo banner is not in the panel list.** It is plain
HTML in all six files so the label saying every figure is invented cannot depend
on a script having run, and giving it an entry animation would undo that.

**A `.pill` modifier must not double as a layout class.** The stage badges are
named after the states they show, so `.dream .seed { padding: ... }` written for
the spark paragraph also matched `<span class="pill seed">` and rendered the
badge as a full-width block. Valid CSS, silently styling the wrong element, and
invisible unless somebody looks. `tests/test_web.py` now fails the build on the
shape rather than the instance.

**The Cmd+K console is NOT part of this layer, and lives in its own closure.**
`render.SCRIPT` answers `prefers-reduced-motion` by returning on line one, which
is right for a starfield and wrong for the only keyboard route to every page:
somebody asking for less motion is asking for fewer moving pixels, not for a way
around the site to be withdrawn. So the palette sits after the projection
closure, outside the bail-out, animating through the stylesheet — which has a
reduced-motion block of its own, so the preference is honoured where it applies.
It reaches hyperspace through `window.MUDHORN_FX` if that layer built one and
falls back to an ordinary navigation if it did not, so a throw up there costs the
starfield rather than the way around the deck. Same principle as the settle
timer: the recovery path must not depend on the code it is recovering from.

Focus returning from the palette is **checked, never assumed**. The shortcut is
global, so it is usually pressed with nothing focused and `activeElement` is the
body — and `body.focus()` silently does nothing, raising no error while a
keyboard user is stranded at the top of the document anyway.

Both were found in a browser and neither was visible from the suite, which is
the general lesson: a closure boundary and a silent no-op are not things a unit
test sees. `tests/test_web.py` pins the shapes; the Playwright pass in the
scratchpad checks the behaviour.

### `prefers-reduced-motion` has TWO right answers, and which one depends on whether the thing is decoration

The starfield answers it by **not existing**: the script returns on line one,
never starts the canvas and never builds the boot overlay, so the work is not
done rather than done invisibly. That is correct for a full-screen radial
starfield accelerating to lightspeed, which is close to a worked example of a
vestibular trigger.

Applying that reading to a **control** is a bug, and it is an easy one to write
because it looks like consistency. The Cmd+K console already got this right by
sitting outside the projection layer's bail-out: somebody asking for fewer
moving pixels is asking for fewer moving pixels, not for the only keyboard
route around the deck to be withdrawn.

So the rule generalises, and both halves are now tested rather than remembered:

- **Decoration switches off.** The wisps drifting around a dream's trade
  (`web/dream_fx.py`) are `display:none` under the preference. Nothing is lost,
  because a badge was never the point of them.
- **A control keeps working and loses only its motion.** The forge window
  (`web/forge_window.py`) is the only route to agreeing to a limit change, so
  the reduced-motion block may touch `animation` and is tested for **not**
  touching `display`, `visibility` or `opacity` on the dialog.
- **Anything carrying information survives either way.** `.from-dream` names
  WHICH dream a trade came from and is tested for surviving the preference, for
  the same reason the treatment exists at all: a marking that cannot be traced
  back to a record is decoration pretending to be provenance.

**A state and the animation announcing it are different things, on different
clocks.** Symbiosis is the worked case. Two dreams fuse in the backend whenever
they fuse — a vault that only joined while a browser was open would make the
feature a function of the operator's attention, which is the opposite of what a
background dreamer is for. The *reveal* waits, because an animation that played
at 3am played to an empty room. `seen.py` answers which one this view is, and
its marker advances to the PREVIOUS request rather than to now, so nothing is
ever marked seen that was not on screen.

The trap underneath is the fail-to-visible rule in its least obvious costume:
it is very natural to draw two cards and have JavaScript merge them, and **that
fails to two cards and a lie.** The card is rendered fused; the animation is
the arrival of something already true. The client may only ever add the
transient `joining` class, never `fused` itself, and a test pins it.

### A timestamp on a page describes the READING, never the render

The Board printed `as at <now>` above figures that came from whatever the poller
last read. Those are different moments, and the gap between them is not small:
`LivePoller` idle-stops once nobody is watching and **keeps its snapshot**, so
the first load of a morning served an overnight reading stamped with the current
time. Every figure a present-tense claim about an account nobody had read since
the night before, and the one element a reader checks to find out saying it was
current.

That is the confident-partial-answer failure this repository exists to prevent,
arriving through the furniture rather than through the model. The whole suite was
green throughout.

Three properties now, and they are the same rule three times:

- **The stamp names `taken_at`**, and a caller that cannot say when the figures
  were read gets "read time unknown" rather than a default of now.
- **A stale reading is announced ahead of everything it qualifies.** The expiry
  and untracked-position banners are each a claim about a broker reading; an
  eight-hour-old one still deserves showing — erring towards warning is the safe
  direction — but a reader has to be told which account state they describe.
- **The stamp carries `data-live-read` so the stream owns it.** A
  server-rendered timestamp is right for exactly one instant, and the figures
  under it repaint every few seconds. Left alone it becomes a time attached to a
  reading it no longer describes, and stays wrong for as long as the tab is open.

**`Broker.orders_degraded` is the `FinnhubCalendar.is_degraded` lesson in a
third place.** `AlpacaBroker.get_open_orders` catches its own SDK errors and
returns `[]` — it has to, because it feeds a display beside positions and risk —
so the poller's `except` around it was dead for the only broker that can
actually fail, and an outage rendered as an account with nothing resting. The
existing test passed because the stub raises where the real one does not, which
is the trap worth remembering: a test double that fails differently from the
thing it doubles pins a path production never takes.

**Four live states must not collapse into two.** `slow` fell through to the
`else` in `paint()` and painted the link green, which said "live" while a read
was outstanding and the figures on screen were the previous ones.

**A poller that has been stopped stays stopped.** Cancelling
`asyncio.to_thread` abandons the future while the worker thread runs on, so an
abandoned poll could reach `_connected_broker`, authenticate a *new* session and
store it after `stop()` had cleared the old one — a connected broker with nobody
left to close it. The closed flag is set before the cancel, the release runs
either side of the await, and `ensure_running` refuses to restart. The idle stop
releases the session too; stopping the reads and holding the session is half an
idle stop.

**An injectable clock must be honoured everywhere or not offered.** `LiveState`
falls back to the wall clock because it is a value object with no clock of its
own, so a poller with an injected one stamped readings with it and measured
their ages against the real one. Figures that look like measurements and are not
are the exact thing this repository refuses.

**`/live` was missing from `tests/test_auth.py` entirely**, and that is the
shape of the miss worth naming: it is not a *page*, so it never came up when
somebody wrote "every page is refused". It serves equity, cash, buying power,
every open position and every resting order. Moving it into `OPEN_PATHS` would
have published all of that with the suite green. The fix is not one more line in
a hand-maintained list — that list failed exactly the way per-route dependencies
were rejected for failing. **The routes are enumerated from the application
now**, and a new one must be classified as refused or as deliberately open
before the suite passes.

### Three agents, three souls, and Hermes only holds one

`souls/yoda.md` answers about the account on `/chat`; `souls/grogu.md` dreams on
`/dreaming`; `souls/armorer.md` keeps the limits on `/settings`. All three
follow the `SOUL.md` convention down to the headings (`## Personality`,
`## Style`, `## What to avoid`).

**Each one is a different job, not a different accent**, and that is the thing
to preserve when editing them:

- **Yoda is a teacher.** The operator's trading companion, for asking questions
  and getting explanations. It answers first and then says what the number
  *means*, because "0.98% at risk" is a reading and "about half of what you
  allow yourself across the whole book" is an answer. It had a Style section
  that *required* inverted syntax; that is pastiche, it was the one thing the
  character must not be, and it is gone.
- **Grogu wonders.** The cute, deep, outside-the-box one — the value is that its
  attention goes to the thing nobody else was watching, and the chain of
  physical facts underneath is what makes that worth reading rather than a
  personality.
- **The Armorer argues.** Asymmetric on purpose: tightening is not an argument,
  loosening is one and it starts it. **It pushes back; it does not deny** — if
  it ends up refusing, it has become the config-load validator it was built to
  replace.

**A soul is a reason to say something SHORTER, never a licence to say more.**
That clause lives in `souls.py`'s prefix rather than only in the files, because
the prefix is the one text guaranteed to reach the model when a file has been
edited on the box. Each file carries a `## How long to be` section and
`tests/test_souls.py` caps each at 1,600 words — **the fix for a breach is to
move mechanics back into the system prompts** (`dreamer.py`, `confer.py`,
`settings_agent.py`), never to raise the cap. Grogu was 2,101 words and most of
the excess was reciting the stage machine at a model that is already told it.

**Hermes loads exactly one soul, from `$HERMES_HOME/SOUL.md`.** Not the working
directory, no CLI flag, no environment variable to point at another file, and
`/personality` is a session overlay rather than a second soul. One instance, one
character. This repository needs three on one instance chosen per request, so
`HermesBridge.ask` prepends the selected soul to the prompt **on stdin**, which
is the only mechanism that can vary per call — and is where it has to go anyway,
because the sudoers rule permits `run-chat.sh` with no arguments so nothing a
signed-in user types can be read as a flag.

**Do not install any of them as `~/.hermes/SOUL.md`.** It would apply to every
agent at once, alongside whichever soul the request injected, and the model
would receive two characters and pick.

**One instance is enough for the souls, and not enough for the tools.** The
account agent's Hermes registers this repo's MCP server, which exposes
`place_order`. Sharing it means the only thing keeping a *speculative* agent
away from the broker is the sentence in `souls/grogu.md` telling it not to —
prose, where this repository uses a structure everywhere else. `RiskGate.evaluate`
still runs on every order path, so the operator's four rules hold either way,
but "it has no broker tool" and "it has one and was asked nicely" are different
claims and only one is worth making.

So `deploy/run-dream.sh` runs a second Hermes from its own `HERMES_HOME`, whose
registry must not contain the bot's MCP server. When it is absent the dreamer
falls back to the shared instance and **the Dreaming page says so in a banner** —
it does not quietly claim an isolation it does not have. Same rule as
`calendar_degraded` and the tailnet status: report the weaker fact rather than
imply the stronger one.

**"Absent" means sudo will not run it, NOT that the file is missing, and
getting that wrong broke both halves at once.** Observed on the droplet 14 Aug
2026: `bootstrap.sh` ships `run-dream.sh` and chmods it on every box whether or
not the second instance exists — it is inert without a sudoers rule naming it —
so `HermesBridge.available`, which is `binary.exists()`, was True on a box with
no `/etc/sudoers.d/mudhorn-dream` and no `/home/hermes/dreamer`. Grogu was
routed to an instance sudo refuses and the panel answered `sudo: a password is
required` instead of falling back; and the page rendered `isolated=True`,
claiming the isolation this paragraph promises it never claims falsely.

`HermesBridge.permitted` asks `sudo -n -l` — the policy, not the filesystem —
and both the routing and the `isolated` flag read it. Three properties:

- **Any failure to establish permission answers False**, which under-claims:
  the caller falls back and the page reports the weaker arrangement. That is
  the OPPOSITE default from `available`, which answers an EACCES
  optimistically — correct there, because being wrong costs a page saying chat
  is unavailable when it works, and being wrong here costs a false claim of
  isolation.
- **It is cached for the life of the process**, which is safe only because the
  scripts that change a grant restart `mudhorn-web` as their last step.
- **`available` is deliberately left alone.** "Can we chat at all" and "may I
  run this particular wrapper" are different questions, and making the
  optimistic one strict would break the working chat panel on a flaky probe.

**A grant to something unfinished is worth LESS than no grant**, and that was
measured the first time `enable-dream.sh` ran. The home, the soul and the
sudoers rule all installed cleanly, so `permitted` went True and the dashboard
duly routed Grogu to the isolated instance — which had no `inference.env` and
no `.hermes/config.yaml`, and answered "No inference provider configured".
Before the grant Grogu worked through the fallback; after it he did not. The
script now copies the account agent's credential and writes a minimal config
naming the model, because Hermes reads its model from its own home and not
from the environment.

The same DigitalOcean key as the account agent, deliberately: a second key is
a second thing to rotate and a second balance to top up, and the isolation
this buys is about TOOLS — no MCP server, so no `place_order` — not billing.

`deploy/enable-dream.sh` is the deliberate act that makes the isolated
arrangement real — the same shape as `enable-chat.sh` and `enable-forge.sh`.
It creates the home (which the sudo grant alone would not have fixed, since
the wrapper does `cd "$HERMES_HOME"`), installs `grogu.md` as its `SOUL.md`,
validates the sudoers rule with `visudo -c` before installing it, refuses a
home whose config registers the MCP server, and proves the result through the
RUNNING SERVICE rather than from the console's own namespace. Its sudoers file
is separate from `mudhorn-chat` on purpose: `enable-chat.sh --off` removes that
one, and the dreamer's grant has no business disappearing because somebody
toggled the chat panel.

This was a real overclaim, caught after shipping. The banner originally read
"no route to the broker", which is true of the dream *records* and was false of
the chat panel on the same page. `tests/test_web.py` now pins both wordings.

The soul name arrives in the request body and `load_soul` builds a path from it,
so the route validates it against a fixed set first. An unknown name falls back
to the account agent rather than erroring: the worst case of getting it wrong is
the wrong voice, and refusing the question would be the larger failure.

**A soul shapes the framing and never touches a figure.** Both files carry that
first and `souls.py` restates it in the prefix that actually reaches the model,
because these are read from disk at call time and could be edited on the box. A
missing soul degrades to a voiceless prompt rather than raising — same failure
direction as `HermesBridge.available`.

### The dreamer has no order path, and that is structural

`src/bot/dreaming.py` produces second-order hypotheses: the cicada brood that
damages two of the three largest sesame producers, making the third, which has
no cicadas, the marginal supplier into a shortage it did not experience.

**`Dream` carries no quantity, no entry price, no stop, no side, and no
`symbol` field an order builder could read.** `OrderProposal` requires all of
them and validates `stop_loss_price`, so nothing turns one into the other
without somebody adding fields and validation by hand.
`tests/test_dreaming.py::test_a_dream_cannot_describe_an_order` asserts that
overlap stays empty. This is the whole safety argument and it is deliberately
not a matter of discipline: a speculative-idea generator wired to an execution
path is the Alpha Arena failure with extra steps, and confidence is what this
module produces most of.

**It does carry `symbols` and `asset_class_key`, and those are a PERMISSION
rather than an instruction.** They name what an adopting trading agent may
trade outside the normal allowlist. The distinction that keeps the guarantee
intact is exact and is worth stating rather than trusting: the overlap test
checks `symbol`, singular, which is the field an order needs — and `symbols`,
plural, is a list of subjects that grants entry to the allowlist and nothing
else. Every gate in `RiskGate.evaluate` still runs on anything traded under
one, under its own class's limits. That is the design, not a loophole in the
test.

`Dream.asset_class_key` is deliberately NOT called `asset_class`, because
`OrderProposal` already has a field by that name and the blanket assertion that
the two types share no field name at all is worth more than the tidier word.
They are different things anyway: one is the `AssetClass` enum the broker
adapter switches on, the other a key into the `instruments:` block of
`config/rules.yaml`.

`instruments` names what a dream is *about* and is free text on purpose, so it
cannot be read as a ticker the bot trades. **Do not collapse it into
`symbols`.** Two fields exist because one must never be readable as a
permission and the other is exactly that.

**Verification is counted, never claimed.** A model asked to rate its own
sourcing rates it generously, so the badge is arithmetic over the `checked`
flags on each hop. An empty chain reads as `unverified` rather than `sourced`:
the good outcome must not be what an absence of evidence looks like, which is
the same rule as the tailnet status reporting `unknown`.

**This is NOT Anthropic's "Dreaming", and the difference is the dangerous part.**
That one, a research preview from May 2026, consolidates an agent's memory from
its own past session transcripts so it learns from its mistakes. Applied to a
trading account, "learn from what happened last time" means learning from profit
and loss, which this repository forbids: forty trades is noise and a model shown
three losses will confidently change approach.

So `DreamLedger` is the shape consolidation is allowed to take here. It counts
how often the dreamer sources a hop, drops a chain, names a trigger — facts
about the **reasoning**, true regardless of how any trade went, with no outcome
sample to overfit to. It reaches the operator on the Dreaming page and stops
there, exactly as `metrics.py` reaches Analytics and stops there.

**The dream timer is in New Zealand time, by NAME rather than by conversion.**
`OnCalendar=*-*-* 07:00:00 Pacific/Auckland`. New Zealand observes daylight
saving, so a hardcoded UTC hour drifts by one twice a year and drifts silently;
naming the zone makes systemd redo the arithmetic on every elapse. The suffix
needs systemd 252 or newer and Ubuntu 24.04 ships 255, verified with
`systemd-analyze calendar`. On anything older the unit fails to parse and the
timer simply never fires.

**Do not set the box timezone to Pacific/Auckland instead.** Everything else
here reasons in UTC deliberately — `sessions_utc`, every journal timestamp,
every figure the dashboard renders — and moving the system clock would silently
reinterpret all of it. The suffix changes one timer and nothing else.

The Settings page reads `OnCalendar=` out of the unit rather than quoting a
constant, so an edit on the box shows up there. It also distinguishes three
states a file check can actually establish — installed, in the repo only, absent
— and says plainly that **whether the timer is ENABLED is not visible from that
process**, naming `systemctl list-timers` instead of guessing. A card announcing
a daily dream because a file exists would be the confident-partial-answer
failure in a new place.

**The dreamer runs on its own command, not on the loop.** `electrum-bot dream`
is one Claude call producing one step: a new chain, or the next step on one it
already started. Separate from `cmd_loop` for two reasons, and the second is the
one that matters. The loop wakes every fifteen minutes because a price moves
that fast and a second-order supply-chain idea does not, so a lateral thought
per cycle would buy ninety-six shallow ones a day. And the loop proposes orders
while this cannot, so keeping them in different processes means no later
refactor quietly connects a dream to the code that places one.

**The dreamer is never shown profit and loss, and that is where the rule is
actually enforced.** `souls/grogu.md` asks it not to learn from the track
record; `build_prompt` is what makes that true, because the figures never enter
the prompt. What closed is given as an EVENT ("SPY closed 04 May, opened on
mean_reversion"), never as an outcome ("SPY made $340"). `tests/test_dreamer.py`
asserts no P&L figure reaches the text. A dreamer that starts chasing what
recently worked is a momentum strategy with a personality.

**An `advance_id` is looked up in what was actually offered, never trusted.** A
model returning an id for a row it was never shown starts a new dream instead of
writing over an unrelated one. A verdict is honoured only on a verdict step, so
a stray value cannot silently close a chain that is still running, and a source
on an unchecked hop is dropped rather than kept, because that pair is a
contradiction and the unchecked flag is the honest half of it.

**`data/dreams.db` is its own file, not the journal.** Keeping them apart means
no query can read a hypothesis as a position, and a bug in the speculative half
cannot corrupt the trading record.

**What losing it costs is no longer "some speculative notes".** That was true
when a dream reached nobody. The `adoptions` table is a live trading permission
now, and `Trade.dream_id` points here. It is still deliberately outside
`backup-journal.sh`: losing it withdraws every grant, which is the safe
direction, and restoring a stale copy over a current one would resurrect
permissions somebody handed back. The journal stays the only irreplaceable file
on the box.

**Tests must not write to `data/` or `audit/`, and a fixture now enforces it.**
A session-scoped autouse guard in `tests/conftest.py` fails the suite if either
directory gains a file — **or if one that was already there CHANGES.** It
compared a listing first, which went blind the moment `data/dreams.db` existed:
a test writing rows into it appeared on both sides of the diff and was reported
as nothing at all, so the guard was strongest on a clean machine and useless on
a developer's. It fingerprints size and mtime now. `DreamStore` landing is how
that regressed in the first place: a new store, a new `build_app` default, and
one call site nobody updated.

### A dream can widen what may be traded, and every property below is load-bearing

The dreamer's output used to reach nobody. A dream now moves between four
places — **workbench**, **prophecy vault**, **dream vault**, **adopted** (plus
an archive) — and an adopted dream **grants permission to trade a symbol that
is not in `config/rules.yaml`**.

That last clause widens what `RiskGate` permits, which is the one thing this
repository is most careful about. Six properties hold it in place. None of them
is decoration.

- **The class hard-block comes from `Rules.enabled_instruments`, never from
  `allowed_symbols`.** Crypto switched off means an adopted dream naming
  BTC/USD grants nothing, whatever the dream says. The dreamer may look outside
  the *symbol* list; it may never cross a *class* boundary the operator has
  shut.

  **This was WRITTEN BEFORE IT WAS TRUE, and an adversarial audit proved it
  false.** The check tested the class key the adoption row *claimed*, and never
  asked what class the symbol actually belongs to — so an adoption saying
  `BTC/USD` under `us_equity` was a live permission to trade crypto under the
  equity book's limits, with crypto's 0.5% risk cap, 15% concentration and
  one-position rules all bypassed. Worse: `AlpacaBroker` routes on `"/" in
  symbol`, so the order reaching Alpaca *is* a crypto order — unbracketed, and
  therefore **with no broker-side stop at all**, which is the operator's third
  rule gone. The guarantee now requires the claimed class to agree with the
  symbol's true class, derived from the same routing rule the broker uses.

  The lesson is bigger than the bug. **A guarantee written in this file is not
  a guarantee, and prose that asserts one is how it stops being checked.** This
  paragraph asserted the property, `tests/test_grants.py` only ever tried
  `{"BTC/USD": "crypto"}` — the case that already worked — and 166 tests were
  green over the hole. Where this file claims a structural property, there must
  be a test that fails when it is removed.
- **The grant is resolved OUTSIDE the gate and passed in**, in the same shape
  as `news_windows`. `src/bot/grants.py` does the resolving; `risk.py` reads no
  database, opens no file and makes no network call. A gate that can fail is a
  gate that can fail OPEN.
- **Any failure yields an empty mapping, so it fails CLOSED.** A missing store,
  a torn row, an exception — the answer is "nothing is granted" and the account
  carries on trading exactly what `config/rules.yaml` already allows. There is
  no partial mapping presented as complete.
- **The grant dies with the adoption.** Handed back or expired, it is gone, and
  both are computed from the adoption row rather than read from a stored flag —
  a third fact about the same thing is a third fact that can disagree with the
  other two.
- **A symbol claimed by two live grants under different classes is DROPPED**,
  not resolved. There is no correct answer to which cap applies, and choosing
  one would be a plausible wrong figure.
- **A granted symbol faces every other gate**, under its resolved class's own
  limits. `RiskGate._class_symbols` unions the granted symbols into the class
  set and hands it to the three gates that measure what a class already holds —
  `_concurrent_positions`, `_class_total_risk`, `_instrument_capital_cap`.

**That last one was a real bypass, found by audit and closed.** Those three
gates identify a class's holdings by membership of `allowed_symbols`, and a
granted symbol is in no such list — so a position held under a grant was
invisible to its own class's concurrency cap, class total-risk cap and capital
cap. The grant would have bought entry to the allowlist *and* a silent
exemption from three limits, including the crypto 0.5% total. Do not "simplify"
`_class_symbols` back to `instrument.allowed_symbols`.

**And an OPEN POSITION keeps its class after the grant ends.** This paragraph
used to say the opposite — that a position held under a lapsed grant "drops back
out of those counts", which was "not new, it was never in them". That was wrong,
and a second audit measured it: before adoption existed a position in an
unlisted symbol could not exist at all, and `return_to_vault` is one of the two
things the trading agent is allowed to do, so the agent chose the moment. Handing
a dream back moved $1,200 of live class risk out of a $1,500 class cap and
flipped a rejection into an approval with nothing closed. `_class_symbols` now
matches a held symbol with `Rules.true_class_key` — the same derivation the
fence and the broker's routing use — so membership follows what a position IS,
not what is granted this instant.

It is read off the account snapshot rather than off the journal's
`Trade.asset_class`, which is copied from the model's own proposal and would let
a mislabelled proposal choose which caps it faced.

Expiry still never force-closes a position. Closing sits outside the gated path
deliberately; what expiry withdraws is the right to OPEN.

**`Dream` still carries no order fields**, and the guarantee is narrower than
it used to be, so state it precisely: no qty, no entry, no stop, no side, and
no `symbol` singular. It DOES carry `symbols` plural and `asset_class_key`, and
those are a permission rather than an instruction. `instruments` remains free
text naming what a dream is *about*, precisely so it cannot be read as a
ticker. **Do not collapse `instruments` and `symbols` into one field.**

**The feature is no longer inert, and the three reasons it was are worth
keeping**, because every one of them was invisible to a green suite and two of
them were invisible to two adversarial audits. It took generating real dreams
against the live model to find them.

- **Nothing promoted a dream off the workbench.** `Dream.is_offerable` was
  defined and never called, and the conference reads only `Vault.VAULT`, so the
  vault was permanently empty and `confer` completed honestly with
  `considered: 0` every day. `dreaming.promotion_for` is the rule now — a `keep`
  verdict **plus** at least one `is_checkable` condition makes a PROPHECY, all
  conditions met makes it a VAULT dream, everything else stays put — and
  `dreamer.promote_dreams` runs it from `electrum-bot dream`, never from the
  trading loop.
- **The dreamer never named a symbol.** Not filtered — `symbols_dropped` was 0
  — simply never filled. The prompt asks now, and asks for the *bridge*: the
  subject of a dream is unrestricted, a `symbols` entry must be something the
  broker can route, and the step from one to the other is a `Hop` like any
  other. **An empty list stays a respectable answer**, and the prompt says so,
  because a weak proxy invented to fill a field is worse than none.
- **A granted symbol never reached the model.** The prompt and every feed ran
  off `rules.allowed_symbols`, so a permitted symbol had no quote and no
  history and a proposal in one would have been dropped for want of a tick
  before reaching the gate that would have allowed it. The loop resolves the
  grant **before** the feeds now and runs them over `allowed_symbols | granted`.

Gate-first was still the right order — a permission path that worked before it
was safe would have been backwards.

Four properties of the prompt half, and they are the ones to defend:

- **The system prompt carries the RULE and never the symbols.** It is cached
  for an hour and built once at loop start, so an interpolated grant would be
  stale within the day and would change the cached bytes every time an adoption
  moved. The per-cycle context is the only place that can be current.
- **The chain never appears without its badge.** `Verification` and
  `weakest_hop` render adjacent to the hops and must not be separated from
  them; an unqualified causal chain in a prompt reads as established fact, and
  `Hop.checked` exists because some of those sentences were invented.
- **The grant block renders LAST**, after every measured figure. It is the one
  speculative section in the document, and a model that reads a story before it
  has seen a number anchors on the story.
- **The briefing fails in the opposite direction to everything else here.**
  `grants.brief_grants` keeps the symbols and drops only the reasoning when the
  store fails, because the symbols come from the resolution the gate already
  holds — dropping them would leave the gate permitting something the model was
  never told about, which is the inert state this closed.

`FinnhubCalendar` is **rebuilt** from the widened set when it changes, never
mutated: the feed caches windows already filtered against its symbol list, so
assigning to `.symbols` looks fixed and behaves inconsistently, which is worse
than the open gap.

**Conditions are graded by code, never by the model.** `DreamCondition` gained
a `symbol` — a `field`/`op`/`value` with no subject is a comparison nothing can
look up — and `grade_conditions` settles them through `as_trigger()` and
`triggers.CycleReadings`, against the figures the decision loop actually
recorded in `MarketInputs.readings`. **There is no horizon**, unlike a watch: a
prophecy is a long-horizon claim by construction, which is why its TTL is 365
days, and adding one would quietly make the prophecy shelf a five-day shelf.
A restated condition keeps the grade it earned (`carry_forward_grading`) — a
grading that reset on every step would make the vault unreachable — but a MOVED
threshold is a new claim and starts ungraded, because inheriting the old
verdict would be back-dating a prediction.

### There are TWO shapes of pre-registration, and a person settles the second

**An honest dreamer could not reach the prophecy shelf at all**, and that was a
conflict between two rules rather than a bug. Promotion needed a condition with
a NUMBER in it; every `TriggerField` is a price or a technical figure; and the
weakest hop of a second-order supply-chain chain never is. Measured over eleven
live steps: **zero checkable conditions, and a second-model judge found no
invented figure anywhere.** The dreamer was obeying the rule that says do not
state a number you did not read, and the shelf was unreachable by construction.

So `DreamCondition` carries two ways to pre-register and `is_pre_registered` is
the **union**:

- **A THRESHOLD** — `symbol`/`field`/`op`/`value`, settled by code against the
  figures the loop records. Unchanged.
- **AN OBSERVATION** — `subject` (the findable thing to look at) / `observable`
  (what it must show) / `observe_by` (the date the answer should exist by),
  settled by the **operator**. All three are required, in the same
  all-or-nothing shape as the triple: a subject with no claim is a thing to
  look at with no question, and a claim with no date never comes due and so
  never reaches anybody's list.

A threshold and an observation are pre-registrations of the same kind — a
falsifiable claim, written before the fact, with the means of settling it named.
What differs is who settles it, and that is not a difference the promotion rule
has any business caring about. **Prose with neither is still not pre-registered
and is still unpromotable.**

`ConditionState` is five-valued because a boolean cannot hold it: `MET`,
`RULED_OUT`, `AWAITING`, `OVERDUE`, `UNSETTLEABLE`. **`OVERDUE` is a fact about
the LOOKING and never about the world** — an unopened dashboard must not read
as a refuted prophecy. `RULED_OUT` is a real answer and must never collapse
into "not met", or a claim somebody looked at and refuted reads as one nobody
has got to yet. Same rule as `has_cycles`, `can_grade_anything` and first-visit.

**Three repairs were rejected, and each one is a rule already held here.**

- **New `TriggerField` members** for what a dreamer reasons about
  (`wholesale_egg_price`, `smelter_restart`). That makes the shelf reachable
  and every prophecy on it permanently `unknown`, which is worse than an empty
  shelf: `get` resolves a trigger by attribute name, so a member with no field
  behind it silently reads `None` for ever. `TriggerField` is now pinned as a
  **subset of `IndicatorSnapshot`'s fields** by a test, so the tempting repair
  is a red build. **Do not widen that enum.**
- **Exempting a dream where "no field measures this".** That is always the
  cheapest true sentence, so the exemption becomes the default path and the
  shelf fills with conclusions nobody committed to anything about. It also
  dead-ends: a dream with nothing settleable can never reach the VAULT either,
  so the conference still starves.
- **Letting the model answer its own condition.** `settle_condition` refuses
  every actor but the operator, and `dreamer.StepCondition` has no field that
  could carry an answer, so there is nothing for a model to say even before the
  check runs. A vaulted dream is what an adoption is taken from and an adoption
  is a live symbol permission, so a model settling its own condition would be
  writing itself a permission.

**No schema migration was needed and that is a fact rather than an oversight.**
Conditions are JSON in a TEXT column, so there is no column to add, and
`from_row` reads an absent key as "not an observation". The test builds the OLD
row shape through raw SQL, which is the only way to exercise it — every other
test constructs a current `DreamCondition` and so always gets the new keys.

`carry_forward_grading` keys on **`is_answered`**, not on `fulfilled`. A dreamer
restating its list would otherwise erase the operator's "no" and put a refuted
claim back on the worklist as though nobody had looked. A moved `subject` or
`observable` is a NEW claim and starts unanswered — the observation equivalent
of moving a threshold — while a moved review date is not, because the date says
when to look and not what is claimed.

### The answer is typed at a terminal, and that is where it belongs

`settle_condition` is the only writer of an operator's answer, and it shipped
with no caller — so an observation-only prophecy reached PROPHECY and stopped
there for ever. `electrum-bot observations` is the worklist and `electrum-bot
settle` is the answer.

**Both are commands on the box rather than a control on the deck**, and the
reason is the chain they sit on: a fulfilled condition can carry a dream to the
VAULT, a vaulted dream is what an adoption is taken from, and an adoption is a
live permission to trade a symbol that is not in `config/rules.yaml`. That write
belongs behind the shell, not behind one shared password on a surface that may
be exposed. Every gate still runs on anything traded under a grant; what this
changes is the allowlist, not the limits.

The Dreaming page carries a read-only **Waiting on you** card, which shows the
questions and names the command that answers them — the same shape as Settings
showing the limits and naming the file that owns each. It is **absent rather
than empty** when nothing is due: a panel announcing zero trains an operator to
stop reading the one thing on that page addressed to them.

Three properties of the surface:

- **The handle is derived, never stored.** `observation_handle` is six hex
  characters over the dream id and the claim's key. The important property is
  the one that looks like a drawback: **it changes when the claim changes**, so
  a dreamer that restates its conditions between the operator reading the list
  and answering it produces a different handle and the answer lands nowhere,
  rather than landing on a claim nobody was shown.
- **There is no default answer.** A default of `--met` manufactures
  confirmations; a default of `--ruled-out` refutes claims nobody meant to.
  There is no safe guess between them, so the command refuses and says so.
- **"Nothing waiting on you" is not "nothing stuck"**, and the empty worklist
  says both. A dream can equally be held by a threshold the market has not
  reached, which no amount of looking settles.

**An observation is settleable, so nothing may describe one as prose.** That
was wrong in three places at once — `render._conditions` said *"No number in
this one, so nothing can settle it"*, which is the worst place for it because
the claim is addressed to the person reading; `confer.render_dream` labelled it
"prose only" to the trading agent; and the MCP dream payload exposed
`is_checkable` and nothing else. All three report the shape now, and the
sentence about nothing being able to settle it is kept only where it is still
true.

### The two agents may talk, once a day, and the fifth cap is the one that works

`src/bot/confer.py` runs a bounded exchange: the dreamer offers a dream from
the vault, the trading agent asks about it and then adopts, declines or parks.
The transcript is stored either way, **including exchanges that ended in
nothing** — a dream the trader kept declining is a fact about the dreamer worth
having.

It runs on the **dream timer**, once a day, in its own module and its own
command. Never on the trading loop's fifteen-minute pulse: ninety-six
unattended negotiations a day on the process that proposes orders is the Alpha
Arena failure shape with two models instead of one. A day is far slower than a
price moves, which is the right speed for deciding whether a second-order
hypothesis is worth acting on.

Six caps. Six turns per exchange, two dreams per run (so twelve model calls at
most), `TEXT_MAX_CHARS` per message, and three counts over a dream's life:
`MAX_EXCHANGES_PER_EPOCH` (3), `MAX_EXCHANGES_LIFETIME` (12), and the change
gate below.

**Every conference ends in one recorded verdict, and one of the five is not a
decision.** The outcome used to be spread across `ConferOutcome` values and side
effects on the dream, so "what did they decide" had to be reconstructed.
`ConferenceVerdict` carries the decision, the reason in the deciding agent's own
words, and the moment.

- **`NO_DECISION` must never collapse into `DEFER`.** A turn cap, a spent epoch
  or a failed model call did not decide to wait — nobody decided anything.
  Reporting that as the mildest real decision is how a silent failure starts
  looking healthy, and it is the same rule as `has_cycles`,
  `can_grade_anything` and first-visit being kept apart from empty.
- **`ARCHIVE` belongs to the dreamer, about its own dream.** The trading agent
  cannot reach it. The operator's rule is that it cannot delete — only action,
  or send back with reasons — and an adopted dream carries a live symbol grant,
  so a power it gained by *talking* would be that rule undone through a
  conversation.
- **A `DEFER` with no wake condition is refused**, and recorded as
  `NO_DECISION` with the cause named. It needs a `DreamCondition` with a symbol,
  a field, an operator and a **number** — the existing rule that a threshold is
  never the name of another figure. A deferral naming nothing is "we ran out of
  things to say" wearing a decision's clothes.
- **The verdict is what the AGENT decided, never inferred from side effects.**
  Reading it back off "did an adoption row appear" would be a second source of
  truth that can disagree with the first.

**The change gate is the one that actually stops them talking forever:** a
dream may only be conferred again if **something changed** since the last
exchange — a condition fulfilled, a hop added or checked, an operator note, a
vault move. A turn limit bounds one conversation and says nothing whatever
about having the same conversation again tomorrow, politely, at cost, with
every other cap still holding while they do it. It is the cap most likely to
look redundant to somebody tidying up, which is why `has_something_changed`
says so in its own docstring.

**The per-dream count is an EPOCH now, not a lifetime, and that was a real
defect.** It used to be three exchanges and then silence forever, so a dream
the trader kept declining could never be reconsidered even after the world
moved under it — which is the opposite of what the change gate exists for. New
information opens a new epoch and the count starts again; the lifetime cap of
12 is what keeps that bounded. **"Not until something changes" and "never
again" are different facts**, so they are different outcomes and the second is
logged at warning rather than sharing the first's quiet path.

Both halves read one pure `change_signals`, rather than growing a second
definition of what counts as new — the epoch takes the newest signal, the gate
filters signals after the last exchange. The comparisons deliberately differ by
one boundary (`>=` against `>`) and the docstring says why.

**The hand-back is answered separately, on purpose.** `return_to_vault` stamps
the dream and the marker turn at the same instant, so the change gate
structurally cannot see it — and loosening that comparison to catch it would
break the trap that says *the end of an exchange is not itself a change*.
`Conference._handed_back_since_the_last_offer` reads the fact instead.

### Symbiosis: two or three dreams fuse, and the parents survive

`DreamStore.fuse` writes the child in one transaction; `plan_fusion` is the
pure merge arithmetic beside it, split out the same way `promotion_for` is
split from `promote`. The back-reference is **derived** through `children_of`
rather than stored, for the same reason `Adoption.is_live` is computed: a third
fact about the same thing is a third fact that can disagree with the other two.

Three properties are load-bearing, and all three make a fusion *weaker* than
the enthusiasm for it:

- **A fusion is never better sourced than its worse parent.** A shared hop
  arrives UNCHECKED even where one parent had sourced it, and
  `verification_ceiling` caps the badge. A link whose sourcing is in dispute
  must not take the flattering reading; the source stays on the parent.
- **The child inherits ALL of both parents' conditions**, so fusing makes a
  dream harder to promote rather than easier. A fusion is not an endorsement.
- **An adopted parent is refused**, because a live grant points at it. Hand it
  back first.

A fusion has no `weakest_hop` and no verdict by design. That reads as *nobody
has attacked this yet*, which is true, and must not be rendered as a gap.

### A chat agent may raise a CONSIDERATION, and may not raise a dream

The operator's rule, and it closes a hole rather than renaming a thing: *"Chat
agents can't raise Dream, the agent can merely put it to consideration, hence
the chat log."*

A dream is the first link of a chain that ends in a live trading permission —
dream → prophecy vault → dream vault → adopted → `grants.resolve_granted_symbols`
→ a symbol `RiskGate` will now allow that is not in `config/rules.yaml`. A
conversational surface holding a tool that inserts at the top of that chain
means a signed-in user can talk a model into the first link. Every gate
downstream still runs, so no rule breaks today — but **"it cannot create one"
and "it can create one and the other steps catch it" are different claims, and
only one is worth making.** Same reasoning that put the dreamer on its own
Hermes instance with no MCP server.

So `raise_consideration` writes **one audit-log line** and never opens
`data/dreams.db`. The containment is structural rather than enforced: nothing
in `dreaming.py` reads the audit log, so there is no code path that turns a
consideration into a shelf row.

- **It carries no `symbols`, no `asset_class_key` and no hops.** Those three
  are what make a dream capable of becoming a permission; a consideration
  holding them would be a dream wearing a different noun. A test asserts the
  field overlap is empty, in the same shape as
  `test_a_dream_cannot_describe_an_order`.
- **The strongest test asserts `dreams.db` DOES NOT EXIST** after one is
  raised — stronger than asserting an empty shelf, because it proves the store
  was never opened.
- **The dreamer decides.** A consideration is a note addressed to it, read on
  its own run, and it may ignore one. The operator can point at something; the
  thing that dreams still chooses.
- **The chat log is the record**, which is what the operator meant by "hence
  the chat log" — so it surfaces in the transcript and as something awaiting
  the dreamer, never as a dream that exists.

**The trading agent's side reaches no broker.** `TraderPowers` has exactly two
public methods — adopt from the vault, hand back with a reason — and a test
parses the module's AST to assert it imports none of `broker`, `risk`,
`models`, `journal`, `mcp_server` or `reconcile`. A failed model call ends the
exchange and is recorded as a failure, never as a completed exchange that
decided nothing.

### The stop is visible now, in both places it was missing

Two different facts, deliberately reported separately, because the interesting
case is when they disagree:

- **`WorkingOrder.stop_price`** — what actually rests at the broker. It carries
  `order_type` alongside it, and that is the half worth defending: without it
  `stop_price is None` cannot be read. On a plain limit order that None is
  correct and dull; on a stop leg it means nobody can say where the stop is.
  `trigger_price_unknown` is therefore a separate question from "is there a
  stop price", which is the missing-versus-absent rule arriving at the order
  layer.
- **`AccountSnapshot.planned_stop_by_symbol`** — what the journal planned,
  rendered into the model's context. `model_client` asks for a `position_plan`
  on every open position with an action of hold, close or **tighten_stop**, and
  the context block used to carry no stop at all. The agent was being asked
  whether to tighten a level it had never been shown.

A position with no journal row renders **STOP UNKNOWN in words** — never a
blank that reads as "no stop needed", never a zero. The exposure is real and
the protection is unknown, and those are different facts.

### A heading is a claim, and a wrong one is not fixed by the row being right

The Board filed the resting stop leg under **"Pending orders"**. Every value in
that row was correct. The heading was not, and the heading is what got read.

"Pending" asserts that something is about to happen — that the agent is part of
the way into a new position. A resting stop leg is the opposite: it is the
guarantee that something will *not* happen. So the page told the operator the
bot was mid-way into another trade, every time they looked at the one row that
exists to promise otherwise. The operator said it plainly: *"for the ui its not
a pending order it's a stop loss isn't it? Pending infers the trading agent is
going to put another trade down."*

**This is the second time that same leg was mistaken for junk.** The first was
`str()` on an SDK enum rendering its status as `other`, which is already
recorded here as *a badly rendered safety mechanism gets mistaken for junk and
asked to be removed*. That one was a VALUE. This one was a LABEL, and the value
was fine — which is why it survived the first fix.

Two groups now, and the split is what makes both headings honest:

- **Protective, in force** — anything that would REDUCE something held, so a
  bracket's take-profit counts too rather than only `is_stop`. It states the
  trigger level, and `trigger_price_unknown` renders as an explicit alert: on a
  plain limit a missing stop price is correct and dull, on a stop leg it means
  nobody can say where the stop is.
- **Pending entries** — orders that will become positions, which is the only
  case where "the agent is about to put a trade down" is a true reading.

**The best part was unplanned: the ABSENCE became legible.** Once protection
has its own section, a position with nothing resting behind it can be shouted
about — and when the check itself fails, the page says so and refuses to claim
either way, so "nothing returned" never reads as "nothing resting". That state
was invisible while everything sat in one list, which is the general form worth
carrying: **grouping by what a thing IS makes the missing member visible, and a
mixed list hides it.**

### A two-class element is decided by DECLARATION ORDER, and it has bitten three times

`class="note alert"` renders **pewter, not amber**. Both are single-class
rules, so the specificity is equal, and `.note` is declared after `.alert` —
the later one wins. The most severe note on the page stops looking like a
warning. Valid CSS, no error, text still readable, and invisible unless
somebody looks at the rendered pixels.

That is the third instance of one bug:

- `.pill.seed` — a stage badge picked up `.dream .seed { padding: … }`, written
  for a paragraph, and rendered as a full-width block.
- `.rung.gate` — same shape.
- `.note.alert` — a colour modifier silently overridden by a base class.

So it is a rule now rather than three anecdotes. **A modifier class must not
depend on winning a tie.** Either give it higher specificity deliberately
(`.note.alert`, which beats both), or put the modifier on its own element so
there is no tie to lose. Guess neither: `tests/test_web.py` pins the shape.

The general form is worth carrying past CSS: **where two things are combined
and precedence is implicit, the result is decided by an ordering nobody is
looking at.** The `.gitignore` depth trap, the SDK enum below, and this are all
the same failure wearing different clothes.

And note what all three have in common — **each was found by looking at the
page, never by a test.** A collision produces valid CSS, valid HTML and a
plausible render.

### `str()` on an SDK enum is a silent, total mapping failure

Bitten twice in one session, in the same file, and the second one had been
wrong since the day it was written.

alpaca-py's enums subclass `(str, Enum)`, so they compare equal to their value
and read like strings — but `str(x)` returns **`"OrderStatus.HELD"`**, not
`"held"`. So:

```python
_order_status(str(getattr(o, "status", "")))    # never matched anything, ever
```

Every mapping arm missed and every order fell through to `OrderStatus.OTHER`.
The Board did not merely mislabel `held`; **it had never once rendered a
correct status for any order**, and the operator's live stop leg showed as
`other`. Nothing raised, nothing logged, and the fallback existed precisely so
an unknown status would not crash — which is what made it invisible.

`order_type` hit the identical trap when it was added days later and was caught
only because a browser audit read the row.

**Take `.value` first, and fall back to the object:**

```python
raw = getattr(o, "order_type", None) or getattr(o, "type", None)
order_type = str(getattr(raw, "value", raw) or "").lower()
```

The general shape is worth carrying beyond Alpaca: **a lenient fallback plus a
silent coercion is a mapping that can fail completely while looking healthy.**
Where a fallback exists so that an unknown value cannot crash, something has to
make the "everything is unknown" case loud — a count on the cycle line, a test
over real SDK objects, or an assertion that at least one arm matches.

And the reason this one mattered: the misrendered status is what made the live
stop leg look like debris, which is what led to it being flagged for deletion.
**A badly rendered safety mechanism gets mistaken for junk and asked to be
removed.**

### One page, three prices, none of them wrong

The Board showed SPY at 774.12 on the tape, 774.0900 in the positions "Now"
column and 774.0800 in the orders "Market" column, in a single render.

All three are correct and they are different measurements: `Position.current_price`
is Alpaca's own mark; `get_tick(symbol).mid` is the midpoint the poller computes
from bid and ask; and the tape runs on its own 60-second clock, deliberately
slower than the five-second account poll.

Nothing is broken and the page still misleads, because three different facts sit
in columns that all read as "the current price". This is the `market_clock` rule
somewhere new — the venue's phase and the gate's window, stated separately and
never merged. **Label them; do not unify them.** The mark is what the broker
settles against, the midpoint is what an order's distance is measured from, and
the tape's whole value is being cheap and slightly behind.

### CI exists now, and it is the only place a green suite means anything

`.github/workflows/ci.yml`: `ruff`, `mypy --strict`, `pytest`, cheapest first,
and **each runs even when an earlier one is red** — the same reasoning
`RiskGate.evaluate` uses for collecting every failure reason instead of
short-circuiting. Running is not passing; any red step fails the job.

It earned its keep on the first day, catching `grants.py` committed while the
`config.py` it depends on was not, on a suite that had just passed locally.
That is the `test_packaging.py` lesson in a new costume: a checkout holding
only what was actually pushed is the one place that class of fault cannot hide.

**No credentials, and none should ever be added.** Tests use `MockBroker`, may
not touch the network, and a `conftest` fixture fails the suite if anything
writes to `data/` or `audit/`. A job here needing a secret means something has
been wired the wrong way round.

### Hermes ships a large surface, and both ways of trimming it fail quietly

`deploy/hermes-config.yaml` disables 25 toolsets and all 77 bundled skills.
Verified against hermes-agent `934546f` by reading the resolver and running it.

**`terminal` CAN be dropped.** This reverses the earlier note. `terminal` is a
plain toolset resolving to `["terminal", "process"]`, and
`model_tools._compute_tool_definitions` applies `disabled_toolsets` as a final
subtraction **regardless of what `enabled_toolsets` selected**, so the tools go
even when a `hermes-*` bundle pulled them in. Measured: 24 tools with
`hermes-cli` enabled, 22 with `terminal` also disabled. The old finding was
about `platform_toolsets.acp`, a different key, which still stands for ACP mode.

Three traps, all of which fail without an error message:

- **`/tools` and the startup banner still list `terminal` after it is dropped.**
  The four `get_tool_definitions` calls in `cli.py` pass `enabled_toolsets` and
  omit `disabled_toolsets`. They are display only and never assign to
  `agent.tools`; the real list comes from `agent/agent_init.py`, which does pass
  it. Verify by **asking the agent to run `ls`**, never by reading `/tools`.
- **`skills.disabled` takes skill NAMES, not categories or directories.** The
  filter is `if name in disabled` against the `name:` in each `SKILL.md`. A
  category name matches nothing and is discarded silently. Four of the shipped
  skills are not named after their directory at all: `mlops/evaluation` and
  `mlops/inference` are grouping folders, so the names are
  `evaluating-llms-harness`, `weights-and-biases`, `llama-cpp` and
  `serving-llms-vllm`. Writing `evaluation` there does nothing and says nothing.
- **A duplicate `agent:` key is not a merge, and nothing raises on one.**
  `yaml.safe_load` keeps the **last** occurrence of a duplicate key rather than
  erroring, so the file parses and Hermes starts on whichever half survived.
  The parse check in the old file header therefore passes on a broken config
  and confirms the wrong thing. **Never `cat >> ~/.hermes/config.yaml`**: Hermes
  writes `approvals:`, `mcp_servers:`, `agent:` and `skills:` itself on first
  run, so appending duplicates all four at once. Observed on the droplet.
  `deploy/merge-hermes-config.py` deep-merges instead — it backs up, keeps the
  file owned by `hermes`, refuses a config that already carries duplicates, and
  re-reads the result to prove both blocks arrived. Dry run by default.

**None of this replaces the user split or the sudoers rule.** A dropped toolset
is a line in a YAML file, and every failure mode above is silent: the agent
comes back holding a shell and nothing says so. Unix permissions do not fail
that way. Config trimming is the second lock, never the first.

The denylist admits whatever the next Hermes release adds. `toolsets:` is an
allowlist and would not, and MCP server names are valid entries there, but it is
deliberately left commented out because it could not be verified end to end from
outside the box, and getting it wrong yields an agent with almost no tools.

### Nothing is published any more, and do not build a second surface

**There was a public marketing site and it is gone.** `brand/` was six static
pages on Vercel — landing, overview, trades, analytics, rules, about — rendering
a committed fixture of invented figures, with `scripts/generate_demo_data.py`
producing it. Deleted, along with the generator, at the operator's instruction:
*"that was just vercel hosting demo to get started. Josh will be the only one
using the app and he will just log straight in."*

**Do not rebuild it, and do not add a demo mode to the real dashboard.** There
is one operator, one account, and no audience. Everything a shop window cost is
a cost with nothing on the other side of it:

- **Two design systems to keep in step.** `brand/assets/app.css` and
  `render.STYLES` were the same projection engine written twice, and this file
  used to say so as a thing to remember. One copy cannot drift from the other.
- **A fixture with its own correctness obligations.** The generator asserted
  that no demo trade breached the 1% cap, that open risk stayed under 2%, and
  that the limits echoed into its JSON still matched `config/rules.yaml` — a
  demo showing a 1.4% risk against a 1% cap teaches the reader the wrong thing
  about what the gate does. All of that was maintenance for numbers nobody
  needed.
- **A public page of invented figures that looks exactly like a trading
  dashboard.** The banner saying so was plain HTML in all six files precisely
  because that label must not depend on a script having run. That care was
  correct and the safest version of it is not publishing the page.

The rule that outlives it: **`src/bot/web/` is the only surface, and what
protects it is a server-side password rather than the absence of a link.** If
live data is ever wanted on a public host, real authentication is the
prerequisite and not a follow-up — that is a separate and much larger project
(an API off the droplet, TLS, a threat model) and not a matter of swapping a
data source. Say so.

**`src/bot/web/` may now be exposed, and the prerequisite was met rather than
waived.** It used to have no login *because* it bound to `127.0.0.1`, and the
rule here was that publishing it needed real authentication built first. The
operator has chosen to expose it, on the grounds that the account behind it is
Alpaca **paper** money and no funds can be lost. `src/bot/web/auth.py` is the
authentication that was named as the prerequisite.

What that gate is: a shared password from `DASHBOARD_PASSWORD`, enforced by
**middleware** rather than a per-route dependency — so a route added later is
protected by default and opening one takes a deliberate edit to `OPEN_PATHS`.
Constant-time comparison, session tokens stored hashed, `HttpOnly` cookies, and
five attempts per five minutes so the password cannot be found by guessing.

What it is not: multi-user authentication. One password, no accounts, no
rotation, no record of who signed in. That is proportionate to paper money and
would **not** be proportionate to a live account. **If this ever fronts real
money, `auth.py` is the file to replace, not to extend.**

Four things about it are load-bearing:

- **The password lives in the environment, never in the repository.** The
  original request was to put it in the public marketing site, which cannot
  work: that was static files in a public GitHub repo, so the password would
  have been readable in the repo and in the page source, and there was no
  server there to check it against. The site is gone; the rule is not.
- **`POST /chat` keeps its own separate token on top — and that separation is
  narrower than it reads.** Viewing an account and driving an agent that can
  reach the broker are different privileges, and the token is what keeps them
  apart from someone holding NEITHER secret. It does not keep them apart from
  someone holding the password: `app.py` passes `dashboard_chat_token` into
  `chat_page` and `settings_page`, which render it into the markup as
  `var TOKEN = "..."`, because the browser genuinely needs the value to POST
  with. So anyone who can sign in can read the chat token out of the page
  source and drive the agent.

  **Say it that way round rather than "one secret must not grant both", which
  is what this said and is not what the code does.** The claim that survives is
  the one that was always doing the work: `RiskGate.evaluate` runs on every
  order path behind chat, so what is gained is an agent that can propose, not a
  route around the four rules. Exposure without chat risks disclosure; exposure
  with chat risks action, and the password is the whole gate on both.

  Fixing it properly means the browser never holding the token — a per-session
  CSRF-style value minted after sign-in, or moving the check to the session
  cookie the middleware already validates. Not done, and it is only worth doing
  if the account stops being paper: see `TODO.md`, where the operator's decision
  to rely on Tailscale device access instead is recorded with what would change
  it.
- **Chat working at all costs the web unit two sandbox settings**, and the
  operator turned it on knowing that. `NoNewPrivileges` and `RestrictSUIDSGID`
  both block `sudo` (systemd makes the second imply the first), so they are
  absent from `mudhorn-web.service` and the unit explains why in place.
  Three things keep the grant bounded and none may be removed casually:
  the sudoers rule names **`deploy/run-chat.sh`**, a root-owned wrapper with no
  arguments, rather than the Hermes binary — which would accept `--yolo` and
  every flag a future release adds; the prompt travels on **stdin**, so nothing
  a signed-in user types can be read as a flag; and the sudo runs **downward**,
  `mudhorn → hermes`, to an account holding no credentials that reaches the
  broker only through the MCP server, where `RiskGate.evaluate` runs on every
  order. Chat cannot become an order path that skips the gate.
  **`ProtectHome` had to come off too, and the obvious reasoning for keeping it
  was wrong.** Putting the wrapper under `/opt/mudhorn` fixes the availability
  check — `Path.exists()` raises rather than returning False on a directory it
  may not traverse, which was a 500 on the Chat page — but it does not fix
  this: **systemd sandboxing is a mount namespace and every descendant inherits
  it, sudo included.** The wrapper ran as `hermes` inside the web unit's
  namespace, where `/home` is empty, and died on `cd /home/hermes: Permission
  denied`. Hermes runs from that home; there is no arrangement where it can run
  without it. The grant is smaller than it looks — the sudoers rule already
  permits *executing as* `hermes`, which exceeds reading its files.
- **No password set still means no gate**, which is correct for the loopback
  deployment and is tested. What must not happen is exposing it *without* one,
  and the app cannot detect that case — a Funnel or a reverse proxy still
  arrives on loopback, so a public request and a local one are identical from
  inside the process. `electrum-bot-web` therefore says which mode it is in at
  startup, because that is the only moment anyone can be told.
- **The sign-in page reveals nothing about the account** — no equity, no
  positions, no symbols, not even whether a trade has ever been placed.
  Everything it renders is already public in the repository.

`bot/web/app.py` imports FastAPI **at module scope**, and that is not tidiness.
`from __future__ import annotations` makes every annotation a string, and
FastAPI resolves them against the module globals; with `Request` imported inside
`build_app` it is unresolvable, so FastAPI treats `request: Request` as a query
parameter and the route answers `422 field required`. Nothing warns.

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
TODO.md                 The work list. Ordered by what blocks, with the
                        reasoning kept beside each item, and the live account
                        state at the top. Read before starting anything.
config/rules.yaml       Trading limits. Enforced in code. The only place to change behaviour.
                        `instruments:` carries each class's own limits, and a value
                        there OVERRIDES the global one IN EITHER DIRECTION — `account:`
                        is the default, not a ceiling, and nothing refuses a looser
                        value. `watchlist:` is DISPLAY ONLY and is deliberately not
                        `allowed_symbols`.
src/bot/
  risk.py               The risk gate. The load-bearing file in this repo.
  reconcile.py          Squares journal against broker each cycle. Populates open risk.
  journal.py            SQLite trade store + persistent stand-down state.
  stand_down.py         Consecutive-loss breaker: when to trigger, when to escalate.
  options.py            OCC parsing and expiry safety. Protective only.
  broker.py             Broker Protocol + AlpacaBroker + MockBroker. Daily bars and
                        resting orders live here.
  market_clock.py       What time it is where the market is, and what the market
                        is doing. Alpaca's five session phases computed in New
                        York time, so daylight saving needs no diary entry. States
                        the venue's phase and the gate's window SEPARATELY and
                        never merges them into one green light. Pure; no network.
                        `render_sessions` tells the model, per instrument class,
                        what an order placed right now would actually become —
                        out of hours that is "rests until the open", never a fill.
                        `BrokerClock` is the one reading it takes from Alpaca,
                        for the one thing arithmetic cannot know: a holiday.
                        Feeds RiskGate._premarket and the ticker tape.
  indicators.py         Averages, ATR, volume ratio, swing levels. Pure functions over
                        daily bars. Computed in Python so the model never derives them.
                        `snapshot()` records them as NUMBERS alongside the rendered
                        line, which is what a stored trigger is graded against.
  stop_watch.py         Is an open position already through the stop it was
                        sized against? The backstop for what a broker-side
                        bracket cannot cover: out of hours, crypto, and adopted
                        positions. Reports; never closes. Pure functions.
  triggers.py           Grades the watch list: did the named condition fire, and
                        did anything follow. Plan-following, never counterfactual
                        P&L. Operator surface only — it must not reach the prompt.
  intraday.py           Five-minute bars: did price CLOSE beyond a level or only wick
                        through it, on what volume, and was it reclaimed. The
                        distinction trend_break turns on.
  mcp_server.py         MCP tools: check_order, place_order, close_position,
                        get_risk_status, review_watches,
                        get_recent_news, get_recent_decisions, query_history,
                        describe_history, search_news, ...
  models.py             Domain models. Quantities are shares/coin units, never "lots".
  config.py             Typed env + rules loader. Validators reject incoherent limits.
  model_client.py      Anthropic SDK wrapper (1h prompt cache, structured output).
  context.py            Renders market state for Claude.
  strategy.py           Base strategies. Placeholders with a shape, not an edge.
                        `requires` names what each one still cannot see.
  data/                 External feeds. marketaux.py = headlines (context only);
                        finnhub.py = earnings calendar (feeds the blackout gate);
                        xfeed.py = posts from watched accounts (context only).
  audit.py              Append-only JSONL decision log, and the reader the
                        Decisions page renders. The only record of a REJECTED
                        proposal.
  session_calendar.py   Which days trade and until what time. Alpaca's trading
                        calendar, cached once a day, answering the two things
                        New York clock arithmetic structurally cannot: which
                        days are SKIPPED and which end EARLY. Keyed to the
                        instrument class, never to a symbol — every US equity
                        shares one session. Gates nothing, is never backed up,
                        and answers `None` for "could not ask" rather than
                        letting an outage read as a quiet quarter.
  exchange_hours.py     Tokyo, Sydney and Auckland: which days they trade and
                        between what hours, from `exchange_calendars` offline.
                        An OPTIONAL dependency whose absence is exactly the old
                        weekday-shaped behaviour, measured by uninstalling it.
                        Deliberately silent about New York, which Alpaca answers.
                        Display badges only; gates nothing, no network.
  news_history.py       What the loop was SHOWN, recalled out of the audit log
                        and deduped across cycles. Reads rather than fetches,
                        because the Marketaux quota belongs to the loop. Pure
                        functions; no network.
  insight.py            Derived SQLite index over audit/*.jsonl, so the whole
                        history is queryable rather than only a recent window.
                        Rebuildable, never authoritative, never backed up, and
                        read by nothing that trades — which is what makes
                        handing an agent read-only SQL over it reasonable.
  metrics.py            Win rate, profit factor, expectancy, R, MAE/MFE. Pure functions.
  tailnet.py            Is the Tailscale link still going to be there next week.
                        Warns at ten days, and says "unknown" rather than "fine"
                        when the check itself has stopped.
  souls.py              Loads the character files in souls/. Degrades to a
                        voiceless prompt rather than taking a page down.
  dreaming.py           Second-order hypotheses: Dream, Hop, the stage machine
                        and the store. Carries NO order fields, which is the
                        whole reason it may sit beside an order path.
  dreamer.py            The thing that fills it. One Claude call per run,
                        driven by `electrum-bot dream`, never by the loop.
                        Shown headlines, posts and what CLOSED; never shown
                        profit and loss.
  web/                  Operator command centre: Board, Decisions, Trades,
                        Analytics, Chat, Dreaming, Settings. Binds 127.0.0.1, and sits
                        behind auth.py's shared password so it MAY be exposed.
                        live.py streams the account over SSE from ONE poller,
                        so a render costs no network and a cold start says
                        unknown rather than zero. seen.py answers "what changed
                        since I last looked", with the marker advancing to the
                        previous request rather than to now.
                        Read-only apart from POST /chat, which is off unless
                        DASHBOARD_CHAT_TOKEN is set. auth.py is the shared
                        password gate; chat.py runs one Hermes process per
                        message and keeps no state of its own.
                        render.STYLES and render.SCRIPT carry the projection
                        layer: starfield, hyperspace jump, panel materialisation.
souls/                  Character files for the three agents, in the SOUL.md shape.
                        yoda.md teaches about the account; grogu.md dreams;
                        armorer.md keeps the limits. tests/test_souls.py
                        caps each at 1,600 words -- a breach is fixed by
                        moving mechanics back into the system prompts.
  main.py               CLI: smoketest, loop, dream, reindex.
  grants.py             Turns a live dream adoption into the symbol permission the
                        risk gate is handed. Applies the enabled-class hard block
                        and answers {} on ANY failure, so the caller fails closed.
data/dreams.db          Speculative notes AND the live symbol grants an adopted
                        dream carries. NOT the journal, NOT backed up — losing
                        it withdraws every grant, which is the safe direction.
                        Gitignored.
deploy/                 VPS provisioning: bootstrap.sh + systemd units. The unit
                        runs the loop WITHOUT --execute; enabling it is a
                        drop-in (mudhorn-bot-execute.conf), never an edit to
                        the unit, which bootstrap.sh would overwrite. src/,
                        config/ and souls/ stay root-owned so the service
                        account cannot edit its own limits -- or its own
                        safety rails, which souls.py reads from disk at call
                        time.
                        backup-journal.sh + mudhorn-backup.timer snapshot the
                        journal hourly with sqlite3 .backup, never cp — and the
                        audit log with plain gzip, which IS correct for
                        append-only text. Audit runs first so a journal problem
                        cannot stop it.
                        check-tailscale.sh + mudhorn-tailnet.timer watch the key
                        expiry that would otherwise take the dashboard away
                        silently.
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
.venv/bin/python -m pytest              # full suite
electrum-bot smoketest --mock           # no credentials needed
electrum-bot smoketest                  # needs Alpaca paper keys
electrum-bot dream                      # one lateral-thinking step; places nothing
electrum-bot loop                       # proposes and vets; places nothing
electrum-bot loop --execute             # places approved orders on PAPER
electrum-bot reindex                    # rebuild the searchable history from audit/
electrum-bot-mcp                        # MCP server, usually launched by Claude Code
electrum-bot-web                        # command centre on http://127.0.0.1:8787
```

`--execute` is off by default. Leave it off until you have watched the proposals
for a while and agree with them.

## What is next

`TODO.md` holds it, ordered by what is actually blocking.

**Every code item that BLOCKS is closed.** One optional one is not — multi-agent
dreaming, below — and everything else left needs something a session in a
container does not have:

- **A deploy.** The droplet still runs code from partway through the session,
  and `deploy/bootstrap.sh` also closes the `souls/` ownership gap — the safety
  rails were writable by the service account they restrain.
- **A live pre-market window**, to verify the one documented-but-untested claim
  in the order path: Alpaca's docs say `extended_hours` is refused on a bracket
  and an OTO, and no such order has ever been sent from here to watch it be
  refused. `allow_extended_hours_fills` is built and off on every class; that
  check comes before it is ever turned on. If Alpaca **downgrades** rather than
  rejecting, a stop goes missing with no error.
- **A subscription** (item 11, the X feed) and **a control panel** (the
  DigitalOcean tier question).
- **One open design question** — whether a dream is gradeable after adoption,
  and how to do that without grading P&L by accident.
- **The souls' rails on their new model.** They run on DigitalOcean now, and
  every prose rail — *"never state a figure you did not read"*, *"push back
  without refusing"* — is exactly the kind of instruction that varies between
  models and fails quietly. Measured: llama held 10/15, deepseek 13/15.
  **A breach is a finding, not an argument against the move.** The answers are
  a sharper soul clause or a different model, never a weaker rail.

The list below is the older deferred set and is duplicated there.

## Deferred, and noted so the shape is not lost

Three entries that used to live here — the settings agent, the trailing stop
and the exit review — are BUILT. Their reasoning has moved into the body above
rather than being deleted with the deferral, because in every case the reason
outlasted the wait.

- **Multi-agent dreaming.** Several dreamers working a topic independently and
  then debating it out before a verdict. `Thought.by` already carries the
  attribution that needs.
- **~~Vercel AI Gateway~~ — DROPPED, and not on cost grounds.** It speaks the
  Anthropic Messages API, so it was a base-URL swap rather than a rewrite, and
  it was never built. It is off the table because the operator wants **one
  account**: DigitalOcean already hosts the droplet, and a gateway on a third
  provider is another login, another subscription and another balance to keep
  topped up for a component whose entire bill is about $8/month. The same
  base-URL swap points at DigitalOcean instead — see `docs/DROPLET_AI.md`, and
  note that the blocker there is structured output rather than plumbing.

## What is deliberately not here

- Live trading. Paper only.
- A trading strategy. The foundation is broker + safety + interface; the strategy
  is the operator's to build and is the genuinely hard part.
- Option *trading*. Greeks, spreads and assignment are deferred; only expiry
  safety exists.
- A backtesting harness. Sketched in `docs/HANDOFF.md`. (The dashboard is
  BUILT — see "The command centre is the real product".)
