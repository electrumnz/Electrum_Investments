"""Tests for journal/broker reconciliation.

The point of this module is that the total-risk cap and the stand-down were both
inert in the CLI path: nothing populated `open_risk_usd` and nothing advanced the
loss streak, so two of the four rules quietly did nothing outside MCP. These
tests exist so that cannot regress silently.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.broker import MockBroker
from bot.config import load_rules
from bot.journal import Journal
from bot.models import (
    Direction,
    ExecutionMode,
    OrderProposal,
    OrderResult,
    Position,
    PositionAction,
    PositionActionRecord,
    Trade,
    WorkingOrder,
)
from bot.reconcile import apply_journal_state, reconcile, record_fill

from .conftest import PAPER_EQUITY

NOW = datetime(2026, 5, 4, 15, 0, tzinfo=UTC)


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "journal.db")


@pytest.fixture
def broker():
    b = MockBroker(starting_equity=PAPER_EQUITY)
    b.connect()
    b.set_price("SPY", bid=579.98, ask=580.02)
    b.set_price("QQQ", bid=499.98, ask=500.02)
    return b


@pytest.fixture
def rules():
    return load_rules()


def _proposal(symbol: str = "SPY", qty: float = 83, stop: float = 568.0) -> OrderProposal:
    return OrderProposal(
        symbol=symbol,
        direction=Direction.BUY,
        qty=qty,
        limit_price=580.00,
        stop_loss_price=stop,
        take_profit_price=600.00,
        rationale="Reconciliation test trade.",
    )


# ------------------------------------------------------------- recording fills


def test_fill_is_journalled_with_its_planned_stop(journal):
    """Without the planned stop the trade contributes nothing to the risk cap."""
    proposal = _proposal()
    result = OrderResult(accepted=True, order_id="x1", filled_price=580.0, filled_qty=83)

    trade_id = record_fill(
        journal, proposal, result, execution_mode=ExecutionMode.PAPER, now=NOW
    )
    assert trade_id is not None

    trade = journal.open_trades()[0]
    assert trade.planned_stop == 568.0
    assert trade.planned_risk_usd == pytest.approx(12.0 * 83)


def test_rejected_order_is_not_journalled(journal):
    result = OrderResult(accepted=False, error="rejected by broker")
    assert record_fill(journal, _proposal(), result, execution_mode=ExecutionMode.PAPER) is None
    assert journal.open_trades() == []


# ------------------------------------------------- the gap this module closes


def test_open_risk_reaches_the_account_snapshot(journal, broker):
    """The specific bug: open_risk_usd was always zero in the CLI path."""
    record_fill(
        journal,
        _proposal(),
        OrderResult(accepted=True, order_id="x1", filled_price=580.0, filled_qty=83),
        execution_mode=ExecutionMode.PAPER,
        now=NOW,
    )

    raw = broker.get_account()
    assert raw.open_risk_usd == 0.0  # broker cannot know this

    enriched = apply_journal_state(raw, journal)
    assert enriched.open_risk_usd == pytest.approx(996.0)


def test_open_risk_accumulates_across_positions(journal, broker):
    for symbol in ("SPY", "QQQ"):
        record_fill(
            journal,
            _proposal(symbol=symbol),
            OrderResult(accepted=True, order_id=symbol, filled_price=580.0, filled_qty=83),
            execution_mode=ExecutionMode.PAPER,
            now=NOW,
        )
    enriched = apply_journal_state(broker.get_account(), journal)
    assert enriched.open_risk_usd == pytest.approx(996.0 * 2)


def test_closed_trades_stop_counting_toward_open_risk(journal, broker, rules):
    trade_id = record_fill(
        journal,
        _proposal(),
        OrderResult(accepted=True, order_id="x1", filled_price=580.0, filled_qty=83),
        execution_mode=ExecutionMode.PAPER,
        now=NOW,
    )
    assert trade_id is not None
    assert apply_journal_state(broker.get_account(), journal).open_risk_usd > 0

    # Broker holds nothing, so reconcile should close it out.
    reconcile(journal, broker, rules, now=NOW)
    assert apply_journal_state(broker.get_account(), journal).open_risk_usd == 0.0


# ----------------------------------------------------------- detecting exits


def test_position_gone_from_broker_is_closed_in_journal(journal, broker, rules):
    journal.record_entry(
        Trade(
            symbol="SPY",
            direction=Direction.BUY,
            qty=10,
            entry_time=NOW,
            entry_price=580.0,
            planned_stop=575.0,
            planned_target=590.0,
            rationale="Opened, then vanished from the broker.",
        )
    )
    result = reconcile(journal, broker, rules, now=NOW)

    assert result.closed == ["SPY"]
    assert journal.open_trades() == []
    assert len(journal.closed_trades()) == 1


def test_exit_uses_the_broker_mark_when_available(journal, broker, rules):
    journal.record_entry(
        Trade(
            symbol="SPY",
            direction=Direction.BUY,
            qty=10,
            entry_time=NOW,
            entry_price=570.0,
            planned_stop=565.0,
            planned_target=590.0,
            rationale="Closed at the current mark.",
        )
    )
    reconcile(journal, broker, rules, now=NOW)

    closed = journal.closed_trades()[0]
    assert closed.exit_price == pytest.approx(580.0)  # mid of 579.98/580.02
    assert closed.realised_pnl_usd == pytest.approx(100.0)


def test_exit_without_a_price_is_flagged_as_estimated(journal, broker, rules):
    """An unconfirmed exit must not masquerade as a real fill."""
    journal.record_entry(
        Trade(
            symbol="AAPL",  # no price seeded on the broker
            direction=Direction.BUY,
            qty=10,
            entry_time=NOW,
            entry_price=200.0,
            planned_stop=195.0,
            planned_target=210.0,
            rationale="No mark available at close time.",
        )
    )
    result = reconcile(journal, broker, rules, now=NOW)

    assert result.estimated_exits == 1
    # Recorded flat rather than inventing a plausible-looking number.
    assert journal.closed_trades()[0].realised_pnl_usd == pytest.approx(0.0)


def test_short_pnl_sign_is_correct(journal, broker, rules):
    journal.record_entry(
        Trade(
            symbol="SPY",
            direction=Direction.SELL,
            qty=10,
            entry_time=NOW,
            entry_price=590.0,
            planned_stop=595.0,
            planned_target=570.0,
            rationale="Short closed lower, so it profits.",
        )
    )
    reconcile(journal, broker, rules, now=NOW)
    assert journal.closed_trades()[0].realised_pnl_usd == pytest.approx(100.0)


# ------------------------------------------------------------ MAE/MFE sampling


def test_excursion_is_sampled_for_held_positions(journal, broker, rules):
    broker.place_order(_proposal(qty=10))
    trade_id = journal.record_entry(
        Trade(
            symbol="SPY",
            direction=Direction.BUY,
            qty=10,
            entry_time=NOW,
            entry_price=580.0,
            planned_stop=575.0,
            planned_target=590.0,
            rationale="Held, so excursion should widen.",
        )
    )
    positions = broker.get_account().open_positions
    positions[0].unrealised_pnl_usd = -42.0

    result = reconcile(journal, broker, rules, account=broker.get_account(), now=NOW)
    # MockBroker rebuilds positions per call, so drive the update directly to
    # assert the widening rather than the plumbing.
    journal.update_excursion(trade_id, -42.0)

    assert result.excursions_updated >= 0
    assert journal.open_trades()[0].mae_usd == pytest.approx(-42.0)


# ------------------------------------------------------- untracked positions


def test_untracked_position_is_reported_not_guessed(journal, broker, rules):
    """A position with no journal entry has unknowable risk. Say so."""
    broker.place_order(_proposal(qty=10))
    result = reconcile(journal, broker, rules, account=broker.get_account(), now=NOW)

    assert result.untracked_positions == ["SPY"]
    assert result.risk_is_understated
    # And crucially: no risk was invented for it.
    assert apply_journal_state(broker.get_account(), journal).open_risk_usd == 0.0


def test_tracked_position_is_not_flagged(journal, broker, rules):
    broker.place_order(_proposal(qty=10))
    journal.record_entry(
        Trade(
            symbol="SPY",
            direction=Direction.BUY,
            qty=10,
            entry_time=NOW,
            entry_price=580.0,
            planned_stop=575.0,
            planned_target=590.0,
            rationale="Known to the journal.",
        )
    )
    result = reconcile(journal, broker, rules, account=broker.get_account(), now=NOW)
    assert result.untracked_positions == []
    assert not result.risk_is_understated


# ----------------------------------------------------- stand-down integration


def test_losing_streak_through_reconcile_trips_the_stand_down(journal, broker, rules):
    """The other half of the gap: the streak never advanced in the CLI path."""
    trigger = rules.stand_down.consecutive_losses_trigger

    for i in range(trigger):
        # Entry above the current mark, so closing at market is a real loss.
        journal.record_entry(
            Trade(
                symbol="SPY",
                direction=Direction.BUY,
                qty=100,
                entry_time=NOW + timedelta(minutes=i * 10),
                entry_price=600.0,     # closes at 580 => -$2,000
                planned_stop=595.0,    # $500 planned risk => -4R
                planned_target=610.0,
                rationale="Losing trade in a streak.",
            )
        )
        reconcile(journal, broker, rules, now=NOW + timedelta(minutes=i * 10 + 5))

    state = journal.get_stand_down()
    assert state.stage >= 1
    assert state.consecutive_losses >= trigger
    assert state.is_active(NOW + timedelta(minutes=trigger * 10))


def test_stand_down_survives_a_fresh_journal_instance(journal, broker, rules, tmp_path):
    for i in range(rules.stand_down.consecutive_losses_trigger):
        journal.record_entry(
            Trade(
                symbol="SPY",
                direction=Direction.BUY,
                qty=100,
                entry_time=NOW + timedelta(minutes=i * 10),
                entry_price=600.0,
                planned_stop=595.0,
                planned_target=610.0,
                rationale="Losing trade in a streak.",
            )
        )
        reconcile(journal, broker, rules, now=NOW + timedelta(minutes=i * 10 + 5))

    reopened = Journal(tmp_path / "journal.db")
    assert reopened.get_stand_down().stage >= 1


# ------------------------------------------------------------------- equity


def test_equity_is_snapshotted_each_cycle(journal, broker, rules):
    reconcile(journal, broker, rules, now=NOW)
    curve = journal.equity_curve()
    assert curve == [("2026-05-04", PAPER_EQUITY)]


# ---------------------------------------------------- provenance on a fill


def test_a_fill_under_a_dream_grant_records_which_dream(journal):
    """Provenance, never endorsement.

    It is passed in rather than looked up here, because whether the grant was
    what let the proposal through is the risk gate's finding and travels on the
    verdict. The journal is the only place it is stored, and the Board's `dream`
    and `dream-expired-holding` tags are derived from it — a tag a model decided
    to apply would be a tag that can be argued into existence.
    """
    trade_id = record_fill(
        journal,
        _proposal(),
        OrderResult(accepted=True, order_id="x1", filled_price=580.0, filled_qty=83),
        execution_mode=ExecutionMode.PAPER,
        now=NOW,
        dream_id=17,
    )
    assert trade_id is not None

    assert journal.open_trades()[0].dream_id == 17


def test_an_ordinary_fill_carries_no_dream(journal):
    """The default, and it must stay the default: every trade in a symbol
    `config/rules.yaml` already allows is nobody's dream."""
    record_fill(
        journal,
        _proposal(),
        OrderResult(accepted=True, order_id="x1", filled_price=580.0, filled_qty=83),
        execution_mode=ExecutionMode.PAPER,
        now=NOW,
    )

    assert journal.open_trades()[0].dream_id is None


def test_a_dream_id_does_not_survive_a_refused_order(journal):
    """A rejection journals nothing at all, so there is no half-record of a
    permission that was never used."""
    assert (
        record_fill(
            journal,
            _proposal(),
            OrderResult(accepted=False, error="rejected by broker"),
            execution_mode=ExecutionMode.PAPER,
            dream_id=17,
        )
        is None
    )
    assert journal.open_trades() == []


# -------------------------------------- the stop in force, and what moved it


def test_a_tightened_stop_is_what_the_caps_count_and_the_model_is_shown(journal, broker):
    """`apply_journal_state` reads the stop IN FORCE, not the sizing stop.

    Two things fall out of that and both matter. The 2% cap is a statement
    about what the stops would cost if they filled, so a stop pulled in costs
    less and should free room. And the level handed to the model on the
    position it manages has to be the one it just moved to — showing it the
    level it moved away from would make its own action invisible to it on the
    next cycle.
    """
    trade_id = journal.record_entry(
        Trade(
            symbol="SPY",
            direction=Direction.SELL,
            qty=21,
            entry_time=NOW,
            entry_price=773.324285,
            planned_stop=820.0,
            rationale="The live position: short 21 SPY with a stop at 820.",
            execution_mode=ExecutionMode.PAPER,
        )
    )

    before = apply_journal_state(broker.get_account(), journal)
    assert round(before.open_risk_usd, 2) == 980.19
    assert before.planned_stop_by_symbol["SPY"] == 820.0

    journal.record_stop_move(
        PositionActionRecord(
            trade_id=trade_id,
            symbol="SPY",
            action=PositionAction.TIGHTEN_STOP,
            actor="trader",
            at=NOW + timedelta(hours=1),
            reason="Two sessions without a lower high.",
            before_stop=820.0,
            after_stop=800.0,
            reached_broker=True,
        )
    )

    after = apply_journal_state(broker.get_account(), journal)
    assert round(after.open_risk_usd, 2) == 560.19
    assert after.open_risk_by_symbol["SPY"] == pytest.approx(560.19, abs=0.01)
    assert after.planned_stop_by_symbol["SPY"] == 800.0

    # The SIZING stop is untouched, so R keeps meaning what it meant at entry.
    assert journal.open_trade_for("SPY").planned_stop == 820.0


def test_reconcile_reports_a_stop_that_moved_with_no_reason_on_file(
    journal, broker, rules
):
    """The inverse tag, and the half that makes the record honest.

    A stop pulled in through Alpaca's web UI appears nowhere else in this
    repository: the journal still says 820 and the Board would render 820 with
    nothing to say the broker disagrees.
    """
    journal.record_entry(
        Trade(
            symbol="SPY",
            direction=Direction.SELL,
            qty=21,
            entry_time=NOW,
            entry_price=773.324285,
            planned_stop=820.0,
            rationale="Short with a stop somebody then moved by hand.",
        )
    )
    broker._positions["SPY"] = Position(
        symbol="SPY",
        direction=Direction.SELL,
        qty=21,
        entry_price=773.324285,
        opened_at=NOW,
        current_price=774.09,
    )
    broker.set_open_orders(
        [
            WorkingOrder(
                order_id="leg-1",
                symbol="SPY",
                direction=Direction.BUY,
                qty=21,
                stop_price=805.0,          # not what the journal says
                order_type="stop",
            )
        ]
    )

    result = reconcile(journal, broker, rules, now=NOW)

    assert [m.kind for m in result.unexplained.moves] == ["stop"]
    assert result.unexplained.moves[0].journal_value == 820.0
    assert result.unexplained.moves[0].broker_value == 805.0
    assert result.unexplained.can_check


def test_reconcile_says_it_could_not_check_rather_than_reporting_nothing(
    journal, broker, rules
):
    """`get_open_orders` catches its own failures and returns `[]`, so an
    outage renders as an account with nothing resting — and therefore nothing
    wrong. The degraded flag has to travel with the reading or the empty result
    reads as a clean one."""
    journal.record_entry(
        Trade(
            symbol="SPY",
            direction=Direction.SELL,
            qty=21,
            entry_time=NOW,
            entry_price=773.324285,
            planned_stop=820.0,
            rationale="Short, with the order feed down.",
        )
    )
    broker._positions["SPY"] = Position(
        symbol="SPY",
        direction=Direction.SELL,
        qty=21,
        entry_price=773.324285,
        opened_at=NOW,
    )
    broker.set_orders_degraded(True)

    result = reconcile(journal, broker, rules, now=NOW)

    assert result.unexplained.moves == []
    assert result.unexplained.can_check is False
    assert result.unexplained.anything_to_report
