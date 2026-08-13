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

from . import jobs, stop_watch
from .audit import AuditLog, AuditView
from .broker import AlpacaBroker, Broker, MockBroker
from .config import Env, LiveTradingRefused, Rules
from .context import (
    build_market_context,
    fetch_indicators,
    fetch_intraday,
    fetch_market_ticks,
)

# `build_calendar_feed` moved to `data.calendar` when the MCP order path
# turned out to need it too: a gate rule enforced on the loop and not on the
# operator's own order path is a rule with a hole in it.
from .data.calendar import build_calendar_feed
from .data.marketaux import MarketauxNews
from .data.news import EmptyNews, NewsFeed
from .data.xfeed import XFeed
from .dreaming import (
    OPERATOR,
    ConditionState,
    Dream,
    DreamCondition,
    DreamStore,
    FusionResult,
    ObservationDue,
    Vault,
    observation_handle,
    pending_observations,
)
from .grants import (
    DEGRADED_STATES as GRANTS_DEGRADED_STATES,
)
from .grants import SWITCHED_OFF as grants_switched_off
from .grants import UNAVAILABLE as grants_unavailable
from .grants import (
    GrantBriefing,
    brief_grants,
    resolve_granted_symbols,
    resolve_grants,
)
from .indicators import snapshot as snapshot_indicators
from .indicators import summarise as summarise_indicators
from .intraday import summarise as summarise_intraday
from .journal import Journal
from .model_client import ModelClient, build_system_prompt
from .models import (
    AccountSnapshot,
    Decision,
    MarketInputs,
    OrderProposal,
    PositionAction,
    RiskVerdict,
    StandDownState,
    SymbolAssessment,
)
from .options import alerts_for_positions
from .position_actions import execute_position_plan, loop_execution_state
from .reconcile import apply_journal_state, reconcile, record_fill
from .risk import RiskGate
from .session_calendar import SessionCalendar
from .triggers import CycleReadings

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

        claude = ModelClient(env, build_system_prompt(rules))
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
            # The whole `Rules`, not only `instruments`, so the context can
            # render the SIZING CEILINGS in dollars. Without this line the
            # block is built and renders nowhere — the wired-and-inert state
            # that made the dream vault do nothing for a week.
            rules=rules,
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


def _standing(
    account: AccountSnapshot, stand_down: StandDownState, moment: datetime
) -> jobs.Standing:
    """The account figures a job record carries, on any of the three outcomes.

    Established above the point where a cycle can be skipped or a model call can
    fail, so a skipped pass reports the same account state a completed one does.
    That is what makes a skip legible as "the market was shut" rather than as a
    blank record.
    """
    return jobs.Standing(
        equity_usd=round(account.equity_usd, 2),
        open_positions=len(account.open_positions),
        open_risk_usd=round(account.open_risk_usd, 2),
        stand_down_stage=stand_down.stage if stand_down.is_active(moment) else 0,
    )


def cmd_loop(
    env: Env, rules: Rules, *, execute: bool = False, force_mock: bool = False
) -> int:
    """Decision loop. Proposals are always vetted; orders are placed only with --execute."""
    audit = AuditLog()
    broker = build_broker(env, force_mock=force_mock)
    broker.connect()
    claude = ModelClient(env, build_system_prompt(rules))
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
    # Which of the five states the empty grant list will be reported as when
    # there is no store to ask. Set here rather than defaulted at the cycle,
    # because "switched off" and "would not open" are the two answers a reader
    # of the heartbeat most needs told apart, and only this block knows which.
    no_store_state = (
        grants_unavailable if rules.dreaming.allow_symbol_grants else grants_switched_off
    )
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

    # Whether this loop may act on its own position plans, and the sentence
    # saying so. Read once at startup and stated out loud, because it is the
    # kind of switch whose OFF state is otherwise indistinguishable from a
    # feature that is broken: a model that proposes a tighten every cycle and a
    # loop that silently ignores every one looks exactly like a loop whose
    # plans are empty. `execute_position_plan` reads the flag again for itself
    # on every plan — this is a statement, not the guard.
    actions_enabled, actions_note = loop_execution_state(rules)
    audit.record_event(
        "loop_start",
        {
            "tier": env.claude_tier.value,
            "execute": execute,
            "paper": env.alpaca_paper_trade,
            "position_actions_enabled": actions_enabled,
            "position_actions_note": actions_note,
        },
    )
    log.info(
        "position_actions_state",
        enabled=actions_enabled,
        # Both switches, because either one being off is enough. `--execute` off
        # means no broker call of any kind this run, plans included.
        execute=execute,
        detail=actions_note,
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

    # The symbol set the feeds and the earnings calendar were last built for.
    # Held across cycles so the calendar is rebuilt when a grant appears or
    # lapses and on no other cycle — see the rebuild below for why it is a
    # rebuild rather than an assignment to `.symbols`.
    feed_symbols: tuple[str, ...] = tuple(sorted(rules.allowed_symbols))

    try:
        while True:
            # One `now` for the whole cycle. The grant resolution, the session
            # check and the figures recorded against the decision all have to
            # describe the same instant, or a permission can be live for the
            # gate and expired on the readout of the same pass.
            now = datetime.now(UTC)
            account = broker.get_account()
            activity = broker.get_activity()

            # **Resolved BEFORE the feeds, and that ordering is the whole
            # point.** It used to sit after the model context was built, which
            # made the permission unusable: the ticks, indicators, intraday
            # bars and headlines had already been fetched over
            # `rules.allowed_symbols` alone, so a granted symbol had no quote
            # and no history, and a proposal in one would have been dropped for
            # want of a tick before it ever reached the gate that would have
            # allowed it.
            #
            # Fails closed on everything: the feature switched off, a store that
            # would not open, a database error, more grants live than the cap
            # allows. All of them come back `{}`, which means the account trades
            # exactly what `config/rules.yaml` already allows. See `grants.py`.
            granted_symbols: dict[str, str] = {}
            granted_dream_ids: dict[str, int] = {}
            briefing = GrantBriefing()
            # Every state this can be in gets a word, so the heartbeat never
            # carries an empty list whose cause a reader has to guess.
            grant_state = no_store_state
            grants_degraded = grant_state in GRANTS_DEGRADED_STATES
            if dreams is not None:
                resolution = resolve_grants(dreams, rules, now=now)
                granted_symbols = resolution.symbols
                grant_state = resolution.state
                grants_degraded = resolution.degraded
                if granted_symbols:
                    # The reasoning behind each grant, for the prompt, and the
                    # provenance for the journal. A second read, so it is only
                    # paid for when something was actually granted.
                    briefing = brief_grants(dreams, resolution, now=now)
                    granted_dream_ids = briefing.dream_ids
                    log.info(
                        "symbol_grants_in_force",
                        symbols=sorted(granted_symbols),
                        classes=sorted(set(granted_symbols.values())),
                    )

            # What the model is shown figures for this cycle: the allowlist plus
            # whatever an adopted dream currently permits. Sorted so two cycles
            # with the same permissions produce the same log line.
            symbols_in_play = sorted(set(rules.allowed_symbols) | set(granted_symbols))

            # **Rebuilt, never mutated.** `FinnhubCalendar` caches the windows
            # it fetched, and those were filtered against the symbol list it was
            # constructed with — so assigning to `.symbols` would leave a cache
            # holding pre-filtered results and produce a feed that looks fixed
            # and behaves inconsistently, which is worse than the open gap. A
            # fresh instance starts with a fresh cache, and this runs only on
            # the cycles where the set actually changed rather than every pass.
            #
            # Without it the earnings blackout could never fire for a granted
            # symbol: the gate's logic was fine and its input was narrowed.
            if tuple(symbols_in_play) != feed_symbols:
                log.info(
                    "feed_symbols_changed",
                    was=list(feed_symbols),
                    now=symbols_in_play,
                    detail=(
                        "Rebuilding the earnings calendar so the news blackout "
                        "can see the symbols an adopted dream permits."
                    ),
                )
                calendar = build_calendar_feed(
                    env, rules, extra_symbols=sorted(granted_symbols)
                )
                feed_symbols = tuple(symbols_in_play)

            # **Quotes stay above the market-closed skip and the bars do not**,
            # and the split is the reason the allowlist can be widened at all.
            #
            # Each of the three feeds costs ONE network call PER SYMBOL PER
            # CYCLE, so the loop's data bill is linear in `allowed_symbols`.
            # The ticks have to be paid on every cycle whatever the market is
            # doing: `alerts_for_positions` prices an option expiry from them
            # and `stop_watch` checks whether an open position is already
            # through its stop — and out-of-hours is precisely the case that
            # backstop exists for, since a resting stop leg cannot fire there.
            #
            # The daily and intraday bars buy nothing on a cycle that will not
            # ask the model. They feed `build_market_context` and `MarketInputs`
            # and nothing above this line reads either, so on a skipped cycle
            # they were fetched, rendered into no prompt and discarded. They are
            # fetched below the skip now, which takes two thirds of the
            # per-symbol cost off roughly half the cycles in a week.
            ticks = fetch_market_ticks(broker, symbols_in_play)
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
                    # The level the breach was measured against — the one in
                    # FORCE, which is the tightened one where a move has been
                    # recorded. `planned_stop` beside it because `through_by` is
                    # arithmetic against `stop`, and a reader given only the
                    # sizing figure would find the numbers did not add up.
                    stop=round(breach.stop, 4),
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
                    # Reconcile runs ABOVE this skip, so these are established
                    # on a shut-market cycle exactly as they are on a full one
                    # — and this is the cycle they matter most on. An entry
                    # placed out of hours rests until the next regular open, so
                    # `entries_resting` and `closes_deferred` are at their most
                    # populated precisely while the market is closed. Carrying
                    # them only on `cycle_complete` would leave the field
                    # absent for the whole stretch it describes, and an absent
                    # field is what an outage looks like.
                    entries_corrected=len(recon.entries_corrected),
                    entries_resting=recon.entries_resting,
                    entries_mid_fill=recon.entries_mid_fill,
                    closes_deferred=recon.closes_deferred,
                    plan_abandoned=len(recon.plan_abandoned),
                    next_cycle_seconds=env.decision_interval_seconds,
                )
                # The durable half of the line above. `record_skipped` has no
                # parameter for a proposal count, so this pass cannot be written
                # down as one that looked and found nothing — which is the whole
                # distinction the job record exists to keep.
                jobs.record_skipped(
                    audit,
                    started_at=now,
                    interval_seconds=env.decision_interval_seconds,
                    standing=_standing(account, stand_down_state, now),
                    detail=(
                        "No enabled instrument class was in session, so the "
                        "model was not asked. Reconcile, the stand-down state, "
                        "the expiry alerts and the stop watch all still ran."
                    ),
                )
                time.sleep(env.decision_interval_seconds)
                continue

            # Below the skip, deliberately. See the note beside `fetch_market_ticks`
            # above: these two are the per-symbol cost that a cycle which will
            # not ask the model has no use for.
            indicators, no_history = fetch_indicators(broker, symbols_in_play)
            intraday, no_intraday = fetch_intraday(broker, symbols_in_play)

            headlines = news.recent_headlines(symbols_in_play)
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
                # The whole `Rules`, not only `instruments`, so the context can
                # render the SIZING CEILINGS in dollars. Without this line the
                # block is built and renders nowhere — the wired-and-inert state
                # that made the dream vault do nothing for a week.
                rules=rules,
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
                # The symbols an adopted dream permits, with the chain that
                # argued for each — verification badge and weakest hop adjacent
                # to the hops, never separated from them. Empty on every cycle
                # where nothing is adopted, which renders as no section at all
                # rather than as an empty heading.
                grants=briefing,
                now=now,
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
                # And as a job, so the pass appears in the loop's own run
                # history rather than only as an error nobody is counting.
                # `record_failed` has no parameter for a proposal count either:
                # a cycle that could not get a decision must never be readable
                # as one that decided to do nothing.
                jobs.record_failed(
                    audit,
                    started_at=now,
                    interval_seconds=env.decision_interval_seconds,
                    standing=_standing(account, stand_down_state, now),
                    detail=detail,
                )
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

            # ---------------------------------------- acting on the positions
            #
            # AFTER the proposal loop, deliberately. A close frees risk, and
            # freeing it first would let a proposal in this same cycle be gated
            # against room that a close has only just been submitted for — an
            # order at the broker is not a settled position, and the cap would
            # be counting against a reading nobody has taken. The plans manage
            # what is already open; they are not an input to what opens next.
            #
            # Two switches, and they are not the same switch:
            #
            # - `--execute` is this process's: without it the loop reaches the
            #   broker for nothing at all, and `electrum-bot loop` is documented
            #   as placing nothing. A close or a stop replace IS a broker call.
            # - `position_actions.enabled` is the operator's, ships false, and
            #   is checked INSIDE `execute_position_plan` so a call site that
            #   forgets it still fails closed. It is deliberately not read here
            #   first: letting the refusal come back from the function is what
            #   keeps that guard exercised on every cycle rather than only in a
            #   test.
            #
            # HOLD is skipped before either. A hold is not a move, it is already
            # in the audit log with the rest of the cycle, and calling through
            # for one would write a log line every fifteen minutes for every
            # open position — burying the moves that matter in the ones that are
            # not moves at all.
            actions_taken: list[str] = []
            actions_refused: list[str] = []
            for plan in decision.position_plans:
                if plan.action is PositionAction.HOLD:
                    continue
                if not execute:
                    actions_refused.append(f"{plan.symbol}:{plan.action.value}")
                    log.info(
                        "position_plan_not_executed",
                        symbol=plan.symbol,
                        action=plan.action.value,
                        reason=(
                            "--execute is off, so this cycle reaches the broker "
                            "for nothing. The plan is recorded in the audit log "
                            "and the position is untouched."
                        ),
                    )
                    continue
                # Caught broadly, and for the reason `claude.propose` and
                # `fetch_market_ticks` are. Every broker method here reports a
                # refusal rather than raising, so the exception that reaches
                # this is the unanticipated one — a locked journal, an SDK
                # change — and letting it out ends the decision loop with real
                # orders resting at the broker and nothing on screen to say the
                # bot has gone. A failed move leaves the position exactly as it
                # was, which is the safe direction, and the next cycle tries
                # again.
                try:
                    outcome = execute_position_plan(
                        plan, rules=rules, journal=journal, broker=broker
                    )
                except Exception as exc:
                    detail = f"{type(exc).__name__}: {exc}"
                    actions_refused.append(f"{plan.symbol}:{plan.action.value}")
                    log.error(
                        "position_plan_failed",
                        symbol=plan.symbol,
                        action=plan.action.value,
                        error=detail,
                    )
                    audit.record_event(
                        "position_action_failed",
                        {
                            "symbol": plan.symbol,
                            "action": plan.action.value,
                            "error": detail,
                        },
                    )
                    continue
                if outcome.ok:
                    actions_taken.append(f"{plan.symbol}:{plan.action.value}")
                else:
                    actions_refused.append(f"{plan.symbol}:{plan.action.value}")
                log.info(
                    "position_plan_executed" if outcome.ok else "position_plan_refused",
                    symbol=plan.symbol,
                    action=plan.action.value,
                    reached_broker=outcome.reached_broker,
                    risk_before_usd=outcome.risk_before_usd,
                    risk_after_usd=outcome.risk_after_usd,
                    reasons=outcome.reasons,
                    warnings=outcome.warnings,
                    detail=outcome.detail,
                )
                audit.record_event(
                    "position_action",
                    {
                        "symbol": plan.symbol,
                        "action": plan.action.value,
                        "ok": outcome.ok,
                        "reached_broker": outcome.reached_broker,
                        "reasons": outcome.reasons,
                        "warnings": outcome.warnings,
                        "detail": outcome.detail,
                    },
                )
            if actions_taken:
                # A tightened stop lowers what the position stands to lose and a
                # close removes it, so the figures reported below have to be
                # read again. Without this the cycle line states the risk the
                # account carried BEFORE the loop acted on it.
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
                    # What changed at the broker with nothing on file to say
                    # why, in full rather than as a count: the audit log is
                    # where a reader goes to find out WHICH position and by how
                    # much, and the count on the cycle line is only a pointer
                    # to it.
                    "unexplained_moves": [
                        m.describe() for m in recon.unexplained.moves
                    ],
                    "unexplained_check_ran": recon.unexplained.can_check,
                    # A resting leg that moves its own trigger by design. Not a
                    # finding, and named anyway: without it, "nothing differed"
                    # and "this one is deliberately not compared" are the same
                    # silence in the record.
                    "trailing_stops": recon.unexplained.trailing_stops,
                    "unreadable_trails": recon.unexplained.unreadable_trails,
                    # The corrections themselves, in full rather than as a
                    # count, for the reason `unexplained_moves` is: the cycle
                    # line is the pointer, this is where a reader finds WHICH
                    # entry was rewritten and from what to what.
                    "entries_corrected": [
                        c.describe() for c in recon.entries_corrected
                    ],
                    "entries_resting": recon.entries_resting,
                    "entries_mid_fill": recon.entries_mid_fill,
                    "entries_unchecked": recon.entries_unchecked,
                    "entries_ambiguous": recon.entries_ambiguous,
                    "closes_deferred": recon.closes_deferred,
                    # Why each position that closed this cycle closed — the
                    # PLAN, never the profit. `exit_review` carries no P&L
                    # figure and none is added here.
                    "exits_reviewed": [
                        f"{r.symbol}: {r.reason.value} — {r.detail}"
                        for r in recon.exits_reviewed
                    ],
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
                # A stop or a quantity that differs from the journal with no
                # recorded reason. On the cycle line for the same reason
                # `stops_breached` is: a stated zero each cycle is a fact, and
                # an absent field is what an outage looks like.
                unexplained_moves=len(recon.unexplained.moves),
                # And whether the comparison could be made at all. `[]` from a
                # failed order fetch is indistinguishable from an account with
                # nothing resting, so a zero above means nothing when this is
                # False — `FinnhubCalendar.is_degraded` again.
                unexplained_check_ran=recon.unexplained.can_check,
                # The two states that are not findings: a stop leg whose
                # trigger the broker would not report, and a position with no
                # stop resting behind it at all. Neither is "clean", and
                # neither is warned about anywhere else every cycle.
                stops_unreadable=recon.unexplained.unreadable_stops,
                positions_without_a_resting_stop=(
                    recon.unexplained.positions_without_a_resting_stop
                ),
                # A leg that sets its own trigger as price runs in its favour.
                # Its level differs from the journal's every cycle it trails,
                # and that is the exit working — so it is stated here rather
                # than counted as an unexplained move above. `unreadable_trails`
                # is the other half: trailing, with no trail size reported, so
                # where the level goes next cannot be read.
                trailing_stops=recon.unexplained.trailing_stops,
                unreadable_trails=recon.unexplained.unreadable_trails,
                # What reconcile found when it squared the journalled ENTRY
                # against what the broker actually did. Every one of these is
                # on the line for the same reason `stops_breached` is: a stated
                # zero each cycle is a fact, and an absent field is what an
                # outage looks like.
                #
                # `record_fill` writes the proposal's quantity and limit price,
                # so until a correction lands the open-risk figure describes
                # what was asked for rather than what filled. Corrected is the
                # ordinary outcome; resting and mid-fill are the two states
                # where that figure is still the proposal — and mid-fill is a
                # READING rather than an outcome, so nothing is written from it.
                entries_corrected=len(recon.entries_corrected),
                entries_resting=recon.entries_resting,
                entries_mid_fill=recon.entries_mid_fill,
                # A close withheld because the entry may still be resting. An
                # out-of-hours order rests until the next open, and without this
                # it was closed as a trade on the very next cycle.
                closes_deferred=recon.closes_deferred,
                # Closed by hand with the price at neither level: the plan
                # ABANDONED rather than completed. A count, never a P&L figure —
                # this grades the plan and `exit_review` reads no realised
                # number to grade it with.
                plan_abandoned=len(recon.plan_abandoned),
                # What the loop DID about its own position plans, and what it
                # deliberately did not. Both switches are off by default, so
                # two empty lists is the ordinary reading — and it has to be
                # stated, or "the loop is not acting on plans" and "the model
                # proposed no moves" look identical.
                position_actions_taken=actions_taken,
                position_actions_not_taken=actions_refused,
                news_windows=len(news_windows),
                # Every symbol an adopted dream is currently widening the
                # allowlist by. On the cycle line for the same reason
                # `stops_breached` is: an empty list each cycle is a stated
                # fact, and a permission in force that is never stated is a
                # permission nobody can audit.
                granted_symbols=sorted(granted_symbols),
                # And WHY the list above is empty when it is, because it has
                # five causes and only two of them are ordinary. The
                # `calendar_degraded` lesson in a fourth place: a switched-off
                # feature, nothing adopted, a store that would not open, an
                # unreadable row and a set over the cap all render as `[]`.
                # `grants_degraded` is the flag; `grant_state` is which.
                grants_degraded=grants_degraded,
                grant_state=grant_state,
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
            # The same pulse, made durable. The line above goes to the systemd
            # journal, which is rotated and which nothing in this repository can
            # read; this goes to the audit log beside the decision it belongs
            # to, so "did the loop run at 14:15 and what happened" is a query
            # rather than a grep. Written AFTER the log line on purpose: the
            # proven channel emits first, so a disk problem here cannot take the
            # heartbeat with it.
            #
            # `proposals=0` here is a real reading — the loop looked and stood
            # pat. That is what makes it different from the skip above, which
            # has no count at all.
            jobs.record_ran(
                audit,
                started_at=now,
                interval_seconds=env.decision_interval_seconds,
                standing=_standing(account, stand_down_state, datetime.now(UTC)),
                proposals=len(decision.proposals),
                approved=sum(1 for v in verdicts if v.approved),
                executed=len(executed),
            )
            time.sleep(env.decision_interval_seconds)
    except KeyboardInterrupt:
        log.info("loop_stopped_by_user")
        return 0
    finally:
        audit.record_event("loop_end", {})
        broker.disconnect()


def _describe_fusion(fusion: FusionResult | None) -> str | None:
    """One field on the dream line for a fusion, including one that was refused.

    Three states and they must not collapse into two. `None` is "the step asked
    to fuse nothing", which is most runs and is the ordinary answer. A written
    fusion names its parents and the child. A refusal names the refusals — and
    that is the state worth surfacing, because a dreamer proposing fusions a
    full workbench keeps turning down looks, from outside, exactly like a
    dreamer that stopped proposing them.
    """
    if fusion is None:
        return None
    parents = "+".join(str(p) for p in fusion.parents) or "none"
    if fusion.ok:
        return f"{parents} -> {fusion.dream_id}"
    return f"refused {parents}: {', '.join(str(r) for r in fusion.refusals) or 'no reason given'}"


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
    from .dreamer import Dreamer, promote_dreams

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
    store = DreamStore()
    # One log, read twice and written once. The dreamer reads the considerations
    # the chat surface put up and records which of them this run was shown; the
    # grading below reads the figures the decision loop recorded. Both are reads
    # of the same append-only file, and neither is authoritative over anything.
    audit = AuditLog()
    dreamer = Dreamer(env, rules, store, journal, audit=audit)
    result = dreamer.run_once(headlines=headlines, posts=posts)

    if result is None:
        # Already logged with its reason inside run_once. Nothing was written:
        # a dream that could not be had is not recorded as one that decided
        # nothing.
        #
        # The promotion step is skipped with it, deliberately. It is cheap and
        # would be harmless, but a failed model call is the one moment to change
        # nothing: the shelves are exactly as the last successful run left them,
        # and a run that moved dreams while failing to dream would be the harder
        # thing to read afterwards.
        return 1

    # **The promotion step, and the reason the vault was ever empty.** The
    # dreamer writes to the workbench and the conference reads only the vault;
    # until this ran, nothing joined them, so `confer` completed honestly with
    # `considered: 0` every single day.
    #
    # Here rather than in the decision loop, on purpose. The loop wakes every
    # fifteen minutes and proposes orders; putting a shelf that widens what may
    # be traded on that pulse would undo the separation this command exists for.
    #
    # Graded against what the LOOP recorded, never against a fresh quote. The
    # figures in the audit log are the ones a decision was actually made on, and
    # re-fetching would score a condition against a number nobody saw — the same
    # reason `news_history` reads the record instead of the feed. It also means
    # this command needs no broker and no market data credentials.
    readings: list[CycleReadings] = []
    try:
        readings = recent_readings(audit.read(limit=CONDITION_CYCLES, days=14))
    except Exception as exc:
        # Costs the grading and nothing else. A prophecy that could not be
        # checked stays a prophecy, which is the direction to fail in: the
        # alternative would be promoting on an incomplete reading of the record.
        log.warning(
            "dream_condition_readings_unavailable",
            error=f"{type(exc).__name__}: {exc}",
            detail="Prophecy conditions are not graded this run; nothing is promoted on them.",
        )

    promotion = promote_dreams(
        store, readings=readings, caps=rules.dreaming.vault_caps()
    )

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
        # What the chat surface put to the dreamer and this run was shown. On
        # the line for the same reason `stops_breached` is on the cycle line: a
        # zero every run is a stated fact, and the absence of a line is what an
        # outage looks like too. `None` is a THIRD state and not a zero — the
        # log could not be read at all, so nobody looked.
        considerations_shown=(
            len(result.considerations.considerations) if result.considerations else None
        ),
        # And whether that count can be believed. An unreadable log produces the
        # same zero as a quiet week; the `calendar_degraded` lesson again.
        considerations_record_incomplete=(
            result.considerations.is_degraded if result.considerations else None
        ),
        # What happened when the step asked to combine two chains, INCLUDING a
        # refusal. It reached `DreamerResult` and stopped there, so a dreamer
        # repeatedly proposing fusions the store keeps refusing was a fact
        # nothing surfaced — which is exactly the silence `scope` was given a
        # log line to avoid.
        fusion=_describe_fusion(result.fusion),
        cost_usd=round(result.usage.estimated_cost_usd, 4) if result.usage else None,
        # On the same line as the dream itself, so one log entry answers both
        # "did it think" and "did anything move". A zero here beside a healthy
        # dream is the signature of the bug this closed, and it should be
        # visible rather than inferred from an empty vault days later.
        dreams_considered=promotion.considered,
        dreams_promoted=[f"{i} -> {v}" for i, v in promotion.promoted],
        conditions_fulfilled=promotion.conditions_fulfilled,
        cycles_available_to_grade=promotion.cycles_available,
    )
    return 0


# How many recorded cycles a prophecy's conditions are checked against.
#
# 400 is a little over four days of a 96-cycle day. A prophecy runs for up to a
# year, so this is not the horizon — `grade_conditions` deliberately has none,
# and the shelf's own TTL retires a claim that never fires. It is a bound on how
# much of the audit log one run reads, and the command runs daily, so a
# condition that fires is seen on the run after it fires.
CONDITION_CYCLES = 400


def recent_readings(view: AuditView) -> list[CycleReadings]:
    """The numeric figures the loop recorded, oldest first, for grading.

    Pure. `AuditLog.read` hands back the newest decision first and
    `grade_conditions` needs ascending order, because the FIRST cycle meeting a
    condition is the one recorded as having fired it — reversed, every condition
    would be stamped with the most recent moment it held rather than the moment
    it became true.

    A cycle with no `readings` is skipped rather than contributed as an empty
    one. `MarketInputs.readings` could not be backfilled: cycles recorded before
    it shipped carry prose about figures and no figures, and counting those as
    cycles-with-no-reading would make `can_grade_anything` claim evidence that
    is not there.
    """
    out: list[CycleReadings] = []
    for entry in view.decisions:
        inputs = entry.decision.inputs
        if inputs is None or not inputs.readings:
            continue
        out.append(
            CycleReadings(
                at=entry.timestamp,
                readings=dict(inputs.readings),
                proposed_symbols={p.symbol for p in entry.decision.proposals},
            )
        )
    out.sort(key=lambda c: c.at)
    return out


# The `kind` an expiry mark carries in a dream's transcript.
#
# Not one of `dreaming.MESSAGE_KINDS`, and that is allowed on purpose: the store
# deliberately does not validate the field, because an unfamiliar kind costs a
# page a badge while refusing it would lose the turn. It is its own kind rather
# than a `note` so that `vault-expire` can find its own previous mark and stay
# idempotent — matching on prose would break the moment the sentence was
# reworded.
EXPIRY_MARK_KIND = "expired"


# How close to lapsing counts as "expiring soon" on the vault readout.
#
# Ten days, the same notice period as the Tailscale banner and for the same
# reason: the failure is notice followed by a loss of capability, and the notice
# period is the only stretch in which anything can be done about it.
#
# `mcp_server.GRANT_WARNING_DAYS` is the same number for the same reason on the
# tool surface. Neither module may import the other at module scope — one pulls
# in the broker SDK and the other the MCP SDK — so if a third caller ever wants
# it, it belongs in `dreaming.py` beside the TTLs rather than in a third copy.
GRANT_WARNING_DAYS = 10.0


def _symbols_line(granted: dict[str, str]) -> str:
    """Symbols and their class keys on one line, or an explicit `none`.

    `none` rather than an empty string, for the reason every readout in this
    repository says a zero out loud: a blank line reads as a rendering fault,
    and an empty permission set is a fact worth stating plainly.
    """
    if not granted:
        return "none"
    return ", ".join(f"{s} ({c})" for s, c in sorted(granted.items()))


def cmd_vault(rules: Rules) -> int:
    """Print the dream vaults: what is on each shelf, what is granted, what lapses.

    **Read-only.** It opens the store, prints, and returns. Nothing moves, no
    dream is marked, no grant is withdrawn and no model is called, so it is safe
    to run at any moment — including while the loop is trading and while a
    conference is in progress.

    Takes no `Env` because it needs nothing from one: no broker, no API key, no
    credentials of any kind. A local SQLite read that demanded Alpaca keys would
    be a check protecting nothing.

    Printed as well as logged. `cmd_loop` and `cmd_dream` are read out of
    `journalctl` and log only; this is a command a person types, so the answer
    goes to the terminal in a shape a person can read and the structured line
    goes to the log, as every other command here emits one.
    """
    now = datetime.now(UTC)
    store = DreamStore()
    caps = rules.dreaming.vault_caps()
    ttls = rules.dreaming.vault_ttls()
    counts = store.counts_by_vault()

    print(f"Dream vaults as at {now.isoformat(timespec='minutes')}")
    print()

    for shelf in Vault:
        cap = caps.limit_for(shelf)
        ttl = ttls.days_for(shelf)
        held = counts.get(shelf, 0)
        cap_text = f"{held}/{cap}" if cap is not None else f"{held} (uncapped)"
        ttl_text = f"{ttl}d" if ttl is not None else "never expires"
        print(f"  {shelf!s:<10} {cap_text:<14} ttl {ttl_text}")
        # Said out loud rather than left as a blank space under the heading. An
        # empty shelf is an ordinary state, an unreadable store is not, and the
        # two look identical if nothing at all is printed.
        if held == 0:
            print("      (empty)")
        for dream in store.in_vault(shelf, limit=50):
            days_here = (now - dream.vault_entered_at).total_seconds() / 86400.0
            left = f"{ttl - days_here:.0f}d left" if ttl is not None else "no expiry"
            print(f"      #{dream.id} {dream.title}")
            print(
                f"          {dream.stage} · {dream.verification} · "
                f"{len(dream.chain)} hop(s), {len(dream.unverified_hops)} unchecked · "
                f"{days_here:.1f}d on this shelf · {left}"
            )
            if dream.symbols:
                klass = dream.asset_class_key or "class unresolved"
                print(f"          symbols {', '.join(dream.symbols)} ({klass})")
    print()

    # What the store thinks it granted, and what the rules will actually honour.
    # The second is a subset of the first — a disabled class grants nothing, an
    # already-listed symbol is not a grant at all — and printing only one of
    # them would either overstate the permission or hide a dream asking for
    # something the account has switched off.
    claimed = store.granted_symbols(now)
    in_force = resolve_granted_symbols(store, rules, now=now)
    print(f"  Claimed by live adoptions: {_symbols_line(claimed)}")
    print(f"  Actually in force:         {_symbols_line(in_force)}")
    if not rules.dreaming.allow_symbol_grants:
        print("  (dreaming.allow_symbol_grants is false, so nothing is in force)")

    expiring: list[tuple[int, list[str], float, datetime]] = []
    for adoption in store.adoptions():
        expires = adoption.expires_at
        if not adoption.is_live(now) or expires is None:
            continue
        days = (expires - now).total_seconds() / 86400.0
        if days <= GRANT_WARNING_DAYS:
            expiring.append((adoption.dream_id, list(adoption.symbols_granted), days, expires))

    print()
    if expiring:
        print(f"  Grants lapsing within {GRANT_WARNING_DAYS:.0f} days:")
        for dream_id, symbols, days, expires in expiring:
            named = ", ".join(symbols) or "no symbols"
            print(
                f"      dream #{dream_id} {named} in {days:.1f}d "
                f"({expires.isoformat(timespec='minutes')})"
            )
    else:
        print(f"  No grant lapses within {GRANT_WARNING_DAYS:.0f} days.")

    # Sitting in ADOPTED with nothing live behind it. Named out loud because
    # this is the `dream-expired-holding` state and silence about it is the
    # worst available answer: the shelf still shows the dream, its own TTL still
    # has months to run, and the permission it implies died some time ago. A
    # reader given only the shelf count would take the grant to be in force.
    lapsed = [
        dream
        for dream in store.in_vault(Vault.ADOPTED, limit=50)
        if dream.id is not None
        and not any(a.is_live(now) for a in store.adoptions(dream.id))
    ]
    if lapsed:
        print()
        print("  Adopted, with a grant that has ALREADY lapsed:")
        for dream in lapsed:
            print(f"      #{dream.id} {dream.title}")
        print("      No new entries are permitted in those symbols. Any position")
        print("      already open stands. `electrum-bot vault-expire` hands them")
        print("      back to the vault and records why.")

    expired = store.expired(now, ttls)
    if expired:
        print()
        print(f"  {len(expired)} dream(s) are past their shelf's TTL and nothing has")
        print("  been marked. `electrum-bot vault-expire` withdraws lapsed grants.")

    log.info(
        "vault_listed",
        counts={str(v): counts.get(v, 0) for v in Vault},
        granted_symbols=sorted(in_force),
        claimed_symbols=sorted(claimed),
        grants_expiring_soon=len(expiring),
        grants_already_lapsed=len(lapsed),
        past_ttl=len(expired),
    )
    return 0


def cmd_confer(env: Env, rules: Rules) -> int:
    """One capped agent-to-agent conference over the dreams in the vault.

    **Deliberately not reachable from `cmd_loop`, and that is a command boundary
    rather than a comment.** The loop wakes every fifteen minutes and proposes
    orders; this decides whether a second-order hypothesis is worth a symbol
    permission. Ninety-six unattended negotiations a day on the same process
    that places orders is the Alpha Arena failure shape with two models instead
    of one. It belongs on the dream timer, a day apart, which is far slower than
    a price moves and about the right speed for the question being asked.

    Every cap lives in `confer.py` — six turns per exchange, two dreams per run,
    three exchanges per dream for its lifetime, the prose cap, and the change
    gate that stops the two of them re-litigating an unchanged dream every day
    forever. Nothing here may raise one.

    Exits non-zero only on a HARD failure: every dream that cost a model call
    came back `CALL_FAILED`. A quiet run is a success — an empty vault, or two
    dreams skipped because nothing has changed since the last exchange, is the
    arrangement working — and reporting that as a failed unit every morning is
    how an operator learns to ignore `systemctl --failed`.
    """
    from .confer import ConferOutcome, run_conference

    if not env.anthropic_api_key:
        log.error("confer_no_api_key", detail="ANTHROPIC_API_KEY is not set")
        return 1

    store = DreamStore()
    report = run_conference(env, rules, store, caps=rules.dreaming.vault_caps())

    failed = [e for e in report.exchanges if e.outcome is ConferOutcome.CALL_FAILED]
    log.info(
        "confer_complete",
        # Separate from `len(exchanges)` on purpose: an empty report because the
        # vault is empty and an empty report because everything in it was
        # skipped are opposite findings that produce the same list.
        considered=report.considered,
        conferred=report.conferred,
        skipped=report.skipped,
        adopted=report.adopted,
        calls=report.calls,
        failed=len(failed),
        cost_usd=round(report.cost_usd, 4),
    )

    if report.conferred and len(failed) == report.conferred:
        log.error(
            "confer_all_calls_failed",
            conferred=report.conferred,
            detail=(
                "Every exchange that reached the model failed. The transcripts "
                "record it and nothing was adopted. Exiting non-zero so the "
                "timer surfaces it in systemctl --failed."
            ),
        )
        return 1
    return 0


def _observation_worklist(store: DreamStore, now: datetime) -> list[ObservationDue]:
    """Every unanswered observation on every shelf, oldest review date first.

    **Every shelf, not only the two where an answer moves something.** An
    observation matters most on the workbench and on the prophecy shelf, since
    those are where an answer promotes a dream — but an operator asking "what
    is waiting on me" and being shown a filtered list would be told nothing is
    waiting while questions sat unanswered somewhere else. The shelf is printed
    beside each row instead, which is information rather than a silent drop.
    """
    dreams: list[Dream] = []
    for shelf in Vault:
        dreams.extend(store.in_vault(shelf, limit=200))
    return pending_observations(dreams, now=now)


def _answered_observation(
    store: DreamStore, handle: str, now: datetime
) -> tuple[int, DreamCondition] | None:
    """The observation this handle names, if somebody has already answered it.

    `pending_observations` deliberately drops answered claims, which is right
    for a worklist and leaves `settle` unable to tell "you answered this on
    Tuesday" from "that claim is gone". This walks the same shelves and rebuilds
    the same handle over the answered ones, so the two can be reported apart.
    """
    for shelf in Vault:
        for dream in store.in_vault(shelf, limit=200):
            for condition in dream.conditions:
                if not condition.is_observable or not condition.is_answered:
                    continue
                if observation_handle(int(dream.id or 0), condition) == handle:
                    return int(dream.id or 0), condition
    return None


def cmd_observations() -> int:
    """What is waiting on a PERSON, and nothing else in this repository asks it.

    An observation is the half of a prophecy that fails silently. A threshold is
    graded by code on every dream run whether or not anybody is watching; an
    observation is graded by somebody looking at the world, and somebody who is
    never asked never looks. Without this command the prophecy shelf becomes a
    place dreams go to wait forever while every surface reports patience.

    **Read-only, and needs no credential of any kind.** A local SQLite read that
    demanded Alpaca keys would be a check protecting nothing.

    Each row carries a HANDLE — six hex characters derived from the dream id and
    the claim — which is what `settle` takes. It is derived rather than stored
    precisely so that it changes if the claim changes: a dreamer that restates
    its conditions between somebody reading this list and answering it produces
    a different handle, so the answer lands nowhere rather than landing on a
    claim nobody was shown.
    """
    now = datetime.now(UTC)
    store = DreamStore()
    due = _observation_worklist(store, now)

    print(f"Observations awaiting an answer, as at {now.isoformat(timespec='minutes')}")
    print()
    if not due:
        # Said out loud. An empty worklist is an ordinary state and an
        # unreadable store is not, and the two look identical if nothing at all
        # is printed.
        print("  Nothing is waiting on you.")
        print()
        print(
            "  That means no dream carries an unanswered observation — not "
            "that no dream is stuck. A dream can also be held up by a "
            "threshold the market has not reached, which nobody can settle by "
            "looking. `electrum-bot vault` shows the shelves."
        )
        return 0

    overdue = [item for item in due if item.is_overdue]
    for item in due:
        condition = item.condition
        when = (
            condition.observe_by.strftime("%d %b %Y")
            if condition.observe_by
            else "no date"
        )
        flag = "OVERDUE" if item.is_overdue else f"by {when}"
        print(f"  [{item.handle}] #{item.dream_id} {item.title}")
        print(f"      {condition.text}")
        print(f"      Look at {condition.subject} — {condition.observable}")
        print(f"      {flag}" + (f" (review date {when})" if item.is_overdue else ""))
        print()

    print(f"  {len(due)} awaiting, {len(overdue)} overdue.")
    print()
    print("  Answer one with:")
    print('    electrum-bot settle <handle> --met --note "what you saw"')
    print('    electrum-bot settle <handle> --ruled-out --note "what you saw"')
    print()
    print(
        "  Both are real answers and neither is reversible. `--ruled-out` "
        "records that the claim did NOT hold, which is a different fact from "
        "nobody having looked."
    )
    log.info("observations_listed", awaiting=len(due), overdue=len(overdue))
    return 0


def cmd_settle(handle: str | None, *, met: bool | None, note: str) -> int:
    """Record the operator's answer to one observation. The only writer of one.

    **This is a write with teeth, and the chain it sits on is worth stating.** A
    fulfilled condition can carry a dream to the VAULT; a vaulted dream is what
    an adoption is taken from; an adoption is a live permission to trade a
    symbol that is not in `config/rules.yaml`. So this command is one link in
    the route that widens what may be traded — which is why it is a command a
    person types on the box rather than a control on a password-gated web page,
    and why `DreamStore.settle_condition` refuses every actor but the operator.

    Every gate still runs on anything traded under a grant. What this changes is
    the allowlist, not the limits.

    The handle is looked up in the CURRENT worklist rather than decoded, so a
    handle for a claim the dreamer has since restated matches nothing and is
    refused. That is the honest outcome: the claim you were shown is no longer
    on this dream.
    """
    if not handle:
        print("Which observation? Run `electrum-bot observations` for the list.")
        return 2
    if met is None:
        # Refused rather than defaulted. A default of `--met` would manufacture
        # confirmations, and a default of `--ruled-out` would refute claims
        # nobody meant to refute; there is no safe guess between them.
        print("Say which answer: --met or --ruled-out. There is no default.")
        return 2

    now = datetime.now(UTC)
    store = DreamStore()
    wanted = handle.strip().lower()
    match = next(
        (item for item in _observation_worklist(store, now) if item.handle == wanted),
        None,
    )
    if match is None:
        # Two different reasons a handle misses, and they are worth telling
        # apart. "You answered this on Tuesday" and "the claim you were shown
        # is no longer on this dream" call for opposite next actions, and a
        # single message covering both would leave the operator guessing which
        # they were looking at — the missing-versus-answered rule arriving at a
        # terminal.
        answered = _answered_observation(store, wanted, now)
        if answered is not None:
            item, condition = answered
            state = "met" if condition.fulfilled else "ruled out"
            who = condition.observed_by or "somebody"
            when = (
                condition.fulfilled_at.isoformat(timespec="minutes")
                if condition.fulfilled_at
                else "an unrecorded moment"
            )
            print(f"Already answered: {state}, by {who} at {when}.")
            print(f"  #{item} {condition.text}")
            print()
            print(
                "  Neither answer is reversible here. A claim that turned out "
                "differently is a NEW claim — the dreamer writes it, with its "
                "own date — rather than an old one quietly reopened."
            )
            return 1

        print(f"No observation with handle {wanted!r} is awaiting an answer.")
        print()
        print(
            "  Nothing on any shelf carries that claim. The handle is derived "
            "from the claim, so a dreamer that reworded the subject or the "
            "observable produced a NEW claim with a new handle. Re-run "
            "`electrum-bot observations`."
        )
        return 1

    result = store.settle_condition(
        match.dream_id,
        match.condition.key,
        by=OPERATOR,
        met=met,
        note=note,
        at=now,
    )
    if not result.ok:
        print(f"Refused: {result.detail}")
        for refusal in result.refusals:
            print(f"  - {refusal.value}")
        log.warning(
            "observation_settle_refused",
            dream_id=match.dream_id,
            handle=wanted,
            refusals=[r.value for r in result.refusals],
        )
        return 1

    answer = "MET" if met else "RULED OUT"
    print(f"#{match.dream_id} {match.title}")
    print(f"  {match.condition.text}")
    print(f"  Recorded {answer} at {now.isoformat(timespec='minutes')}.")
    print()
    # What happens NEXT is the thing an operator wants and cannot see from
    # here. Promotion runs on `electrum-bot dream`, not on this command, and
    # saying so beats leaving somebody to wonder why the shelf did not move.
    print(
        "  This does not promote anything on its own. `electrum-bot dream` "
        "runs the promotion rule, and a prophecy moves to the vault only once "
        "EVERY condition on it is answered."
    )
    log.info(
        "observation_settled",
        dream_id=match.dream_id,
        handle=wanted,
        met=met,
        state=ConditionState.MET.value if met else ConditionState.RULED_OUT.value,
    )
    return 0


def cmd_vault_expire(rules: Rules) -> int:
    """Mark dreams past their shelf's TTL, and withdraw the grants that lapsed.

    **Marks; never deletes.** Nothing is removed and no reasoning is destroyed.
    An expired dream on the workbench keeps its chain, its thoughts and its
    transcript and gains one line saying it aged out — the same shape as
    `stop_watch`, which reports a breached stop and does not close the position.
    A command that quietly archived a chain somebody was halfway through would
    be the worse of the two by a distance.

    For an ADOPTED dream it does the one thing with teeth: it hands the dream
    back to the vault with a stated reason, which closes the adoption row and
    **withdraws the symbol permission**. That is the point of a time-boxed grant
    and it is the only write here that changes what may be traded.

    **It never closes a position.** One opened under a grant that has just
    lapsed stands: expiry withdraws the right to OPEN, and an unattended
    auto-close at 3am is a new execution path nobody chose. The Board's
    `dream-expired-holding` tag is what makes that state visible instead.

    Idempotent by design, because this is the kind of command that ends up on a
    timer. The mark is written once per stay on a shelf — `vault_entered_at` is
    reset by every move, so a note recorded after it belongs to the current stay
    — and a second run the same day writes nothing and reports nothing new.
    """
    # `CONFERENCE` is the machine narrating itself, and it is the right speaker
    # for this note rather than `OPERATOR`. An operator note is one of the five
    # things that makes a dream worth conferring again, so a machine note
    # wearing the operator's name would trip `has_something_changed` on its own
    # writing and hand the two agents a fresh exchange about a dream whose only
    # news is that it got old.
    from .confer import CONFERENCE

    now = datetime.now(UTC)
    store = DreamStore()
    ttls = rules.dreaming.vault_ttls()

    # Two clocks, and both have to be read. `expired` measures a dream against
    # its shelf's TTL; an adoption carries its own `expires_at`, which is what
    # `granted_symbols` actually honours. They are set from the same moment
    # today, so they normally agree — but an adoption taken with a shorter TTL
    # would lapse while the dream is still inside its shelf's window, and a
    # grant that is already dead by arithmetic must not leave a dream sitting in
    # ADOPTED as though it were live.
    stale: dict[int, Dream] = {}
    for dream in store.expired(now, ttls):
        if dream.id is not None:
            stale[dream.id] = dream
    for dream in store.in_vault(Vault.ADOPTED, limit=1000):
        if dream.id is None:
            continue
        if not any(a.is_live(now) for a in store.adoptions(dream.id)):
            stale[dream.id] = dream

    withdrawn: list[tuple[int, list[str]]] = []
    marked: list[int] = []
    refused: list[str] = []

    for dream_id, dream in sorted(stale.items()):
        if dream.vault is Vault.ADOPTED:
            lost = sorted(
                {
                    symbol
                    for adoption in store.adoptions(dream_id)
                    if adoption.returned_at is None
                    for symbol in adoption.symbols_granted
                }
            )
            days = (now - dream.vault_entered_at).total_seconds() / 86400.0
            result = store.return_to_vault(
                dream_id,
                reason=(
                    f"The adoption expired after {days:.0f} days. The symbol "
                    "permission is withdrawn; any position still open under it "
                    "stands and is untouched."
                ),
                at=now,
            )
            if not result.ok:
                refused.append(f"#{dream_id}: {result.detail}")
                continue
            withdrawn.append((dream_id, lost))
            log.warning(
                "dream_grant_expired",
                dream_id=dream_id,
                title=dream.title,
                symbols=lost,
                days_adopted=round(days, 1),
                detail=(
                    "No new entries are permitted in these symbols. An open "
                    "position is NOT closed by this and remains yours to manage."
                ),
            )
            continue

        # Everything else is marked and left exactly where it is.
        if any(
            m.kind == EXPIRY_MARK_KIND and m.at >= dream.vault_entered_at
            for m in store.messages(dream_id)
        ):
            continue
        days = (now - dream.vault_entered_at).total_seconds() / 86400.0
        store.add_message(
            dream_id,
            speaker=CONFERENCE,
            kind=EXPIRY_MARK_KIND,
            text=(
                f"Past the {ttls.days_for(dream.vault)}-day limit for "
                f"{dream.vault}: {days:.0f} days on this shelf. Nothing has been "
                "deleted or moved — the dreamer decides whether to rework it, "
                "offer it or retire it."
            ),
            at=now,
        )
        marked.append(dream_id)

    lost_symbols = sorted({s for _, symbols in withdrawn for s in symbols})
    log.info(
        "vault_expire_complete",
        past_ttl=len(stale),
        grants_withdrawn=len(withdrawn),
        symbols_no_longer_granted=lost_symbols,
        newly_marked=len(marked),
        refused=refused,
    )

    print(f"{len(stale)} dream(s) past their shelf's TTL.")
    print(f"{len(marked)} newly marked; nothing was deleted or moved.")
    if withdrawn:
        for dream_id, symbols in withdrawn:
            named = ", ".join(symbols) or "no symbols"
            print(f"  grant withdrawn: dream #{dream_id} — {named}")
        print("  Those symbols are no longer granted. Any open position is untouched.")
    else:
        print("No grant lapsed.")
    for problem in refused:
        print(f"  could not expire {problem}")

    # A refusal is a write this command intended and did not make, which is
    # exactly what a timer should surface. An ordinary quiet run is still 0.
    return 1 if refused else 0


def cmd_settings_apply(
    target: str | None,
    rules_path: Path,
    *,
    dry_run: bool = False,
    revert: bool = False,
    requests_path: Path | None = None,
) -> int:
    """Apply — or revert — a change the settings agent argued. **As root.**

    The privileged half of the settings agent. `config/` is owned by root on the
    droplet so the service account cannot edit its own limits, and the dashboard
    runs as the service account; this command is the only thing that writes
    `config/rules.yaml`, and it is reached two ways:

    - a person at a root shell, which is this file's original audience
    - the Armorer, through `deploy/apply-settings.sh` — root-owned, named in a
      sudoers rule, taking no arguments, with the id on stdin

    Both run the same code, and the second is why the wrapper exists rather than
    a sudoers rule naming this binary: a rule on `electrum-bot` would permit
    every subcommand it has and every one a future release adds, chosen by
    whoever is signed in to the dashboard.

    **Nothing here checks for root**, deliberately. An unprivileged run fails on
    the write with a permission error from the operating system, which is a
    guarantee; a `geteuid` check in Python is a comment somebody can delete. The
    same reasoning as the user split behind the chat wrapper — Unix permissions
    do not fail quietly, and config trimming is always the second lock.

    With no id it lists what is pending, because the alternative is an operator
    guessing at a number, and a wrong guess here edits a risk limit. It exits
    non-zero in that case so a script cannot read "nothing applied" as success.
    """
    from .settings_agent import (
        SettingsRequestStore,
        apply_request,
        commit_message,
        revert_request,
    )

    # Parsed BEFORE the store is opened, which is not tidiness: opening it
    # creates `data/settings_requests.db`, and a typo at the shell should not
    # bring a database into existence on a box that has never used the feature.
    request_id: int | None = None
    if target:
        try:
            request_id = int(target)
        except ValueError:
            print(f"'{target}' is not a request id. Run with no id to list them.")
            return 2

    store = (
        SettingsRequestStore(requests_path)
        if requests_path is not None
        else SettingsRequestStore()
    )
    if request_id is None:
        # A revert with no id lists what there is to undo, which is a different
        # list from what is waiting to be applied. Offering the pending ones
        # here would invite reverting an id that never moved the file.
        wanted = "applied" if revert else "pending"
        listed = store.recent(limit=25, status=wanted)
        if not listed:
            print(f"No {wanted} settings requests.")
            return 1
        print(f"{wanted.capitalize()} settings requests:")
        for req in listed:
            print(
                f"  {req.id}: {req.key} {req.old_value} -> {req.new_value} "
                f"({req.stance}, argued {req.created_at:%Y-%m-%d %H:%M} UTC)"
            )
            print(f"      because: {req.reason or 'no reason recorded'}")
            if req.objection.strip():
                print(f"      objection: {req.objection}")
            if req.applied_at is not None:
                print(
                    f"      applied {req.applied_at:%Y-%m-%d %H:%M} UTC"
                    + (f" by {req.applied_by}" if req.applied_by else "")
                )
        verb = "settings-revert" if revert else "settings-apply"
        print(f"\n{'Revert' if revert else 'Apply'} one with: electrum-bot {verb} <id>")
        return 1

    request = store.get(request_id)
    run = revert_request if revert else apply_request
    result = run(store, request_id, rules_path=rules_path, dry_run=dry_run)
    if result.diff:
        print(result.diff)
    print(result.detail)
    if result.ok and request is not None and not dry_run and not revert:
        # The commit message goes to stdout rather than into a file, because
        # `CLAUDE.md` is explicit that a limit changes in its own commit with a
        # reason — and the reason, along with the objection the operator
        # overruled, is already recorded. Printing it means the commit that
        # lands carries the argument rather than "tweak config".
        print("\nCommit it with:\n")
        print(commit_message(request))
    return 0 if result.ok else 1


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


def cmd_jobs(*, hours: float, at: str | None) -> int:
    """Print the loop's recorded passes, or say what it was doing at one moment.

    The command that replaces `journalctl | grep cycle_complete`, and it can
    answer two things that grep never could: a pass that was SKIPPED because
    the market was shut, and a pass that FAILED to get a decision. Neither ever
    reached the audit log before, so both were invisible to everything except
    the systemd journal.

    Reads the log and nothing else. No broker, no credential, no network.
    """
    audit = AuditLog()
    result = jobs.read(audit, hours=hours)

    for line in jobs.render(result):
        print(line)

    if at is not None:
        try:
            moment = datetime.fromisoformat(at)
        except ValueError:
            print(f"\nCould not read '{at}' as a time. Use ISO-8601, e.g. 2026-08-11T14:15.")
            return 2
        print()
        print(result.at(moment).describe())
        return 0

    if not result.has_jobs:
        return 0

    print()
    for job in result.jobs:
        counts = (
            f"{job.proposals} proposed, {job.approved} approved, {job.executed} executed"
            if job.decided
            # Never a zero. This pass did not look, so there is nothing to count.
            else job.detail.split(".")[0]
        )
        print(
            f"{job.started_at.isoformat(timespec='seconds')}  "
            f"{job.outcome.value:<7}  {counts}"
        )
    return 0


def main() -> int:
    structlog.configure(processors=[structlog.processors.JSONRenderer()])
    parser = argparse.ArgumentParser(prog="electrum-bot")
    parser.add_argument(
        "command",
        choices=[
            "smoketest",
            "loop",
            "dream",
            "confer",
            "vault",
            "vault-expire",
            "observations",
            "settle",
            "jobs",
            "reindex",
            "settings-apply",
            "settings-revert",
        ],
        help=(
            "smoketest: one-shot connectivity check. loop: run the decision "
            "loop. dream: one lateral-thinking step, written to data/dreams.db "
            "and shown on the Dreaming page, which places nothing ever. "
            "confer: one capped agent-to-agent conference over the dreams in "
            "the vault, which can grant a symbol permission and can place "
            "nothing. vault: print the dream vaults, their caps, the live "
            "grants and anything expiring — read-only. vault-expire: mark "
            "dreams past their shelf's TTL and withdraw the grants that "
            "lapsed; it marks and never deletes, and never closes a position. "
            "observations: what is waiting on a PERSON — the half of a "
            "prophecy no figure the loop records can settle — read-only. "
            "settle: record your answer to one of them, by handle, with a "
            "reason; it is the only writer of one and neither answer is "
            "reversible. "
            "jobs: what the loop actually did, pass by pass, out of the audit "
            "log — including the passes that were SKIPPED with the market shut "
            "and the ones that FAILED to get a decision, neither of which is a "
            "cycle that ran and stood pat. "
            "reindex: rebuild the searchable history from audit/. "
            "settings-apply: apply a change the settings agent argued — RUN AS "
            "ROOT, because config/ is root-owned so the service account cannot "
            "edit its own limits. settings-revert: put the previous value back, "
            "through the same validation. With no id either one lists what it "
            "could act on."
        ),
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help=(
            "The settings request id, for settings-apply and settings-revert; "
            "the observation handle, for settle. Ignored by every other "
            "command."
        ),
    )
    parser.add_argument(
        "--met",
        dest="settled",
        action="store_const",
        const=True,
        default=None,
        help=(
            "settle only: the observation held. There is deliberately no "
            "default — a default of --met would manufacture confirmations."
        ),
    )
    parser.add_argument(
        "--ruled-out",
        dest="settled",
        action="store_const",
        const=False,
        help=(
            "settle only: the observation did NOT hold. A real answer, and a "
            "different fact from nobody having looked."
        ),
    )
    parser.add_argument(
        "--note",
        default="",
        help=(
            "settle only: what you saw, in your own words. Required — it is "
            "the only record of what settled the claim, and an answer with "
            "nothing behind it cannot afterwards be told from a mis-click."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "settings-apply and settings-revert only: show the diff and prove "
            "the edited file loads, then write nothing."
        ),
    )
    parser.add_argument(
        "--requests",
        default=None,
        help=(
            "Path to the settings request store (default: "
            "data/settings_requests.db, relative to the working directory). "
            "settings-apply and settings-revert only."
        ),
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=jobs.DEFAULT_WINDOW_HOURS,
        help=(
            f"jobs only: how far back to read, in hours (default: "
            f"{jobs.DEFAULT_WINDOW_HOURS:g})."
        ),
    )
    parser.add_argument(
        "--at",
        default=None,
        help=(
            "jobs only: an ISO-8601 moment to ask about, e.g. 2026-08-11T14:15. "
            "Answers what the loop was doing then — including that it recorded "
            "nothing, which is its own state and not a quiet cycle."
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

    # Before the credential check for the same reason, and with one of its own.
    # This reads dated files already on disk and touches no broker, so gating it
    # behind an Alpaca key would protect nothing — and the moment somebody most
    # wants to ask "was the loop running at 14:15" is a moment when the loop is
    # not running, which is frequently a moment when its credentials are the
    # thing that broke.
    #
    # It is also dispatched HERE rather than at the foot with the other
    # read-only commands, because the foot of this function falls through to
    # `cmd_loop`: a command in `choices` with no branch of its own does not
    # error, it silently starts the decision loop.
    if args.command == "jobs":
        return cmd_jobs(hours=args.hours, at=args.at)

    # Before the credential check, and dispatched here rather than at the foot
    # for the same two reasons as `jobs`: neither touches a broker, and the foot
    # of this function falls through to `cmd_loop`, so a command in `choices`
    # with no branch of its own silently starts the decision loop.
    #
    # `settle` writes, but what it writes is an answer about the world, and the
    # question of whether Alpaca is reachable has nothing to say about whether
    # the barge draft restriction happened.
    if args.command == "observations":
        return cmd_observations()
    if args.command == "settle":
        return cmd_settle(args.target, met=args.settled, note=args.note)

    # Also before the credential check, and for a stronger reason than reindex's.
    # This is meant to be run as root on a box that may be halfway through a
    # problem — Alpaca unreachable, a key rotated out, the loop refusing to
    # start. Tightening a limit is exactly what somebody would want to do at
    # that moment, and gating it behind a broker credential it never touches
    # would refuse the operator the one safe action available to them.
    #
    # It cannot loosen anything on its own: the request it applies was argued,
    # confirmed and recorded already, and the file is re-validated through
    # `Rules.load` before it is replaced.
    #
    # The revert path is here for a stronger version of the same reason. Undoing
    # a limit that was moved under pressure is the most time-critical safe action
    # this CLI has, and refusing it because a broker key is missing would withhold
    # it at precisely the moment it is wanted.
    if args.command in {"settings-apply", "settings-revert"}:
        return cmd_settings_apply(
            args.target,
            Path(args.rules),
            dry_run=args.dry_run,
            revert=args.command == "settings-revert",
            requests_path=Path(args.requests) if args.requests else None,
        )

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
    if args.command == "confer":
        return cmd_confer(env, rules)
    # Neither of these needs an `Env` at all — no broker, no key, no credential
    # — but both run after the paper-only check anyway. A box configured for
    # live trading must refuse everything, and a read of the vaults is not the
    # place to make the one exception.
    if args.command == "vault":
        return cmd_vault(rules)
    if args.command == "vault-expire":
        return cmd_vault_expire(rules)
    return cmd_loop(env, rules, execute=args.execute, force_mock=args.mock)


if __name__ == "__main__":
    sys.exit(main())
