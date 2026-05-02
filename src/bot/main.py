"""Entry point for the Electrum trading bot.

Phase A: connectivity smoke test only — does NOT place orders.
Set BOT_MODE=demo and run `electrum-bot smoketest` to verify the wiring.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import structlog

from .audit import AuditLog
from .broker import Broker, MockBroker, MT5Broker
from .claude_client import ClaudeClient, build_system_prompt
from .config import BotMode, Env, Rules
from .context import build_market_context, fetch_market_ticks
from .data.calendar import EmptyCalendar
from .data.news import EmptyNews
from .models import Decision
from .risk import RiskGate

log = structlog.get_logger()


def _build_broker(env: Env) -> Broker:
    if sys.platform == "win32":
        return MT5Broker(env)
    log.warning("non_windows_using_mock_broker", platform=sys.platform)
    return MockBroker()


def cmd_smoketest(env: Env, rules: Rules) -> int:
    """Connect to broker, ask Claude one trivial question, log the decision. No order."""
    audit = AuditLog()
    broker = _build_broker(env)
    broker.connect()
    try:
        account = broker.get_account()
        ticks = fetch_market_ticks(broker, rules.allowed_symbols)
        log.info("connected", equity=account.equity_usd, ticks=len(ticks))

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
        )
        decision_obj, usage = claude.propose(context)
        log.info(
            "claude_responded",
            proposal_count=len(decision_obj.proposals),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.estimated_cost_usd,
        )
        audit.record(
            Decision(
                timestamp=datetime.now(UTC),
                proposals=decision_obj.proposals,
                verdicts=[],
                executed=[],
                claude_input_tokens=usage.input_tokens,
                claude_output_tokens=usage.output_tokens,
                claude_cached_tokens=usage.cache_read_tokens,
                estimated_cost_usd=usage.estimated_cost_usd,
                notes=f"smoketest assessment: {decision_obj.market_assessment}",
            )
        )
        return 0
    finally:
        broker.disconnect()


def cmd_loop(env: Env, rules: Rules) -> int:
    """Decision loop — Phase C will add execution. For now: propose & vet, no orders."""
    audit = AuditLog()
    broker = _build_broker(env)
    broker.connect()
    claude = ClaudeClient(env, build_system_prompt(rules))
    news = EmptyNews()
    calendar = EmptyCalendar()

    account = broker.get_account()
    risk = RiskGate(rules, equity_at_session_start=account.equity_usd)

    audit.record_event("loop_start", {"mode": env.bot_mode.value, "tier": env.claude_tier.value})
    try:
        while True:
            account = broker.get_account()
            ticks = fetch_market_ticks(broker, rules.allowed_symbols)
            news_windows = calendar.upcoming_windows(lookahead_minutes=60)
            context = build_market_context(
                account=account,
                ticks=ticks,
                headlines=news.recent_headlines(rules.allowed_symbols),
                news_windows=news_windows,
            )
            decision, usage = claude.propose(context)
            verdicts = []
            for proposal in decision.proposals:
                if proposal.symbol not in ticks:
                    continue
                verdict = risk.evaluate(
                    proposal,
                    account=account,
                    tick=ticks[proposal.symbol],
                    news_windows=news_windows,
                )
                verdicts.append(verdict)
                log.info(
                    "verdict",
                    symbol=proposal.symbol,
                    direction=proposal.direction.value,
                    approved=verdict.approved,
                    reasons=verdict.reasons,
                )

            audit.record(
                Decision(
                    timestamp=datetime.now(UTC),
                    proposals=decision.proposals,
                    verdicts=verdicts,
                    executed=[],
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
    args = parser.parse_args()

    env = Env()
    rules = Rules.load(Path(args.rules))

    if env.bot_mode == BotMode.LIVE:
        log.warning("live_mode_active_disabled_until_phase_e")

    if args.command == "smoketest":
        return cmd_smoketest(env, rules)
    return cmd_loop(env, rules)


if __name__ == "__main__":
    sys.exit(main())
