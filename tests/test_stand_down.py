"""Tests for the consecutive-loss circuit breaker.

Two properties matter most and both have explicit tests: a stand-down survives a
process restart (otherwise restarting defeats it, which is exactly what someone
tilting would do), and paper trading is never blocked by one (the rule is "can't
trade money", not "stop trading").
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.config import Rules, StandDownRules
from bot.journal import Journal
from bot.models import (
    AccountSnapshot,
    Direction,
    ExecutionMode,
    PositionAction,
    PositionActionRecord,
    StandDownState,
    StreakBreaker,
    Trade,
)
from bot.risk import RiskGate
from bot.stand_down import describe, evaluate_stand_down

from .conftest import INSIDE_SESSION, PAPER_EQUITY

RULES = StandDownRules(
    consecutive_losses_trigger=3,
    loss_threshold_r=0.25,
    stage_one_days=3,
    stage_two_days=10,
    repeat_window_days=30,
)


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "journal.db")


def _close_trade(journal: Journal, pnl: float, *, minutes: int = 0) -> None:
    """Open and close one trade with $50 of planned risk, so pnl/50 is its R."""
    when = INSIDE_SESSION + timedelta(minutes=minutes)
    trade_id = journal.record_entry(
        Trade(
            symbol="SPY",
            direction=Direction.BUY,
            qty=10,
            entry_time=when,
            entry_price=580.0,
            planned_stop=575.0,
            planned_target=590.0,
            rationale="Test trade.",
        )
    )
    journal.record_exit(
        trade_id,
        exit_time=when + timedelta(minutes=30),
        exit_price=580.0,
        realised_pnl_usd=pnl,
    )


# ---------------------------------------------------------------- triggering


def test_no_stand_down_below_the_trigger(journal):
    _close_trade(journal, -100.0, minutes=0)
    _close_trade(journal, -100.0, minutes=60)
    state = evaluate_stand_down(journal, RULES, now=INSIDE_SESSION + timedelta(hours=2))
    assert state.stage == 0
    assert not state.is_active(INSIDE_SESSION + timedelta(hours=2))


def test_three_losses_trigger_stage_one(journal):
    for i in range(3):
        _close_trade(journal, -100.0, minutes=i * 60)
    now = INSIDE_SESSION + timedelta(hours=4)
    state = evaluate_stand_down(journal, RULES, now=now)

    assert state.stage == 1
    assert state.is_active(now)
    assert state.days_remaining(now) == pytest.approx(3.0, abs=0.01)


def test_scratches_do_not_trigger(journal):
    """Five small losses inside the threshold are not a losing streak."""
    for i in range(5):
        _close_trade(journal, -5.0, minutes=i * 60)  # -0.1R each
    state = evaluate_stand_down(journal, RULES, now=INSIDE_SESSION + timedelta(hours=6))
    assert state.stage == 0


def test_a_win_between_losses_prevents_the_trigger(journal):
    _close_trade(journal, -100.0, minutes=0)
    _close_trade(journal, -100.0, minutes=60)
    _close_trade(journal, 100.0, minutes=120)
    _close_trade(journal, -100.0, minutes=180)
    state = evaluate_stand_down(journal, RULES, now=INSIDE_SESSION + timedelta(hours=4))
    assert state.stage == 0


# --------------------------------------------------------------- escalation


def test_repeat_inside_the_window_escalates_to_stage_two(journal):
    for i in range(3):
        _close_trade(journal, -100.0, minutes=i * 60)
    first = evaluate_stand_down(journal, RULES, now=INSIDE_SESSION + timedelta(hours=4))
    assert first.stage == 1

    # Well after stage one expires, but still inside the 30-day repeat window.
    later = INSIDE_SESSION + timedelta(days=10)
    for i in range(3):
        _close_trade(journal, -100.0, minutes=10 * 24 * 60 + i * 60)
    second = evaluate_stand_down(journal, RULES, now=later)

    assert second.stage == 2
    assert second.days_remaining(later) == pytest.approx(10.0, abs=0.01)


def test_repeat_outside_the_window_stays_at_stage_one(journal):
    for i in range(3):
        _close_trade(journal, -100.0, minutes=i * 60)
    evaluate_stand_down(journal, RULES, now=INSIDE_SESSION + timedelta(hours=4))

    # 60 days later — outside the 30-day window, so this reads as variance.
    later = INSIDE_SESSION + timedelta(days=60)
    for i in range(3):
        _close_trade(journal, -100.0, minutes=60 * 24 * 60 + i * 60)
    second = evaluate_stand_down(journal, RULES, now=later)

    assert second.stage == 1


def test_active_stand_down_is_not_re_triggered(journal):
    """An in-force stand-down runs its course rather than restarting."""
    for i in range(3):
        _close_trade(journal, -100.0, minutes=i * 60)
    first = evaluate_stand_down(journal, RULES, now=INSIDE_SESSION + timedelta(hours=4))

    # A day into it, more losses arrive. The end date must not move.
    _close_trade(journal, -100.0, minutes=25 * 60)
    second = evaluate_stand_down(journal, RULES, now=INSIDE_SESSION + timedelta(days=1))

    assert second.ends_at == first.ends_at
    assert second.stage == 1


def test_a_served_streak_does_not_re_trigger_on_a_scratch(journal):
    """**The same three losses must not buy a second sentence.**

    `Journal.consecutive_losses` counts back through the journal, so a streak
    does not reset when a stand-down expires — only a WIN clears it. So every
    later close that was neither a win nor a loss found the same three losses
    still standing.

    Measured against the shipped config before this was fixed: three losses
    triggered a 3-day stage 1; once it expired, ONE SCRATCH close escalated
    straight to a 10-day stage 2 on the identical three losses, and the next
    scratch did it again, without limit. A scratch neither counts nor resets a
    streak by design; it must not be able to re-arm the breaker either.
    """
    for i in range(3):
        _close_trade(journal, -100.0, minutes=i * 60)
    first = evaluate_stand_down(journal, RULES, now=INSIDE_SESSION + timedelta(hours=4))
    assert first.stage == 1

    # Serve the sentence, then close one scratch — no new information at all.
    after = INSIDE_SESSION + timedelta(days=4)
    _close_trade(journal, 0.0, minutes=4 * 24 * 60)
    again = evaluate_stand_down(journal, RULES, now=after)

    assert again.stage == 0, "an expired stand-down was re-imposed"
    assert not again.is_active(after)
    # The streak itself is still on record — it is the SENTENCE that was served,
    # not the losses that were forgotten.
    assert again.consecutive_losses == 3

    # And it does not creep back on the next one either.
    later = INSIDE_SESSION + timedelta(days=5)
    _close_trade(journal, 0.0, minutes=5 * 24 * 60)
    third = evaluate_stand_down(journal, RULES, now=later)
    assert third.stage == 0
    assert not third.is_active(later)


def test_a_new_loss_after_a_served_stand_down_still_escalates(journal):
    """The guard above must not defuse the breaker.

    A FOURTH loss is new information and is exactly what stage 2 is for. If
    this passed only because nothing ever triggers again, the fix would have
    replaced an over-firing breaker with a dead one.
    """
    for i in range(3):
        _close_trade(journal, -100.0, minutes=i * 60)
    evaluate_stand_down(journal, RULES, now=INSIDE_SESSION + timedelta(hours=4))

    after = INSIDE_SESSION + timedelta(days=4)
    _close_trade(journal, -100.0, minutes=4 * 24 * 60)
    again = evaluate_stand_down(journal, RULES, now=after)

    assert again.stage == 2
    assert again.is_active(after)
    assert again.consecutive_losses == 4


def test_stand_down_expires_and_clears(journal):
    for i in range(3):
        _close_trade(journal, -100.0, minutes=i * 60)
    evaluate_stand_down(journal, RULES, now=INSIDE_SESSION + timedelta(hours=4))

    after = INSIDE_SESSION + timedelta(days=4)  # stage one was 3 days
    assert not journal.get_stand_down().is_active(after)


# -------------------------------------------------------------- persistence


def test_stand_down_survives_a_restart(journal, tmp_path):
    """Restarting the process must not clear a stand-down."""
    for i in range(3):
        _close_trade(journal, -100.0, minutes=i * 60)
    now = INSIDE_SESSION + timedelta(hours=4)
    evaluate_stand_down(journal, RULES, now=now)

    reopened = Journal(tmp_path / "journal.db")
    state = reopened.get_stand_down()

    assert state.stage == 1
    assert state.is_active(now)


# ------------------------------------------------------- interaction with the gate


def _gate(rules: Rules, mode: ExecutionMode, now: datetime) -> RiskGate:
    return RiskGate(
        rules,
        equity_at_session_start=PAPER_EQUITY,
        execution_mode=mode,
        now=now,
    )


def _account() -> AccountSnapshot:
    return AccountSnapshot(
        equity_usd=PAPER_EQUITY,
        cash_usd=PAPER_EQUITY,
        buying_power_usd=PAPER_EQUITY,
        open_positions=[],
    )


def _active_state(now: datetime) -> StandDownState:
    return StandDownState(
        stage=1,
        started_at=now,
        ends_at=now + timedelta(days=3),
        consecutive_losses=3,
        last_triggered_at=now,
    )


def test_stand_down_blocks_live_entries(rules, spy_tick, buy_proposal):
    verdict = _gate(rules, ExecutionMode.LIVE, INSIDE_SESSION).evaluate(
        buy_proposal,
        account=_account(),
        tick=spy_tick,
        stand_down=_active_state(INSIDE_SESSION),
    )
    assert not verdict.approved
    assert any("stand-down" in r.lower() for r in verdict.reasons)


def test_stand_down_does_not_block_paper(rules, spy_tick, buy_proposal):
    """The whole point: he keeps trading, just not with money."""
    verdict = _gate(rules, ExecutionMode.PAPER, INSIDE_SESSION).evaluate(
        buy_proposal,
        account=_account(),
        tick=spy_tick,
        stand_down=_active_state(INSIDE_SESSION),
    )
    assert verdict.approved, verdict.reasons


def test_expired_stand_down_does_not_block_live(rules, spy_tick, buy_proposal):
    expired = StandDownState(
        stage=1,
        ends_at=INSIDE_SESSION - timedelta(seconds=1),
        consecutive_losses=3,
    )
    verdict = _gate(rules, ExecutionMode.LIVE, INSIDE_SESSION).evaluate(
        buy_proposal, account=_account(), tick=spy_tick, stand_down=expired
    )
    assert verdict.approved, verdict.reasons


def test_no_stand_down_state_is_treated_as_clear(rules, spy_tick, buy_proposal):
    verdict = _gate(rules, ExecutionMode.LIVE, INSIDE_SESSION).evaluate(
        buy_proposal, account=_account(), tick=spy_tick, stand_down=None
    )
    assert verdict.approved, verdict.reasons


# -------------------------------------------------------------------- describe


def test_describe_reads_clearly_when_active():
    now = datetime.now(UTC)
    text = describe(_active_state(now), now)
    assert "Stage 1" in text
    assert "paper trading continues" in text.lower()


def test_describe_reads_clearly_when_clear():
    assert describe(StandDownState(), datetime.now(UTC)) == "No stand-down."


# ------------------------------------------------- open trades and the streak
#
# The counter used to walk CLOSED trades in EXIT order, which made the breaker
# fire on a book that was working. A stop guarantees a loser closes; nothing
# guarantees a winner does, so the closed-trade sequence is the sub-sample the
# stops created rather than a sample of decisions. Observed live: a stage 1
# stand-down in force with three positions open.
#
# The rule now: entry order, over open trades as well as closed ones, and an
# open trade breaks the streak only once its stop is resting in profit.


def _open_trade(
    journal: Journal,
    *,
    minutes: int,
    stop: float | None = None,
    direction: Direction = Direction.BUY,
) -> int:
    """Open a trade and leave it open. `stop` moves it after entry."""
    when = INSIDE_SESSION + timedelta(minutes=minutes)
    entry, planned = (580.0, 575.0) if direction is Direction.BUY else (580.0, 585.0)
    trade_id = journal.record_entry(
        Trade(
            symbol="QQQ",
            direction=direction,
            qty=10,
            entry_time=when,
            entry_price=entry,
            planned_stop=planned,
            planned_target=None,
            rationale="Test trade, left open.",
        )
    )
    if stop is not None:
        journal.record_stop_move(
            PositionActionRecord(
                trade_id=trade_id,
                symbol="QQQ",
                action=PositionAction.TIGHTEN_STOP,
                actor="operator",
                at=when + timedelta(minutes=5),
                reason="Locking the trade in for the test.",
                before_stop=planned,
                after_stop=stop,
                reached_broker=True,
            )
        )
    return trade_id


def test_an_open_trade_with_its_stop_in_profit_breaks_the_streak(journal):
    """The operator's rule: a stop past entry is a decision that already went right.

    Not a mark-to-market reading — the stop is a live order, so if it fills the
    trade is a win. That is what makes it countable where an unrealised gain is
    not.
    """
    _close_trade(journal, -100.0, minutes=0)
    _open_trade(journal, minutes=60, stop=585.0)  # +$50 secured = +1.0R
    _close_trade(journal, -100.0, minutes=120)
    _close_trade(journal, -100.0, minutes=180)

    streak = journal.loss_streak(RULES.loss_threshold_r)
    assert streak.count == 2
    assert streak.broken_by is StreakBreaker.SECURED_OPEN

    state = evaluate_stand_down(journal, RULES, now=INSIDE_SESSION + timedelta(hours=4))
    assert state.stage == 0


def test_a_short_whose_stop_is_below_entry_breaks_it_too(journal):
    """The secured side is direction-dependent, and both sides must agree."""
    _close_trade(journal, -100.0, minutes=0)
    _open_trade(journal, minutes=60, stop=575.0, direction=Direction.SELL)
    _close_trade(journal, -100.0, minutes=120)
    _close_trade(journal, -100.0, minutes=180)

    streak = journal.loss_streak(RULES.loss_threshold_r)
    assert streak.count == 2
    assert streak.broken_by is StreakBreaker.SECURED_OPEN


def test_an_open_trade_that_has_secured_nothing_still_triggers(journal):
    """The one that proves this REJECTS.

    An open position whose stop has never moved is live risk, not a good
    outcome. If it broke the streak, one held position would switch the
    operator's fourth rule off — failing open on the thing the breaker exists
    for. It is skipped, and the three losses around it still trip.
    """
    _close_trade(journal, -100.0, minutes=0)
    _open_trade(journal, minutes=60)  # stop untouched, at the planned level
    _close_trade(journal, -100.0, minutes=120)
    _close_trade(journal, -100.0, minutes=180)

    streak = journal.loss_streak(RULES.loss_threshold_r)
    assert streak.count == 3
    assert streak.open_skipped == 1
    assert streak.broken_by is StreakBreaker.NOTHING

    now = INSIDE_SESSION + timedelta(hours=4)
    state = evaluate_stand_down(journal, RULES, now=now)
    assert state.stage == 1
    assert state.is_active(now)


def test_a_stop_moved_to_breakeven_secures_nothing(journal):
    """Breakeven is a scratch, not a win, judged by the same threshold.

    A stop exactly at entry guarantees getting the money back and nothing more.
    Letting that break a run would make "stopped scratching" read as "trading
    well".
    """
    _close_trade(journal, -100.0, minutes=0)
    _open_trade(journal, minutes=60, stop=580.0)  # entry exactly: 0.0R secured
    _close_trade(journal, -100.0, minutes=120)
    _close_trade(journal, -100.0, minutes=180)

    assert journal.loss_streak(RULES.loss_threshold_r).count == 3


def test_the_breaker_still_trips_on_losses_after_a_secured_winner(journal):
    """The counter cannot be switched off by holding one locked-in position.

    Every new loss is a newer ENTRY than the trade that broke the last run, so
    the count rebuilds behind it. This is why no separate backstop is needed.
    """
    _open_trade(journal, minutes=0, stop=585.0)
    for i in range(3):
        _close_trade(journal, -100.0, minutes=60 + i * 60)

    streak = journal.loss_streak(RULES.loss_threshold_r)
    assert streak.count == 3
    assert streak.broken_by is StreakBreaker.SECURED_OPEN

    now = INSIDE_SESSION + timedelta(hours=5)
    assert evaluate_stand_down(journal, RULES, now=now).stage == 1


def test_the_streak_reports_what_it_walked(journal):
    """A bare integer was the defect. The count has to carry its own context."""
    _close_trade(journal, -5.0, minutes=0)  # scratch
    _open_trade(journal, minutes=60)
    _close_trade(journal, -100.0, minutes=120)

    streak = journal.loss_streak(RULES.loss_threshold_r)
    assert streak.count == 1
    assert streak.open_skipped == 1
    assert streak.scratches_skipped == 1
    assert "1 open trade(s) still unresolved" in streak.describe()


def test_describe_without_a_streak_claims_nothing_about_one(journal):
    """`None` is 'not supplied', never 'nothing was skipped'."""
    plain = describe(StandDownState(), datetime.now(UTC))
    assert plain == "No stand-down."
    assert "open trade" not in plain
