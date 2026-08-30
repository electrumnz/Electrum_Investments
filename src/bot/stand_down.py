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

import structlog

from .config import StandDownRules
from .journal import Journal
from .models import LossStreak, StandDownState

log = structlog.get_logger()


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
    # The full result rather than the bare count, so a trigger can say what it
    # counted. Suspending live trading is the most consequential thing this
    # repository does on its own, and "3" is not an account of why.
    detail = journal.loss_streak(rules.loss_threshold_r)
    streak = detail.count

    # An active stand-down runs its course. Results during it — paper or
    # otherwise — do not extend or shorten it: an end date that moves with
    # performance is hard to reason about and easy to argue with, which is
    # exactly what this rule exists to prevent.
    if state.is_active(moment):
        state.consecutive_losses = streak
        journal.save_stand_down(state)
        return state

    # The streak recorded against the last write, which is the streak the last
    # stand-down was served for. A trigger has to rest on a loss that has NOT
    # already been paid for; see `already_served` below.
    served = state.consecutive_losses

    if streak < rules.consecutive_losses_trigger:
        # Not in a stand-down and not in trouble: keep the counter current but
        # leave any previous trigger timestamp intact, since escalation looks
        # back at it.
        state.consecutive_losses = streak
        if state.stage and state.ends_at and moment >= state.ends_at:
            state.stage = 0  # previous stand-down has expired
        journal.save_stand_down(state)
        return state

    # **A streak already served does not trigger again**, and this is the guard
    # that was missing. `consecutive_losses` counts back through the journal, so
    # it does not reset when a stand-down expires — only a WIN clears it. So
    # every subsequent close that was neither a win nor a loss found the same
    # three losses still standing and started the sentence over.
    #
    # Measured against the shipped config: three losses trigger a 3-day stage 1;
    # once it expires, ONE SCRATCH close escalates straight to a 10-day stage 2
    # on the identical three losses, and the next scratch does it again, without
    # limit. A scratch neither counts nor resets a streak — that is deliberate —
    # so it must not be able to re-arm the breaker either.
    #
    # Escalation is supposed to mean "a pattern rather than variance", which is
    # a claim about a SECOND run of losses. Firing it on no new information
    # makes stage 2 mean nothing and locks live trading out indefinitely, and
    # a breaker whose severity is not tied to what happened is one an operator
    # stops reading.
    #
    # The comparison is against the recorded streak rather than a new stored
    # field, and that is not a shortcut: `evaluate_stand_down` is called on
    # every close, so `state.consecutive_losses` is always the streak as of the
    # previous close. Each additional loss therefore lengthens the streak past
    # what is on file and buys exactly one trigger. A new field would need a
    # migration on `stand_down_state` for a fact the row already carries.
    already_served = state.last_triggered_at is not None and streak <= served
    if already_served:
        if state.stage and state.ends_at and moment >= state.ends_at:
            state.stage = 0  # the sentence was served; it is not re-imposed
        state.consecutive_losses = streak
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
    log.warning(
        "stand_down_triggered",
        stage=stage,
        days=days,
        consecutive_losses=streak,
        broken_by=detail.broken_by.value,
        open_skipped=detail.open_skipped,
        scratches_skipped=detail.scratches_skipped,
        detail=detail.describe(),
    )
    return triggered


def describe(
    state: StandDownState,
    now: datetime | None = None,
    streak: LossStreak | None = None,
) -> str:
    """One-line human summary, for logs, the dashboard banner and MCP output.

    `streak` is optional and appends what the count actually walked. `None` is
    "not supplied" and adds nothing — never a claim that nothing was skipped,
    which is the difference a caller reading a bare count cannot see.
    """
    moment = now or datetime.now(UTC)
    context = f" {streak.describe()}" if streak is not None else ""
    if not state.is_active(moment):
        if state.consecutive_losses:
            return (
                f"No stand-down. {state.consecutive_losses} consecutive "
                f"losses on record.{context}"
            )
        return f"No stand-down.{context}"
    ends = state.ends_at.date().isoformat() if state.ends_at else "unknown"
    return (
        f"Stage {state.stage} stand-down until {ends} "
        f"({state.days_remaining(moment):.1f} days left) after "
        f"{state.consecutive_losses} consecutive losses. "
        f"Live trading suspended; paper trading continues.{context}"
    )
