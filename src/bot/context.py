"""Format current market state into a single text blob for Claude.

Rendering, and one piece of arithmetic that had to move in here. Every risk cap
reaches the model as a PERCENTAGE, in the cached system prompt, and every input
reaches it in DOLLARS, in this document — so naming a quantity meant multiplying
three caps by equity, subtracting the open risk from one of them, dividing by
the stop distance and taking the minimum, across two documents. `sizing_ceilings`
does those steps in Python and renders the answers. See the block comment above
it for what that cost before it existed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from .broker import Broker
from .config import InstrumentRules, Rules
from .dreaming import Verification
from .grants import GrantBriefing
from .indicators import Indicators, compute
from .indicators import render as render_indicators
from .intraday import IntradayView
from .intraday import compute as compute_intraday
from .intraday import render as render_intraday
from .market_clock import BrokerClock, render_sessions
from .models import (
    AccountSnapshot,
    OrderProposal,
    RiskVerdict,
    SymbolAssessment,
    Tick,
    TradingActivity,
)
from .options import ExpiryAlert, render_alerts
from .risk import NewsWindow
from .session_calendar import SessionCalendar

log = structlog.get_logger()


def build_market_context(
    *,
    account: AccountSnapshot,
    ticks: dict[str, Tick],
    headlines: list[str],
    news_windows: list[NewsWindow],
    activity: TradingActivity | None = None,
    expiry_alerts: list[ExpiryAlert] | None = None,
    indicators: dict[str, Indicators] | None = None,
    symbols_without_history: list[str] | None = None,
    intraday: dict[str, IntradayView] | None = None,
    symbols_without_intraday: list[str] | None = None,
    previous_assessments: list[SymbolAssessment] | None = None,
    previous_at: datetime | None = None,
    previous_verdicts: list[tuple[OrderProposal, RiskVerdict]] | None = None,
    social_posts: list[str] | None = None,
    social_degraded: bool = False,
    instruments: dict[str, InstrumentRules] | None = None,
    # The whole rules file, so the caps can be multiplied out into dollars HERE
    # rather than in the model's head. Optional, and a caller that omits it gets
    # no ceilings block at all rather than a guessed one — the same rule as
    # `instruments` above, for the same reason: a limit computed from nothing
    # would be a confident statement about an account nobody described.
    #
    # It overlaps `instruments`, which is `rules.instruments` at every call
    # site. The ceilings are taken from `rules` alone rather than from the two
    # together, so there is one source for the numbers and no arrangement of the
    # arguments in which the block can describe a different account from the one
    # the gate will judge.
    rules: Rules | None = None,
    broker_clock: BrokerClock | None = None,
    calendar: SessionCalendar | None = None,
    # What an adopted dream permits beyond `allowed_symbols`, with the chain
    # that argued for it. Optional and absent on every deployment that does not
    # use dreams; see `render_grants` for why it is rendered last.
    grants: GrantBriefing | None = None,
    now: datetime | None = None,
) -> str:
    """Render a stable, parseable text blob. Goes AFTER the cached system prompt."""
    now = now or datetime.now(UTC)
    lines: list[str] = [f"Current UTC time: {now.isoformat(timespec='seconds')}", ""]

    # Deliberately first. Everything else here is an opportunity; this is the
    # only section where doing nothing has an automatic, irreversible outcome.
    urgent = [a for a in (expiry_alerts or []) if a.needs_action]
    if urgent:
        lines.append("## ⚠ OPTION EXPIRY — ACTION REQUIRED")
        lines.extend(render_alerts(urgent))
        lines.append("")
        lines.append(
            "Alpaca auto-exercises anything $0.01 in the money and liquidates "
            "un-fundable positions in the final hour. Do Not Exercise cannot be "
            "filed through the API, so closing the position is the only way to "
            "choose a different outcome."
        )
        lines.append("")

    # Before the account and the quotes, deliberately. Every figure below is a
    # reading; this says what a proposal built on those readings would actually
    # become, and out of hours the answer is "an order that rests and fills at
    # a price not shown anywhere in this document". A model that reads the
    # snapshot first and the session last has already anchored on a fill price
    # it will not get.
    #
    # Omitted entirely rather than guessed at when the caller supplied no
    # instruments: a session block computed from nothing would be a confident
    # statement about a market nobody described.
    if instruments:
        lines.extend(
            render_sessions(
                now,
                windows_by_class={
                    name: inst.windows_by_day
                    for name, inst in instruments.items()
                    if inst.enabled
                },
                broker_clock=broker_clock,
                calendar=calendar,
            )
        )
        lines.append("")

    lines.append("## Account")
    lines.append(f"- Equity: ${account.equity_usd:,.2f}")
    lines.append(f"- Cash: ${account.cash_usd:,.2f} ({account.cash_pct:.1f}% of equity)")
    lines.append(f"- Buying power: ${account.buying_power_usd:,.2f}")
    lines.append(f"- Gross exposure: ${account.gross_exposure_usd:,.2f}")
    lines.append(f"- Realised P&L today: ${account.realised_pnl_today_usd:,.2f}")
    lines.append(
        f"- Open risk (loss if every stop filled): ${account.open_risk_usd:,.2f}"
    )
    lines.append("")

    # Directly under the account figures, and the adjacency is the point. The
    # line immediately above says what is already at risk; this block is that
    # figure ALREADY SUBTRACTED from the cap it eats into. Putting a page of
    # quotes and indicators between the two is what left the subtraction to the
    # model, and the subtraction is the step it was measured skipping.
    #
    # Before the market snapshot for the same reason the session block is: a
    # ceiling read after a price is a ceiling read against a size already in
    # mind.
    if rules is not None:
        lines.extend(
            render_sizing_ceilings(
                account=account,
                rules=rules,
                # The same mapping the gate is handed, taken from the same
                # resolution — `GrantBriefing.symbols` is copied from it — so
                # a granted symbol counts towards its class's caps here exactly
                # as `RiskGate._class_symbols` will count it.
                granted_symbols=grants.symbols if grants is not None else None,
            )
        )
        lines.append("")

    if activity is not None:
        lines.append("## Recent trading activity")
        lines.append(f"- Trades today: {activity.trades_today}")
        lines.append(f"- Trades this week: {activity.trades_this_week}")
        lines.append("")

    # ## Open positions — and these are YOURS to manage.
    #
    # The stop and the risk are rendered here because the model is ASKED about
    # them. `model_client` requests a `position_plan` for every open position
    # with an action of hold, close or **tighten_stop**, and this block used to
    # carry direction, quantity, entry, current price and P&L — and no stop at
    # all. So the agent was being asked whether to tighten a level it had never
    # been shown, and whether a thesis still held without the number that
    # defines what being wrong costs.
    #
    # A position with no journalled stop says so in words. It must never render
    # as a blank that reads like "no stop", nor as a zero: the position is real,
    # the protection is unknown, and those are different facts.
    lines.append("## Open positions — these are yours to manage")
    if not account.open_positions:
        lines.append("- (none)")
    else:
        for p in account.open_positions:
            current = f"{p.current_price:,.4f}" if p.current_price else "n/a"
            stop = account.planned_stop_by_symbol.get(p.symbol)
            risk = account.open_risk_by_symbol.get(p.symbol)
            if stop is None:
                protection = (
                    "STOP UNKNOWN — no journal entry, so this position's "
                    "protection and its risk cannot be stated"
                )
            else:
                protection = f"stop {stop:,.4f}"
                if risk is not None:
                    protection += f", risking ${risk:,.2f} if it fills"
            lines.append(
                f"- {p.direction.value} {p.qty:g} {p.symbol} @ {p.entry_price:,.4f} "
                f"(now {current}, P&L ${p.unrealised_pnl_usd:,.2f}) — {protection}"
            )
        lines.append("")
        lines.append(
            "  The stop shown is the one the JOURNAL planned. What actually "
            "rests at the broker is a separate fact and may differ; say so "
            "rather than assuming they agree."
        )
    lines.append("")

    lines.append("## Market snapshot")
    if not ticks:
        lines.append("- (none)")
    else:
        for symbol, tick in sorted(ticks.items()):
            lines.append(
                f"- {symbol}: bid {tick.bid:,.4f} / ask {tick.ask:,.4f} "
                f"(spread {tick.spread:,.4f})"
            )
    lines.append("")

    # Computed in Python from daily bars, in src/bot/indicators.py, and handed
    # over as answers rather than as bars to do arithmetic on. A model asked to
    # work out a 200-day average from a list will produce a number, and nobody
    # downstream can tell a correct one from an invented one.
    lines.append("## Indicators (computed from daily bars, not estimated)")
    if not indicators:
        lines.append("- (none)")
    else:
        for symbol in sorted(indicators):
            lines.extend(render_indicators(indicators[symbol]))
    if symbols_without_history:
        lines.append(
            "- NO PRICE HISTORY AVAILABLE for: "
            + ", ".join(sorted(symbols_without_history))
            + ". Treat these as having no indicators at all. Do not estimate a "
            "moving average, an ATR or a level for them from the single quote "
            "above, and propose nothing on them."
        )
    lines.append("")

    # Separate from the daily block above, because they answer different
    # questions and conflating them would hide which one is missing. The daily
    # figures say where price sits relative to its own history; these say what
    # it just did at a level, which is the only thing that tells a break from a
    # wick. Computed in `src/bot/intraday.py`, never handed over as bars.
    lines.append("## Intraday (computed from 5-minute bars, not estimated)")
    if not intraday:
        lines.append("- (none)")
    else:
        for symbol in sorted(intraday):
            lines.extend(render_intraday(intraday[symbol]))
    if symbols_without_intraday:
        lines.append(
            "- NO INTRADAY BARS AVAILABLE for: "
            + ", ".join(sorted(symbols_without_intraday))
            + ". For these symbols you cannot tell a close through a level from "
            "a wick through it. Do not claim a break on them, and propose "
            "nothing that depends on one."
        )
    lines.append("")

    # Placed AFTER the figures, deliberately. The point of this section is that
    # the model checks a trigger it wrote earlier against numbers it has just
    # read, so the numbers have to come first. Placed before the headlines
    # because a trigger is a specific, checkable claim and a headline is not.
    #
    # Only the previous cycle, never a history. Two reasons: a running
    # transcript costs output tokens on every cycle for diminishing value, and
    # a model handed its own narrative starts defending it. One cycle back is
    # enough to answer "did the thing I said I was waiting for happen".
    lines.append("## What you said last cycle")
    if not previous_assessments:
        lines.append(
            "- (nothing on record — this is the first cycle since a restart, or "
            "the last one produced no assessments)"
        )
    else:
        # The age is stated rather than implied. Cycles are skipped when the
        # market is shut, so "last cycle" on a Monday morning is Friday
        # afternoon, and a trigger written three days ago should not be read as
        # one written fifteen minutes ago.
        if previous_at is not None:
            age = now - previous_at
            hours = age.total_seconds() / 3600
            when = (
                f"{age.total_seconds() / 60:.0f} minutes ago"
                if hours < 1
                else f"{hours:.1f} hours ago"
            )
            lines.append(f"- Recorded {when}, at {previous_at.isoformat(timespec='minutes')}.")
        for assessment in previous_assessments:
            waiting = (
                f" — you said you were waiting for: {assessment.waiting_for}"
                if assessment.waiting_for
                else ""
            )
            lines.append(f"- {assessment.symbol}: {assessment.stance.value}{waiting}")
        lines.append(
            "For each of these, check the trigger against the figures ABOVE and "
            "say in this cycle's assessment whether it has now fired. If it has, "
            "that is the setup you named and you should act on it or say why not. "
            "If the reading that produced it no longer holds, drop it and say so "
            "— a trigger you wrote is not evidence, and restating one you can no "
            "longer justify is worse than passing."
        )
    lines.append("")

    # The gate's answer to what you proposed last time.
    #
    # Deliberately separate from the P&L question, and safe in a way that is
    # not. A rejection is a deterministic fact about a rule, not an inference
    # from a small sample: "risk 1,131.00 exceeds the per-trade cap 1,000.00"
    # is true regardless of how the trade would have gone, and it will be true
    # again next cycle for the same proposal. Feeding it back cannot overfit to
    # noise, because there is no noise in it.
    #
    # Observed live and the reason this exists: the model proposed 87 AAPL with
    # $1,131 of risk against a $1,000 cap — 13% over, not wildly wrong, which is
    # the dangerous kind — and with no feedback it would size the same way again
    # every cycle forever.
    if previous_verdicts:
        lines.append("## What the risk gate did with your last proposals")
        for proposal, verdict in previous_verdicts:
            outcome = "APPROVED" if verdict.approved else "REJECTED"
            lines.append(
                f"- {outcome}: {proposal.direction.value} {proposal.qty:g} "
                f"{proposal.symbol} @ {proposal.limit_price:,.4f}, "
                f"stop {proposal.stop_loss_price:,.4f}"
            )
            for reason in verdict.reasons:
                lines.append(f"    - {reason}")
            # **How wide that stop was, in the instrument's own daily range.**
            # Deterministic, outcome-free and true regardless of how the trade
            # would have gone, which is what makes it safe to feed back — the
            # same test the gate's verdicts pass and the P&L history fails.
            # Nothing here refuses anything: `RiskGate` holds no opinion on
            # placement and neither does this line. See `StopWidth`.
            lines.append(f"    - {stop_width(proposal, indicators or {}).render()}")
        lines.append(
            "The gate is deterministic code, not a reader you can persuade. A "
            "rejection above will happen again, identically, for the same "
            "proposal. If you still want the trade, fix the specific defect "
            "named — size down to fit the cap, move the stop, wait for the "
            "session — and do not re-send it unchanged or argue with the reason."
        )
        # The ATR is THIS cycle's, not the one that was on screen when the
        # proposal was made. Stated rather than implied, for the same reason the
        # age of a carried-over watch is: a figure whose vintage is unstated is
        # read as current, and the cycle gap can be a whole weekend.
        lines.append(
            "The ATR each stop is measured against is the one in THIS cycle's "
            "Indicators section, not the reading you had when you proposed it. "
            "No gate refuses a stop for being tight — where it goes is yours — "
            "so the multiple is a fact about the plan and never a rejection."
        )
        lines.append("")

    # Ahead of the headlines, deliberately. These accounts move a price before
    # the wire story exists, so by the time a headline carries it the gap has
    # already opened. Reading them second would invert that.
    lines.append("## Posts from watched accounts (context only, gates nothing)")
    if social_degraded:
        lines.append(
            "- FEED DEGRADED: the last fetch failed, so this list is incomplete. "
            "An empty list here does NOT mean nothing was posted."
        )
    if not social_posts:
        lines.append("- (none in the lookback window)")
    else:
        for post in social_posts:
            lines.append(f"- {post}")
    lines.append("")

    lines.append("## Recent headlines")
    if not headlines:
        lines.append("- (none)")
    else:
        for h in headlines:
            lines.append(f"- {h}")
    lines.append("")

    lines.append("## Upcoming news windows (<= 60 min)")
    if not news_windows:
        lines.append("- (none)")
    else:
        for w in news_windows:
            lines.append(
                f"- {w.timestamp.isoformat(timespec='minutes')} "
                f"affects: {', '.join(sorted(w.affected_symbols))}"
            )

    if grants is not None and grants.has_grants:
        lines.append("")
        lines.extend(render_grants(grants, now=now))

    return "\n".join(lines)


# ---------------------------------------------------------- sizing ceilings
#
# **The model sizes over the cap, and it is an arithmetic-delegation bug rather
# than a prompting one.** Measured live twice, and the gate caught both.
#
# Every cap reached the model as a PERCENTAGE, in the cached system prompt —
# `Max risk per trade: 1.00% of equity`, `Max COMBINED risk: 2.00%`, `Max single
# position value: 50%` — and every input reached it in DOLLARS, in this
# document. So before naming a quantity it had to, across two documents,
# multiply three caps by equity, SUBTRACT the current open risk from the
# combined budget, divide by the stop distance and take the minimum. It skipped
# the subtraction. Recovered from the gate's own rejection text on 12 Aug 2026:
# the per-trade cap allowed 180 shares, the concentration cap 163, and the
# remaining combined-risk budget — the only one needing a subtraction — 91. It
# proposed 185. The earlier instance is the same shape: 87 AAPL, 13% over the
# per-trade cap.
#
# The prompt already said the combined cap "is the binding constraint most of
# the time — size each trade so the total stays under it", explicitly, and the
# subtraction still did not happen. More instruction was not the fix.
#
# **This is `indicators.py`'s founding rule broken in the one place that
# directly produces an order quantity.** SMA and ATR are computed in Python
# precisely so the model never derives a figure it would then state
# confidently — the model reads figures, it does not derive them. Position
# sizing was the exception, so the three error-prone operations move in here
# where they are testable, and five steps become one division.
#
# Two things this deliberately does NOT do, both decided by the operator:
#
# - **No worked maximum quantity.** It cannot be precomputed, because it depends
#   on the stop and the stop is the agent's choice — and a worked example at the
#   current price would read as a recommendation to trade at that size.
# - **Zero or negative headroom renders in WORDS.** Never `$0.00`, which reads
#   as "cheap, size small" rather than "the portfolio budget is spent". Same
#   shape as the `STOP UNKNOWN` treatment beside an open position, with money
#   attached.

# The two units, which do not convert into one another without a stop distance.
RISK_UNIT = "risk"
VALUE_UNIT = "position value"

# The words that stand in for a figure, as module constants rather than inline
# strings: three surfaces already key off `STOP UNKNOWN`-style tokens, and one
# spelling is what keeps a test and a renderer talking about the same state.
BUDGET_SPENT = "BUDGET SPENT"
HEADROOM_UNKNOWN = "HEADROOM UNKNOWN"
# Deliberately NOT one of the other two. "Spent" and "unknown" are both answers
# about a budget; this is the account being shut, which is a different sentence
# and must not be read as a very small budget or as a figure nobody could
# establish.
ACCOUNT_HALTED = "ACCOUNT HALTED"

# **The words start above the rounding boundary, not at zero.** Every figure in
# this document prints at two decimal places, so a headroom of $0.004 is a
# positive number that renders as `$0.00` — the exact string the operator ruled
# out, arriving through the formatter rather than through the arithmetic. Half a
# cent is also below any tradeable size, so nothing real is lost by calling it
# spent.
NEGLIGIBLE_USD = 0.005


@dataclass(frozen=True)
class Ceiling:
    """One limit `RiskGate` applies to size, already multiplied out into dollars.

    `headroom_usd` is what is LEFT rather than what the limit is: a 2% cap on a
    $100,000 account with $1,486.95 already at risk is a ceiling of $513.05, and
    that subtraction is the whole point of this type existing.

    Three states, kept apart on purpose:

    - **A figure**, when there is room and it is larger than the rounding.
    - **Spent**, when the headroom is zero, negative, or small enough to print
      as `$0.00`. Rendered in WORDS. "Nothing fits at any size" and "size small"
      are opposite instructions and a zero reads as the second one.
    - **Unknown**, when the figure cannot be established at all. A held position
      with no journal row has an unknowable planned stop, so its class's open
      risk cannot be computed and `RiskGate._class_total_risk` REFUSES rather
      than counting the unknown as zero. Rendering a number there would invent
      the one input the gate declines to invent.

    `scope` is the instrument-class key whose limits produced it, or `""` for a
    portfolio-wide limit that applies to every class.
    """

    scope: str
    label: str
    unit: str
    headroom_usd: float | None
    basis: str = ""
    spent_note: str = ""
    unknown_note: str = ""
    # An established figure that is nonetheless not the whole truth. Rendered on
    # its own line under the figure rather than folded into `basis`, because a
    # caveat inside parentheses at the end of a money line does not get read.
    caveat: str = ""

    def applies_to(self, class_key: str) -> bool:
        """Portfolio-wide ceilings bind every class; a class's own bind only it."""
        return self.scope in ("", class_key)

    @property
    def is_spent(self) -> bool:
        """Zero, negative, or too small to print as anything but `$0.00`."""
        return self.headroom_usd is not None and self.headroom_usd < NEGLIGIBLE_USD

    def render(self) -> list[str]:
        # **The unit is repeated on the figure itself, not left to the heading
        # above it.** Measured 13 Aug 2026: every material over-size in a
        # 160-call run was a model that did the RISK division and never checked
        # a VALUE ceiling, so a figure lifted out of its section and divided
        # into is exactly what happens. A line that carries its own unit cannot
        # be read as the other one, and the cost is four words.
        if self.headroom_usd is None:
            return [f"- {self.label}: {HEADROOM_UNKNOWN} — {self.unknown_note}"]
        if self.is_spent:
            return [f"- {self.label}: {BUDGET_SPENT} — {self.spent_note}"]
        out = [
            f"- {self.label}: ${self.headroom_usd:,.2f} of {self.unit} ({self.basis})"
        ]
        if self.caveat:
            out.append(f"    {self.caveat}")
        return out


def _symbols_counting_as(
    class_key: str,
    instrument: InstrumentRules,
    *,
    account: AccountSnapshot,
    rules: Rules,
    granted_symbols: Mapping[str, str],
) -> set[str]:
    """Every symbol that counts as this class: listed, granted, or HELD.

    **A deliberate mirror of `RiskGate._class_symbols`, and the duplication is
    the hazard worth naming.** The gate cannot lend its version out: it is a
    private method taking a `ResolvedClass` built inside `evaluate`, and
    `risk.py` must not grow a public API for a renderer. So the membership rule
    lives in two places, and the thing that keeps them in step is not this
    comment — it is
    `tests/test_context.py::test_the_rendered_class_headroom_is_where_the_gate_flips`,
    which drives the real `RiskGate` at the rendered figure and one cent past
    it. If that test goes, this function is free to drift and the block starts
    quoting a cap nobody enforces.

    All three sources are load-bearing for the same reason they are in the gate:

    - **Listed** is the class as `config/rules.yaml` defines it.
    - **Granted** is what an adopted dream currently adds. A grant buys entry to
      the allowlist and must not buy an exemption from the caps that entry falls
      under.
    - **Held** covers a position opened under a grant that has since lapsed.
      Membership computed from live grants alone let handing a dream back move
      real class risk out of a class cap with nothing closed.

    Pure: `true_class_key` walks lists already in memory.
    """
    symbols = set(instrument.allowed_symbols)
    symbols |= {s for s, key in granted_symbols.items() if key == class_key}
    symbols |= {
        p.symbol
        for p in account.open_positions
        if p.symbol not in symbols and rules.true_class_key(p.symbol) == class_key
    }
    return symbols


def sizing_ceilings(
    *,
    account: AccountSnapshot,
    rules: Rules,
    granted_symbols: Mapping[str, str] | None = None,
) -> list[Ceiling]:
    """Every ceiling the gate applies to SIZE, in dollars, with the subtractions done.

    **One rule governs this function: each figure must be exactly the point at
    which its gate flips from approving to rejecting.** A rendered ceiling that
    disagrees with `risk.py` is not a convenience, it is a new way to mislead
    the model — worse than the arithmetic it replaces, because it looks
    measured. Every branch below names the method it mirrors, and the
    comparison in each case is `>` rather than `>=`, so the headroom is
    inclusive: a proposal risking exactly the figure shown is approved.

    Seven ceilings, in two units that do not convert into one another without a
    stop distance:

    RISK — `|entry - stop| x qty`
      `_total_risk`        portfolio-wide, minus what is already at risk
      `_per_trade_risk`    per class, a flat fraction of equity
      `_class_total_risk`  per class where configured, minus that class's risk

    POSITION VALUE — `limit_price x qty`
      `_gross_notional`          portfolio-wide, minus what is already held
      `_buying_power`            a fraction of buying power, per order
      `_position_size`           per class, a flat fraction of equity
      `_instrument_capital_cap`  per class where configured, minus that class

    Not every gate is here, and the renderer says so out loud. The session
    window, the news blackout, the cooldown, the trade counters, the
    concurrent-position count and the stand-down all refuse trades that fit
    every figure above, and none of them is a ceiling on size.

    Returns an empty list on zero or negative equity, which is not a ceiling of
    zero — it is a set of percentages that cannot be turned into money at all.
    The renderer answers that in words.
    """
    equity = account.equity_usd
    if equity <= 0:
        return []

    grants = granted_symbols or {}
    out: list[Ceiling] = []

    # --- `_total_risk`. The one that needs a subtraction, and the one that was
    # skipped. Portfolio-wide and usually the binding constraint, exactly as the
    # system prompt already says in percentage terms.
    total_pct = rules.account.max_total_risk_pct
    total_cap = equity * total_pct / 100
    caveat = ""
    # "already at risk" claims the subtracted figure is the WHOLE of what is at
    # risk. With a position the journal has never seen it is not, and on that
    # account the figure is frequently $0.00 — so the basis would assert that
    # nothing is at risk, one line above the caveat correcting it. The phrasing
    # narrows to what the journal can account for, which is all the subtraction
    # ever measured.
    subtracted = f"${account.open_risk_usd:,.2f} already at risk"
    if account.symbols_with_unknown_risk:
        subtracted = f"${account.open_risk_usd:,.2f} the journal can account for"
        # **The figure is still rendered, and it is still the gate's own.**
        # `_total_risk` does not refuse on an unknown — it computes with the
        # understated total — so a ceiling that went `HEADROOM UNKNOWN` here
        # would disagree with the gate, which is the one thing this function may
        # not do. What is added is the direction of the error, in the same
        # words `reconcile.risk_is_understated` uses.
        caveat = (
            "OVERSTATED: "
            + ", ".join(sorted(account.symbols_with_unknown_risk))
            + " held with no journal row, so the planned risk there is MISSING "
            "rather than zero. This is the figure the gate will use; the real "
            "headroom is smaller by an unknown amount."
        )
    out.append(
        Ceiling(
            scope="",
            label="Combined risk left across ALL open positions",
            unit=RISK_UNIT,
            headroom_usd=total_cap - account.open_risk_usd,
            basis=f"${total_cap:,.2f} at {total_pct:.2f}% of equity, less {subtracted}",
            spent_note=(
                f"the {total_pct:.2f}% portfolio cap is ${total_cap:,.2f} and "
                f"${account.open_risk_usd:,.2f} is already at risk across open "
                f"positions. No new position fits at ANY size. Close one, or "
                f"tighten a stop on one, before proposing another."
            ),
            caveat=caveat,
        )
    )

    # --- `_gross_notional`. Portfolio-wide, in the other unit.
    gross_pct = rules.margin.max_gross_notional_pct
    gross_cap = equity * gross_pct / 100
    held_notional = account.gross_exposure_usd
    out.append(
        Ceiling(
            scope="",
            label="Gross exposure left across ALL open positions",
            unit=VALUE_UNIT,
            headroom_usd=gross_cap - held_notional,
            basis=(
                f"${gross_cap:,.2f} at {gross_pct:.0f}% of equity, less "
                f"${held_notional:,.2f} already held"
            ),
            spent_note=(
                f"the {gross_pct:.0f}% gross-exposure cap is ${gross_cap:,.2f} and "
                f"${held_notional:,.2f} is already held. Nothing can be added "
                f"until something is closed."
            ),
        )
    )

    # --- `_buying_power`. Per ORDER rather than cumulative, so nothing is
    # subtracted: it is a share of what the broker says is available right now.
    # Zero or negative buying power falls out as spent, which is the same answer
    # the gate gives in words ("no buying power available").
    bp_pct = rules.margin.max_buying_power_utilisation_pct
    out.append(
        Ceiling(
            scope="",
            label="Most THIS ONE order may be worth against buying power",
            unit=VALUE_UNIT,
            headroom_usd=account.buying_power_usd * bp_pct / 100,
            basis=(
                f"{bp_pct:.0f}% of the ${account.buying_power_usd:,.2f} available"
            ),
            spent_note=(
                f"buying power reads ${account.buying_power_usd:,.2f}, so no order "
                f"can be funded at all."
            ),
        )
    )

    for class_key, instrument in sorted(rules.enabled_instruments.items()):
        symbols = _symbols_counting_as(
            class_key,
            instrument,
            account=account,
            rules=rules,
            granted_symbols=grants,
        )
        held_symbols = [p.symbol for p in account.open_positions if p.symbol in symbols]

        # --- `_per_trade_risk`. A class value OVERRIDES the account default in
        # either direction — `account:` is the default, not a ceiling — and the
        # override is named when it differs, so a looser class limit reads as a
        # deliberate setting rather than as a figure somebody got wrong.
        trade_default = rules.account.max_risk_per_trade_pct
        # `is not None`, never `or`: the gate tests for None and a falsy-but-set
        # value would take a different branch here than in `_per_trade_risk`.
        trade_pct = (
            trade_default
            if instrument.max_risk_per_trade_pct is None
            else instrument.max_risk_per_trade_pct
        )
        out.append(
            Ceiling(
                scope=class_key,
                label=f"Most ONE {class_key} trade may risk",
                unit=RISK_UNIT,
                headroom_usd=equity * trade_pct / 100,
                basis=f"{trade_pct:.2f}% of equity"
                + _override_note(class_key, trade_pct, trade_default, 2),
            )
        )

        # --- `_position_size`. The concentration backstop, deliberately
        # generous and rarely the limit that binds.
        size_default = rules.account.max_position_pct
        size_pct = (
            size_default
            if instrument.max_position_pct is None
            else instrument.max_position_pct
        )
        out.append(
            Ceiling(
                scope=class_key,
                label=f"Most ONE {class_key} position may be worth",
                unit=VALUE_UNIT,
                headroom_usd=equity * size_pct / 100,
                basis=f"{size_pct:.1f}% of equity"
                + _override_note(class_key, size_pct, size_default, 1),
            )
        )

        # --- `_class_total_risk`. Optional, and the only gate in the repository
        # that fails CLOSED on missing data, so the unknown branch has to say
        # the gate will refuse rather than that the number is merely absent.
        class_pct = instrument.max_class_total_risk_pct
        if class_pct is not None:
            unknown = sorted(set(held_symbols) & set(account.symbols_with_unknown_risk))
            class_cap = equity * class_pct / 100
            if unknown:
                out.append(
                    Ceiling(
                        scope=class_key,
                        label=f"Combined risk left inside {class_key}",
                        unit=RISK_UNIT,
                        headroom_usd=None,
                        unknown_note=(
                            f"{', '.join(unknown)} held with no journal row, so "
                            f"this class's open risk cannot be established and the "
                            f"{class_pct:.2f}% class cap cannot be enforced. The "
                            f"gate REJECTS every new {class_key} position until "
                            f"that is closed or journalled — an unknown is not "
                            f"counted as zero."
                        ),
                    )
                )
            else:
                held_risk = sum(
                    account.open_risk_by_symbol.get(s, 0.0) for s in held_symbols
                )
                out.append(
                    Ceiling(
                        scope=class_key,
                        label=f"Combined risk left inside {class_key}",
                        unit=RISK_UNIT,
                        headroom_usd=class_cap - held_risk,
                        basis=(
                            f"${class_cap:,.2f} at {class_pct:.2f}% of equity, less "
                            f"${held_risk:,.2f} already at risk in this class"
                        ),
                        spent_note=(
                            f"the {class_pct:.2f}% {class_key} cap is "
                            f"${class_cap:,.2f} and ${held_risk:,.2f} is already at "
                            f"risk in this class. The gate will not size a new trade "
                            f"down to fit and unrealised profit does not offset open "
                            f"risk: close an existing {class_key} position first."
                        ),
                    )
                )

        # --- `_instrument_capital_cap`. Optional ceiling on one class's share
        # of the portfolio, in position value.
        capital_pct = instrument.capital_cap_pct
        if capital_pct is not None:
            held_class_value = sum(
                p.notional_usd for p in account.open_positions if p.symbol in symbols
            )
            capital_cap = equity * capital_pct / 100
            out.append(
                Ceiling(
                    scope=class_key,
                    label=f"Capital left for {class_key} as a whole",
                    unit=VALUE_UNIT,
                    headroom_usd=capital_cap - held_class_value,
                    basis=(
                        f"${capital_cap:,.2f} at {capital_pct:.1f}% of equity, less "
                        f"${held_class_value:,.2f} already held in this class"
                    ),
                    spent_note=(
                        f"the {capital_pct:.1f}% {class_key} allocation cap is "
                        f"${capital_cap:,.2f} and ${held_class_value:,.2f} is already "
                        f"held in this class. Close one before opening another."
                    ),
                )
            )

    return out


def _override_note(class_key: str, applied: float, default: float, places: int) -> str:
    """Name a class limit that differs from the account default, in which direction.

    A class value overrides the portfolio one in EITHER direction and is
    deliberately not floored back with a `min`, so a class figure looser than
    the account default is a real setting rather than a mistake. Saying which it
    is stops the looser case reading as an error and the tighter case reading as
    the account-wide rule.

    `places` matches the precision the same limit is written at in the gate's
    own rejection messages, so a figure here and the same figure in a rejection
    are recognisably the one number.
    """
    if applied == default:
        return ""
    direction = "looser" if applied > default else "tighter"
    return (
        f" — {class_key}'s own limit, {direction} than the "
        f"{default:.{places}f}% default"
    )


def _tightest_line(ceilings: list[Ceiling], class_key: str, unit: str) -> str:
    """The smallest applicable ceiling, or the words that replace it.

    Computed PER CLASS rather than once, because a class's own limits bind only
    that class: with two enabled classes, one that cannot be sized at all says
    nothing about the other, and a single global "tightest" would report the
    stricter class's refusal as the account's.

    Precedence is unknown, then spent, then the minimum — in that order, and it
    matters. An unknown could be smaller than anything established, so taking a
    minimum over what happens to be known would answer a question nobody asked
    and answer it optimistically.

    **A caveat on ANY applicable ceiling qualifies this line, including when
    some other ceiling is the one that won.** This is a claim about the whole
    set — the smallest of them — so a member that is overstated can be
    genuinely smaller than the figure printed here, and the reader has no way to
    tell from this line which member was chosen. Carried on the line itself
    rather than left to the note four lines above it: this is the summary, it is
    the line a size gets divided out of, and a qualification a reader has to
    scroll for is a qualification that only reaches the people who did not need
    it.

    The unknown and spent branches need no such qualification and deliberately
    return before it. Both are already stronger statements than "this may be
    lower" — nothing fits at any size, and nothing can be established at all —
    so adding a hedge to either would soften the two answers that must not be
    softened.
    """
    applicable = [c for c in ceilings if c.unit == unit and c.applies_to(class_key)]
    if not applicable:
        return ""

    unknown = [c for c in applicable if c.headroom_usd is None]
    if unknown:
        return (
            f"- Tightest {unit} ceiling for a {class_key} trade: "
            f"{HEADROOM_UNKNOWN} — {unknown[0].label.lower()} cannot be "
            f"established, and the gate refuses rather than reading an unknown "
            f"as room. Nothing can be sized in this class until it is resolved."
        )

    spent = [c for c in applicable if c.is_spent]
    if spent:
        names = "; ".join(c.label.lower() for c in spent)
        return (
            f"- Tightest {unit} ceiling for a {class_key} trade: {BUDGET_SPENT} "
            f"— {names}. No {class_key} position fits at any size."
        )

    known = [(c.headroom_usd, c.label) for c in applicable if c.headroom_usd is not None]
    smallest, label = min(known, key=lambda pair: pair[0])
    line = (
        f"- Tightest {unit} ceiling for a {class_key} trade: ${smallest:,.2f} "
        f"of {unit} — {label.lower()}."
    )

    overstated = [c for c in applicable if c.caveat]
    if overstated:
        # The symbols are named once, on the qualified ceiling's own line, so
        # there is one place the set of unjournalled positions is written down.
        # What is repeated here is the DIRECTION of the error, which is the part
        # that changes what a reader does with this number.
        names = "; ".join(c.label.lower() for c in overstated)
        line += (
            f" UPPER BOUND, not a measurement: {names} is OVERSTATED (see the "
            f"note above it), so the true tightest figure may be lower than "
            f"this. The gate will use the same overstated figure and will not "
            f"refuse you for it — the correct response to an unknown is a "
            f"smaller size or no trade, never a larger one."
        )
    return line


# ------------------------------------------------- which of the two units binds
#
# **Item 21 moved the error rather than removing it, and this is where it moved
# to.** Five arithmetic steps became one division, and the models now do that
# division reliably and stop. Measured 13 Aug 2026 over 160 live calls through
# the real `ModelClient`, the real `build_market_context` and the real
# `RiskGate`: every material over-size in the run was a model that divided the
# RISK ceiling by its stop and never looked at a POSITION VALUE ceiling.
# `deepseek-v4-pro` — five proposals at 7.06-7.27x buying power, three at
# 2.06-2.13x gross exposure, one at 14.39x. `nemotron-3-ultra-550b`, the model
# live in production, missed it once at 1.51x.
#
# **The obvious repair is the one that has already failed.** Printing a second
# worked figure and asking for the minimum of two is exactly the delegation that
# broke one level up: the block ALREADY printed both units, and already said
# "then check what that quantity is worth against the tightest POSITION VALUE
# ceiling and take the smaller". That sentence was in the document for every one
# of those over-sized proposals.
#
# (The one-share round-up is a different and much smaller defect and must not be
# counted with these — seven of deepseek's sixteen over-sizes are 1.00-1.01x,
# 145 where 144.3 was permitted. The gate refuses them correctly and the remedy
# is the sentence telling the model to round DOWN.)
#
# Worse, "take the smaller" is not even well posed. The two figures are in
# different units, so the smaller NUMBER is not the tighter constraint — a $500
# risk ceiling and a $50,000 value ceiling say nothing about each other until a
# stop distance exists to convert between them. A model asked to compare them as
# numbers will pick the risk ceiling every time, which is what it was doing
# anyway.
#
# **So the two are made comparable instead, and the conversion factor is a
# number this module can compute.** With a risk ceiling R, a value ceiling V, an
# entry price p and a stop distance d:
#
#     risk binds when   q <= R/d        value binds when   q <= V/p
#     V/p < R/d   <=>   d/p < R/V
#
# `R/V` is dimensionless. It needs no price and no stop, so it can be computed
# here, and it says the whole thing in one number: **below a stop this fraction
# of entry, the POSITION VALUE ceiling is the binding one.** The model then does
# ONE comparison against its own stop and ONE division — the same shape as the
# fix that worked, rather than a second figure beside the first.
#
# Two things that shape the wording and are not decoration:
#
# - **It is not a minimum stop distance and must never read as one.** The gate
#   holds no opinion on where a stop goes and neither does this file; a
#   crossover phrased as "your stop must be at least X%" is that opinion
#   arriving through the renderer. It is stated as a property of the ARITHMETIC,
#   with the disclaimer on the same line, and `tests/test_context.py` pins it.
# - **The value branch is the ordinary case, not the exception.** On the shipped
#   rules the crossover for an equity is around 2% of entry, and one ATR on a
#   $500 name is well under that. `CLAUDE.md` has said the consequence out loud
#   for a year — "a 1%-risk trade with a 2% stop is a position worth about 48%
#   of equity" — and the block presented the ceiling that binds most often as
#   the afterthought check.


def _tightest_figure(
    ceilings: list[Ceiling], class_key: str, unit: str
) -> float | None:
    """The smallest applicable ceiling in one unit, as a number, or `None`.

    `None` covers three states deliberately — no applicable ceiling, one that
    cannot be established, and one that is spent — because the only caller,
    `crossover_stop_pct`, must refuse to answer in all three. `_tightest_line`
    is what tells them apart, in words, and it is the right place for that: this
    is arithmetic and arithmetic has nothing to say about a budget that is gone.

    A spent ceiling is excluded rather than used at its (tiny, possibly
    negative) figure. Dividing by it would produce a crossover, and a crossover
    computed from a budget that is spent is a plausible wrong figure standing
    where "nothing fits at any size" belongs.
    """
    applicable = [c for c in ceilings if c.unit == unit and c.applies_to(class_key)]
    if not applicable:
        return None
    if any(c.headroom_usd is None or c.is_spent for c in applicable):
        return None
    figures = [c.headroom_usd for c in applicable if c.headroom_usd is not None]
    return min(figures) if figures else None


def crossover_stop_pct(ceilings: list[Ceiling], class_key: str) -> float | None:
    """Stop distance as a percentage of entry price, where the two units cross.

    Below this the POSITION VALUE ceiling binds; at or above it the RISK ceiling
    does. Derived in the block comment above; it is `tightest risk / tightest
    value`, which is dimensionless and therefore needs neither a price nor a
    stop.

    `None` means there is no crossover to state, which is never the same as
    "there is no constraint": it is either that one of the two units has no
    figure at all — spent, unknown, absent — or that the value ceiling is
    non-positive. Each of those is already a stronger statement than a
    crossover, and the renderer says which.
    """
    risk = _tightest_figure(ceilings, class_key, RISK_UNIT)
    value = _tightest_figure(ceilings, class_key, VALUE_UNIT)
    if risk is None or value is None or value <= 0:
        return None
    return risk / value * 100


def _no_crossover_note(ceilings: list[Ceiling], class_key: str) -> str:
    """Why a class has no crossover, in the same words the two units already use.

    Named rather than left blank, and named per UNIT. "No crossover" on its own
    invites the reading that nothing constrains this class, which is the exact
    inversion of the truth in the case that produces it most often — a budget
    that is spent.
    """
    reasons: list[str] = []
    for unit in (RISK_UNIT, VALUE_UNIT):
        applicable = [c for c in ceilings if c.unit == unit and c.applies_to(class_key)]
        if not applicable:
            reasons.append(f"no {unit} ceiling is in force")
        elif any(c.headroom_usd is None for c in applicable):
            reasons.append(f"the tightest {unit} ceiling is {HEADROOM_UNKNOWN}")
        elif any(c.is_spent for c in applicable):
            reasons.append(f"the tightest {unit} ceiling is {BUDGET_SPENT}")
    return "; ".join(reasons)


def _crossover_lines(ceilings: list[Ceiling], class_key: str) -> list[str]:
    """One class's answer to "which unit binds", as the two branches of a divide.

    The formulae carry the class's own figures substituted in, which is NOT the
    worked maximum quantity the operator ruled out: both sides still divide by
    something only the agent knows — its stop distance on one branch, its limit
    price on the other — so neither resolves to a share count here. What is
    removed is the step that was measured being skipped, not the step that is
    the agent's to take.
    """
    risk = _tightest_figure(ceilings, class_key, RISK_UNIT)
    value = _tightest_figure(ceilings, class_key, VALUE_UNIT)
    crossover = crossover_stop_pct(ceilings, class_key)
    if risk is None or value is None or crossover is None:
        note = _no_crossover_note(ceilings, class_key)
        return [
            f"- {class_key}: NO CROSSOVER TO STATE — {note}. That is a stronger "
            f"answer than a crossover, not a weaker one: no stop distance makes "
            f"a {class_key} position fit, so there is nothing to divide."
        ]

    lines = [
        f"- {class_key}: the two ceilings cross at a stop {crossover:.3f}% of "
        f"your entry price.",
        f"    stop that far from entry or WIDER — RISK binds: "
        f"qty = ${risk:,.2f} / |limit_price - stop_loss_price|",
        f"    stop TIGHTER than that — POSITION VALUE binds: "
        f"qty = ${value:,.2f} / limit_price",
    ]
    if crossover >= 100:
        # A long's stop is above zero, so its distance is always under 100% of
        # entry. Stating it rather than leaving the reader to notice: a
        # crossover it is arithmetically impossible to reach reads as an
        # unreachable requirement unless somebody says which branch that leaves.
        lines.append(
            "    That crossover is at or above 100% of entry, which a long's "
            "stop cannot reach — so POSITION VALUE binds on every long here."
        )
    # **A caveat on either side travels into the crossover, and the two sides
    # travel in OPPOSITE directions.** `R/V` is overstated when R is and
    # understated when V is, so a single sentence covering both would be wrong
    # half the time — which is why the unit is tested rather than the set. Today
    # only the portfolio risk ceiling carries a caveat, so the value branch is
    # unreachable; it is written because the branch that cannot be reached is
    # the one nobody checks when a second caveat is added.
    caveated = {
        c.unit for c in ceilings if c.caveat and c.applies_to(class_key)
    }
    if RISK_UNIT in caveated:
        # The safe direction: an overstated R makes the crossover too HIGH,
        # which sends more stops into the POSITION VALUE branch and therefore to
        # a smaller size. Said out loud anyway — a figure that is an upper bound
        # must not print as a measurement whichever way the error runs.
        lines.append(
            "    UPPER BOUND: the RISK ceiling above it is overstated (see its "
            "own note), so the true crossover is lower. That errs toward the "
            "POSITION VALUE branch, which is the smaller size and the safe "
            "direction."
        )
    if VALUE_UNIT in caveated:
        lines.append(
            "    LOWER BOUND: the POSITION VALUE ceiling above it is overstated "
            "(see its own note), so the true crossover is HIGHER. That errs "
            "toward the RISK branch, which is the larger size — treat this "
            "crossover as a floor and size below what it selects."
        )
    return lines


def render_sizing_ceilings(
    *,
    account: AccountSnapshot,
    rules: Rules,
    granted_symbols: Mapping[str, str] | None = None,
) -> list[str]:
    """The caps in dollars, so sizing is one division rather than five steps.

    **No maximum quantity appears here and none should be added.** It depends on
    the stop, which is the agent's own choice — the gate deliberately holds no
    opinion on where a stop goes, only on what it costs — and a worked example
    at the current price would read as a recommendation to trade at that size.
    What is supplied is the numerator; the agent supplies the divisor.

    The block states its own limits at the end rather than implying
    completeness. These are ceilings on SIZE, and half the gates in `risk.py`
    are not about size at all.

    **One of those non-size gates is stated here rather than only in that list,
    because it is HALTING and it is visible from this snapshot.**
    `RiskGate._equity_floor` refuses every proposal once equity drops below
    `min_equity_floor_usd`, and audit drove it: at $89,999 against the shipped
    $90,000 floor this block rendered a $899.99 risk budget and a $44,999.50
    value budget, every figure correct as a size limit, while the gate refused a
    proposal sized at exactly those. The floor appears nowhere in the cached
    system prompt either, so an agent in that state had no way whatever to know
    the account was halted — a budget quoted to an account that may open
    nothing is the confident partial answer this repository exists to refuse,
    arriving through the furniture.

    The other halting gate, the daily-loss kill switch, is deliberately NOT
    stated as a fact: it is state held inside `RiskGate` and is not on the
    snapshot, so this could only guess at it. It is named in the closing list
    instead, which is the honest half.
    """
    out = ["## Sizing ceilings, in dollars (computed — do not re-derive them)"]

    if account.equity_usd <= 0:
        # Not a ceiling of zero. A percentage of nothing is not a small budget,
        # it is a budget that cannot be stated, and the two would be read
        # completely differently.
        out.append(
            f"- {HEADROOM_UNKNOWN} — equity reads ${account.equity_usd:,.2f}, so "
            "none of the percentage limits above can be turned into money. "
            "Propose nothing until the account reads back."
        )
        return out

    # Ahead of every figure it qualifies, for the same reason a stale reading is
    # announced above the banners that describe it: each line below is a
    # statement about what may be opened, and all of them are moot.
    floor = rules.account.min_equity_floor_usd
    if account.equity_usd < floor:
        out.append(
            f"- {ACCOUNT_HALTED} — equity ${account.equity_usd:,.2f} is below the "
            f"${floor:,.2f} floor in config/rules.yaml, so the gate REFUSES every "
            f"new position whatever it is sized at. The figures below are still "
            f"the size limits and they are not permission: nothing opens until "
            f"equity is back above the floor. Closing a position and moving a "
            f"stop are never gated and are unaffected."
        )

    ceilings = sizing_ceilings(
        account=account, rules=rules, granted_symbols=granted_symbols
    )
    out.append(
        "Every limit in the system prompt is a PERCENTAGE and every figure in "
        "this document is in DOLLARS. These are those limits already multiplied "
        "out against this account, with what is already at risk ALREADY "
        "SUBTRACTED. Read them; do not recompute them from the percentages."
    )
    out.append("")
    out.append("Two units, and neither converts into the other without your stop:")
    out.append(
        "- RISK is |limit_price - stop_loss_price| x qty — what the trade loses "
        "if the stop fills."
    )
    out.append("- POSITION VALUE is limit_price x qty — what it costs to hold.")
    out.append("")
    out.append(
        "No maximum quantity is given here, deliberately: it depends on where "
        "you put the stop, and the stop is yours. What is given instead is the "
        "stop distance at which the two units CROSS, in the last section below "
        "— one comparison against your own stop, then one division. A figure "
        "below is a ceiling, not a target."
    )
    out.append(
        "The two figures cannot be compared as numbers. A $500 risk ceiling and "
        "a $50,000 value ceiling say nothing about each other until your stop "
        "exists to convert between them, so the smaller NUMBER is not the "
        "tighter constraint and 'take the smaller of the two' is not a rule "
        "that can be followed."
    )
    out.append(
        "Do not move the stop to make a size fit. A tighter stop buys more "
        "shares and a wider one buys fewer; the stop belongs where the thesis "
        "is wrong, and a stop chosen to justify a quantity is a quantity with "
        "no stop behind it."
    )

    classes = sorted(rules.enabled_instruments)
    for unit, heading in (
        (RISK_UNIT, "### RISK ceilings — |entry - stop| x qty"),
        (VALUE_UNIT, "### POSITION VALUE ceilings — limit price x qty"),
    ):
        out.append("")
        out.append(heading)
        # Portfolio-wide first, then each class. A stable sort, so the order
        # within each group is the order `sizing_ceilings` built them in.
        for ceiling in sorted(
            (c for c in ceilings if c.unit == unit), key=lambda c: c.scope != ""
        ):
            out.extend(ceiling.render())
        for class_key in classes:
            line = _tightest_line(ceilings, class_key, unit)
            if line:
                out.append(line)

    # **The section that closes the cross-unit gap, and it goes LAST on
    # purpose.** It is the only thing here a quantity is arrived at through, so
    # it sits after every figure it selects between rather than above them —
    # the same reason the sizing block as a whole sits under the account
    # figures. A recipe printed before its inputs is a recipe read from memory.
    if classes:
        out.append("")
        out.append("### Which ceiling binds — ONE comparison, then ONE division")
        out.append(
            "Measure your stop as a fraction of your entry — "
            "|limit_price - stop_loss_price| / limit_price — and compare it "
            "with the crossover for the class. That comparison picks ONE of the "
            "two ceilings above. Divide by the matching denominator and round "
            "DOWN."
        )
        for class_key in classes:
            out.extend(_crossover_lines(ceilings, class_key))
        out.append(
            "Round DOWN in both branches. A quantity rounded UP is refused by "
            "the gate, and it is refused for a single share."
        )
        out.append(
            "Take the branch. Do NOT do the RISK division and stop there: a "
            "stop tighter than the crossover is the ORDINARY case for an equity "
            "— one ATR on a $500 name is well under 1% of it — so POSITION "
            "VALUE is the unit that binds most of the time, and the RISK "
            "division alone then returns a quantity several times what the gate "
            "will take. That has been measured at 2x, at 7x, and once at 14x."
        )
        out.append(
            "The crossover is NOT a minimum stop distance and NOT a view on "
            "where your stop belongs. Nothing here has an opinion on placement. "
            "Widening a stop to land on the other side of it buys a larger loss "
            "at the same size: what the comparison changes is which division "
            "you do, never which trade is worth making."
        )

    out.append("")
    # The two HALTING gates lead, because they are a different kind of refusal
    # from the rest of this list: the others refuse a trade, these refuse every
    # trade. Both were missing, and both are the ones an agent most needs to
    # know are there — a rejection it cannot see coming is one it will keep
    # walking into every fifteen minutes.
    out.append(
        "These bound SIZE only. The equity floor and the daily-loss kill switch "
        "each stop the account opening ANYTHING, whatever it is sized at. The "
        "session window, the news blackout, the per-symbol cooldown, the daily "
        "and weekly trade counts, the concurrent-position limit and a "
        "consecutive-loss stand-down can each still refuse a trade that fits "
        "every figure above."
    )
    return out


# ------------------------------------------- how wide a stop actually is
#
# **Size is the ceiling divided by the stop distance, so a stop tightened
# towards nothing buys a position tending towards infinity.** Measured 13 Aug
# 2026 in the same run as the cross-unit gap: `qwen3-coder-flash` proposed KO
# with a $0.05 stop against a $1.32 ATR — 0.04 ATR, inside the spread — which at
# the same stated risk is a position roughly twenty-six times the size a 1-ATR
# stop would have bought. Its median was 0.83 ATR against 1.65-1.66 for
# `nemotron-3-ultra-550b` and `deepseek-v4-pro`.
#
# **This REPORTS and it must never refuse.** A minimum stop distance is the
# opinion-on-placement `RiskGate` exists without: `_stops_on_correct_side`
# checks which SIDE of entry a level sits on and nothing else, deliberately,
# because a wider stop already buys a smaller position and the cost is what the
# gate measures. A floor here would be a rule nobody agreed to arriving through
# the back door — and it would arrive in the module that renders prompts, which
# is worse, because a limit stated in a document is a limit nobody can read off
# `config/rules.yaml`.
#
# So this is arithmetic and a sentence, in the same shape as `stops_unchecked`:
#
# - **A symbol with no ATR is UNMEASURED, never fine.** `fetch_indicators` drops
#   a symbol whose bars failed, so an absent ATR means "could not ask". Reading
#   that as a stop of acceptable width is the `calendar_degraded` mistake with a
#   position attached.
# - **The multiple is stated on every proposal, not only on the tight ones.** A
#   zero-breach cycle has to be a stated fact rather than the absence of a
#   warning, which is also what an outage looks like.
# - **The threshold flags; it does not judge.** 0.5 ATR sits below the 0.83
#   median of the worst model measured and far below the 1.65 of the two best,
#   so it names the pathological case without calling an ordinary tight stop
#   wrong. Nothing downstream reads it as permission or refusal.
TIGHT_STOP_ATR = 0.5


@dataclass(frozen=True)
class StopWidth:
    """One proposal's stop distance, in the instrument's own daily range.

    Three states rather than two, for the usual reason: measured and wide,
    measured and tight, and NOT MEASURED. The third is not a mild version of the
    first — it is the question having been asked and not answered.
    """

    symbol: str
    stop_distance_usd: float
    atr_14: float | None

    @property
    def atr_multiple(self) -> float | None:
        """Stop distance in ATRs, or `None` when there is no ATR to divide by.

        A non-positive ATR is `None` too rather than a division: it is what a
        flat or unreadable series produces, and an infinite multiple would be a
        confident answer built on nothing.
        """
        if self.atr_14 is None or self.atr_14 <= 0:
            return None
        return self.stop_distance_usd / self.atr_14

    @property
    def is_measured(self) -> bool:
        return self.atr_multiple is not None

    @property
    def is_tight(self) -> bool:
        """Under the reporting threshold, and MEASURED. An unknown is not tight."""
        multiple = self.atr_multiple
        return multiple is not None and multiple < TIGHT_STOP_ATR

    def render(self) -> str:
        """One clause, for a surface or for the model's own feedback block."""
        if not self.is_measured:
            return (
                f"stop ${self.stop_distance_usd:,.4f} from entry — ATR UNMEASURED "
                f"for {self.symbol}, so the stop cannot be put in the "
                f"instrument's own terms. Not checked, which is not the same as "
                f"fine."
            )
        multiple = self.atr_multiple
        atr = self.atr_14
        # Guarded by `is_measured`; restated for the type checker rather than
        # asserted, because a renderer that raised would take a page down over a
        # descriptive line.
        if multiple is None or atr is None:  # pragma: no cover - unreachable
            return ""
        line = (
            f"stop ${self.stop_distance_usd:,.4f} from entry = {multiple:.2f} ATR "
            f"(ATR {atr:,.4f})"
        )
        if self.is_tight:
            line += (
                f", under {TIGHT_STOP_ATR:.2f} ATR — inside a single day's "
                f"average range, so ordinary noise reaches it before the thesis "
                f"is tested, at the largest size the ceilings allow. Reported, "
                f"not refused: where the stop goes is yours."
            )
        return line


def stop_width(
    proposal: OrderProposal, indicators: Mapping[str, Indicators]
) -> StopWidth:
    """Measure one proposal's stop against the ATR the loop actually recorded.

    The ATR is read rather than derived, exactly as everywhere else — the model
    is handed figures and this is handed the same ones. A symbol absent from
    `indicators` yields `atr_14=None`, which renders as unmeasured.
    """
    reading = indicators.get(proposal.symbol)
    return StopWidth(
        symbol=proposal.symbol,
        stop_distance_usd=abs(proposal.limit_price - proposal.stop_loss_price),
        atr_14=reading.atr_14 if reading is not None else None,
    )


def measure_stop_widths(
    proposals: list[OrderProposal], indicators: Mapping[str, Indicators]
) -> list[StopWidth]:
    """Every proposal's stop width, in order, including the unmeasurable ones.

    A list rather than a mapping, and that is load-bearing: two proposals on one
    symbol are two different stops, and keying by symbol would silently report
    one of them. Order matches `proposals`, so a caller can zip the two.
    """
    return [stop_width(p, indicators) for p in proposals]


def render_grants(grants: GrantBriefing, *, now: datetime) -> list[str]:
    """Symbols an adopted dream permits, and the chain that argued for each.

    **Placed last, and that is deliberate.** Everything above is a measurement;
    this is the one block in the document that is speculative by construction,
    and a model that reads it first anchors on a story before it has seen a
    figure. The order says which is which.

    Three properties are load-bearing and none is decoration:

    - **The chain never appears without its badge.** `Verification` and
      `weakest_hop` are rendered adjacent to the hops they qualify and must not
      be separated from them. An unqualified causal chain in a prompt reads as
      established fact, and the entire reason `Hop.checked` exists is that some
      of those sentences were invented. An UNVERIFIED chain says so on the line
      above the first hop.
    - **The expiry travels with the symbol.** A permission rendered without an
      end reads as permanent, and this one is not.
    - **It says plainly that a dream permits and does not propose.** The symbol
      is available; direction, entry, stop and size are still the agent's to
      justify on its own evidence. A dream is a reason a symbol is on the table,
      never a reason to be in it.
    """
    out = ["## Symbols an adopted dream permits (speculative — read the caveats)"]
    out.append(
        "These are tradeable this cycle ONLY because an adopted dream permits "
        "them. They are not in config/rules.yaml, the permission EXPIRES, and "
        "it can be handed back at any time."
    )
    out.append(
        "A dream permits a symbol. It does NOT propose a position. Direction, "
        "entry, stop and size are yours to justify on the evidence above — the "
        "figures, the session, the spread — exactly as for any listed symbol. "
        "The risk gate applies this symbol's own instrument-class limits in "
        "full; adoption buys entry to the allowlist and nothing else."
    )
    out.append("")

    for symbol, class_key in sorted(grants.symbols.items()):
        dream_id = grants.dream_ids.get(symbol)
        expiry = grants.expires_at.get(dream_id) if dream_id is not None else None
        if expiry is None:
            # Never rendered as an absent limit. A permission whose end cannot
            # be read is the state to be MORE careful about, not less.
            when = "expiry unknown — treat this permission as ending at any moment"
        else:
            days = (expiry - now).total_seconds() / 86_400
            when = (
                f"permission ends {expiry.isoformat(timespec='minutes')} "
                f"({days:.1f} days)"
            )
        out.append(f"- {symbol} ({class_key}) — {when}")

    if not grants.chains_available:
        out.append("")
        out.append(
            "The reasoning behind these grants could not be read this cycle, so "
            "no chain is shown. That is a missing record, NOT an absence of "
            "speculation: judge the symbols on the figures above and nothing else."
        )
        return out

    for dream in grants.dreams:
        out.append("")
        # The badge sits on the heading line, so no arrangement of this block
        # can put a hop on screen without the qualification that governs it.
        out.append(
            f"### Dream {dream.id}: {dream.title} "
            f"[{str(dream.verification).upper()}]"
        )
        if dream.verification is not Verification.SOURCED:
            unchecked = len(dream.unverified_hops)
            out.append(
                f"  {unchecked} of {len(dream.chain)} hop(s) below are "
                "UNCHECKED — the dreamer was not shown anything establishing "
                "them. Treat those sentences as assertions, not facts."
            )
        out.append(f"  spark: {dream.seed}")
        for i, hop in enumerate(dream.chain, 1):
            mark = "checked" if hop.checked else "UNCHECKED"
            source = f" [{hop.source}]" if hop.checked and hop.source else ""
            out.append(f"  hop {i} ({mark}): {hop.claim}{source}")
        # Named right under the chain rather than at the end of the block: a
        # reader with time for one sentence should be given the one that could
        # kill the whole thing.
        if dream.weakest_hop:
            out.append(f"  WEAKEST HOP: {dream.weakest_hop}")
        else:
            out.append(
                "  WEAKEST HOP: not named by the dreamer, which is itself a "
                "reason for more caution rather than less."
            )
        if dream.conditions:
            met = dream.conditions_met
            out.append(
                f"  conditions pre-registered: {met} of "
                f"{len(dream.conditions)} met"
            )
    return out


def fetch_market_ticks(broker: Broker, symbols: list[str]) -> dict[str, Tick]:
    """Pull a tick for every allowed symbol, skipping any that error out.

    Catches broadly, and deliberately. The narrow `(KeyError, RuntimeError)`
    this used to catch covers `MockBroker` and the hand-raised errors in
    `AlpacaBroker`, but not what the Alpaca SDK actually raises on a bad day:
    `APIError`, an `httpx` timeout, a JSON decode failure. Any of those would
    propagate out of here and end the decision loop, which is the worst
    available outcome — the journal stops being reconciled and open positions
    stop being watched. Same reasoning as `data/_http.fetch_json`: there is no
    exception from an HTTP client worth crashing a trading loop over.
    """
    result: dict[str, Tick] = {}
    for symbol in symbols:
        try:
            result[symbol] = broker.get_tick(symbol)
        except Exception as exc:
            log.warning("tick_fetch_failed", symbol=symbol, error=f"{type(exc).__name__}: {exc}")
            continue
    return result


def fetch_indicators(
    broker: Broker, symbols: list[str]
) -> tuple[dict[str, Indicators], list[str]]:
    """Compute indicators for every allowed symbol, and name the ones that failed.

    Returns the indicators and, separately, the symbols that produced nothing.
    The second value is the point: a symbol dropped silently from the first dict
    is indistinguishable from a symbol nobody asked about, and the model would
    reason about its live quote with no history and no warning. Same principle
    as `reconcile`'s `risk_is_understated` and `FinnhubCalendar.is_degraded`.

    Catches broadly for the same reason `fetch_market_ticks` does: this runs a
    network call per symbol on every cycle, and an Alpaca `APIError` or an
    `httpx` timeout escaping here would kill the loop rather than degrade it.
    A symbol that failed is reported as missing history, which is already the
    honest description of what the model has for it.

    No cache, deliberately. The Marketaux and Finnhub caches exist because
    those free tiers allow 100 requests a day against a loop that wakes 96
    times, so the TTL is a hard requirement. Alpaca's market-data limit is per
    minute, not per day, and six symbols every fifteen minutes is nowhere near
    it. Adding a TTL here would be an optimisation dressed as a rate-limit
    control, and it would make the indicators lag the quote in the same
    context block for no benefit.
    """
    found: dict[str, Indicators] = {}
    missing: list[str] = []

    for symbol in symbols:
        try:
            bars = broker.get_daily_bars(symbol)
        except Exception as exc:
            log.warning("bars_fetch_failed", symbol=symbol, error=f"{type(exc).__name__}: {exc}")
            missing.append(symbol)
            continue

        result = compute(symbol, bars)
        if result is None:
            missing.append(symbol)
        else:
            found[symbol] = result

    return found, missing


def fetch_intraday(
    broker: Broker, symbols: list[str], minutes: int = 5
) -> tuple[dict[str, IntradayView], list[str]]:
    """Intraday views per symbol, and the symbols that produced none.

    Same contract as `fetch_indicators`, and for the same reasons: catches
    broadly so a bad minute at the bars endpoint degrades the cycle instead of
    ending the loop, and returns the failures by name so a symbol that silently
    vanished from the block cannot be mistaken for one nobody asked about.

    The distinction matters more here than for the daily figures. A symbol with
    no intraday history is one where a break and a wick are indistinguishable,
    which is the exact condition `trend_break` must not be applied under.
    """
    found: dict[str, IntradayView] = {}
    missing: list[str] = []

    for symbol in symbols:
        try:
            bars = broker.get_intraday_bars(symbol, minutes)
        except Exception as exc:
            log.warning(
                "intraday_fetch_failed", symbol=symbol, error=f"{type(exc).__name__}: {exc}"
            )
            missing.append(symbol)
            continue

        result = compute_intraday(symbol, bars, minutes)
        if result is None:
            missing.append(symbol)
        else:
            found[symbol] = result

    return found, missing
