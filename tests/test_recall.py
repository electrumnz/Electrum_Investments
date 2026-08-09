"""Carrying the previous cycle's assessments into the next prompt.

The model has no memory between cycles. It writes `waiting_for` triggers like
"SPY closing below 641.20" and, until this existed, never saw them again — so a
watch was a sentence written to nobody and the stance meant nothing.

The assertions that matter are the ones about honesty rather than recall: the
age of the recollection is stated rather than implied, and a cycle that
produced no decision leaves the last real one standing instead of blanking it.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

import bot.main as main_mod
from bot.audit import AuditLog
from bot.claude_client import CallUsage, ClaudeDecision
from bot.config import Env, Rules, load_rules
from bot.context import build_market_context
from bot.journal import Journal
from bot.models import AccountSnapshot, Decision, Stance, SymbolAssessment

ACCOUNT = AccountSnapshot(
    equity_usd=100_000.0, cash_usd=100_000.0, buying_power_usd=100_000.0
)

WATCHING = SymbolAssessment(
    symbol="SPY",
    stance=Stance.WATCH,
    reasoning="Stretched but not yet at the level.",
    waiting_for="SPY closing below 641.20, roughly 1 ATR under the 20-day",
)


def _context(**kwargs) -> str:
    return build_market_context(
        account=ACCOUNT, ticks={}, headlines=[], news_windows=[], **kwargs
    )


# --------------------------------------------------------------- the prompt


def test_the_previous_trigger_is_quoted_back_verbatim():
    """A paraphrased trigger is a different trigger."""
    context = _context(previous_assessments=[WATCHING])

    assert "SPY closing below 641.20" in context
    assert "SPY: watch" in context


def test_the_model_is_told_to_check_the_trigger_not_restate_it():
    """Recall without instruction would just teach it to repeat itself."""
    context = _context(previous_assessments=[WATCHING])

    assert "check the trigger against the figures ABOVE" in context
    assert "a trigger you wrote is not evidence" in context.lower()


def test_the_age_of_the_recollection_is_stated():
    """Cycles are skipped while the market is shut, so "last cycle" can be Friday.

    Without the age, a three-day-old trigger reads exactly like one written
    fifteen minutes ago, and the model would treat a stale note as a standing
    order.
    """
    context = _context(
        previous_assessments=[WATCHING],
        previous_at=datetime.now(UTC) - timedelta(hours=62),
    )

    assert "62.0 hours ago" in context


def test_minutes_are_used_for_a_recent_cycle():
    context = _context(
        previous_assessments=[WATCHING],
        previous_at=datetime.now(UTC) - timedelta(minutes=15),
    )

    assert "15 minutes ago" in context


def test_an_empty_recollection_says_so_rather_than_being_absent():
    """A missing section reads as "nothing was watched", which is a different claim."""
    context = _context()

    assert "## What you said last cycle" in context
    assert "nothing on record" in context


def test_the_section_sits_after_the_figures_it_must_be_checked_against():
    """Order is load-bearing: the trigger is checked against numbers read first."""
    context = _context(previous_assessments=[WATCHING])

    assert context.index("## Indicators") < context.index("## What you said last cycle")
    assert context.index("## Intraday") < context.index("## What you said last cycle")
    assert context.index("## What you said last cycle") < context.index("## Recent headlines")


# ------------------------------------------------------------------ the loop


class _StubClaude:
    def __init__(self, decision: ClaudeDecision) -> None:
        self._decision = decision

    def propose(self, market_context: str) -> tuple[ClaudeDecision, CallUsage]:
        self.seen = market_context
        return self._decision, CallUsage(
            input_tokens=1, output_tokens=1, cache_read_tokens=0,
            cache_write_tokens=0, estimated_cost_usd=0.001,
        )


def _run(monkeypatch, tmp_path, client, *, in_session: bool = True) -> list[Any]:
    def _stop(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", _stop)
    monkeypatch.setattr(main_mod, "Journal", lambda: Journal(tmp_path / "journal.db"))
    monkeypatch.setattr(main_mod, "AuditLog", lambda: AuditLog(tmp_path / "audit"))
    monkeypatch.setattr(main_mod, "ClaudeClient", lambda *a, **k: client)
    monkeypatch.setattr(Rules, "any_class_in_session", lambda self, moment: in_session)

    with structlog.testing.capture_logs() as logs:
        assert main_mod.cmd_loop(
            Env(_env_file=None), load_rules(), execute=False, force_mock=True  # type: ignore[call-arg]
        ) == 0
    return logs


def test_a_restart_recovers_the_last_assessments_from_the_audit_log(
    monkeypatch, tmp_path
):
    """A deploy mid-session must not silently discard every open watch.

    This is the moment the recall is worth most, and an in-memory-only version
    would lose exactly then.
    """
    audit = AuditLog(tmp_path / "audit")
    audit.record(
        Decision(timestamp=datetime.now(UTC), proposals=[], assessments=[WATCHING])
    )

    client = _StubClaude(ClaudeDecision(market_assessment="Quiet.", proposals=[]))
    logs = _run(monkeypatch, tmp_path, client)

    recalled = [e for e in logs if e["event"] == "recalled_previous_assessments"]
    assert len(recalled) == 1
    assert recalled[0]["count"] == 1
    # And it actually reached the prompt, not merely the log line.
    assert "SPY closing below 641.20" in client.seen


def test_a_broken_audit_log_costs_the_recall_and_nothing_else(monkeypatch, tmp_path):
    """Same rule as every feed: degrade the cycle, never end the loop."""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    (audit_dir / f"{stamp}.jsonl").write_text("{ this is not json\n", encoding="utf-8")

    client = _StubClaude(ClaudeDecision(market_assessment="Quiet.", proposals=[]))
    logs = _run(monkeypatch, tmp_path, client)

    assert [e for e in logs if e["event"] == "cycle_complete"]
    assert "nothing on record" in client.seen


def test_a_skipped_cycle_does_not_blank_the_recollection(monkeypatch, tmp_path):
    """A weekend would otherwise erase every open watch before Monday.

    The market-closed skip runs before the model call, so if the carry-forward
    sat anywhere other than after a real decision, a Saturday would wipe it.
    """
    audit = AuditLog(tmp_path / "audit")
    audit.record(
        Decision(timestamp=datetime.now(UTC), proposals=[], assessments=[WATCHING])
    )

    client = _StubClaude(ClaudeDecision(market_assessment="unused", proposals=[]))
    logs = _run(monkeypatch, tmp_path, client, in_session=False)

    # The cycle skipped, so the model never saw anything...
    assert [e for e in logs if e["event"] == "cycle_skipped_market_closed"]
    assert not hasattr(client, "seen")
    # ...and the recall still loaded, ready for the next open session.
    assert [e for e in logs if e["event"] == "recalled_previous_assessments"]
