"""Every pass of the decision loop, recorded as a job.

"Did the loop run at 14:15, and what happened?" is answered today by grepping
`journalctl` for `cycle_complete`. That is the wrong place for it in three
separate ways: the systemd journal is rotated, it is not readable by the
dashboard or by a chat agent, and — worst — **it holds only the passes that
finished.** A cycle skipped because the market was shut writes
`cycle_skipped_market_closed` and a cycle whose model call failed writes
`model_call_failed`; neither reaches the audit log at all, so the durable record
of the loop's own working day has holes exactly where the interesting cases are.

So a job is **one pass of the loop**, written to the audit log as an event of
kind `cycle_job`.

## Why there is no new store

There already are two, and a third would be one too many.

- `audit/*.jsonl` is the append-only, authoritative record and **must never be
  migrated**. A new event KIND costs it nothing: nothing already written is
  touched, and a reader that has never heard of `cycle_job` skips it. That is
  the same move `dream_considerations_seen` made, for the same reason.
- `insight.py` is the derived, rebuildable SQLite index over that log. It
  already indexes every event into its `events` table, so a job record becomes
  queryable there with no schema change and no migration.

Everything in this module below the writers is a **pure function over an
`AuditView`** — there is nothing to back up, nothing to rebuild, and nothing
that can go stale, because the answer is recomputed from the log every time it
is asked. Same shape as `news_history.py`, and for the same reason: the record
is the source, and a second copy of it is a second thing that can disagree.

## The three outcomes, and the fourth state that is not one of them

This is the whole point of the type, so it is worth being blunt about it.

- **ran** — the loop completed a pass and got a decision. A quiet cycle is this,
  with `proposals == 0`. Doing nothing is a valid and frequently correct output.
- **skipped** — no enabled class was in session, so the model was deliberately
  not asked. Everything safety-critical still ran: reconcile, the stand-down
  state, the expiry alerts, the stop watch.
- **failed** — the pass could not get a decision. A cycle that could not decide
  must never be recorded as one that decided to do nothing.

And the fourth, which is **not an outcome and must never be reported as one**:

- **not recorded** — no job covers that moment. The loop was stopped, was
  restarting, or the record was lost. `JobHistory.at` answers this as its own
  state, and `Coverage.OUT_OF_RANGE` is kept apart again for a moment the read
  could not speak for at all.

Two mechanisms hold that apart rather than a convention:

- **The counts are `None` on a skip and on a failure, never `0`.** A skipped
  cycle did not propose nothing; it did not look. The writers are three separate
  functions and `record_skipped` HAS NO PARAMETER for a proposal count, so the
  distinction cannot be lost by a careless call site.
- **An unrecognised `outcome` is counted as unreadable, never defaulted.** A
  lenient fallback plus a silent coercion is a mapping that can fail completely
  while looking healthy — see what `str()` on an SDK enum did to the Board.

## What it gates

Nothing, ever. This is an operator surface. It reads a record written after the
fact and no figure in it reaches `RiskGate`, the prompt or a sizing decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import pairwise

from .audit import AuditLog, AuditView

#: The audit `kind` one pass of the loop is written under.
#:
#: A new kind rather than a column on anything existing, because the log is
#: append-only and is never migrated. Every record already on disk stays exactly
#: as it was and simply has no job beside it, which `JobHistory` reports as a
#: window it cannot speak for rather than as a loop that was down.
JOB_EVENT = "cycle_job"

#: The process-level events already written by `cmd_loop`, read here so a gap
#: with a shutdown inside it can be told from a gap without one.
LOOP_START_EVENT = "loop_start"
LOOP_END_EVENT = "loop_end"

DEFAULT_WINDOW_HOURS = 24.0

#: How many audit entries to pull when a caller does not say. The loop wakes 96
#: times a day, so this covers a full day and a half of passes before the limit
#: can bind — and when it does bind, `JobHistory.truncated` says so rather than
#: letting a short read look like a quiet one.
DEFAULT_LIMIT = 200

#: Slack allowed before two consecutive passes are called a gap.
#:
#: A cycle sleeps for the interval AFTER doing its work, so the next pass starts
#: at roughly `interval + duration`, never exactly `interval`. Without grace
#: every single pair of passes would be reported as a gap, which would make the
#: finding worthless on the first read.
GAP_GRACE_SECONDS = 90.0


class Outcome(StrEnum):
    """What one pass of the loop did. Three values, and they are not a scale."""

    RAN = "ran"
    SKIPPED = "skipped"
    FAILED = "failed"


class Coverage(StrEnum):
    """The answer to "what was the loop doing at this moment?".

    `FOUND` is the only one that carries an outcome. The other two are the
    states a reader has to be stopped from collapsing into "nothing happened":
    one says the loop recorded no pass covering that moment, the other says the
    read did not reach far enough to have an opinion.
    """

    FOUND = "found"
    NOT_RECORDED = "not_recorded"
    OUT_OF_RANGE = "out_of_range"


@dataclass(frozen=True)
class Standing:
    """What the account looked like on this pass.

    Known on all three outcomes, because everything that establishes it —
    `reconcile`, `apply_journal_state`, the stand-down read — runs above the
    point where a cycle can be skipped or a model call can fail. Every field is
    `None`-able anyway: a figure that could not be established is reported as
    missing rather than as zero, which on an equity figure would be a plausible
    wrong number rather than an obvious gap.
    """

    equity_usd: float | None = None
    open_positions: int | None = None
    open_risk_usd: float | None = None
    stand_down_stage: int | None = None


@dataclass(frozen=True)
class Job:
    """One recorded pass of the decision loop.

    `started_at` is the cycle's own `now` — the single instant the whole pass
    reasons from — and `recorded_at` is when the record was written, so the two
    together are the pass's duration without anything having to store it twice.
    """

    outcome: Outcome
    started_at: datetime
    recorded_at: datetime

    #: The loop's configured sleep, as it was on this pass. Stored per record
    #: rather than read from `Env` at query time, because the cadence is a fact
    #: about the pass and a config edit must not silently rewrite history.
    interval_seconds: float | None = None

    #: Why it was skipped, or what the failure was. Empty on a pass that ran.
    detail: str = ""

    standing: Standing = field(default_factory=Standing)

    #: `None` on a skip and on a failure — those passes did not look, so a zero
    #: would be a claim they made and did not.
    proposals: int | None = None
    approved: int | None = None
    executed: int | None = None

    @property
    def duration_seconds(self) -> float:
        """How long the pass took. Derived, so it cannot disagree with itself."""
        return max(0.0, (self.recorded_at - self.started_at).total_seconds())

    @property
    def decided(self) -> bool:
        """Whether a model decision was actually obtained on this pass."""
        return self.outcome is Outcome.RAN

    @property
    def quiet(self) -> bool:
        """Ran, looked, and proposed nothing. NOT the same as skipped.

        False when the count is unknown, which is what a skipped or failed pass
        reports — a pass that never looked must not be able to answer "yes" to
        a question about what it decided.
        """
        return self.outcome is Outcome.RAN and self.proposals == 0

    @property
    def span_seconds(self) -> float | None:
        """What this pass is answerable for, or `None` when it did not say.

        Absent rather than defaulted. A record that never stated its cadence
        cannot be stretched over a window somebody assumed, and everything that
        reads this answers "cannot say" instead of guessing.
        """
        return self.interval_seconds

    @property
    def overdue_after(self) -> datetime | None:
        """When the NEXT pass should have started by. `None` if the cadence is unknown.

        **The single definition of what this pass accounts for**, used by
        `JobHistory.at` to answer coverage and by `_gaps` to find the stretches
        nothing accounts for. Two definitions of the same thing would eventually
        disagree, and the way that shows up is a moment reported as unrecorded
        while the gap list says nothing is missing.

        The arithmetic is the loop's own shape: a cycle does its work and THEN
        sleeps for the interval, so the next pass starts at roughly
        `started_at + duration + interval`, never at `started_at + interval`.
        `GAP_GRACE_SECONDS` is the slack on top; without it every consecutive
        pair of passes would be reported as a gap.
        """
        span = self.span_seconds
        if span is None:
            return None
        return self.started_at + timedelta(
            seconds=span + self.duration_seconds + GAP_GRACE_SECONDS
        )


@dataclass(frozen=True)
class Gap:
    """A stretch the loop recorded no pass for.

    `explained_by_shutdown` is the distinction worth having: a `loop_end` inside
    the gap means somebody stopped the process, which is an ordinary event. A
    gap with no shutdown in it is the loop having gone away while it was
    supposed to be running, and only one of those is a fault.
    """

    after: datetime
    before: datetime
    explained_by_shutdown: bool = False

    #: How many further passes the recorded cadence says should have fitted.
    #: `None` when the pass before the gap never stated its interval, because a
    #: count derived from an assumed cadence is a made-up number.
    missed: int | None = None

    @property
    def seconds(self) -> float:
        return max(0.0, (self.before - self.after).total_seconds())


@dataclass(frozen=True)
class JobAnswer:
    """What the loop was doing at one particular moment.

    `coverage` is read first and `job` is only meaningful when it is `FOUND`.
    The two `nearest` fields exist so a renderer can say something useful about
    a moment nothing covers without inventing a pass for it.
    """

    moment: datetime
    coverage: Coverage
    job: Job | None = None
    nearest_before: Job | None = None
    nearest_after: Job | None = None

    def describe(self) -> str:
        """One sentence, and never a cheerful one about a moment nothing covers."""
        stamp = self.moment.isoformat(timespec="minutes")
        if self.coverage is Coverage.OUT_OF_RANGE:
            return (
                f"{stamp} is outside the span that was read, so what the loop "
                "was doing then cannot be established from this window."
            )
        if self.coverage is Coverage.NOT_RECORDED:
            near = self.nearest_before
            if near is not None and near.span_seconds is None:
                return (
                    f"No pass covers {stamp}. The nearest recorded pass began at "
                    f"{near.started_at.isoformat(timespec='minutes')} and did not "
                    "state its cadence, so what it covered is unknown."
                )
            return (
                f"The loop recorded no pass covering {stamp}. It was stopped, "
                "restarting, or the record was lost — this is NOT a report that "
                "it ran and did nothing."
            )
        job = self.job
        assert job is not None  # FOUND is only ever set with a job
        started = job.started_at.isoformat(timespec="seconds")
        if job.outcome is Outcome.SKIPPED:
            return (
                f"The pass at {started} was SKIPPED: no enabled class was in "
                f"session, so the model was not asked. {job.detail}".strip()
            )
        if job.outcome is Outcome.FAILED:
            return (
                f"The pass at {started} FAILED and got no decision: {job.detail}. "
                "It is not a cycle that decided to do nothing."
            )
        return (
            f"The pass at {started} ran: {job.proposals} proposal(s), "
            f"{job.approved} approved, {job.executed} executed."
        )


@dataclass(frozen=True)
class JobHistory:
    """The loop's working day, as it was actually recorded.

    Everything here is derived from an `AuditView` a caller already read off
    disk, so there is no store behind it and nothing to keep in step.
    """

    window_hours: float = DEFAULT_WINDOW_HOURS

    #: Newest first, matching every other reader in this repository.
    jobs: list[Job] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)

    #: The span this history is entitled to have an opinion about.
    window_from: datetime | None = None
    window_to: datetime | None = None

    #: Whether the underlying read hit its limit. When it did, the absence of a
    #: pass older than the oldest one held here says nothing about the loop —
    #: it says the read stopped. Same trap as `seen.reaches_past_marker`.
    truncated: bool = False

    #: Job records that could not be read as one: no start time, or an outcome
    #: word this build does not know. Counted rather than dropped, because a
    #: reader silently short a pass is worse than one that admits the gap.
    unreadable_records: int = 0

    malformed_lines: int = 0
    unreadable_files: list[str] = field(default_factory=list)

    @property
    def has_jobs(self) -> bool:
        """Whether the loop recorded any pass at all inside the window.

        Deliberately a different question from "did it propose anything". False
        here means nobody looked; jobs present with nothing proposed means it
        looked and stood pat. Same rule as `news_history.has_cycles` and
        `triggers.can_grade_anything`.
        """
        return bool(self.jobs)

    @property
    def ran(self) -> int:
        return sum(1 for j in self.jobs if j.outcome is Outcome.RAN)

    @property
    def skipped(self) -> int:
        return sum(1 for j in self.jobs if j.outcome is Outcome.SKIPPED)

    @property
    def failed(self) -> int:
        return sum(1 for j in self.jobs if j.outcome is Outcome.FAILED)

    @property
    def quiet(self) -> int:
        """Passes that ran, looked and proposed nothing. Never counts a skip."""
        return sum(1 for j in self.jobs if j.quiet)

    @property
    def is_degraded(self) -> bool:
        """Whether this history is known to be incomplete."""
        return (
            self.truncated
            or self.unreadable_records > 0
            or self.malformed_lines > 0
            or bool(self.unreadable_files)
        )

    @property
    def latest(self) -> Job | None:
        return self.jobs[0] if self.jobs else None

    @property
    def oldest(self) -> Job | None:
        return self.jobs[-1] if self.jobs else None

    def at(self, moment: datetime) -> JobAnswer:
        """What the loop was doing at `moment`. The question this module exists for.

        **A pass accounts for the stretch from when it started until the next
        recorded pass started** — unless that stretch runs past
        `Job.overdue_after`, which is where the loop stopped keeping up and the
        answer becomes `NOT_RECORDED`. That is the same predicate `_gaps` uses,
        deliberately: a moment reported as unrecorded while the gap list says
        nothing is missing would be two definitions of one thing disagreeing in
        public.
        """
        when = _aware(moment)

        if self.window_from is not None and when < self.window_from:
            return JobAnswer(moment=when, coverage=Coverage.OUT_OF_RANGE)
        if self.window_to is not None and when > self.window_to:
            return JobAnswer(moment=when, coverage=Coverage.OUT_OF_RANGE)

        # `jobs` is newest-first, so the first match walking forward is the
        # latest pass at or before the moment, and the last of the later ones is
        # the earliest pass after it.
        before = next((j for j in self.jobs if j.started_at <= when), None)
        later = [j for j in self.jobs if j.started_at > when]
        after = later[-1] if later else None

        if before is not None:
            horizon = before.overdue_after
            # With no stated cadence there is no horizon to test against, so a
            # LATER pass is the only evidence available: something demonstrably
            # ran after this moment and `before` is the honest answer. Nothing
            # after it leaves the stretch open-ended and unanswerable.
            covered = after is not None if horizon is None else when <= horizon
            if covered:
                return JobAnswer(
                    moment=when,
                    coverage=Coverage.FOUND,
                    job=before,
                    nearest_before=before,
                    nearest_after=after,
                )

        # Nothing accounts for it. Before calling that "the loop did not run",
        # check the read actually reached back far enough to say so: a limited
        # read that stopped at the oldest job held here has no evidence either
        # way. Same trap as `seen.reaches_past_marker`.
        oldest = self.oldest
        if self.truncated and oldest is not None and when < oldest.started_at:
            return JobAnswer(
                moment=when,
                coverage=Coverage.OUT_OF_RANGE,
                nearest_before=before,
                nearest_after=after,
            )

        return JobAnswer(
            moment=when,
            coverage=Coverage.NOT_RECORDED,
            nearest_before=before,
            nearest_after=after,
        )


# --------------------------------------------------------------------- writing
#
# Three functions rather than one with an `outcome` argument, and that is the
# guarantee rather than a style choice: `record_skipped` and `record_failed`
# have NO parameter for a proposal count, so a call site cannot record a skipped
# pass as one that looked and found nothing. The distinction the reader depends
# on is enforced by the signature instead of by everyone remembering it.


def record_ran(
    audit: AuditLog,
    *,
    started_at: datetime,
    interval_seconds: float,
    standing: Standing,
    proposals: int,
    approved: int,
    executed: int,
) -> None:
    """A pass that got a decision. `proposals=0` is a quiet cycle, not a skip."""
    _record(
        audit,
        outcome=Outcome.RAN,
        started_at=started_at,
        interval_seconds=interval_seconds,
        standing=standing,
        detail="",
        proposals=proposals,
        approved=approved,
        executed=executed,
    )


def record_skipped(
    audit: AuditLog,
    *,
    started_at: datetime,
    interval_seconds: float,
    standing: Standing,
    detail: str,
) -> None:
    """A pass that deliberately did not ask the model. It proposed nothing
    because it did not look, which is why there is nowhere here to put a zero."""
    _record(
        audit,
        outcome=Outcome.SKIPPED,
        started_at=started_at,
        interval_seconds=interval_seconds,
        standing=standing,
        detail=detail,
    )


def record_failed(
    audit: AuditLog,
    *,
    started_at: datetime,
    interval_seconds: float,
    standing: Standing,
    detail: str,
) -> None:
    """A pass that could not get a decision. Never a cycle that decided to hold."""
    _record(
        audit,
        outcome=Outcome.FAILED,
        started_at=started_at,
        interval_seconds=interval_seconds,
        standing=standing,
        detail=detail,
    )


def _record(
    audit: AuditLog,
    *,
    outcome: Outcome,
    started_at: datetime,
    interval_seconds: float,
    standing: Standing,
    detail: str,
    proposals: int | None = None,
    approved: int | None = None,
    executed: int | None = None,
) -> None:
    """Write one job line.

    The count keys are written as explicit nulls rather than omitted, so a
    person reading the raw JSONL sees that the field exists and was not
    established — an absent key reads as an older format, which is a different
    thing and would send somebody looking for a migration that must not exist.
    """
    audit.record_event(
        JOB_EVENT,
        {
            "outcome": outcome.value,
            "started_at": _aware(started_at).isoformat(),
            "interval_seconds": float(interval_seconds),
            "detail": detail,
            "equity_usd": standing.equity_usd,
            "open_positions": standing.open_positions,
            "open_risk_usd": standing.open_risk_usd,
            "stand_down_stage": standing.stand_down_stage,
            "proposals": proposals,
            "approved": approved,
            "executed": executed,
        },
    )


# --------------------------------------------------------------------- reading


def history(
    view: AuditView,
    *,
    hours: float = DEFAULT_WINDOW_HOURS,
    now: datetime | None = None,
    limit_reached: bool = False,
) -> JobHistory:
    """Collapse an `AuditView` into the loop's recorded passes over `hours`.

    Pure and offline: the only input is a view somebody else already read off
    disk. `limit_reached` is the caller telling this whether that read stopped
    early — see `read` below, which works it out so most callers do not have to.
    """
    moment = _aware(now or datetime.now(UTC))
    cutoff = moment - timedelta(hours=hours)

    jobs: list[Job] = []
    unreadable = 0
    shutdowns: list[datetime] = []

    for event in view.events:
        stamp = _aware(event.timestamp)
        if event.kind == LOOP_END_EVENT and cutoff <= stamp <= moment:
            shutdowns.append(stamp)
        if event.kind != JOB_EVENT:
            continue
        if not (cutoff <= stamp <= moment):
            continue
        job = _as_job(event.payload, stamp)
        if job is None:
            unreadable += 1
            continue
        jobs.append(job)

    jobs.sort(key=lambda j: j.started_at, reverse=True)

    return JobHistory(
        window_hours=hours,
        jobs=jobs,
        gaps=_gaps(jobs, shutdowns),
        window_from=cutoff,
        window_to=moment,
        truncated=limit_reached,
        unreadable_records=unreadable,
        malformed_lines=view.malformed,
        unreadable_files=list(view.unreadable_files),
    )


def read(
    audit: AuditLog,
    *,
    hours: float = DEFAULT_WINDOW_HOURS,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> JobHistory:
    """Read the log and build a history, reporting whether the read was cut short.

    `AuditLog.read` bounds decisions and events by the same `limit`, so a busy
    window can fill the event list before the requested hours are covered. That
    is exactly the case where an absent pass must not be read as a loop that
    stopped, so it is detected here rather than left to a caller to remember.
    """
    days = max(1, int(hours // 24) + 2)
    view = audit.read(limit=limit, days=days)
    return history(view, hours=hours, now=now, limit_reached=len(view.events) >= limit)


def render(result: JobHistory, *, now: datetime | None = None) -> list[str]:
    """Plain lines carrying the caveats the raw counts do not.

    Written for a reader that will quote it. Every sentence that separates "the
    loop ran and stood pat" from "the loop was not running" appears here as
    prose, because a caller handed only the numbers reaches for the first
    reading every time.
    """
    moment = _aware(now or datetime.now(UTC))
    lines: list[str] = []

    if not result.has_jobs:
        lines.append(
            f"No pass of the loop is on file for the last {result.window_hours:g}h. "
            "That means it was not running, was restarting, or its records were "
            "lost — it does NOT mean it ran and found nothing to do."
        )
        if result.truncated:
            lines.append(
                "The read also hit its limit, so this window was not fully "
                "examined."
            )
        return lines

    lines.append(
        f"{len(result.jobs)} pass(es) in the last {result.window_hours:g}h: "
        f"{result.ran} ran, {result.skipped} skipped with the market shut, "
        f"{result.failed} could not get a decision."
    )
    lines.append(
        f"{result.quiet} of the {result.ran} that ran proposed nothing. That is "
        "a decision to stand pat, and it is not the same as a skipped pass."
    )

    latest = result.latest
    if latest is not None:
        age = max(0.0, (moment - latest.started_at).total_seconds() / 60.0)
        lines.append(
            f"Newest pass began {age:.0f} minutes ago and {latest.outcome.value}."
        )

    for gap in result.gaps:
        missed = "an unknown number of" if gap.missed is None else str(gap.missed)
        cause = (
            "the loop was shut down inside it"
            if gap.explained_by_shutdown
            else "no shutdown was recorded inside it"
        )
        lines.append(
            f"Gap of {gap.seconds / 60.0:.0f} minutes after "
            f"{gap.after.isoformat(timespec='minutes')} — {missed} pass(es) "
            f"missing, and {cause}."
        )

    if result.truncated:
        lines.append(
            "The read hit its limit, so anything older than the oldest pass "
            "above is unexamined rather than absent."
        )
    if result.unreadable_records:
        lines.append(
            f"{result.unreadable_records} job record(s) could not be read — no "
            "start time, or an outcome this build does not recognise."
        )
    if result.malformed_lines or result.unreadable_files:
        lines.append(
            f"{result.malformed_lines} unparseable audit line(s) and "
            f"{len(result.unreadable_files)} unreadable file(s) were skipped."
        )

    return lines


# --------------------------------------------------------------------- private


def _as_job(payload: dict[str, object], recorded_at: datetime) -> Job | None:
    """One log payload as a job, or `None` when it cannot be read as one.

    An unknown outcome word returns `None` rather than falling back to anything.
    A lenient fallback plus a silent coercion is a mapping that can fail
    completely while looking healthy, and the one place that must not happen is
    the field that says whether a pass decided, skipped or failed.
    """
    raw_outcome = str(payload.get("outcome") or "")
    try:
        outcome = Outcome(raw_outcome)
    except ValueError:
        return None

    started = _parse_stamp(payload.get("started_at"))
    if started is None:
        return None

    return Job(
        outcome=outcome,
        started_at=started,
        recorded_at=recorded_at,
        interval_seconds=_number(payload.get("interval_seconds")),
        detail=str(payload.get("detail") or ""),
        standing=Standing(
            equity_usd=_number(payload.get("equity_usd")),
            open_positions=_count(payload.get("open_positions")),
            open_risk_usd=_number(payload.get("open_risk_usd")),
            stand_down_stage=_count(payload.get("stand_down_stage")),
        ),
        proposals=_count(payload.get("proposals")),
        approved=_count(payload.get("approved")),
        executed=_count(payload.get("executed")),
    )


def _gaps(newest_first: list[Job], shutdowns: list[datetime]) -> list[Gap]:
    """Stretches between consecutive passes that the cadence cannot account for.

    Walked oldest-first because a gap is a statement about what came next.
    """
    out: list[Gap] = []
    ordered = list(reversed(newest_first))
    for previous, following in pairwise(ordered):
        span = previous.span_seconds
        expected = previous.overdue_after
        if span is None or expected is None:
            # No cadence on the earlier pass, so there is no expectation to
            # measure the distance against. Reporting a gap here would be an
            # assertion built on an assumed interval.
            continue
        if following.started_at <= expected:
            continue
        elapsed = (following.started_at - previous.started_at).total_seconds()
        out.append(
            Gap(
                after=previous.started_at,
                before=following.started_at,
                explained_by_shutdown=any(
                    previous.started_at <= s <= following.started_at for s in shutdowns
                ),
                missed=max(0, int(elapsed // span) - 1),
            )
        )
    return out


def _parse_stamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _aware(parsed)


def _number(value: object) -> float | None:
    """A float, or `None`. A bool is not a number here, whatever Python says."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _count(value: object) -> int | None:
    """An integer count, or `None`. `None` means not established, never zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _aware(stamp: datetime) -> datetime:
    """Treat a naive timestamp as UTC rather than raising on the comparison.

    Everything the loop writes is timezone-aware, but the log is append-only and
    read tolerantly, so a hand-edited or older line must not take a reader down.
    Same principle as `audit._timestamp` and `news_history._aware`.
    """
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
