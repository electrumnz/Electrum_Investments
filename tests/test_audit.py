"""Reading the audit log back.

The log is the only record of a REJECTED proposal. A trade the gate refused
never reaches the journal and never reaches the broker, so if the reader drops
it the reasoning is gone for good. That is why the tolerance assertions here
matter as much as the happy path: a reader that raises on one bad line, or
silently swallows it, loses the trail either way.
"""

from __future__ import annotations

from datetime import UTC, datetime

from bot.audit import AuditLog, DecisionEntry, EventEntry
from bot.models import Decision, Direction, OrderProposal, OrderResult, RiskVerdict


def proposal(symbol: str = "SPY") -> OrderProposal:
    return OrderProposal(
        symbol=symbol,
        direction=Direction.BUY,
        qty=3,
        limit_price=580.0,
        stop_loss_price=575.0,
        take_profit_price=590.0,
        rationale="Risking 0.4% on this one. Invalidated below 575.",
    )


def test_a_decision_round_trips_through_the_log(tmp_path):
    log = AuditLog(tmp_path)
    log.record(
        Decision(
            timestamp=datetime(2026, 8, 9, 14, 30, tzinfo=UTC),
            proposals=[proposal()],
            verdicts=[RiskVerdict.approve()],
            executed=[OrderResult(accepted=True, order_id="abc")],
            notes="Stretched below the 20-day average.",
        )
    )

    view = log.read()

    assert len(view.decisions) == 1
    entry = view.decisions[0]
    assert isinstance(entry, DecisionEntry)
    assert entry.decision.proposals[0].symbol == "SPY"
    assert entry.approved == 1
    assert entry.acted is True
    assert entry.outcome == "executed"
    assert view.is_degraded is False


def test_a_rejection_keeps_every_reason_the_gate_gave(tmp_path):
    """The gate collects all failure reasons rather than short-circuiting.

    That property is worth nothing if the surface showing it keeps only the
    first one.
    """
    log = AuditLog(tmp_path)
    log.record(
        Decision(
            timestamp=datetime(2026, 8, 9, 14, 30, tzinfo=UTC),
            proposals=[proposal()],
            verdicts=[
                RiskVerdict.reject(
                    "risk 1.4% exceeds the 1.0% per-trade cap",
                    "total open risk would reach 2.6% against a 2.0% cap",
                    "SPY traded 4 minutes ago, inside the 1800s cooldown",
                )
            ],
        )
    )

    entry = log.read().decisions[0]

    assert entry.outcome == "rejected"
    assert entry.rejected == 1
    assert len(entry.decision.verdicts[0].reasons) == 3


def test_doing_nothing_is_recorded_as_held_rather_than_as_a_failure(tmp_path):
    """Most days the correct action is none, and the page must not cry wolf."""
    log = AuditLog(tmp_path)
    log.record(
        Decision(
            timestamp=datetime(2026, 8, 9, 14, 30, tzinfo=UTC),
            notes="Nothing worth taking today.",
        )
    )

    assert log.read().decisions[0].outcome == "held"


def test_events_and_decisions_are_kept_apart(tmp_path):
    log = AuditLog(tmp_path)
    log.record_event("loop_start", {"execute": False})
    log.record(Decision(timestamp=datetime(2026, 8, 9, tzinfo=UTC)))

    view = log.read()

    assert len(view.decisions) == 1
    assert len(view.events) == 1
    assert isinstance(view.events[0], EventEntry)
    assert view.events[0].kind == "loop_start"
    assert view.events[0].payload == {"execute": False}


def test_a_torn_final_line_is_counted_not_fatal(tmp_path):
    """The writer appends to a live file, so the last line can be half-written.

    Taking the dashboard down over that would protect nothing.
    """
    log = AuditLog(tmp_path)
    log.record(Decision(timestamp=datetime(2026, 8, 9, tzinfo=UTC)))
    path = log.files()[0]
    with path.open("a", encoding="utf-8") as f:
        f.write('{"timestamp": "2026-08-09T14:00:00+00:0')

    view = log.read()

    assert len(view.decisions) == 1
    assert view.malformed == 1
    assert view.is_degraded is True


def test_newest_entries_come_back_first(tmp_path):
    log = AuditLog(tmp_path)
    for hour in (9, 10, 11):
        log.record(
            Decision(
                timestamp=datetime(2026, 8, 9, hour, tzinfo=UTC),
                notes=f"cycle at {hour}",
            )
        )

    stamps = [e.timestamp.hour for e in log.read().decisions]

    assert stamps == [11, 10, 9]


def test_the_limit_is_respected(tmp_path):
    log = AuditLog(tmp_path)
    for hour in range(1, 13):
        log.record(Decision(timestamp=datetime(2026, 8, 9, hour, tzinfo=UTC)))

    assert len(log.read(limit=5).decisions) == 5


def test_reading_an_absent_log_directory_is_empty_not_an_error(tmp_path):
    view = AuditLog(tmp_path / "never-written").read()

    assert view.decisions == []
    assert view.is_degraded is False


def test_a_proposal_without_a_matching_verdict_is_admitted_rather_than_mispaired(tmp_path):
    """The loop skips a proposal whose symbol has no tick.

    Rendering one proposal's reasons against another would be worse than
    showing nothing, so the pairing refuses when the lists disagree.
    """
    log = AuditLog(tmp_path)
    log.record(
        Decision(
            timestamp=datetime(2026, 8, 9, tzinfo=UTC),
            proposals=[proposal("SPY"), proposal("QQQ")],
            verdicts=[RiskVerdict.reject("only one verdict for two proposals")],
        )
    )

    entry = log.read().decisions[0]

    assert entry.verdict_for(0) is None
    assert entry.verdict_for(1) is None


def test_a_broker_refusal_is_not_counted_as_an_execution(tmp_path):
    """A refusal is recorded as an OrderResult too.

    Treating the mere presence of one as success labels the cycle where money
    did NOT move as the cycle where it did, which is exactly backwards on the
    surface an operator scans to see what happened.
    """
    log = AuditLog(tmp_path)
    log.record(
        Decision(
            timestamp=datetime(2026, 8, 9, tzinfo=UTC),
            proposals=[proposal()],
            verdicts=[RiskVerdict.approve()],
            executed=[OrderResult(accepted=False, error="insufficient buying power")],
        )
    )

    entry = log.read().decisions[0]

    assert entry.acted is False
    assert entry.broker_refused is True
    assert entry.outcome == "refused"
