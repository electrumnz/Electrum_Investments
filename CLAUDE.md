# Mudhorn Capital — trading bot. Instructions for Claude Code sessions.

An AI trading bot running against an **Alpaca paper-trading account**. Read this
before touching orders, risk, or config.

**Scope:** single operator, personal trading, paper money. Not a product, not
multi-user. That assumption is why the dashboard has one shared password rather
than accounts, why it binds to `127.0.0.1` and is reached over Tailscale or a
Funnel, and why the non-permissive licences in `reference/` are not a
constraint. The dashboard **may** now be exposed publicly, behind
`DASHBOARD_PASSWORD` — see `src/bot/web/auth.py` for exactly what that gate is
and is not. If the paper-money assumption ever stops being true, that file is
the first thing to replace, and re-read `reference/STATUS.md` too.

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

### The command centre is the real product; brand/ is the shop window

Two surfaces, and confusing them is the mistake to avoid.

`src/bot/web/` renders **live** journal, broker and audit state, on seven pages:
Board, Decisions, Trades, Analytics, Dreaming, Settings, Chat. It has no login *because*
it binds to `127.0.0.1` and is reached over Tailscale. That is where operator
features belong, and building one there needs no auth because nothing is
published.

`brand/` is public and **every figure on it is invented**. It is a shop window.

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

**Settings shows the limits and offers no way to change them.** A settings
screen that could widen a cap would be used to widen one during a losing run,
which is exactly when the cap is doing its job. Each limit names the file that
owns it. Credentials are reported as configured or not configured, never
rendered: loopback-bound is not the same as private, and a screenshot travels.

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
in `MarketInputs`, which only starts paying from the day it ships.

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
login form. It has a second effect worth keeping: Decisions, Trades, Settings
and Chat have no live figures either, so a session that never opens the Board
never talks to Alpaca at all.

**The client may only UPDATE a figure the server already rendered**, never be
what reveals one. Same rule as the projection layer. Every value is in the
markup before the script runs, so a blocked script shows the reading it was
served — one page-load old and honest about it — rather than an empty box.

**That bug was invisible to the test suite.** 731 tests, `ruff` and `mypy
--strict` were all green while the login page 401ed on every view. It took
driving the thing in a browser. Same shape as the `.gitignore` finding below: a
green local suite says nothing about what actually happens.

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

Both surfaces carry a starfield, a hyperspace jump on navigation, HUD bracket
corners and panels that materialise: `render.STYLES` plus `render.SCRIPT` on the
dashboard, `brand/assets/app.css` plus `brand/assets/fx.js` on the public site.
Same engine twice, the same split those two design systems already live with.

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

### Two agents, two souls, and Hermes only holds one

`souls/yoda.md` answers about the account on `/chat`; `souls/grogu.md` dreams on
`/dreaming`. Both follow the `SOUL.md` convention down to the headings
(`## Personality`, `## Style`, `## What to avoid`).

**Hermes loads exactly one soul, from `$HERMES_HOME/SOUL.md`.** Not the working
directory, no CLI flag, no environment variable to point at another file, and
`/personality` is a session overlay rather than a second soul. One instance, one
character. This repository needs two on one instance chosen per request, so
`HermesBridge.ask` prepends the selected soul to the prompt **on stdin**, which
is the only mechanism that can vary per call — and is where it has to go anyway,
because the sudoers rule permits `run-chat.sh` with no arguments so nothing a
signed-in user types can be read as a flag.

**Do not install either file as `~/.hermes/SOUL.md`.** It would apply to both
agents at once, alongside whichever soul the request injected, and the model
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

**`Dream` carries no quantity, no entry price, no stop, no side and no symbol.**
`OrderProposal` requires all of them and validates `stop_loss_price`, so nothing
turns one into the other without somebody adding fields and validation by hand.
`tests/test_dreaming.py` asserts that overlap stays empty. This is the whole
safety argument and it is deliberately not a matter of discipline: a
speculative-idea generator wired to an execution path is the Alpha Arena failure
with extra steps, and confidence is what this module produces most of.

`instruments` names what a dream is *about* and is free text on purpose, so it
cannot be read as a ticker the bot trades.

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

**`data/dreams.db` is its own file, not the journal.** Losing every dream costs
some speculative notes; `backup-journal.sh` covers the one irreplaceable file
and deliberately does not cover this. Keeping them apart also means no query can
read a hypothesis as a position.

**Tests must not write to `data/` or `audit/`, and a fixture now enforces it.**
A session-scoped autouse guard in `tests/conftest.py` fails the suite if either
directory gains a file. `DreamStore` landing is exactly how that regressed: a
new store, a new `build_app` default, and one call site nobody updated.

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
- **There is no sign-in form, and there must not be one.** There used to be:
  prefilled, accepting anything, gating nothing, with every page reachable
  without it. It was removed because a gate-shaped ornament implies the site
  holds something worth protecting, and this one holds a committed fixture. The
  landing page is now a link straight into the overview, plus a link out to the
  **live** dashboard, which is a different host with a real server-side
  password. Do not reintroduce a decorative login here; if live data ever needs
  showing on this host, real authentication is the prerequisite, not a
  follow-up.
- **The generator asserts its own output** before writing: no trade over the 1%
  cap, open risk under 2%, a stop on every trade, and the limits echoed into the
  JSON still matching `config/rules.yaml`. A demo showing a 1.4% risk against a
  1% cap teaches the reader the wrong thing about what the gate does.

`config/rules.yaml` is the single exception, copied verbatim onto the Rules page.
It is limits rather than secrets and is already public.

If asked to point the public site at the real journal: that is a separate and
much larger project — real authentication, an API off the droplet, TLS, a threat
model — and not a matter of swapping the data source. Say so.

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
  original request was to put it in `brand/`, which cannot work: that is static
  files in a public GitHub repo, so the password would be readable in the repo
  and in the page source, and there is no server there to check it against.
- **`POST /chat` keeps its own separate token on top.** Viewing an account and
  driving an agent that can reach the broker are different privileges, and one
  secret must not grant both. Exposure used to risk disclosure; with chat it
  risks action.
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
config/rules.yaml       Trading limits. Enforced in code. The only place to change behaviour.
src/bot/
  risk.py               The risk gate. The load-bearing file in this repo.
  reconcile.py          Squares journal against broker each cycle. Populates open risk.
  journal.py            SQLite trade store + persistent stand-down state.
  stand_down.py         Consecutive-loss breaker: when to trigger, when to escalate.
  options.py            OCC parsing and expiry safety. Protective only.
  broker.py             Broker Protocol + AlpacaBroker + MockBroker. Daily bars and
                        resting orders live here.
  indicators.py         Averages, ATR, volume ratio, swing levels. Pure functions over
                        daily bars. Computed in Python so the model never derives them.
                        `snapshot()` records them as NUMBERS alongside the rendered
                        line, which is what a stored trigger is graded against.
  triggers.py           Grades the watch list: did the named condition fire, and
                        did anything follow. Plan-following, never counterfactual
                        P&L. Operator surface only — it must not reach the prompt.
  intraday.py           Five-minute bars: did price CLOSE beyond a level or only wick
                        through it, on what volume, and was it reclaimed. The
                        distinction trend_break turns on.
  mcp_server.py         MCP tools: check_order, place_order, get_risk_status,
                        get_recent_news, get_recent_decisions, query_history,
                        describe_history, search_news, ...
  models.py             Domain models. Quantities are shares/coin units, never "lots".
  config.py             Typed env + rules loader. Validators reject incoherent limits.
  claude_client.py      Anthropic SDK wrapper (1h prompt cache, structured output).
  context.py            Renders market state for Claude.
  strategy.py           Base strategies. Placeholders with a shape, not an edge.
                        `requires` names what each one still cannot see.
  data/                 External feeds. marketaux.py = headlines (context only);
                        finnhub.py = earnings calendar (feeds the blackout gate);
                        xfeed.py = posts from watched accounts (context only).
  audit.py              Append-only JSONL decision log, and the reader the
                        Decisions page renders. The only record of a REJECTED
                        proposal.
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
                        Analytics, Dreaming, Chat, Settings. Binds 127.0.0.1.
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
souls/                  Character files for the two agents, in the SOUL.md shape.
                        yoda.md answers about the account; grogu.md dreams.
data/dreams.db          Speculative notes. NOT the journal, NOT backed up.
                        Gitignored.
  main.py               CLI: `electrum-bot smoketest`, `electrum-bot loop`.
deploy/                 VPS provisioning: bootstrap.sh + systemd units. The unit
                        runs the loop WITHOUT --execute; enabling it is a
                        drop-in (mudhorn-bot-execute.conf), never an edit to
                        the unit, which bootstrap.sh would overwrite. src/ and
                        config/ stay root-owned so the service account cannot
                        edit its own limits.
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
.venv/bin/python -m pytest              # full suite (733 tests)
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

## Deferred, and noted so the shape is not lost

- **A settings agent.** "Open settings agent": a deliberately conservative,
  strict, stubborn character, the only route to changing `config/rules.yaml`
  from the interface. Asymmetric on purpose — it makes the operator argue for a
  limit getting looser and encourages one getting tighter. It does NOT have to
  run on Hermes. It needs read access to the settings and a written file
  covering each limit: what it is, why it sits there, and the goal it serves.
  Settings has no edit control today and `tests/test_web.py` enforces that, so
  this is a deliberate change to that rule rather than an addition beside it.
- **Multi-agent dreaming.** Several dreamers working a topic independently and
  then debating it out before a verdict. `Thought.by` already carries the
  attribution that needs.
- **Vercel AI Gateway.** `https://ai-gateway.vercel.sh` speaks the Anthropic
  Messages API, so it is a base-URL swap rather than a rewrite. The Vercel AI
  SDK itself is TypeScript only and does not apply to this Python codebase.

## What is deliberately not here

- Live trading. Paper only.
- A trading strategy. The foundation is broker + safety + interface; the strategy
  is the operator's to build and is the genuinely hard part.
- Option *trading*. Greeks, spreads and assignment are deferred; only expiry
  safety exists.
- A backtesting harness and a dashboard. Both sketched in `docs/HANDOFF.md`.
