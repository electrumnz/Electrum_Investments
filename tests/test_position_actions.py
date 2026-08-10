"""Tests for acting on an open position, and for recording why.

**The load-bearing test in this file is the one that proves a WIDEN is
refused**, on both sides of the market and with the position left untouched.
Everything else here is about a feature working; that one is about the only
thing standing between an open position and a larger loss, because
`RiskGate.evaluate` never sees a position move.

The fixtures use the live position's own numbers — SHORT 21 SPY at 773.324285
with a stop at 820, whose stop leg is a resting BUY 21 — so a short is the
default case rather than an afterthought. A tightening rule written for the long
case silently inverts on a short, and the one real position this bot holds is
short.

Nothing here touches the network or a real account: `MockBroker` only, and every
journal is built in a `tmp_path`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bot.broker import MockBroker
from bot.config import load_rules
from bot.journal import Journal
from bot.models import (
    Direction,
    ExecutionMode,
    OrderStatus,
    Position,
    PositionAction,
    PositionActionRecord,
    PositionPlan,
    Trade,
    WorkingOrder,
)
from bot.position_actions import (
    StopMove,
    classify_stop_move,
    close_with_reason,
    detect_unexplained_moves,
    execute_position_plan,
    loop_execution_state,
    resting_stop_legs,
    tighten_stop,
)

# The live position, exactly. See TODO.md CURRENT STATE.
ENTRY_TIME = datetime(2026, 8, 10, 13, 37, 40, tzinfo=UTC)
NOW = datetime(2026, 8, 10, 16, 0, tzinfo=UTC)
SPY_ENTRY = 773.324285
SPY_STOP = 820.0
SPY_QTY = 21.0
STOP_LEG_ID = "952237ac-d7ec-426e-bb5f-5c6ce7294260"


def _short_spy() -> Trade:
    """Journal row 1: short 21 SPY, stop 820, $980.19 of open risk."""
    return Trade(
        symbol="SPY",
        direction=Direction.SELL,
        strategy="manual",
        qty=SPY_QTY,
        entry_time=ENTRY_TIME,
        entry_price=SPY_ENTRY,
        planned_stop=SPY_STOP,
        planned_target=None,
        rationale="Placed by hand as an operator test of the out-of-hours path.",
        execution_mode=ExecutionMode.PAPER,
        entry_order_id="a76f0545-5db0-445e-9b31-c538b371b7a6",
    )


def _stop_leg(stop_price: float | None = SPY_STOP, order_id: str = STOP_LEG_ID):
    """The resting BUY 21 that closes the short. It is the only thing between
    that position and an unbounded loss, which is why TODO item 5 says not to
    clear it."""
    return WorkingOrder(
        order_id=order_id,
        symbol="SPY",
        direction=Direction.BUY,
        qty=SPY_QTY,
        limit_price=None,
        stop_price=stop_price,
        order_type="stop",
        status=OrderStatus.OTHER,
        broker_status="held",
        submitted_at=datetime(2026, 8, 10, 13, 35, tzinfo=UTC),
    )


def _position(qty: float = SPY_QTY) -> Position:
    return Position(
        symbol="SPY",
        direction=Direction.SELL,
        qty=qty,
        entry_price=SPY_ENTRY,
        opened_at=ENTRY_TIME,
        current_price=774.09,
    )


@pytest.fixture
def journal(tmp_path) -> Journal:
    j = Journal(tmp_path / "journal.db")
    j.record_entry(_short_spy())
    return j


@pytest.fixture
def broker() -> MockBroker:
    b = MockBroker()
    b.connect()
    b.set_price("SPY", bid=774.08, ask=774.10)
    b.set_open_orders([_stop_leg()])
    return b


# ------------------------------------------------- which way is tighter


def test_a_long_tightens_upward_and_a_short_downward():
    """The direction test comes from the POSITION, never from an assumption.

    Half the trades this repository can hold are shorts, and the live one is.
    A comparison written for the long case reads a short's tightening as a
    widening and vice versa — which would refuse the safe move and permit the
    dangerous one, on exactly the position it was needed for.
    """
    long_ = Direction.BUY
    assert (
        classify_stop_move(direction=long_, current_stop=575.0, new_stop=578.0)
        is StopMove.TIGHTER
    )
    assert (
        classify_stop_move(direction=long_, current_stop=575.0, new_stop=570.0)
        is StopMove.WIDER
    )

    short = Direction.SELL
    assert (
        classify_stop_move(direction=short, current_stop=SPY_STOP, new_stop=800.0)
        is StopMove.TIGHTER
    )
    assert (
        classify_stop_move(direction=short, current_stop=SPY_STOP, new_stop=840.0)
        is StopMove.WIDER
    )


def test_a_stop_moved_past_entry_is_still_tightening():
    """Breakeven and beyond is the END of tightening, not a different act.

    Measured along the side's own axis rather than as distance from entry: on a
    short, 770 is below the 773.32 entry and therefore FURTHER from it than 800,
    while being unambiguously the same pull downward. A rule written against
    entry would call locking in a profit a widening.
    """
    assert (
        classify_stop_move(
            direction=Direction.SELL, current_stop=800.0, new_stop=SPY_ENTRY
        )
        is StopMove.TIGHTER
    )
    assert (
        classify_stop_move(direction=Direction.SELL, current_stop=800.0, new_stop=770.0)
        is StopMove.TIGHTER
    )


def test_a_move_smaller_than_a_price_tick_is_unchanged():
    """Prices are carried to four places, so anything under half a
    ten-thousandth is the same price written twice. An exact float comparison
    would report a move that is entirely an artefact of binary representation."""
    assert (
        classify_stop_move(
            direction=Direction.SELL, current_stop=820.0, new_stop=820.000001
        )
        is StopMove.UNCHANGED
    )


# ------------------------------------------------------- the refusals


def test_widening_a_short_stop_is_refused_and_nothing_moves(journal, broker):
    """**The test this feature exists for.**

    `RiskGate.evaluate` gates proposals that OPEN exposure and never sees a
    position move — deliberately, so a stand-down cannot strand an open trade.
    That exemption is safe only for moves that reduce exposure. Widening a stop
    increases the loss at unchanged size on a live position with no gate behind
    it anywhere, so it has to be impossible here or it is possible everywhere.

    Verified on three surfaces at once: the refusal, the leg still resting at
    its original trigger, and the journal untouched. A refusal that had already
    replaced the order would be worse than no refusal, because it would report
    safety while having moved the stop.
    """
    outcome = tighten_stop(
        journal,
        broker,
        symbol="SPY",
        new_stop=840.0,
        reason="Give it room to breathe through the print.",
        actor="trader",
        now=NOW,
    )

    assert not outcome.ok
    assert any("REFUSED" in r for r in outcome.reasons)
    assert any("further from entry" in r for r in outcome.reasons)

    # The leg is exactly where it was, under its original id.
    resting = resting_stop_legs(broker.get_open_orders(), "SPY")
    assert [(o.order_id, o.stop_price) for o in resting] == [(STOP_LEG_ID, SPY_STOP)]

    # And nothing was written: no action, and the trade's stop is untouched.
    assert journal.position_actions() == []
    held = journal.open_trade_for("SPY")
    assert held is not None
    assert held.current_stop is None
    assert held.effective_stop == SPY_STOP


def test_widening_a_long_stop_is_refused_too(tmp_path):
    """The same rule on the other side, because getting one right and the other
    backwards is the specific way this fails."""
    journal = Journal(tmp_path / "journal.db")
    journal.record_entry(
        Trade(
            symbol="QQQ",
            direction=Direction.BUY,
            qty=10,
            entry_time=ENTRY_TIME,
            entry_price=500.0,
            planned_stop=495.0,
            rationale="A long, so its stop tightens upward.",
        )
    )
    broker = MockBroker()
    broker.connect()
    broker.set_open_orders(
        [
            WorkingOrder(
                order_id="leg-qqq",
                symbol="QQQ",
                direction=Direction.SELL,
                qty=10,
                stop_price=495.0,
                order_type="stop",
            )
        ]
    )

    outcome = tighten_stop(
        journal,
        broker,
        symbol="QQQ",
        new_stop=490.0,
        reason="Widen it under the swing low.",
        actor="trader",
        now=NOW,
    )
    assert not outcome.ok
    assert any("REFUSED" in r for r in outcome.reasons)
    assert broker.get_open_orders()[0].stop_price == 495.0
    assert journal.position_actions() == []


@pytest.mark.parametrize("reason", ["", "   ", "\n\t "])
def test_a_move_with_no_reason_is_refused(journal, broker, reason):
    """The reason is the whole point of recording the move.

    A blank one would be an explained move that explains nothing — and it would
    silence the unexplained-move report while doing it, which is worse than
    never having recorded the move at all.
    """
    outcome = tighten_stop(
        journal,
        broker,
        symbol="SPY",
        new_stop=800.0,
        reason=reason,
        actor="trader",
        now=NOW,
    )
    assert not outcome.ok
    assert any("reason is required" in r for r in outcome.reasons)
    assert broker.get_open_orders()[0].stop_price == SPY_STOP
    assert journal.position_actions() == []

    closed = close_with_reason(
        journal, broker, symbol="SPY", reason=reason, actor="trader", now=NOW
    )
    assert not closed.ok
    assert journal.position_actions() == []


def test_a_reason_that_is_only_whitespace_cannot_be_stored_at_all(journal):
    """The guard is in the model and again in the journal, and this proves the
    second one rather than the first. A caller that built the record some other
    way still cannot write a blank reason."""
    with pytest.raises(ValueError):
        PositionActionRecord(
            symbol="SPY",
            action=PositionAction.CLOSE,
            actor="trader",
            at=NOW,
            reason="   ",
        )

    record = PositionActionRecord(
        symbol="SPY",
        action=PositionAction.CLOSE,
        actor="trader",
        at=NOW,
        reason="real",
    )
    record.reason = "  "  # bypasses validation, as an assignment does
    with pytest.raises(ValueError, match="needs a reason"):
        journal.record_position_action(record)


def test_a_move_to_the_same_level_is_refused_rather_than_recorded(journal, broker):
    """Nothing moved, so there is nothing to record. A row saying the stop went
    from 820 to 820 is noise in the one log that has to stay readable."""
    outcome = tighten_stop(
        journal,
        broker,
        symbol="SPY",
        new_stop=SPY_STOP,
        reason="Reaffirming the level.",
        actor="trader",
        now=NOW,
    )
    assert not outcome.ok
    assert any("already at" in r for r in outcome.reasons)
    assert journal.position_actions() == []


def test_every_refusal_is_collected_rather_than_the_first(journal, broker):
    """The property `RiskGate` is built around, for the same reason: a caller
    told one thing wrong, fixing it and being told the second has been made to
    ask twice for information that existed at once."""
    outcome = tighten_stop(
        journal, broker, symbol="SPY", new_stop=-5.0, reason="", actor="trader", now=NOW
    )
    assert not outcome.ok
    assert len(outcome.reasons) >= 2
    assert any("reason is required" in r for r in outcome.reasons)
    assert any("not a price" in r for r in outcome.reasons)


def test_a_symbol_with_no_journal_row_cannot_have_its_stop_moved(tmp_path, broker):
    """The stop in force is unknowable without a journal row, so a move measured
    against it would be a number nobody can check. Same fact as the symbol
    appearing in `symbols_with_unknown_risk`."""
    journal = Journal(tmp_path / "empty.db")
    outcome = tighten_stop(
        journal,
        broker,
        symbol="SPY",
        new_stop=800.0,
        reason="Pull it in.",
        actor="trader",
        now=NOW,
    )
    assert not outcome.ok
    assert any("no open journal row" in r for r in outcome.reasons)


def test_a_degraded_order_read_refuses_rather_than_moving_blind(journal, broker):
    """An empty order list from a failed fetch looks exactly like an account
    with nothing resting. Moving a stop on that reading could replace the wrong
    thing, so it fails closed — the `FinnhubCalendar.is_degraded` rule arriving
    on the order path."""
    broker.set_orders_degraded(True)
    outcome = tighten_stop(
        journal,
        broker,
        symbol="SPY",
        new_stop=800.0,
        reason="Pull it in ahead of the close.",
        actor="trader",
        now=NOW,
    )
    assert not outcome.ok
    assert any("could not be read" in r for r in outcome.reasons)
    assert journal.position_actions() == []


def test_two_resting_stop_legs_are_ambiguous_and_refused(journal, broker):
    """Replacing the wrong one would leave the other at its old trigger, with
    the response reporting success."""
    broker.set_open_orders([_stop_leg(), _stop_leg(order_id="second-leg")])
    outcome = tighten_stop(
        journal,
        broker,
        symbol="SPY",
        new_stop=800.0,
        reason="Pull it in.",
        actor="trader",
        now=NOW,
    )
    assert not outcome.ok
    assert any("resting stop legs" in r for r in outcome.reasons)


def test_a_broker_refusal_writes_nothing(journal, broker):
    """A journal saying 800 while 820 rests at Alpaca is worse than no record:
    the caps would count a risk figure that does not exist and the model would
    be shown a level it cannot rely on."""
    broker.set_replace_refused("order is not replaceable in its current state")
    outcome = tighten_stop(
        journal,
        broker,
        symbol="SPY",
        new_stop=800.0,
        reason="Pull it in ahead of the close.",
        actor="trader",
        now=NOW,
    )
    assert not outcome.ok
    assert any("broker refused the replace" in r for r in outcome.reasons)
    assert journal.position_actions() == []
    held = journal.open_trade_for("SPY")
    assert held is not None and held.current_stop is None
    assert broker.get_open_orders()[0].stop_price == SPY_STOP


# --------------------------------------------------------- the move itself


def test_tightening_replaces_the_leg_updates_the_journal_and_reads_back(journal, broker):
    """The whole path, end to end, on the live position's own numbers.

    Four things have to be true together, and each of them is a different
    surface: the broker's leg carries the new trigger under a NEW id, the
    journal's stop in force follows it, the risk the caps count falls, and the
    reason is readable back afterwards.
    """
    before_risk = journal.open_risk_usd()
    assert round(before_risk, 2) == 980.19  # |773.324285 - 820| x 21

    outcome = tighten_stop(
        journal,
        broker,
        symbol="SPY",
        new_stop=800.0,
        reason="Two sessions without a lower high; taking the room back.",
        actor="trader",
        now=NOW,
    )

    assert outcome.ok
    assert outcome.reasons == []
    assert outcome.reached_broker

    # Alpaca REPLACES a leg rather than editing it, so the id changes and the
    # old one is gone. A mock that edited in place would let this pass while
    # production held a stale id.
    legs = resting_stop_legs(broker.get_open_orders(), "SPY")
    assert len(legs) == 1
    assert legs[0].stop_price == 800.0
    assert legs[0].order_id != STOP_LEG_ID
    assert legs[0].order_id == outcome.broker_order_id

    # The journal's stop in force followed; the SIZING stop did not.
    held = journal.open_trade_for("SPY")
    assert held is not None
    assert held.current_stop == 800.0
    assert held.effective_stop == 800.0
    assert held.planned_stop == SPY_STOP
    assert round(held.planned_risk_usd, 2) == 980.19
    assert round(held.current_risk_usd, 2) == 560.19  # |773.324285 - 800| x 21

    # So the cap counts the risk that is actually in force.
    assert round(journal.open_risk_usd(), 2) == 560.19
    assert round(outcome.risk_reduced_usd or 0.0, 2) == 420.0

    # And the reason is readable back.
    actions = journal.position_actions(symbol="SPY")
    assert len(actions) == 1
    recorded = actions[0]
    assert recorded.action is PositionAction.TIGHTEN_STOP
    assert recorded.actor == "trader"
    assert recorded.before_stop == SPY_STOP
    assert recorded.after_stop == 800.0
    assert recorded.reached_broker
    assert recorded.broker_order_id == outcome.broker_order_id
    assert "lower high" in recorded.reason
    assert recorded.at == NOW


def test_the_sizing_stop_survives_so_R_keeps_meaning_what_it_meant(journal, broker):
    """`planned_stop` is the denominator of R. Overwriting it on a tighten would
    silently redefine one R after the fact, and a trade that lost half of what
    it risked would read as a full stop-out."""
    tighten_stop(
        journal,
        broker,
        symbol="SPY",
        new_stop=790.0,
        reason="Halving the risk into the weekend.",
        actor="operator",
        now=NOW,
    )
    held = journal.open_trade_for("SPY")
    assert held is not None
    assert held.planned_stop == SPY_STOP
    assert held.stop_has_moved


def test_a_stop_moved_past_entry_reports_zero_risk_rather_than_growing_risk(
    journal, broker
):
    """`abs()` would report a locked-in profit as risk growing the further into
    profit it went. Zero is the honest answer: there is nothing left to lose."""
    outcome = tighten_stop(
        journal,
        broker,
        symbol="SPY",
        new_stop=770.0,  # below the 773.32 short entry
        reason="Locking the position in above water.",
        actor="trader",
        now=NOW,
    )
    assert outcome.ok
    assert outcome.risk_after_usd == 0.0
    assert journal.open_risk_usd() == 0.0


def test_a_symbol_with_no_resting_stop_moves_in_the_journal_and_says_so(journal, broker):
    """Not refused — crypto has never had a broker-side stop and an equity whose
    bracket was cancelled is in the same position — but the response must never
    let "the stop moved" pass for "the stop moved at the broker"."""
    broker.set_open_orders([])
    outcome = tighten_stop(
        journal,
        broker,
        symbol="SPY",
        new_stop=800.0,
        reason="Pulling it in even though nothing is resting.",
        actor="operator",
        now=NOW,
    )
    assert outcome.ok
    assert not outcome.reached_broker
    assert any("JOURNAL FIGURE ONLY" in w for w in outcome.warnings)
    assert any("not normal for an equity" in w for w in outcome.warnings)
    assert journal.position_actions()[0].reached_broker is False


def test_an_unreadable_trigger_is_warned_about_rather_than_assumed(journal, broker):
    """A stop leg whose trigger the broker did not report is a leg nobody can
    say the level of. The replace still goes out — against the journal's figure,
    which is the only one there is — and the response says which."""
    broker.set_open_orders([_stop_leg(stop_price=None)])
    outcome = tighten_stop(
        journal,
        broker,
        symbol="SPY",
        new_stop=800.0,
        reason="Pull it in; the leg reads blank on every surface.",
        actor="operator",
        now=NOW,
    )
    assert outcome.ok
    assert any("no trigger price" in w for w in outcome.warnings)


# ------------------------------------------------------------- closing


def test_closing_records_the_reason_and_leaves_the_exit_to_reconcile(journal, broker):
    """The reason is the one thing `record_exit` can never reconstruct: a
    stop-hit, a target-hit and a deliberate close look identical afterwards."""
    broker._positions["SPY"] = _position()  # the mock holds it so the close fills

    outcome = close_with_reason(
        journal,
        broker,
        symbol="SPY",
        reason="Thesis is done; the gap filled this morning.",
        actor="trader",
        now=NOW,
    )
    assert outcome.ok
    assert outcome.reached_broker

    actions = journal.position_actions(symbol="SPY")
    assert len(actions) == 1
    assert actions[0].action is PositionAction.CLOSE
    assert actions[0].after_qty == 0.0
    assert actions[0].before_qty == SPY_QTY
    assert "gap filled" in actions[0].reason

    # The trade row is still open here on purpose: reconcile writes the exit
    # price, the time and the realised figure on the next cycle.
    assert journal.open_trade_for("SPY") is not None


def test_a_broker_refusal_on_a_close_records_nothing(journal, broker):
    """A close that did not happen is not a close."""
    outcome = close_with_reason(
        journal,
        broker,
        symbol="NOPE",
        reason="Not held.",
        actor="trader",
        now=NOW,
    )
    assert not outcome.ok
    assert journal.position_actions() == []


def test_a_position_the_journal_never_saw_is_still_closed(tmp_path, broker):
    """Refusing to reduce exposure over incomplete paperwork would be the wrong
    way round. The record is written with no trade behind it and says so."""
    journal = Journal(tmp_path / "empty.db")
    broker._positions["SPY"] = _position()

    outcome = close_with_reason(
        journal,
        broker,
        symbol="SPY",
        reason="Adopted from the broker and unwanted.",
        actor="operator",
        now=NOW,
    )
    assert outcome.ok
    assert any("no open journal row" in w for w in outcome.warnings)
    assert journal.position_actions()[0].trade_id is None


# ------------------------------------------------ the loop's switch is off


def test_the_shipped_config_does_not_let_the_loop_act_unattended():
    """The operator asked for the AGENT to be able to action its trade, which
    the MCP tools satisfy with a person in the conversation. A loop closing a
    position at 3am is a different proposition and ships off."""
    rules = load_rules()
    enabled, sentence = loop_execution_state(rules)
    assert enabled is False
    assert "records its position plans and acts on none of them" in sentence


def test_execute_position_plan_refuses_while_the_switch_is_off(journal, broker):
    """The flag is read HERE rather than by the caller, so a new call site that
    forgets to check it still gets a refusal. Fails closed."""
    rules = load_rules()
    assert rules.position_actions.enabled is False

    plan = PositionPlan(
        symbol="SPY",
        action=PositionAction.CLOSE,
        reasoning="The thesis is finished and I want out.",
    )
    outcome = execute_position_plan(
        plan, rules=rules, journal=journal, broker=broker, now=NOW
    )
    assert not outcome.ok
    assert any("position_actions.enabled is false" in r for r in outcome.reasons)
    assert journal.position_actions() == []


def test_with_the_switch_on_the_loop_still_cannot_widen(journal, broker):
    """The flag arms the trigger; it does not change what may be pulled."""
    rules = load_rules()
    rules.position_actions.enabled = True

    plan = PositionPlan(
        symbol="SPY",
        action=PositionAction.TIGHTEN_STOP,
        new_stop_price=850.0,
        reasoning="Widening it to survive the print, which must not be possible.",
    )
    outcome = execute_position_plan(
        plan, rules=rules, journal=journal, broker=broker, now=NOW
    )
    assert not outcome.ok
    assert any("REFUSED" in r for r in outcome.reasons)
    assert broker.get_open_orders()[0].stop_price == SPY_STOP


def test_a_tighten_plan_with_no_level_is_refused_rather_than_guessed(journal, broker):
    """A stop invented to fill an empty field is an exit nobody chose — the
    invented-take-profit failure arriving on the stop side, where it is worse."""
    rules = load_rules()
    rules.position_actions.enabled = True

    plan = PositionPlan(
        symbol="SPY",
        action=PositionAction.TIGHTEN_STOP,
        reasoning="Bring the stop in, somewhere, I have not said where.",
    )
    outcome = execute_position_plan(
        plan, rules=rules, journal=journal, broker=broker, now=NOW
    )
    assert not outcome.ok
    assert any("names no level" in r for r in outcome.reasons)


def test_a_hold_executes_nothing_and_records_nothing(journal, broker):
    """A hold is already in the audit log on every cycle. Writing a row every
    fifteen minutes for every open position would bury the moves that matter."""
    rules = load_rules()
    rules.position_actions.enabled = True

    plan = PositionPlan(
        symbol="SPY",
        action=PositionAction.HOLD,
        reasoning="Still short, still working, nothing has changed.",
    )
    outcome = execute_position_plan(
        plan, rules=rules, journal=journal, broker=broker, now=NOW
    )
    assert outcome.ok
    assert journal.position_actions() == []


# -------------------------------------------- the inverse: unexplained moves


def test_a_stop_changed_at_the_broker_with_no_reason_is_reported(journal):
    """The half that makes the record honest. A stop pulled in through Alpaca's
    web UI shows nowhere else at all."""
    report = detect_unexplained_moves(
        positions=[_position()],
        orders=[_stop_leg(stop_price=805.0)],
        open_trades=journal.open_trades(),
        actions=[],
    )
    assert [m.kind for m in report.moves] == ["stop"]
    assert report.moves[0].journal_value == SPY_STOP
    assert report.moves[0].broker_value == 805.0
    assert "no recorded action" in report.moves[0].describe()


def test_a_recorded_move_explains_the_level_now_resting(journal, broker):
    """The whole point: a move made through this path is NOT reported."""
    tighten_stop(
        journal,
        broker,
        symbol="SPY",
        new_stop=800.0,
        reason="Taking the room back after two flat sessions.",
        actor="trader",
        now=NOW,
    )
    report = detect_unexplained_moves(
        positions=[_position()],
        orders=broker.get_open_orders(),
        open_trades=journal.open_trades(),
        actions=journal.position_actions(),
    )
    assert report.moves == []


def test_a_quantity_that_changed_with_no_reason_is_reported(journal):
    """A partial fill nobody journalled, or a hand-trimmed position."""
    report = detect_unexplained_moves(
        positions=[_position(qty=14.0)],
        orders=[_stop_leg()],
        open_trades=journal.open_trades(),
        actions=[],
    )
    assert [m.kind for m in report.moves] == ["quantity"]
    assert report.moves[0].broker_value == 14.0


def test_an_unreadable_trigger_is_its_own_finding_not_a_clean_one(journal):
    """`stop_price is None` on a stop leg means whether it moved is UNKNOWN.
    Folding it into "no moves found" would let an unreadable stop read as a
    verified one."""
    report = detect_unexplained_moves(
        positions=[_position()],
        orders=[_stop_leg(stop_price=None)],
        open_trades=journal.open_trades(),
        actions=[],
    )
    assert report.moves == []
    assert report.unreadable_stops == ["SPY"]
    assert report.anything_to_report


def test_a_degraded_order_read_cannot_clear_anything(journal):
    """An empty `moves` means nothing at all when the orders could not be read.
    Same rule as `has_cycles` and `can_grade_anything`: the absence of evidence
    is its own state and must not share a shape with the good outcome."""
    report = detect_unexplained_moves(
        positions=[_position()],
        orders=[],
        open_trades=journal.open_trades(),
        actions=[],
        orders_degraded=True,
    )
    assert report.moves == []
    assert report.can_check is False
    assert report.anything_to_report


def test_an_equity_with_no_resting_stop_is_named_and_crypto_is_not(tmp_path):
    """Alpaca accepts no bracket on crypto, so a crypto position with no stop
    order is the normal arrangement rather than a finding. An equity with none
    means nothing at the broker would close it."""
    journal = Journal(tmp_path / "journal.db")
    journal.record_entry(_short_spy())
    journal.record_entry(
        Trade(
            symbol="BTC/USD",
            direction=Direction.BUY,
            qty=0.1,
            entry_time=ENTRY_TIME,
            entry_price=60_000.0,
            planned_stop=58_000.0,
            rationale="Crypto carries no broker-side stop, by construction.",
        )
    )
    crypto_position = Position(
        symbol="BTC/USD",
        direction=Direction.BUY,
        qty=0.1,
        entry_price=60_000.0,
        opened_at=ENTRY_TIME,
    )

    report = detect_unexplained_moves(
        positions=[_position(), crypto_position],
        orders=[],
        open_trades=journal.open_trades(),
        actions=[],
    )
    assert report.positions_without_a_resting_stop == ["SPY"]


def test_a_held_position_the_journal_never_saw_is_not_an_unexplained_move(tmp_path):
    """It is already reported by name as `symbols_with_unknown_risk`. Counting
    it here would be the same fact told twice under a tag meaning something
    else."""
    journal = Journal(tmp_path / "empty.db")
    report = detect_unexplained_moves(
        positions=[_position()],
        orders=[_stop_leg(stop_price=805.0)],
        open_trades=journal.open_trades(),
        actions=[],
    )
    assert report.moves == []
    assert report.positions_without_a_resting_stop == []
