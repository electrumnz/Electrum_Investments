"""Entry point for the Electrum trading bot.

Paper trading only. `smoketest` proves the wiring end to end; `loop` runs the
decision cycle. Neither places an order without an approving risk verdict, and
`--execute` must be passed explicitly before any order reaches the broker.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import structlog

from . import stop_watch
from .audit import AuditLog
from .broker import AlpacaBroker, Broker, MockBroker
from .claude_client import ClaudeClient, build_system_prompt
from .config import Env, LiveTradingRefused, Rules
from .context import (
    build_market_context,
    fetch_indicators,
    fetch_intraday,
    fetch_market_ticks,
)
from .data.calendar import CalendarFeed, EmptyCalendar
from .data.finnhub import FinnhubCalendar
from .data.marketaux import MarketauxNews
from .data.news import EmptyNews, NewsFeed
from .data.xfeed import XFeed
from .dreaming import DreamStore
from .grants import resolve_grant_dream_ids, resolve_granted_symbols
from .indicators import snapshot as snapshot_indicators
from .indicators import summarise as summarise_indicators
from .intraday import summarise as summarise_intraday
from .journal import Journal
from .models import (
    Decision,
    MarketInputs,
    OrderProposal,
    RiskVerdict,
    SymbolAssessment,
)
from .options import alerts_for_positions
from .reconcile import apply_journal_state, reconcile, record_fill
from .risk import RiskGate
from .session_calendar import SessionCalendar

log = structlog.get_logger()


def build_broker(env: Env, *, force_mock: bool = False) -> Broker:
    """Return a live paper broker, or a MockBroker when credentials are absent."""
    if force_mock:
        return MockBroker()
    if not env.alpaca_api_key or not env.alpaca_secret_key:
        log.warning("no_alpaca_credentials_using_mock_broker")
        return MockBroker()
    return AlpacaBroker(env)


def build_news_feed(env: Env) -> NewsFeed:
    """Marketaux when a key is present, otherwise nothing.

    Headlines are context only. Running without them is a supported
    configuration, not a degraded one, so this logs at info rather than warning.
    """
    if not env.marketaux_api_key:
        log.info("no_marketaux_key_running_without_headlines")
        return EmptyNews()
    return MarketauxNews(api_key=env.marketaux_api_key)


def build_calendar_feed(env: Env, rules: Rules) -> CalendarFeed:
    """Finnhub's earnings calendar when a key is present, otherwise nothing.

    Unlike headlines this one feeds a risk rule: with no calendar,
    `RiskGate._news_blackout` has no windows and therefore never fires. That is
    a real gap rather than a preference, so it warns.
    """
    if not env.finnhub_api_key:
        log.warning(
            "no_finnhub_key_news_blackout_inactive",
            detail=(
                "Without an earnings calendar the news blackout rule in "
                "config/rules.yaml cannot fire. Trades will not be held back "
                "around announcements."
            ),
        )
        return EmptyCalendar()
    return FinnhubCalendar(api_key=env.finnhub_api_key, symbols=list(rules.allowed_symbols))


def build_social_feed(env: Env, rules: Rules) -> XFeed | None:
    """The X feed, when both a token and an enabled account list exist.

    Off is the normal state. Reading timelines needs a paid X tier, and this
    gates nothing, so a deployment without it is fully functional with slightly
    less context. Logged at info for that reason rather than at warning, the
    same as Marketaux and unlike Finnhub, which does feed a risk rule.
    """
    if not rules.social.enabled:
        return None
    if not env.x_bearer_token:
        log.info(
            "no_x_bearer_token_social_feed_off",
            detail=(
                "config/rules.yaml enables the social feed but X_BEARER_TOKEN is "
                "unset, so no posts will be read. This gates nothing."
            ),
        )
        return None
    return XFeed(
        bearer_token=env.x_bearer_token,
        accounts=list(rules.social.accounts),
        lookback_minutes=rules.social.lookback_minutes,
        max_posts=rules.social.max_posts,
    )


def cmd_smoketest(env: Env, rules: Rules, *, force_mock: bool = False) -> int:
    """Connect, print account state, ask Claude one question. Never places an order."""
    audit = AuditLog()
    broker = build_broker(env, force_mock=force_mock)
    broker.connect()
    try:
        account = broker.get_account()
        ticks = fetch_market_ticks(broker, rules.allowed_symbols)
        indicators, no_history = fetch_indicators(broker, rules.allowed_symbols)
        log.info(
            "connected",
            equity=account.equity_usd,
            cash=account.cash_usd,
            positions=len(account.open_positions),
            ticks=len(ticks),
            indicators=len(indicators),
            symbols_without_history=no_history,
        )

        if not env.anthropic_api_key:
            log.warning("smoketest_skipping_claude_no_api_key")
            audit.record_event(
                "smoketest",
                {"ok": True, "claude_called": False, "tick_count": len(ticks)},
            )
            return 0

        claude = ClaudeClient(env, build_system_prompt(rules))
        context = build_market_context(
            account=account,
            ticks=ticks,
            headlines=build_news_feed(env).recent_headlines(rules.allowed_symbols),
            news_windows=build_calendar_feed(env, rules).upcoming_windows(
                lookahead_minutes=60
            ),
            activity=broker.get_activity(),
            indicators=indicators,
            symbols_without_history=no_history,
            instruments=rules.instruments,
            broker_clock=broker.get_clock(),
        )
        decision, usage = claude.propose(context)
        log.info(
            "claude_responded",
            proposal_count=len(decision.proposals),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=round(usage.estimated_cost_usd, 6),
        )
        audit.record(
            Decision(
                timestamp=datetime.now(UTC),
                proposals=decision.proposals,
                claude_input_tokens=usage.input_tokens,
                claude_output_tokens=usage.output_tokens,
                claude_cached_tokens=usage.cache_read_tokens,
                estimated_cost_usd=usage.estimated_cost_usd,
                notes=f"smoketest assessment: {decision.market_assessment}",
            )
        )
        return 0
    finally:
        broker.disconnect()


def cmd_loop(
    env: Env, rules: Rules, *, execute: bool = False, force_mock: bool = False
) -> int:
    """Decision loop. Proposals are always vetted; orders are placed only with --execute."""
    audit = AuditLog()
    broker = build_broker(env, force_mock=force_mock)
    broker.connect()
    claude = ClaudeClient(env, build_system_prompt(rules))
    news = build_news_feed(env)
    calendar = build_calendar_feed(env, rules)
    social = build_social_feed(env, rules)

    # Which days trade and until what time. Distinct from `calendar` above,
    # which is Finnhub's EARNINGS calendar and feeds a gate rule; this one is
    # Alpaca's TRADING calendar, feeds no rule, and exists because a half-day
    # is invisible while it is happening.
    session_calendar = SessionCalendar()

    journal = Journal()

    # The dream store, opened once and only when the feature is switched on.
    #
    # It is the source of the symbol grants an adopted dream carries, and it is
    # deliberately NOT reached from inside the risk gate: `RiskGate` has to stay
    # deterministic and must not fail open, so the grant is resolved out here
    # each cycle and passed in, in the same shape as `news_windows`.
    #
    # A store that will not open costs the permissions and nothing else. The
    # cycle continues, the gate applies `config/rules.yaml` exactly as it always
    # did, and the failure is named rather than left as a silence — the same
    # trade as `fetch_market_ticks` catching broadly, for the same reason: an
    # unanticipated exception here would end the decision loop, which is the
    # worst available outcome once `--execute` is on.
    dreams: DreamStore | None = None
    if rules.dreaming.allow_symbol_grants:
        try:
            dreams = DreamStore()
        except Exception as exc:
            log.warning(
                "dream_store_unavailable",
                error=f"{type(exc).__name__}: {exc}",
                detail=(
                    "No dream can widen the allowed symbols this run. Trading "
                    "continues on config/rules.yaml alone."
                ),
            )

    account = broker.get_account()
    risk = RiskGate(
        rules,
        equity_at_session_start=account.equity_usd,
        execution_mode=env.execution_mode,
    )

    audit.record_event(
        "loop_start",
        {"tier": env.claude_tier.value, "execute": execute, "paper": env.alpaca_paper_trade},
    )
    if not execute:
        log.info("dry_run_no_orders_will_be_placed")

    # What the model said last cycle, carried into the next prompt so a
    # `waiting_for` trigger is something it can check rather than something it
    # writes and never sees again.
    #
    # Seeded from the audit log rather than starting empty, because a restart is
    # the moment this is most valuable: a deploy in the middle of a session
    # would otherwise throw away every open watch. The log is the record of
    # what was actually said, so reading it back is not a second source of
    # truth. A failure here costs the recall and nothing else.
    previous_assessments: list[SymbolAssessment] = []
    previous_at: datetime | None = None
    previous_verdicts: list[tuple[OrderProposal, RiskVerdict]] = []
    try:
        recent = audit.read(limit=1, days=4).decisions
        if recent:
            previous_assessments = list(recent[0].decision.assessments)
            previous_at = recent[0].timestamp
            # Zipped because the two lists are written in proposal order and a
            # RiskVerdict carries reasons but not the symbol they belong to.
            previous_verdicts = list(
                zip(
                    recent[0].decision.proposals,
                    recent[0].decision.verdicts,
                    strict=False,
                )
            )
            log.info(
                "recalled_previous_assessments",
                count=len(previous_assessments),
                recorded_at=previous_at.isoformat(timespec="minutes"),
            )
    except Exception as exc:
        log.warning("previous_assessment_recall_failed", error=f"{type(exc).__name__}: {exc}")

    try:
        while True:
            account = broker.get_account()
            activity = broker.get_activity()
            ticks = fetch_market_ticks(broker, rules.allowed_symbols)
            indicators, no_history = fetch_indicators(broker, rules.allowed_symbols)
            intraday, no_intraday = fetch_intraday(broker, rules.allowed_symbols)
            news_windows = calendar.upcoming_windows(lookahead_minutes=60)

            # Bring the journal in step before anything is evaluated: this is
            # what populates open risk and advances the loss streak, so the
            # total-risk cap and the stand-down both depend on it having run.
            recon = reconcile(journal, broker, rules, account=account)
            account = apply_journal_state(account, journal)
            stand_down_state = journal.get_stand_down()

            # A no-op on 95 of 96 cycles a day: `refresh` returns immediately
            # unless the cache is stale or its horizon has walked forward. It
            # never raises, and a failure keeps the days already held rather
            # than clearing them — dates published months ago do not stop being
            # true because one fetch timed out.
            session_calendar.refresh(broker)

            # Checked every cycle, before anything else. An option expiry is the
            # one thing here that resolves itself automatically and irreversibly
            # if nobody is watching.
            expiry_alerts = alerts_for_positions(
                [(p.symbol, p.qty) for p in account.open_positions],
                now=datetime.now(UTC),
                warn_days=rules.options.warn_days_before_expiry,
                buying_power_usd=account.buying_power_usd,
                underlying_prices={s: t.mid for s, t in ticks.items()},
            )
            for alert in expiry_alerts:
                if alert.needs_action:
                    log.warning(
                        "option_expiry_action_required",
                        symbol=alert.symbol,
                        days_to_expiry=alert.days_to_expiry,
                        in_the_money=alert.in_the_money,
                        can_fund_exercise=alert.can_fund_exercise,
                        detail=alert.message,
                    )

            # Is anything already through the stop it was sized against?
            #
            # The bracket at the broker is the primary protection and covers the
            # regular session completely. This covers what it structurally
            # cannot: a stop leg cannot fire outside regular hours, because a
            # stop becomes a MARKET order and extended-hours venues take limit
            # orders only; Alpaca does not accept brackets on crypto at all; and
            # a position adopted from the broker has a journalled stop with no
            # order behind it.
            #
            # Reported, never acted on. Closing out of hours needs a marketable
            # limit order, which is a new execution path, and one that fires
            # unattended at 3am is a different proposition from one somebody
            # watches. Making the breach loud is the honest intermediate.
            breaches = stop_watch.check(journal.open_trades(), ticks)
            for breach in breaches:
                log.warning(
                    "stop_breached",
                    symbol=breach.trade.symbol,
                    direction=breach.trade.direction.value,
                    price=round(breach.price, 4),
                    planned_stop=round(breach.trade.planned_stop, 4),
                    through_by=round(breach.distance_usd, 4),
                    loss_if_closed_now=round(breach.loss_if_closed_now_usd, 2),
                    detail=breach.describe(),
                )
                audit.record_event("stop_breached", {"detail": breach.describe()})
            # Named separately, never folded into "no breaches". A symbol whose
            # quote failed is one this cycle could not check, which is not the
            # same as one it checked and found fine.
            unchecked_stops = stop_watch.unchecked(journal.open_trades(), ticks)
            if unchecked_stops:
                log.warning("stops_unchecked", symbols=unchecked_stops)

            # Nothing enabled is open, so a proposal cannot be approved whatever
            # it says. Skipping the model call here is a cost control, not a
            # risk one: everything above this line has already run, so the
            # journal is still reconciled, the stand-down state still advances
            # and an option expiry is still warned about on the cycle it needs
            # to be.
            #
            # The window is derived from the instruments block, so a 24/7 class
            # is always in session and this becomes a no-op the moment one is
            # enabled. It cannot quietly switch the bot off as the instrument
            # mix grows.
            #
            # Logged rather than passed over in silence, on the same principle
            # as the heartbeat: a cycle that deliberately did nothing has to be
            # distinguishable from one that never ran.
            now = datetime.now(UTC)
            if rules.loop.skip_model_call_when_all_markets_closed and not (
                rules.any_class_in_session(now)
            ):
                log.info(
                    "cycle_skipped_market_closed",
                    equity_usd=round(account.equity_usd, 2),
                    open_positions=len(account.open_positions),
                    open_risk_usd=round(account.open_risk_usd, 2),
                    stand_down_stage=stand_down_state.stage
                    if stand_down_state.is_active(now)
                    else 0,
                    risk_understated=recon.risk_is_understated,
                    next_cycle_seconds=env.decision_interval_seconds,
                )
                time.sleep(env.decision_interval_seconds)
                continue

            # What an adopted dream permits right now, resolved once for the
            # whole cycle so every proposal is gated against the same answer.
            #
            # Fails closed on everything: the feature switched off, a store that
            # would not open, a database error, more grants live than the cap
            # allows. All of them come back `{}`, which means the account trades
            # exactly what `config/rules.yaml` already allows. See `grants.py`.
            #
            # **Nothing here can currently be proposed**, and that is worth
            # knowing before wondering why a grant never fires: the system
            # prompt lists `rules.allowed_symbols`, and the tick and indicator
            # fetches above run over the same list, so the model is never told a
            # granted symbol exists and a proposal in one would be dropped for
            # want of a quote. Widening those is the prompt-side half of this
            # feature and it lives in `context.py` and `claude_client.py`. The
            # gate side is wired and inert until then, which is the same shape
            # as crypto being fully configured while disabled.
            granted_symbols: dict[str, str] = {}
            granted_dream_ids: dict[str, int] = {}
            if dreams is not None:
                granted_symbols = resolve_granted_symbols(dreams, rules, now=now)
                if granted_symbols:
                    # Provenance for the journal, and a second read, so it is
                    # only paid for when something was actually granted.
                    granted_dream_ids = resolve_grant_dream_ids(
                        dreams, granted_symbols, now=now
                    )
                    log.info(
                        "symbol_grants_in_force",
                        symbols=sorted(granted_symbols),
                        classes=sorted(set(granted_symbols.values())),
                    )

            headlines = news.recent_headlines(rules.allowed_symbols)
            posts = [p.render() for p in social.recent_posts()] if social else []
            social_degraded = bool(social and social.is_degraded)
            context = build_market_context(
                account=account,
                ticks=ticks,
                headlines=headlines,
                news_windows=news_windows,
                activity=activity,
                expiry_alerts=expiry_alerts,
                indicators=indicators,
                symbols_without_history=no_history,
                intraday=intraday,
                symbols_without_intraday=no_intraday,
                previous_assessments=previous_assessments,
                previous_at=previous_at,
                previous_verdicts=previous_verdicts,
                social_posts=posts,
                social_degraded=social_degraded,
                instruments=rules.instruments,
                # One extra call per cycle, and it buys what `market_clock`
                # structurally cannot compute: whether the broker agrees the
                # regular session is running right now. Returns None rather
                # than raising, so a bad minute at Alpaca costs the check and
                # not the cycle.
                broker_clock=broker.get_clock(),
                # Refreshed at most every 20 hours inside `refresh`, so this is
                # a dict lookup on all but one cycle a day. It answers which
                # days are skipped and which end EARLY — the second being the
                # one nothing else here can see, because a half-day looks
                # exactly like an ordinary session until 13:00 passes.
                calendar=session_calendar,
            )
            # Catches broadly, and for the same reason `fetch_market_ticks`
            # does. This is a network call to a model that returns free text,
            # and the SDK validates the answer with Pydantic, so the failures
            # are `APIError`, an httpx timeout, an overload, and — observed on
            # the droplet — a `ValidationError` because one rationale came back
            # over the character cap. Any of those propagating from here ends
            # the loop, which is the worst available outcome and got worse when
            # `--execute` went on: the journal stops being reconciled and open
            # positions stop being watched, with real orders resting at the
            # broker and nothing on screen to say the bot has gone.
            #
            # A cycle that cannot get a decision has nothing to propose, which
            # is also the honest description of what happened. Everything
            # safety-critical above this line — reconcile, the stand-down
            # state, the expiry alerts — has already run.
            try:
                decision, usage = claude.propose(context)
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                log.error("model_call_failed", error=detail)
                audit.record_event("model_call_failed", {"error": detail})
                time.sleep(env.decision_interval_seconds)
                continue

            # Recorded alongside the decision, not merely rendered into the
            # prompt and discarded. "Why did it pass on SPY on Tuesday" cannot
            # be answered from a snapshot taken later: it needs the headlines,
            # the calendar and the indicators that were actually in front of the
            # model at the time.
            inputs = MarketInputs(
                headlines=headlines,
                social_posts=posts,
                social_degraded=social_degraded,
                news_windows=[
                    f"{w.timestamp.isoformat(timespec='minutes')} affects "
                    f"{', '.join(sorted(w.affected_symbols))}"
                    for w in news_windows
                ],
                indicators={
                    symbol: summarise_indicators(ind)
                    for symbol, ind in sorted(indicators.items())
                },
                symbols_without_history=no_history,
                # Numbers as well as the rendered line. This is the half a
                # stored trigger can be checked against, and it is the half
                # that cannot be backfilled: a cycle recorded before it existed
                # has no figures, only prose about them.
                readings={
                    symbol: snapshot_indicators(ind)
                    for symbol, ind in sorted(indicators.items())
                },
                intraday={
                    symbol: summarise_intraday(view)
                    for symbol, view in sorted(intraday.items())
                },
                symbols_without_intraday=no_intraday,
                calendar_degraded=bool(getattr(calendar, "is_degraded", False)),
            )

            verdicts = []
            executed = []
            for proposal in decision.proposals:
                tick = ticks.get(proposal.symbol)
                if tick is None:
                    log.warning("no_tick_for_proposal", symbol=proposal.symbol)
                    continue

                verdict = risk.evaluate(
                    proposal,
                    account=account,
                    tick=tick,
                    activity=activity,
                    news_windows=news_windows,
                    stand_down=stand_down_state,
                    granted_symbols=granted_symbols,
                )
                verdicts.append(verdict)
                log.info(
                    "verdict",
                    symbol=proposal.symbol,
                    direction=proposal.direction.value,
                    qty=proposal.qty,
                    approved=verdict.approved,
                    reasons=verdict.reasons,
                    # None on every ordinary trade. When it is set, a dream is
                    # the only reason this symbol could be considered at all,
                    # and a permission that leaves no trace is not auditable.
                    granted_by_dream_class=verdict.granted_by_dream_class,
                )

                if verdict.approved and execute:
                    result = broker.place_order(proposal)
                    executed.append(result)
                    trade_id = record_fill(
                        journal,
                        proposal,
                        result,
                        execution_mode=env.execution_mode,
                        # Tagged from the instrument class rather than asked of
                        # the model, so the label is always accurate and
                        # metrics.breakdown_by can separate strategies.
                        strategy=rules.strategy_for(proposal.symbol),
                        # Only when the gate says the grant is what let this
                        # symbol through. A listed symbol a stale grant also
                        # names is an ordinary trade and must not be journalled
                        # as a dream's.
                        dream_id=(
                            granted_dream_ids.get(proposal.symbol)
                            if verdict.permitted_by_dream_grant
                            else None
                        ),
                    )
                    log.info(
                        "order_submitted",
                        symbol=proposal.symbol,
                        accepted=result.accepted,
                        order_id=result.order_id,
                        trade_id=trade_id,
                        error=result.error,
                    )
                    # A new position changes open risk, so later proposals in
                    # this same cycle are gated against the updated figure
                    # rather than a stale one.
                    if trade_id is not None:
                        account = apply_journal_state(account, journal)

            audit.record(
                Decision(
                    timestamp=datetime.now(UTC),
                    proposals=decision.proposals,
                    verdicts=verdicts,
                    executed=executed,
                    assessments=decision.assessments,
                    position_plans=decision.position_plans,
                    inputs=inputs,
                    claude_input_tokens=usage.input_tokens,
                    claude_output_tokens=usage.output_tokens,
                    claude_cached_tokens=usage.cache_read_tokens,
                    estimated_cost_usd=usage.estimated_cost_usd,
                    notes=decision.market_assessment,
                )
            )
            # Carried forward only on a cycle that produced a decision. A cycle
            # skipped because the market was shut, or one whose model call
            # failed, leaves the last real assessment in place rather than
            # blanking it — otherwise a weekend would erase every open watch and
            # Monday would start from nothing.
            previous_assessments = list(decision.assessments)
            previous_at = datetime.now(UTC)
            previous_verdicts = list(zip(decision.proposals, verdicts, strict=False))

            audit.record_event(
                "reconcile",
                {
                    "opened": recon.opened,
                    "closed": recon.closed,
                    "excursions_updated": recon.excursions_updated,
                    "estimated_exits": recon.estimated_exits,
                    "untracked_positions": recon.untracked_positions,
                    "risk_understated": recon.risk_is_understated,
                    "open_risk_usd": round(account.open_risk_usd, 2),
                },
            )

            # One line per cycle, unconditionally.
            #
            # Everything above logs only when something happened: a verdict per
            # proposal, a warning per expiry alert. But doing nothing is the
            # common output here and is meant to be — so on a quiet day the
            # journal stays completely silent, and a healthy bot is
            # indistinguishable from a wedged one at a glance. The audit file
            # has the detail; this is the pulse.
            log.info(
                "cycle_complete",
                equity_usd=round(account.equity_usd, 2),
                open_positions=len(account.open_positions),
                open_risk_usd=round(account.open_risk_usd, 2),
                proposals=len(decision.proposals),
                approved=sum(1 for v in verdicts if v.approved),
                executed=len(executed),
                stand_down_stage=stand_down_state.stage
                if stand_down_state.is_active(datetime.now(UTC))
                else 0,
                risk_understated=recon.risk_is_understated,
                # On the cycle line rather than only in a warning, so a reader
                # scanning the log sees it without grepping — and so "0" is a
                # stated fact each cycle rather than the absence of a warning,
                # which is what an outage also looks like.
                stops_breached=len(breaches),
                stops_unchecked=unchecked_stops,
                news_windows=len(news_windows),
                # Every symbol an adopted dream is currently widening the
                # allowlist by. On the cycle line for the same reason
                # `stops_breached` is: an empty list each cycle is a stated
                # fact, and a permission in force that is never stated is a
                # permission nobody can audit.
                granted_symbols=sorted(granted_symbols),
                calendar_degraded=getattr(calendar, "is_degraded", False),
                # Same reasoning as calendar_degraded: an empty post list from a
                # dead token looks exactly like a quiet morning, and the
                # difference matters when this is being used to explain a move.
                social_degraded=social_degraded,
                # Same reason calendar_degraded is here. A symbol whose bars
                # failed to fetch is one the model sees a live quote for and no
                # history at all, and that has to be visible from the log line
                # rather than only inside the prompt.
                symbols_without_history=no_history,
                # Same reason as symbols_without_history. A symbol with no
                # intraday bars is one where a break and a wick cannot be told
                # apart, which is the condition trend_break must not run under.
                symbols_without_intraday=no_intraday,
                cost_usd=round(usage.estimated_cost_usd, 6),
                next_cycle_seconds=env.decision_interval_seconds,
            )
            time.sleep(env.decision_interval_seconds)
    except KeyboardInterrupt:
        log.info("loop_stopped_by_user")
        return 0
    finally:
        audit.record_event("loop_end", {})
        broker.disconnect()


def cmd_dream(env: Env, rules: Rules) -> int:
    """One dream step.

    Its own command rather than a step inside `cmd_loop`, and deliberately so.
    The loop wakes every fifteen minutes because a price moves that fast; a
    second-order supply-chain idea does not, and a lateral thought every quarter
    hour would buy ninety-six shallow ones a day. Drive this from a timer at
    whatever cadence suits, or by hand.

    It also keeps the two apart structurally: the loop proposes orders and this
    cannot, because a `Dream` has no field an order needs.

    Returns 0 on a dream and 1 on a failed call. A non-zero exit is what a timer
    unit surfaces through `systemctl --failed`, and a model call that failed is
    worth noticing rather than logging into the void.
    """
    from .dreamer import Dreamer

    if not env.anthropic_api_key:
        log.error("dream_no_api_key", detail="ANTHROPIC_API_KEY is not set")
        return 1

    # The same feeds the decision loop reads, and for the opposite purpose: the
    # loop wants to know what happened to its six symbols, and this wants
    # something to pull on. A headline about crop insurance is useless to the
    # loop and is exactly the kind of spark this is for.
    #
    # Both degrade to empty rather than failing, as everywhere else: a dreamer
    # with no headlines has less to work with, which is a quieter run and not a
    # broken one. `recent_headlines` and `recent_posts` already catch their own
    # network failures.
    news = build_news_feed(env)
    social = build_social_feed(env, rules)

    headlines = news.recent_headlines(rules.allowed_symbols)
    posts = [p.render() for p in social.recent_posts()] if social else []

    journal = Journal()
    dreamer = Dreamer(env, rules, DreamStore(), journal)
    result = dreamer.run_once(headlines=headlines, posts=posts)

    if result is None:
        # Already logged with its reason inside run_once. Nothing was written:
        # a dream that could not be had is not recorded as one that decided
        # nothing.
        return 1

    dream = result.dream
    log.info(
        "dream_complete",
        dream_id=dream.id,
        title=dream.title,
        stage=str(dream.stage),
        verdict=str(dream.verdict) if dream.verdict else None,
        advanced=result.advanced,
        hops=len(dream.chain),
        unchecked_hops=len(dream.unverified_hops),
        verification=str(dream.verification),
        # Named for the same reason the loop reports `calendar_degraded` and
        # `symbols_without_history`: a run with no headlines is a different run
        # from one where nothing was in the news, and only the log can tell them
        # apart afterwards.
        headlines_seen=len(headlines),
        posts_seen=len(posts),
        social_degraded=bool(social and social.is_degraded),
        cost_usd=round(result.usage.estimated_cost_usd, 4) if result.usage else None,
    )
    return 0


def cmd_reindex() -> int:
    """Rebuild the searchable history from the audit log.

    The query tools index incrementally on every call, so this is not needed
    for freshness. It exists for the case where the index is suspect — after a
    schema change, or a half-written database — and the answer is always the
    same: throw it away and replay the log, which is still the source of truth.
    """
    from .insight import DEFAULT_DB_PATH, InsightIndex

    index = InsightIndex(DEFAULT_DB_PATH)
    report = index.rebuild()
    described = index.describe()

    log.info(
        "reindex_complete",
        decisions=report.decisions_indexed,
        events=report.events_indexed,
        files=report.files_scanned,
        malformed_lines=report.malformed,
        unreadable_files=report.unreadable,
        covers=described["covers_days"],
    )
    print(
        f"Indexed {report.decisions_indexed} decisions and "
        f"{report.events_indexed} events from {report.files_scanned} file(s)."
    )
    span = described["covers_days"]
    if span["first"]:
        print(f"Covers {span['first']} to {span['last']}.")
    if report.malformed:
        # Named rather than swallowed: an index quietly missing lines is worse
        # than one that says how many it could not read.
        print(f"{report.malformed} line(s) could not be parsed and were skipped.")
    for problem in report.unreadable:
        print(f"unreadable: {problem}")
    return 0


def main() -> int:
    structlog.configure(processors=[structlog.processors.JSONRenderer()])
    parser = argparse.ArgumentParser(prog="electrum-bot")
    parser.add_argument(
        "command",
        choices=["smoketest", "loop", "dream", "reindex"],
        help=(
            "smoketest: one-shot connectivity check. loop: run the decision "
            "loop. dream: one lateral-thinking step, written to data/dreams.db "
            "and shown on the Dreaming page, which places nothing ever. "
            "reindex: rebuild the searchable history from audit/."
        ),
    )
    parser.add_argument(
        "--rules",
        default="config/rules.yaml",
        help="Path to rules.yaml (default: config/rules.yaml)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually place approved orders on the paper account. Off by default.",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Force the in-memory MockBroker, ignoring any Alpaca credentials.",
    )
    args = parser.parse_args()

    # Before the credential check, deliberately. Rebuilding an index over files
    # already on disk needs no broker and no keys, and refusing to run it
    # because Alpaca is unconfigured would be a check protecting nothing.
    if args.command == "reindex":
        return cmd_reindex()

    env = Env()
    try:
        env.assert_paper_only()
    except LiveTradingRefused as e:
        log.error("live_trading_refused", detail=str(e))
        return 2

    rules = Rules.load(Path(args.rules))

    if args.command == "smoketest":
        return cmd_smoketest(env, rules, force_mock=args.mock)
    if args.command == "dream":
        return cmd_dream(env, rules)
    return cmd_loop(env, rules, execute=args.execute, force_mock=args.mock)


if __name__ == "__main__":
    sys.exit(main())
