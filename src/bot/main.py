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
from .data.calendar import EmptyCalendar
from .data.news import EmptyNews
from .models import Decision
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
            headlines=EmptyNews().recent_headlines(rules.allowed_symbols),
            news_windows=EmptyCalendar().upcoming_windows(lookahead_minutes=60),
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
    news = EmptyNews()
    calendar = EmptyCalendar()

    account = broker.get_account()
    risk = RiskGate(rules, equity_at_session_start=account.equity_usd)

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

            context = build_market_context(
                account=account,
                ticks=ticks,
                headlines=news.recent_headlines(rules.allowed_symbols),
                news_windows=news_windows,
                activity=activity,
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
                    log.info(
                        "order_submitted",
                        symbol=proposal.symbol,
                        accepted=result.accepted,
                        order_id=result.order_id,
                        error=result.error,
                    )

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
