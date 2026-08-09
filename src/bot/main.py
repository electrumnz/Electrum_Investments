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

from .audit import AuditLog
from .broker import AlpacaBroker, Broker, MockBroker
from .claude_client import ClaudeClient, build_system_prompt
from .config import Env, LiveTradingRefused, Rules
from .context import build_market_context, fetch_market_ticks
from .data.calendar import CalendarFeed, EmptyCalendar
from .data.finnhub import FinnhubCalendar
from .data.marketaux import MarketauxNews
from .data.news import EmptyNews, NewsFeed
from .journal import Journal
from .models import Decision
from .options import alerts_for_positions
from .reconcile import apply_journal_state, reconcile, record_fill
from .risk import RiskGate

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


def cmd_smoketest(env: Env, rules: Rules, *, force_mock: bool = False) -> int:
    """Connect, print account state, ask Claude one question. Never places an order."""
    audit = AuditLog()
    broker = build_broker(env, force_mock=force_mock)
    broker.connect()
    try:
        account = broker.get_account()
        ticks = fetch_market_ticks(broker, rules.allowed_symbols)
        log.info(
            "connected",
            equity=account.equity_usd,
            cash=account.cash_usd,
            positions=len(account.open_positions),
            ticks=len(ticks),
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

    journal = Journal()
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

    try:
        while True:
            account = broker.get_account()
            activity = broker.get_activity()
            ticks = fetch_market_ticks(broker, rules.allowed_symbols)
            news_windows = calendar.upcoming_windows(lookahead_minutes=60)

            # Bring the journal in step before anything is evaluated: this is
            # what populates open risk and advances the loss streak, so the
            # total-risk cap and the stand-down both depend on it having run.
            recon = reconcile(journal, broker, rules, account=account)
            account = apply_journal_state(account, journal)
            stand_down_state = journal.get_stand_down()

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

            context = build_market_context(
                account=account,
                ticks=ticks,
                headlines=news.recent_headlines(rules.allowed_symbols),
                news_windows=news_windows,
                activity=activity,
                expiry_alerts=expiry_alerts,
            )
            decision, usage = claude.propose(context)

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
                )
                verdicts.append(verdict)
                log.info(
                    "verdict",
                    symbol=proposal.symbol,
                    direction=proposal.direction.value,
                    qty=proposal.qty,
                    approved=verdict.approved,
                    reasons=verdict.reasons,
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
                    claude_input_tokens=usage.input_tokens,
                    claude_output_tokens=usage.output_tokens,
                    claude_cached_tokens=usage.cache_read_tokens,
                    estimated_cost_usd=usage.estimated_cost_usd,
                    notes=decision.market_assessment,
                )
            )
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
                news_windows=len(news_windows),
                calendar_degraded=getattr(calendar, "is_degraded", False),
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


def main() -> int:
    structlog.configure(processors=[structlog.processors.JSONRenderer()])
    parser = argparse.ArgumentParser(prog="electrum-bot")
    parser.add_argument(
        "command",
        choices=["smoketest", "loop"],
        help="smoketest: one-shot connectivity check. loop: run the decision loop.",
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

    env = Env()
    try:
        env.assert_paper_only()
    except LiveTradingRefused as e:
        log.error("live_trading_refused", detail=str(e))
        return 2

    rules = Rules.load(Path(args.rules))

    if args.command == "smoketest":
        return cmd_smoketest(env, rules, force_mock=args.mock)
    return cmd_loop(env, rules, execute=args.execute, force_mock=args.mock)


if __name__ == "__main__":
    sys.exit(main())
