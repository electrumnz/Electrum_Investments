"""Is the decision loop still running, and may anything be done about it?

The loop died at 12:04 UTC on 22 Aug 2026 and was still dead four days later.
Alpaca's paper API answered `503 service temporary unavailable` while
`cmd_loop` was calling `broker.connect()`, the process exited 1, systemd
restarted it five times over 165 seconds, tripped `StartLimitBurst=5` inside
`StartLimitIntervalSec=300`, and stopped trying. Alpaca came back within the
hour. Nothing on the box was allowed to notice.

**The restart policy is fixed separately and is the larger half of the repair.**
This module is for what a restart policy structurally cannot see: a loop that is
`active (running)` and not doing anything. systemd watches the process; only the
audit log knows whether the process did any work.

## Why the audit log answers this at all

`cmd_loop` records a job every pass through `jobs.record_ran`,
`record_skipped` or `record_failed` — and critically, **it records a SKIPPED
pass when no instrument class is in session**. So a pass is written every
fifteen minutes around the clock, through the night, over a weekend and on
Christmas Day. That is what makes this checkable without a market calendar:
there is no hour in which silence is expected, so silence never has to be
interpreted. A watchdog that had to know whether the market was open would be
carrying a second copy of `market_clock`, and the copy that was wrong would be
the one deciding whether to restart a trading loop.

## The four refusals

Restarting a trading loop is an action, so the bar is evidence rather than
suspicion. Every one of these is a state where the honest answer is "I cannot
establish that a restart is warranted", and in all four this reports instead:

- **The unit state could not be read.** `UNKNOWN` is not `FAILED`. A watchdog
  that treats its own broken reading as a fault restarts a healthy loop, which
  is the one outcome worse than the outage it exists to shorten.
- **The operator stopped it.** `INACTIVE` means somebody ran `systemctl stop`.
  Restarting that is a machine overruling a person, and the person is the one
  who knows why. Note this is deliberately NOT read from the audit log's
  `loop_end` event: that is written in a `finally:`, so it appears on a crash
  too, and SIGTERM does not run it at all. systemd's own unit state is the only
  thing that actually distinguishes "stopped" from "died", so it is what is
  asked.
- **The read is known to be incomplete.** `JobHistory.is_degraded` covers a
  truncated read, unreadable files and unparseable records. An absent pass in a
  short read says the read stopped, not that the loop did — the same trap
  `seen.reaches_past_marker` and `jobs.truncated` already exist for.
- **It has been restarted too often already.** A watchdog with no cap turns a
  permanently broken box into a quietly thrashing one, which looks healthier
  from a distance than it is. After `MAX_RESTARTS_IN_WINDOW` the answer becomes
  a person's.

## What it deliberately does not do

It does not close a position, cancel an order, or touch the broker in any way,
and it imports none of `broker`, `risk`, `journal` or `models` — the same rail
`research.py` and `TraderPowers` are held to, checked the same way, by parsing
this module's imports in `tests/test_watchdog.py`. The worst a bug here can do
is restart a service or decline to.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from .jobs import JobHistory

#: How long a freshly started loop may go without recording a pass before that
#: silence counts as evidence of anything.
#:
#: The first pass does the most work of any: `reconcile`, the session calendar
#: refresh, every feed, and a model call. Measured on the live droplet on 26 Aug
#: 2026 the first `cycle_complete` landed 43 seconds after start. Five minutes is
#: generous against that, and being generous is the right direction — restarting
#: a loop that was merely still starting would be a self-inflicted outage.
STARTUP_GRACE_SECONDS = 300.0

#: How many passes may be missed before the loop is treated as stopped.
#:
#: `Job.overdue_after` already carries the pass's own duration plus
#: `jobs.GAP_GRACE_SECONDS`, so one missed pass is genuinely late rather than
#: merely slow. Two is the margin over that: a single stalled network call must
#: not restart a loop that would have recovered on its own next cycle.
MISSED_PASSES_BEFORE_RESTART = 2

#: The restart ledger's span, and how many restarts it will allow inside it.
#:
#: Three in six hours tolerates a genuinely transient outage that spans a couple
#: of cycles, and refuses to keep papering over a box that cannot stay up. The
#: window slides rather than resetting on a schedule, so a slow thrash — one
#: restart every two hours, forever — is caught as well as a fast one.
RESTART_WINDOW_HOURS = 6.0
MAX_RESTARTS_IN_WINDOW = 3

#: Where the ledger lives. Beside `tailnet-status.json`, in the directory the
#: dashboard already reads, and NOT in the journal: this is derived operational
#: state, it is rebuildable by simply forgetting it, and `backup-journal.sh`
#: covers the two files that are irreplaceable. Losing this file costs the
#: restart count, which fails in the safe direction — a fresh ledger permits a
#: restart, and the cap is a brake on thrashing rather than a safety interlock.
DEFAULT_STATE_PATH = Path("data/watchdog-state.json")


class UnitState(StrEnum):
    """What systemd says about the loop unit.

    Four values, and the fourth is the point. `UNKNOWN` is what a failed or
    unparseable `systemctl is-active` produces, and it must never collapse into
    `FAILED` — one of those is a fault in the loop and the other is a fault in
    the question. Same rule as `BrokerClock` answering `None` for "could not
    ask" rather than "the market is shut".
    """

    ACTIVE = "active"
    FAILED = "failed"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


class Action(StrEnum):
    """What this run concluded should happen.

    `REPORT` is not a weaker `RESTART`. It is the answer whenever acting would
    be acting on something unestablished, and it carries its reason so the
    journal line says which of the refusals fired.
    """

    NONE = "none"
    RESTART = "restart"
    REPORT = "report"


@dataclass(frozen=True)
class Verdict:
    """One reading, with the reason in words.

    `keeping_up` is three-valued on purpose. `None` means the question could not
    be answered — no unit state, a degraded read, or a newest pass that never
    stated its cadence — and it must not read as either health or fault.
    """

    action: Action
    reason: str
    keeping_up: bool | None = None

    @property
    def wants_restart(self) -> bool:
        return self.action is Action.RESTART

    @property
    def needs_a_person(self) -> bool:
        """Whether a human should look. Drives the process exit code.

        A restart that worked is not this: self-healing is the feature, and
        exiting non-zero every time one happened would leave `systemctl
        --failed` permanently dirty and train the reader to ignore it — the
        reasoning that put `RECHECK_COMMAND` on the tailnet banner.
        """
        return self.action is Action.REPORT


@dataclass(frozen=True)
class WatchdogState:
    """The restart ledger. Nothing else is remembered between runs."""

    restarts: list[datetime] = field(default_factory=list)

    def within(self, *, now: datetime, hours: float = RESTART_WINDOW_HOURS) -> list[datetime]:
        cutoff = now - timedelta(hours=hours)
        return [r for r in self.restarts if r >= cutoff]

    def with_restart(self, moment: datetime) -> WatchdogState:
        """A new ledger with this restart on it, pruned to twice the window.

        Pruned rather than unbounded because this file is rewritten by a timer
        every few minutes forever, and trimmed to twice the window rather than
        exactly it so a reader can still see the restart that has just aged out
        of counting.
        """
        keep = moment - timedelta(hours=RESTART_WINDOW_HOURS * 2)
        return WatchdogState(restarts=sorted([*(r for r in self.restarts if r >= keep), moment]))


def assess(
    *,
    unit: UnitState,
    history: JobHistory,
    now: datetime,
    active_for_seconds: float | None,
    recent_restarts: Sequence[datetime] = (),
) -> Verdict:
    """Decide what to do about the loop. Pure, and offline by construction.

    The order of the checks is the design. Every refusal is tested before any
    reason to act, so a state this cannot establish can never reach a restart by
    falling through — the same shape as `RiskGate` collecting failures before it
    approves anything.
    """
    if unit is UnitState.UNKNOWN:
        return Verdict(
            Action.REPORT,
            "systemd's state for the loop unit could not be read, so whether it "
            "is running is unknown. Nothing is restarted on a reading that "
            "failed.",
            keeping_up=None,
        )

    if unit is UnitState.INACTIVE:
        return Verdict(
            Action.REPORT,
            "The loop unit is stopped rather than failed, which is what "
            "`systemctl stop` leaves behind. That is somebody's decision and "
            "this does not overrule it. Start it by hand when it should be back.",
            keeping_up=False,
        )

    if history.is_degraded:
        return Verdict(
            Action.REPORT,
            "The audit read is incomplete "
            f"(truncated={history.truncated}, "
            f"unreadable_records={history.unreadable_records}, "
            f"malformed_lines={history.malformed_lines}), so a missing pass "
            "would say the read stopped rather than the loop did.",
            keeping_up=None,
        )

    spent = list(recent_restarts)
    if len(spent) >= MAX_RESTARTS_IN_WINDOW:
        return Verdict(
            Action.REPORT,
            f"Already restarted {len(spent)} times in the last "
            f"{RESTART_WINDOW_HOURS:g}h, which is the cap. Restarting again "
            "would keep a broken box looking like a working one.",
            keeping_up=False,
        )

    if unit is UnitState.FAILED:
        return Verdict(
            Action.RESTART,
            "The loop unit is in the failed state, so systemd has given up on "
            "it and nothing else will bring it back.",
            keeping_up=False,
        )

    # Everything below is the ACTIVE case: the process exists, so the only
    # question left is whether it is doing any work.
    latest = history.latest
    if latest is None:
        if active_for_seconds is None:
            return Verdict(
                Action.REPORT,
                "No pass is on file and how long the unit has been up could not "
                "be read, so a loop that has only just started cannot be told "
                "from one that has stopped working.",
                keeping_up=None,
            )
        if active_for_seconds < STARTUP_GRACE_SECONDS:
            return Verdict(
                Action.NONE,
                f"No pass on file yet, but the unit has only been up "
                f"{active_for_seconds:.0f}s and the first pass does the most "
                "work of any. Still inside the startup grace.",
                keeping_up=None,
            )
        return Verdict(
            Action.RESTART,
            f"The unit has been active for {active_for_seconds:.0f}s and has "
            f"recorded no pass at all in the last {history.window_hours:g}h. "
            "The process is up and is doing nothing.",
            keeping_up=False,
        )

    overdue = latest.overdue_after
    if overdue is None:
        return Verdict(
            Action.REPORT,
            "The newest recorded pass did not state its cadence, so there is no "
            "expectation to measure lateness against and a restart would be "
            "acting on an assumed interval.",
            keeping_up=None,
        )

    span = latest.span_seconds or 0.0
    deadline = overdue + timedelta(seconds=span * MISSED_PASSES_BEFORE_RESTART)
    if now > deadline:
        late = (now - overdue).total_seconds()
        return Verdict(
            Action.RESTART,
            f"The newest pass started "
            f"{latest.started_at.isoformat(timespec='minutes')} and the next "
            f"was due by {overdue.isoformat(timespec='minutes')} — "
            f"{late / 60:.0f} minutes overdue, past the "
            f"{MISSED_PASSES_BEFORE_RESTART}-pass margin. The process is up and "
            "has stopped keeping up.",
            keeping_up=False,
        )

    return Verdict(
        Action.NONE,
        f"The loop is keeping up: newest pass "
        f"{latest.started_at.isoformat(timespec='minutes')}, next due by "
        f"{overdue.isoformat(timespec='minutes')}.",
        keeping_up=True,
    )


def parse_unit_state(raw: str) -> UnitState:
    """Read `systemctl is-active` output. Anything unrecognised is UNKNOWN.

    `activating`, `deactivating` and `reloading` land on UNKNOWN deliberately
    rather than being mapped to the state they are heading towards: a unit
    mid-transition has not arrived anywhere, and guessing which way it will
    settle is how a restart lands on top of a start that was already working.
    """
    word = raw.strip().lower()
    if word == "active":
        return UnitState.ACTIVE
    if word == "failed":
        return UnitState.FAILED
    if word == "inactive":
        return UnitState.INACTIVE
    return UnitState.UNKNOWN


def read_state(path: Path | None = None) -> WatchdogState:
    """The restart ledger, or an empty one.

    An unreadable or malformed file answers empty rather than raising. That
    permits a restart, which is the safe direction here for the reason given on
    `DEFAULT_STATE_PATH`: the cap is a brake on thrashing, not a safety
    interlock, and a watchdog that refused to work because its own scratch file
    was corrupt would be a worse failure than the one it guards.
    """
    target = path or DEFAULT_STATE_PATH
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return WatchdogState()
    if not isinstance(payload, dict):
        return WatchdogState()

    stamps: list[datetime] = []
    raw = payload.get("restarts")
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, str):
                continue
            try:
                parsed = datetime.fromisoformat(item)
            except ValueError:
                continue
            stamps.append(parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC))
    return WatchdogState(restarts=sorted(stamps))


def write_state(state: WatchdogState, path: Path | None = None) -> None:
    """Replace the ledger. Best effort — a ledger that will not write must not
    stop a restart that is already warranted."""
    target = path or DEFAULT_STATE_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"restarts": [r.isoformat() for r in state.restarts]}, indent=2),
            encoding="utf-8",
        )
    except OSError:
        return


def main(argv: list[str] | None = None) -> int:
    """CLI for the timer. Decides; never restarts anything itself.

    The privileged action stays in `deploy/check-loop.sh`, where it is one
    visible line in a file a person can read, rather than buried in a
    subprocess call inside a module that is otherwise pure. This exits 10 to ask
    for the restart, 1 when a person should look, and 0 when all is well.

    **The ledger is written before the restart is carried out**, so a restart
    that the shell then fails to perform is still counted. That over-counts,
    which is the safe direction: the cap is reached sooner and the answer
    becomes a person's, rather than a failing restart being retried invisibly
    every few minutes forever.
    """
    import argparse

    from .audit import AuditLog
    from .jobs import DEFAULT_WINDOW_HOURS
    from .jobs import read as read_jobs

    parser = argparse.ArgumentParser(description="Decide whether the decision loop needs a kick.")
    parser.add_argument(
        "--unit-state",
        default="",
        help="Output of `systemctl is-active`. Anything unrecognised reads as unknown.",
    )
    parser.add_argument(
        "--active-for-seconds",
        type=float,
        default=None,
        help="How long the unit has been active. Omitted means could-not-ask, not zero.",
    )
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--window-hours", type=float, default=DEFAULT_WINDOW_HOURS)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Say what would happen and write nothing. Never returns 10.",
    )
    args = parser.parse_args(argv)

    now = datetime.now(UTC)
    history = read_jobs(AuditLog(), hours=args.window_hours, now=now)
    state = read_state(args.state)

    verdict = assess(
        unit=parse_unit_state(args.unit_state),
        history=history,
        now=now,
        active_for_seconds=args.active_for_seconds,
        recent_restarts=state.within(now=now),
    )

    print(f"{verdict.action.value.upper()}: {verdict.reason}")

    if verdict.action is not Action.RESTART:
        return 1 if verdict.needs_a_person else 0

    if args.dry_run:
        print("(dry run — nothing written, no restart requested)")
        return 0

    write_state(state.with_restart(now), args.state)
    return 10


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
