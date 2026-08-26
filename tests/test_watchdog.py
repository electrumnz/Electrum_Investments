"""The watchdog restarts a loop that has stopped working, and refuses four times.

Every refusal gets a test that proves it REFUSES, which is this repository's
rule for anything that can act. A watchdog is not a risk rule, but it is the
only thing here that takes an action nobody asked for at the moment it takes it,
so the same bar applies: the tests that matter are the ones proving it declines.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bot.jobs import Job, JobHistory, Outcome
from bot.watchdog import (
    MAX_RESTARTS_IN_WINDOW,
    STARTUP_GRACE_SECONDS,
    Action,
    UnitState,
    WatchdogState,
    assess,
    parse_unit_state,
    read_state,
    write_state,
)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
INTERVAL = 900.0


def _job(*, started: datetime, interval: float | None = INTERVAL) -> Job:
    return Job(
        outcome=Outcome.RAN,
        started_at=started,
        recorded_at=started + timedelta(seconds=40),
        interval_seconds=interval,
    )


def _history(*jobs: Job, truncated: bool = False, unreadable: int = 0) -> JobHistory:
    ordered = sorted(jobs, key=lambda j: j.started_at, reverse=True)
    return JobHistory(
        window_hours=24.0,
        jobs=list(ordered),
        window_from=NOW - timedelta(hours=24),
        window_to=NOW,
        truncated=truncated,
        unreadable_records=unreadable,
    )


# --------------------------------------------------------------------------
# The four refusals. These are the point of the module.
# --------------------------------------------------------------------------


def test_a_deliberately_stopped_loop_is_never_restarted() -> None:
    """`systemctl stop` is somebody's decision and this does not overrule it."""
    verdict = assess(
        unit=UnitState.INACTIVE,
        history=_history(),
        now=NOW,
        active_for_seconds=None,
    )
    assert verdict.action is Action.REPORT
    assert not verdict.wants_restart
    assert "stopped rather than failed" in verdict.reason


def test_an_unreadable_unit_state_is_never_restarted() -> None:
    """UNKNOWN is a fault in the question, never a fault in the loop.

    A watchdog that read its own broken `systemctl` as evidence would restart a
    perfectly healthy trading loop, which is worse than the outage it shortens.
    """
    verdict = assess(
        unit=UnitState.UNKNOWN,
        history=_history(_job(started=NOW - timedelta(days=3))),
        now=NOW,
        active_for_seconds=99999.0,
    )
    assert verdict.action is Action.REPORT
    assert verdict.keeping_up is None


def test_a_truncated_read_is_never_restarted() -> None:
    """An absent pass in a short read says the READ stopped, not the loop."""
    verdict = assess(
        unit=UnitState.ACTIVE,
        history=_history(truncated=True),
        now=NOW,
        active_for_seconds=99999.0,
    )
    assert verdict.action is Action.REPORT
    assert verdict.keeping_up is None
    assert "incomplete" in verdict.reason


def test_unreadable_records_also_refuse() -> None:
    """`is_degraded` is more than truncation, and all of it has to refuse."""
    verdict = assess(
        unit=UnitState.ACTIVE,
        history=_history(unreadable=2),
        now=NOW,
        active_for_seconds=99999.0,
    )
    assert verdict.action is Action.REPORT


def test_the_restart_cap_refuses_rather_than_thrashing() -> None:
    """A watchdog with no cap turns a broken box into a quietly thrashing one."""
    spent = [NOW - timedelta(minutes=m) for m in (30, 90, 150)]
    assert len(spent) == MAX_RESTARTS_IN_WINDOW
    verdict = assess(
        unit=UnitState.FAILED,
        history=_history(),
        now=NOW,
        active_for_seconds=None,
        recent_restarts=spent,
    )
    assert verdict.action is Action.REPORT
    assert "cap" in verdict.reason


def test_the_cap_counts_only_the_window() -> None:
    """Restarts that have aged out do not hold the brake on forever."""
    stale = [NOW - timedelta(hours=h) for h in (20, 30, 40)]
    state = WatchdogState(restarts=stale)
    assert state.within(now=NOW) == []
    verdict = assess(
        unit=UnitState.FAILED,
        history=_history(),
        now=NOW,
        active_for_seconds=None,
        recent_restarts=state.within(now=NOW),
    )
    assert verdict.action is Action.RESTART


# --------------------------------------------------------------------------
# When it does act.
# --------------------------------------------------------------------------


def test_a_failed_unit_is_restarted() -> None:
    verdict = assess(
        unit=UnitState.FAILED,
        history=_history(),
        now=NOW,
        active_for_seconds=None,
    )
    assert verdict.action is Action.RESTART
    assert verdict.keeping_up is False


def test_a_loop_that_is_up_and_has_stopped_recording_is_restarted() -> None:
    """The case a restart policy structurally cannot see: alive, doing nothing."""
    verdict = assess(
        unit=UnitState.ACTIVE,
        history=_history(_job(started=NOW - timedelta(hours=3))),
        now=NOW,
        active_for_seconds=20000.0,
    )
    assert verdict.action is Action.RESTART
    assert "stopped keeping up" in verdict.reason


def test_a_loop_keeping_up_is_left_alone() -> None:
    verdict = assess(
        unit=UnitState.ACTIVE,
        history=_history(
            _job(started=NOW - timedelta(minutes=5)),
            _job(started=NOW - timedelta(minutes=20)),
        ),
        now=NOW,
        active_for_seconds=20000.0,
    )
    assert verdict.action is Action.NONE
    assert verdict.keeping_up is True


def test_one_late_pass_is_not_enough_to_restart() -> None:
    """A single stalled cycle must not restart a loop that would recover itself.

    `overdue_after` is start + interval + duration + 90s grace. A pass 20 minutes
    old is past that on a 15-minute cadence and is still inside the two-pass
    margin, so this proves the margin is doing work rather than decorating.
    """
    verdict = assess(
        unit=UnitState.ACTIVE,
        history=_history(_job(started=NOW - timedelta(minutes=20))),
        now=NOW,
        active_for_seconds=20000.0,
    )
    assert verdict.action is Action.NONE


# --------------------------------------------------------------------------
# Startup, which is where a naive watchdog eats its own tail.
# --------------------------------------------------------------------------


def test_a_freshly_started_loop_is_left_alone() -> None:
    """No pass yet is not evidence when the unit came up thirty seconds ago."""
    verdict = assess(
        unit=UnitState.ACTIVE,
        history=_history(),
        now=NOW,
        active_for_seconds=30.0,
    )
    assert verdict.action is Action.NONE
    assert verdict.keeping_up is None


def test_an_active_unit_long_up_with_no_passes_is_restarted() -> None:
    verdict = assess(
        unit=UnitState.ACTIVE,
        history=_history(),
        now=NOW,
        active_for_seconds=STARTUP_GRACE_SECONDS + 60,
    )
    assert verdict.action is Action.RESTART
    assert "doing nothing" in verdict.reason


def test_unknown_uptime_with_no_passes_reports_rather_than_guessing() -> None:
    """Could-not-ask must not become zero seconds, in either direction."""
    verdict = assess(
        unit=UnitState.ACTIVE,
        history=_history(),
        now=NOW,
        active_for_seconds=None,
    )
    assert verdict.action is Action.REPORT
    assert verdict.keeping_up is None


def test_a_pass_with_no_cadence_reports_rather_than_assuming_one() -> None:
    """A record that never stated its interval cannot be called late."""
    verdict = assess(
        unit=UnitState.ACTIVE,
        history=_history(_job(started=NOW - timedelta(hours=5), interval=None)),
        now=NOW,
        active_for_seconds=99999.0,
    )
    assert verdict.action is Action.REPORT
    assert verdict.keeping_up is None


# --------------------------------------------------------------------------
# The outage this was written after.
# --------------------------------------------------------------------------


def test_the_22_aug_outage_is_now_caught() -> None:
    """The real shape: systemd gave up, the unit sat failed, nobody was told.

    On 22 Aug 2026 `mudhorn-bot` exited 1 on an Alpaca 503, systemd exhausted
    `StartLimitBurst` in 165 seconds and stopped trying, and the unit was still
    `failed` when somebody looked four days later. The restart policy is fixed
    separately; this proves the watchdog would also have caught it.
    """
    died = NOW - timedelta(days=4)
    verdict = assess(
        unit=UnitState.FAILED,
        history=_history(_job(started=died)),
        now=NOW,
        active_for_seconds=None,
    )
    assert verdict.action is Action.RESTART


def test_parse_unit_state_reads_a_transition_as_unknown() -> None:
    """`activating` has not arrived anywhere, and guessing is how a restart
    lands on top of a start that was already working."""
    assert parse_unit_state("active") is UnitState.ACTIVE
    assert parse_unit_state("failed") is UnitState.FAILED
    assert parse_unit_state("inactive") is UnitState.INACTIVE
    for word in ("activating", "deactivating", "reloading", "", "  ", "banana"):
        assert parse_unit_state(word) is UnitState.UNKNOWN


# --------------------------------------------------------------------------
# The ledger.
# --------------------------------------------------------------------------


def test_the_ledger_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "watchdog-state.json"
    state = WatchdogState().with_restart(NOW)
    write_state(state, target)
    assert read_state(target).restarts == [NOW]


def test_a_missing_or_corrupt_ledger_reads_as_empty(tmp_path: Path) -> None:
    """It fails towards permitting a restart, which is the safe direction here:
    the cap is a brake on thrashing, not a safety interlock."""
    assert read_state(tmp_path / "nope.json").restarts == []

    torn = tmp_path / "torn.json"
    torn.write_text('{"restarts": ["not-a-date", 7, ', encoding="utf-8")
    assert read_state(torn).restarts == []

    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps(["a", "list"]), encoding="utf-8")
    assert read_state(wrong).restarts == []


def test_a_naive_stamp_in_the_ledger_is_read_as_utc(tmp_path: Path) -> None:
    """Everything in this repository reasons in UTC; a stamp with no zone must
    not come back naive and blow up the comparison in `within`."""
    target = tmp_path / "naive.json"
    target.write_text(json.dumps({"restarts": ["2026-08-26T11:00:00"]}), encoding="utf-8")
    (stamp,) = read_state(target).restarts
    assert stamp.tzinfo is not None
    assert read_state(target).within(now=NOW) == [stamp]


def test_the_ledger_prunes_but_keeps_more_than_it_counts() -> None:
    """So a reader can still see the restart that has just aged out of counting."""
    old = WatchdogState(restarts=[NOW - timedelta(hours=100)])
    fresh = old.with_restart(NOW)
    assert fresh.restarts == [NOW]

    recent = WatchdogState(restarts=[NOW - timedelta(hours=8)]).with_restart(NOW)
    assert len(recent.restarts) == 2
    assert recent.within(now=NOW) == [NOW]


# --------------------------------------------------------------------------
# The rail.
# --------------------------------------------------------------------------


def test_the_watchdog_reaches_no_broker_and_no_journal() -> None:
    """It restarts a service or declines to. That is the whole blast radius.

    Parsed rather than asserted in prose, in the same shape as the
    `TraderPowers` and `research.py` boundaries — a guarantee written in a
    docstring is not a guarantee.
    """
    path = Path(__file__).resolve().parents[1] / "src" / "bot" / "watchdog.py"
    assert path.exists(), "watchdog.py has moved — update this boundary"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    forbidden = {"broker", "risk", "journal", "models", "reconcile", "mcp_server", "grants"}
    offending = sorted(m for m in imported if m.lstrip(".").split(".")[0] in forbidden)
    assert not offending, (
        f"watchdog.py imports {offending}. It may restart a systemd unit and "
        "nothing else; a path from here to the broker would make a process "
        "whose entire job is acting unattended into one that can trade."
    )
