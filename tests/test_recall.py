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
from bot.config import Env, Rules, load_rules
from bot.context import build_market_context
from bot.dreaming import DreamStore
from bot.journal import Journal
from bot.model_client import CallUsage, ModelDecision
from bot.models import (
    AccountSnapshot,
    Decision,
    Direction,
    OrderProposal,
    RiskVerdict,
    Stance,
    SymbolAssessment,
)

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


# -------------------------------------------------------- the gate's answer


OVERSIZED = OrderProposal(
    symbol="AAPL",
    direction=Direction.BUY,
    qty=87,
    limit_price=232.50,
    stop_loss_price=219.50,
    take_profit_price=250.00,
    rationale="Deliberately 13% over the per-trade cap, as observed live.",
)
REFUSED = RiskVerdict.reject(
    "risk 1,131.00 exceeds the per-trade cap 1,000.00 (1.00% of equity)"
)


def test_a_rejection_is_quoted_back_with_its_reason():
    """Observed live: 87 AAPL, 13% over the cap. Not wildly wrong, which is the danger.

    Without this the model sizes the same way every cycle forever, because
    nothing it can see ever tells it the last attempt was refused.
    """
    context = _context(previous_verdicts=[(OVERSIZED, REFUSED)])

    assert "REJECTED: buy 87 AAPL" in context
    assert "exceeds the per-trade cap" in context


def test_the_model_is_told_the_gate_cannot_be_argued_with():
    context = _context(previous_verdicts=[(OVERSIZED, REFUSED)])

    assert "deterministic code, not a reader you can persuade" in context
    assert "do not re-send it unchanged" in context


def test_an_approval_is_shown_too_not_only_a_refusal():
    """One-sided feedback would read as "the gate only ever says no"."""
    context = _context(previous_verdicts=[(OVERSIZED, RiskVerdict.approve())])

    assert "APPROVED: buy 87 AAPL" in context


def test_the_section_is_absent_when_nothing_was_proposed():
    """Most cycles propose nothing, and a heading over an empty list is noise."""
    context = _context(previous_assessments=[WATCHING])

    assert "What the risk gate did" not in context


# ------------------------------------------------------------------ the loop


class _StubClaude:
    def __init__(self, decision: ModelDecision) -> None:
        self._decision = decision

    def propose(self, market_context: str) -> tuple[ModelDecision, CallUsage]:
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
    monkeypatch.setattr(main_mod, "ModelClient", lambda *a, **k: client)
    # The loop opens the dream store to resolve symbol grants, and the shipped
    # rules turn grants on, so without this the cycle writes `data/dreams.db`
    # beside the real journal.
    monkeypatch.setattr(
        main_mod, "DreamStore", lambda: DreamStore(tmp_path / "dreams.db")
    )
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

    client = _StubClaude(ModelDecision(market_assessment="Quiet.", proposals=[]))
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

    client = _StubClaude(ModelDecision(market_assessment="Quiet.", proposals=[]))
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

    client = _StubClaude(ModelDecision(market_assessment="unused", proposals=[]))
    logs = _run(monkeypatch, tmp_path, client, in_session=False)

    # The cycle skipped, so the model never saw anything...
    assert [e for e in logs if e["event"] == "cycle_skipped_market_closed"]
    assert not hasattr(client, "seen")
    # ...and the recall still loaded, ready for the next open session.
    assert [e for e in logs if e["event"] == "recalled_previous_assessments"]
