"""Consecutive-loss circuit breaker.

Separate from `risk.py` because it does a different job. The risk gate answers
"may this order be placed right now?" from state handed to it. This decides
whether a stand-down should *start*, which needs trade history and writes
persistent state.

The intent is behavioural, not statistical. A run of losses is when discipline
is weakest and position sizing gets worst, so the breaker interrupts the loop
rather than trying to predict the next trade. Trading continues on paper
throughout — the money stops, the practice does not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .config import StandDownRules
from .journal import Journal
from .models import StandDownState


def evaluate_stand_down(
    journal: Journal,
    rules: StandDownRules,
    *,
    now: datetime | None = None,
) -> StandDownState:
    """Recompute stand-down state from trade history and persist it.

    Call after any trade closes. Returns the state now in force.
    """
    moment = now or datetime.now(UTC)
    state = journal.get_stand_down()
    streak = journal.consecutive_losses(rules.loss_threshold_r)

    # An active stand-down runs its course. Results during it — paper or
    # otherwise — do not extend or shorten it: an end date that moves with
    # performance is hard to reason about and easy to argue with, which is
    # exactly what this rule exists to prevent.
    if state.is_active(moment):
        state.consecutive_losses = streak
        journal.save_stand_down(state)
        return state

    if streak < rules.consecutive_losses_trigger:
        # Not in a stand-down and not in trouble: keep the counter current but
        # leave any previous trigger timestamp intact, since escalation looks
        # back at it.
        state.consecutive_losses = streak
        if state.stage and state.ends_at and moment >= state.ends_at:
            state.stage = 0  # previous stand-down has expired
        journal.save_stand_down(state)
        return state

    # Trigger. Escalate if the last one was recent enough to look like a pattern
    # rather than variance.
    repeat_cutoff = moment - timedelta(days=rules.repeat_window_days)
    is_repeat = state.last_triggered_at is not None and state.last_triggered_at >= repeat_cutoff

    stage = 2 if is_repeat else 1
    days = rules.stage_two_days if is_repeat else rules.stage_one_days

    triggered = StandDownState(
        stage=stage,
        started_at=moment,
        ends_at=moment + timedelta(days=days),
        consecutive_losses=streak,
        last_triggered_at=moment,
    )
    journal.save_stand_down(triggered)
    return triggered


def describe(state: StandDownState, now: datetime | None = None) -> str:
    """One-line human summary, for logs, the dashboard banner and MCP output."""
    moment = now or datetime.now(UTC)
    if not state.is_active(moment):
        if state.consecutive_losses:
            return f"No stand-down. {state.consecutive_losses} consecutive losses on record."
        return "No stand-down."
    ends = state.ends_at.date().isoformat() if state.ends_at else "unknown"
    return (
        f"Stage {state.stage} stand-down until {ends} "
        f"({state.days_remaining(moment):.1f} days left) after "
        f"{state.consecutive_losses} consecutive losses. "
        "Live trading suspended; paper trading continues."
    )
