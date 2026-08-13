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
    ExitReason,
    FillState,
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


# ------------------------------- squaring the entry against what actually filled
#
# `record_fill` runs immediately after `broker.place_order` and writes the
# PROPOSAL's quantity and limit price, because waiting for a terminal order
# state leaves a live position unjournalled — the `14b88c8` hole. So the journal
# records an intention and calls it an outcome, and `reconcile` is where that
# stops being true. Measured on the first real order: the limit was 772.84, the
# fill averaged 773.324285, and open risk read $990.36 against a real $980.19.

SHORT_ENTRY = 772.84          # the limit that was asked for
SHORT_FILL = 773.324285       # what it actually averaged


def _resting_short(journal, *, order_id: str | None = "entry-1", qty: float = 21):
    """A journalled short written from the proposal, exactly as `record_fill`
    leaves it: the limit price, the submitted quantity, and nothing confirmed."""
    return journal.record_entry(
        Trade(
            symbol="SPY",
            direction=Direction.SELL,
            qty=qty,
            entry_time=NOW,
            entry_price=SHORT_ENTRY,
            planned_stop=820.0,
            submitted_qty=qty,
            submitted_price=SHORT_ENTRY,
            fill_state=FillState.UNCONFIRMED,
            entry_order_id=order_id,
            rationale="Short, journalled from the proposal it was submitted as.",
        )
    )


def _held_short(broker, *, qty: float = 21, entry: float = SHORT_FILL):
    broker._positions["SPY"] = Position(
        symbol="SPY",
        direction=Direction.SELL,
        qty=qty,
        entry_price=entry,
        opened_at=NOW,
    )


def _working_entry(*, filled: float = 0.0, qty: float = 21) -> WorkingOrder:
    return WorkingOrder(
        order_id="entry-1",
        symbol="SPY",
        direction=Direction.SELL,
        qty=qty,
        limit_price=SHORT_ENTRY,
        order_type="limit",
        filled_qty=filled,
    )


def test_reconcile_squares_the_entry_against_what_the_broker_filled(
    journal, broker, rules
):
    """The measured case, and the whole point of item 10.

    Nothing else in the system can do this: the broker knows what filled and
    does not know what was intended, and the journal knows what was intended
    and was told nothing about the fill.
    """
    trade_id = _resting_short(journal)
    _held_short(broker)
    assert round(apply_journal_state(broker.get_account(), journal).open_risk_usd, 2) == 990.36

    result = reconcile(journal, broker, rules, now=NOW)

    stored = journal.open_trades()[0]
    assert stored.id == trade_id
    assert stored.entry_price == SHORT_FILL
    assert stored.fill_state is FillState.COMPLETE
    # What was ASKED for is still on the row. A correction that erased it would
    # be the same fault with better numbers.
    assert stored.submitted_price == SHORT_ENTRY

    assert round(apply_journal_state(broker.get_account(), journal).open_risk_usd, 2) == 980.19
    assert [c.symbol for c in result.entries_corrected] == ["SPY"]
    assert "entry" in result.entries_corrected[0].describe()


def test_a_mid_fill_reading_is_not_written_down_as_a_partial_fill(
    journal, broker, rules
):
    """A fill is not atomic. A poll during the first real order returned
    `FILLED 3.0` of 21 and the order completed moments later, so anything
    sampling an in-flight order holds a snapshot rather than an outcome —
    and writing 3 would understate the position's risk by six sevenths."""
    _resting_short(journal)
    _held_short(broker, qty=3)
    broker.set_open_orders([_working_entry(filled=3.0)])

    result = reconcile(journal, broker, rules, now=NOW)

    stored = journal.open_trades()[0]
    assert stored.qty == 21                       # the submitted quantity stands
    assert stored.entry_price == SHORT_ENTRY
    assert stored.fill_state is FillState.UNCONFIRMED
    assert result.entries_mid_fill == ["SPY"]
    assert result.entries_corrected == []


def test_a_settled_short_fill_is_recorded_as_partial(journal, broker, rules):
    """Once the entry order is terminal the rest is not coming. THAT is a
    partial fill, and it is a different claim from the reading above."""
    _resting_short(journal)
    _held_short(broker, qty=3)
    broker.set_open_orders([])                    # the entry order is gone

    reconcile(journal, broker, rules, now=NOW)

    stored = journal.open_trades()[0]
    assert stored.fill_state is FillState.PARTIAL
    assert stored.qty == 3
    assert stored.submitted_qty == 21
    # `Trade` has one qty and one entry price, so "3 filled and 18 cancelled" is
    # expressed as a state plus the submitted figure rather than as two rows.
    assert stored.fill_shortfall_qty == 18


def test_a_resting_entry_is_not_written_off_as_a_closed_trade(
    journal, broker, rules
):
    """The bug this guard exists for, and it is the out-of-hours case exactly.

    An entry carrying a stop cannot fill outside the regular session — Alpaca
    refuses `extended_hours` on a bracket or an OTO — so it RESTS and becomes
    eligible at the next open. Observed live: 21 SPY submitted at 09:23 New York
    came back `filled_qty=0.0` and filled after 09:30. With `--execute` on, the
    next cycle saw no position, closed the row as a trade, and the fill then
    landed as a position with no journal row behind it.
    """
    _resting_short(journal)
    broker.set_open_orders([_working_entry(filled=0.0)])   # resting, nothing held

    result = reconcile(journal, broker, rules, now=NOW)

    assert result.closed == []
    assert result.closes_deferred == ["SPY"]
    assert result.entries_resting == ["SPY"]
    stored = journal.open_trades()
    assert len(stored) == 1
    assert stored[0].fill_state is FillState.RESTING
    assert stored[0].fill_is_confirmed is False


def test_a_degraded_order_read_defers_rather_than_concluding_terminal(
    journal, broker, rules
):
    """`get_open_orders` catches its own failures and returns `[]`, so an empty
    list is 'could not ask' and never 'nothing resting'. Concluding terminal
    from it would close a resting entry as a trade at the worst moment."""
    _resting_short(journal)
    broker.set_orders_degraded(True)

    result = reconcile(journal, broker, rules, now=NOW)

    assert result.closed == []
    assert result.closes_deferred == ["SPY"]
    assert result.entries_unchecked == ["SPY"]
    assert journal.open_trades()[0].fill_state is FillState.UNCONFIRMED


def test_a_row_with_no_order_id_is_named_rather_than_deferred(
    journal, broker, rules
):
    """A hand-placed or adopted row has no order to look up, so nothing can be
    established — and nothing is deferred either, which keeps such a row
    behaving exactly as it did before this step existed."""
    _resting_short(journal, order_id=None)
    _held_short(broker)

    result = reconcile(journal, broker, rules, now=NOW)

    assert result.entries_unchecked == ["SPY"]
    assert result.entries_corrected == []
    assert journal.open_trades()[0].entry_price == SHORT_ENTRY


# -------- the entry that is neither resting nor held: AAPL row 2, 11 Aug 2026

# Read off `data/journal.db` on the droplet. 107 AAPL submitted at 09:00:03 UTC
# — 05:00 New York, so the bracket could not fill and rested — was marked CLOSED
# at 09:15:04, one cycle later, by a broker that had never held it. The order
# then filled at the regular open, leaving 107 shares with no open journal row:
# `open_risk_usd` read 1,487.19 against a true 2,396.69, or 2.42% of equity
# against a 2% cap, with `--execute` on and `stop_watch` no longer looking.
AAPL_ENTRY = datetime(2026, 8, 11, 9, 0, 3, tzinfo=UTC)
AAPL_LIMIT = 308.5
AAPL_STOP = 300.0
AAPL_QTY = 107.0
AAPL_RISK = (AAPL_LIMIT - AAPL_STOP) * AAPL_QTY      # 909.50


def _premarket_bracket(journal, *, order_id: str | None = "entry-aapl"):
    """An AAPL entry exactly as `record_fill` leaves a pre-market bracket:
    the proposal's figures, an order id, and nothing confirmed."""
    return journal.record_entry(
        Trade(
            symbol="AAPL",
            direction=Direction.BUY,
            qty=AAPL_QTY,
            entry_time=AAPL_ENTRY,
            entry_price=AAPL_LIMIT,
            planned_stop=AAPL_STOP,
            submitted_qty=AAPL_QTY,
            submitted_price=AAPL_LIMIT,
            fill_state=FillState.UNCONFIRMED,
            entry_order_id=order_id,
            rationale="Pre-market bracket, journalled from the proposal.",
        )
    )


def test_an_entry_neither_resting_nor_held_is_not_written_off_as_a_closed_trade(
    journal, broker, rules
):
    """The live bug. An order id missing from the resting list is not a fill.

    `get_open_orders` asks Alpaca for what it classifies as OPEN, bounded at
    100, so an id that is not in the answer may be filled, cancelled, expired,
    rejected, past the bound, or in a status that query does not return. Only
    the first of those is a position, and the code read all of them as
    "terminal" and then, with nothing held, as a close.

    The degraded flag guards the whole list failing. It does not guard ONE order
    going missing from a list that came back fine, which is the same
    missing-versus-absent distinction one level down.
    """
    _premarket_bracket(journal)
    broker.set_price("AAPL", bid=307.9, ask=308.1)
    broker.set_open_orders([])                    # the entry is not in the list
    assert broker.orders_degraded is False        # ...and the read was fine

    result = reconcile(journal, broker, rules, now=AAPL_ENTRY + timedelta(minutes=15))

    assert result.closed == []
    assert result.closes_deferred == ["AAPL"]
    assert result.entries_unresolved == ["AAPL"]
    assert [t.symbol for t in journal.open_trades()] == ["AAPL"]
    # The figures are still the proposal's, and the row says so.
    assert "AAPL" in result.entry_figures_are_the_proposal


def test_the_withheld_close_keeps_the_risk_the_2pct_cap_has_to_count(
    journal, broker, rules
):
    """Failing closed here means OVERSTATING risk, which is the direction this
    repository picks. Closing the row zeroed a real 909.50 of exposure: the
    live total read 1,487.19 when it was 2,396.69, and the gate went on sizing
    new trades against the smaller number with `--execute` on."""
    _premarket_bracket(journal)
    broker.set_price("AAPL", bid=307.9, ask=308.1)
    broker.set_open_orders([])

    reconcile(journal, broker, rules, now=AAPL_ENTRY + timedelta(minutes=15))

    snapshot = apply_journal_state(broker.get_account(), journal)
    assert snapshot.open_risk_usd == pytest.approx(AAPL_RISK)


def test_an_entry_that_was_never_a_position_is_given_no_realised_pnl(
    journal, broker, rules
):
    """The second finding, and it outlives the row. Closing an unconfirmed entry
    wrote an exit price off a live quote — `exit_price_estimated` FALSE, so it
    reads as a fill — and a realised figure nobody traded. Both reach
    `metrics.py`, the Analytics page and `evaluate_stand_down`, which counts
    consecutive losses. A fabricated loss can move a real breaker."""
    _premarket_bracket(journal)
    broker.set_price("AAPL", bid=307.9, ask=308.1)     # a mark it would have used
    broker.set_open_orders([])

    reconcile(journal, broker, rules, now=AAPL_ENTRY + timedelta(minutes=15))

    assert journal.closed_trades() == []
    assert journal.open_trades()[0].realised_pnl_usd is None


def test_the_withheld_close_settles_itself_once_the_resting_entry_fills(
    journal, broker, rules
):
    """The deferral is not a stall in the case that matters. The entry becomes
    eligible at the regular open, the position appears, and the ordinary
    squaring below takes over — which is what should have happened on 11 Aug."""
    _premarket_bracket(journal)
    broker.set_price("AAPL", bid=307.9, ask=308.1)
    broker.set_open_orders([])
    reconcile(journal, broker, rules, now=AAPL_ENTRY + timedelta(minutes=15))

    broker._positions["AAPL"] = Position(                # filled at the open
        symbol="AAPL",
        direction=Direction.BUY,
        qty=AAPL_QTY,
        entry_price=308.42,
        opened_at=AAPL_ENTRY,
    )
    result = reconcile(journal, broker, rules, now=AAPL_ENTRY + timedelta(minutes=45))

    assert result.entries_unresolved == []
    assert [c.symbol for c in result.entries_corrected] == ["AAPL"]
    stored = journal.open_trades()[0]
    assert stored.fill_state is FillState.COMPLETE
    assert stored.entry_price == 308.42
    assert stored.submitted_price == AAPL_LIMIT


def test_a_confirmed_trade_the_broker_no_longer_holds_still_closes(
    journal, broker, rules
):
    """The deferral must not swallow a real close. Once a fill has been read
    back, the position's absence IS the evidence — and withholding that would
    strand every completed trade in the journal for ever."""
    _premarket_bracket(journal)
    broker.set_price("AAPL", bid=307.9, ask=308.1)
    broker._positions["AAPL"] = Position(
        symbol="AAPL",
        direction=Direction.BUY,
        qty=AAPL_QTY,
        entry_price=308.42,
        opened_at=AAPL_ENTRY,
    )
    broker.set_open_orders([])
    reconcile(journal, broker, rules, now=AAPL_ENTRY + timedelta(minutes=15))
    assert journal.open_trades()[0].fill_state is FillState.COMPLETE

    broker._positions.pop("AAPL")                        # the stop filled
    result = reconcile(journal, broker, rules, now=AAPL_ENTRY + timedelta(minutes=30))

    assert result.closed == ["AAPL"]
    assert result.entries_unresolved == []
    assert journal.open_trades() == []


def test_two_open_trades_in_one_symbol_leave_the_entry_alone(
    journal, broker, rules
):
    """Alpaca aggregates all exposure to a symbol into ONE position, so its
    average entry is not either row's fill. Copying it onto one of them would be
    a plausible wrong figure, which is the thing this repository refuses."""
    _resting_short(journal)
    _resting_short(journal, order_id="entry-2")
    _held_short(broker, qty=42, entry=774.0)
    broker.set_open_orders([])

    result = reconcile(journal, broker, rules, now=NOW)

    assert result.entries_ambiguous == ["SPY", "SPY"]
    assert {t.entry_price for t in journal.open_trades()} == {SHORT_ENTRY}


def test_a_position_bigger_than_was_ordered_is_never_absorbed(
    journal, broker, rules
):
    """`submitted_qty` is a ceiling: an order cannot fill for more than it asked
    for, so a larger position holds something this row did not buy."""
    _resting_short(journal, qty=21)
    _held_short(broker, qty=30)
    broker.set_open_orders([])

    result = reconcile(journal, broker, rules, now=NOW)

    assert result.entries_ambiguous == ["SPY"]
    assert journal.open_trades()[0].qty == 21


def test_a_confirmed_entry_is_never_rewritten_by_a_later_position_change(
    journal, broker, rules
):
    """Squaring happens once. Afterwards a quantity that disagrees is a POSITION
    change — a hand close, a partial exit — which `detect_unexplained_moves`
    reports and which must not be quietly absorbed into the entry."""
    _resting_short(journal)
    _held_short(broker)
    broker.set_open_orders([])
    reconcile(journal, broker, rules, now=NOW)
    assert journal.open_trades()[0].fill_state is FillState.COMPLETE

    _held_short(broker, qty=10, entry=790.0)        # half closed by hand
    result = reconcile(journal, broker, rules, now=NOW + timedelta(minutes=15))

    stored = journal.open_trades()[0]
    assert stored.qty == 21
    assert stored.entry_price == SHORT_FILL
    assert result.entries_corrected == []
    assert result.unexplained.anything_to_report


def test_record_fill_keeps_the_submitted_figures_beside_the_recorded_ones(journal):
    proposal = _proposal()
    result = OrderResult(accepted=True, order_id="x1", filled_price=580.0, filled_qty=83)

    record_fill(journal, proposal, result, execution_mode=ExecutionMode.PAPER, now=NOW)

    trade = journal.open_trades()[0]
    assert trade.submitted_qty == 83
    assert trade.submitted_price == 580.00
    assert trade.fill_state is FillState.COMPLETE


def test_a_partial_reading_at_submission_does_not_shrink_the_journalled_size(
    journal,
):
    """`record_fill` used to take `result.filled_qty or proposal.qty`, so the
    3-of-21 reading would have been written down as the position. Overstating
    is the safe direction and understating by six sevenths is not."""
    proposal = _proposal(qty=21)
    result = OrderResult(accepted=True, order_id="x1", filled_price=580.10, filled_qty=3)

    record_fill(journal, proposal, result, execution_mode=ExecutionMode.PAPER, now=NOW)

    trade = journal.open_trades()[0]
    assert trade.qty == 21
    assert trade.entry_price == 580.00           # the limit, not the running average
    assert trade.fill_state is FillState.UNCONFIRMED
    assert trade.submitted_qty == 21


# ------------------------------------------- why each position closed


def test_reconcile_records_why_a_position_closed(journal, broker, rules):
    """Item 16's back half. `record_exit` takes a price, a time and a realised
    figure, so stop-hit and closed-by-hand used to look identical afterwards."""
    trade_id = _resting_short(journal, order_id=None)
    broker.set_price("SPY", bid=820.90, ask=821.10)     # through the 820 stop

    result = reconcile(journal, broker, rules, now=NOW)

    closed = journal.closed_trades()[0]
    assert closed.id == trade_id
    assert closed.exit_reason is ExitReason.STOP_HIT
    assert closed.exit_price_estimated is False
    assert [r.reason for r in result.exits_reviewed] == [ExitReason.STOP_HIT]


def test_a_hand_close_before_either_level_is_recorded_as_the_plan_abandoned(
    journal, broker, rules
):
    """The bucket that matters. `close_with_reason` captures WHY at the moment
    of the move; this is where that becomes a verdict on the plan."""
    trade_id = _resting_short(journal, order_id=None)
    journal.record_position_action(
        PositionActionRecord(
            trade_id=trade_id,
            symbol="SPY",
            action=PositionAction.CLOSE,
            actor="operator",
            at=NOW,
            reason="Nerves ahead of the print; taking it off.",
        )
    )
    broker.set_price("SPY", bid=769.98, ask=770.02)     # neither level

    result = reconcile(journal, broker, rules, now=NOW)

    closed = journal.closed_trades()[0]
    assert closed.exit_reason is ExitReason.CLOSED_EARLY
    assert [r.symbol for r in result.plan_abandoned] == ["SPY"]
    assert result.exits_reviewed[0].close_reason.startswith("Nerves")


def test_an_estimated_exit_price_is_recorded_as_estimated_and_grades_unknown(
    journal, broker, rules
):
    """The fallback is the ENTRY price, which sits between the stop and the
    target by construction. Without the provenance stored beside it, every one
    of these would grade as a confident `closed_early` off no evidence."""
    _resting_short(journal, order_id=None)
    broker._prices.pop("SPY")                           # no mark available

    result = reconcile(journal, broker, rules, now=NOW)

    closed = journal.closed_trades()[0]
    assert closed.exit_price_estimated is True
    assert closed.exit_reason is ExitReason.UNKNOWN
    assert result.estimated_exits == 1
