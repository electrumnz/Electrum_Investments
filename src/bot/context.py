"""Format current market state into a single text blob for Claude."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from .broker import Broker
from .config import InstrumentRules
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

    if activity is not None:
        lines.append("## Recent trading activity")
        lines.append(f"- Trades today: {activity.trades_today}")
        lines.append(f"- Trades this week: {activity.trades_this_week}")
        lines.append("")

    # ## Open positions — and these are YOURS to manage.
    #
    # The stop and the risk are rendered here because the model is ASKED about
    # them. `claude_client` requests a `position_plan` for every open position
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
        lines.append(
            "The gate is deterministic code, not a reader you can persuade. A "
            "rejection above will happen again, identically, for the same "
            "proposal. If you still want the trade, fix the specific defect "
            "named — size down to fit the cap, move the stop, wait for the "
            "session — and do not re-send it unchanged or argue with the reason."
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
