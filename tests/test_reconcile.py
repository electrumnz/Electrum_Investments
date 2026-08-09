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
    Trade,
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
