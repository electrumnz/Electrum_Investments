"""Grading the watches: did the condition fire, and did the bot act on it.

A `watch` stance says "not yet, and here is what would change that". Until
`AssessmentTrigger` existed the condition was prose, so nothing could score it,
and a watch was an opinion with no consequence. This module is the scoring.

## What is being measured, and what is deliberately not

The question here is **plan-following, not hindsight**. For every watch that
named a checkable condition: did that condition later hold, and if it did, did
the next cycles propose anything on that symbol.

What this does NOT do is estimate what the trade would have made. "You missed
$2,400 on SPY" assumes the fill, assumes the size, and assumes the stop was not
taken out intraday before the target — three guesses stacked into one confident
number. The value of a trigger is that it was **pre-registered**: written down
before the fact, as a falsifiable claim, which is exactly what a counterfactual
P&L is not.

So a fired trigger that produced no proposal is reported as what it is — the
bot said it was waiting for something, the something happened, and nothing
followed — and the operator draws the conclusion.

## Why none of this may reach the prompt

`build_market_context` already hands the model its own previous assessments and
asks it to check each trigger against figures it has just read. That is a
point-in-time self-check against current data, and it is fine.

**This is different and must stay out.** It is an aggregated track record of
counterfactuals, which is strictly noisier than the realised P&L that
`metrics.py` is already kept away from the model for. A model shown "your
triggers fired 11 times and you acted twice" will change behaviour to score
better on that number, on a sample far too small to mean anything. That is the
Alpha Arena failure with the noise turned up. Operator surface only.

## Missing is missing, again

A trigger measured against a figure the bars could not supply comes back
`UNKNOWN`, never `NOT_FIRED`. The distinction is the whole reason
`IndicatorSnapshot` keeps `None` rather than storing a zero: a symbol with no
200-day average has not failed a test against it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .models import AssessmentTrigger, IndicatorSnapshot

# How long a watch stays live. A trigger written on Monday that fires three
# weeks later is not the setup that was described — the reading behind it is
# long gone — so it expires rather than being scored forever.
DEFAULT_HORIZON_DAYS = 5.0


class Verdict(StrEnum):
    FIRED = "fired"
    NOT_FIRED = "not_fired"
    # The figure the trigger names was unavailable in every later cycle. Not a
    # failure to fire: a failure to be able to tell.
    UNKNOWN = "unknown"
    # Still inside its horizon with no reading yet meeting it. Distinct from
    # NOT_FIRED, which is a settled answer.
    PENDING = "pending"


@dataclass(frozen=True)
class WatchOutcome:
    """One watch, graded."""

    symbol: str
    stated_at: datetime
    trigger: AssessmentTrigger
    waiting_for: str
    verdict: Verdict
    # When the condition first held, if it did.
    fired_at: datetime | None = None
    # The reading that met it, so the verdict can be checked rather than
    # trusted.
    fired_value: float | None = None
    # Whether any proposal on this symbol followed the firing, inside the
    # horizon. Only meaningful when the verdict is FIRED.
    acted: bool = False
    cycles_checked: int = 0

    @property
    def followed_through(self) -> bool | None:
        """True if it fired and something was proposed. None if it never fired.

        Deliberately three-valued. Reporting "did not follow through" for a
        trigger that never fired would count a correct wait as a failure, which
        inverts the thing being measured.
        """
        if self.verdict is not Verdict.FIRED:
            return None
        return self.acted


@dataclass
class WatchReport:
    """Every graded watch in a window, plus what could not be graded."""

    outcomes: list[WatchOutcome] = field(default_factory=list)

    # Watches that named nothing at all — no prose condition and no trigger.
    # Counted rather than dropped: "waiting for more confirmation" is not a
    # plan, and how often that happens is one of the more useful numbers here.
    watches_naming_nothing: int = 0

    # Watches with a prose condition but no checkable one.
    #
    # This deliberately does NOT try to separate "written before structured
    # triggers shipped" from "the model wrote prose and skipped the structured
    # field". Nothing in the record distinguishes them — both are a row with
    # `waiting_for` set and `trigger_field` NULL — so the counter is named for
    # what can actually be observed rather than for a cause that would have to
    # be guessed at. As pre-trigger cycles age out of the window this becomes
    # a clean measure of the model skipping the field.
    watches_with_prose_only: int = 0

    cycles_with_numeric_readings: int = 0
    cycles_without_numeric_readings: int = 0

    @property
    def ungradeable(self) -> int:
        return self.watches_naming_nothing + self.watches_with_prose_only

    @property
    def fired(self) -> list[WatchOutcome]:
        return [o for o in self.outcomes if o.verdict is Verdict.FIRED]

    @property
    def missed(self) -> list[WatchOutcome]:
        """Fired, and nothing was proposed. The list the operator wants."""
        return [o for o in self.fired if not o.acted]

    @property
    def graded(self) -> int:
        return len(self.outcomes)

    @property
    def can_grade_anything(self) -> bool:
        """Whether this report rests on any evidence at all.

        Separate from "are the lists empty", the same way `has_cycles` is in
        `news_history`. No graded watches because nothing was recorded and no
        graded watches because every watch was honoured are opposite findings,
        and only one of them says anything about the bot.
        """
        return self.graded > 0 or self.cycles_with_numeric_readings > 0


@dataclass(frozen=True)
class CycleReadings:
    """One cycle's numeric figures, keyed by symbol, plus what it proposed."""

    at: datetime
    readings: dict[str, IndicatorSnapshot]
    proposed_symbols: set[str] = field(default_factory=set)


def grade_watch(
    *,
    symbol: str,
    stated_at: datetime,
    trigger: AssessmentTrigger,
    waiting_for: str,
    later: list[CycleReadings],
    horizon_days: float = DEFAULT_HORIZON_DAYS,
) -> WatchOutcome:
    """Score one watch against the cycles that followed it.

    `later` must be in ascending time order and contain only cycles after
    `stated_at`. Pure: no I/O, no clock.
    """
    horizon_seconds = horizon_days * 86_400
    checked = 0
    saw_a_reading = False

    for cycle in later:
        if (cycle.at - stated_at).total_seconds() > horizon_seconds:
            break
        checked += 1

        reading = cycle.readings.get(symbol)
        value = reading.get(trigger.field) if reading else None
        if value is not None:
            saw_a_reading = True

        if trigger.holds(value) is not True:
            continue

        # Fired. Did anything follow, in this cycle or any later one still
        # inside the horizon? The same cycle counts: the loop reads the
        # indicators and proposes in one pass, so acting on a trigger the
        # moment it fires shows up on the firing cycle itself.
        acted = any(
            symbol in c.proposed_symbols
            and (c.at - stated_at).total_seconds() <= horizon_seconds
            for c in later
            if c.at >= cycle.at
        )
        return WatchOutcome(
            symbol=symbol,
            stated_at=stated_at,
            trigger=trigger,
            waiting_for=waiting_for,
            verdict=Verdict.FIRED,
            fired_at=cycle.at,
            fired_value=value,
            acted=acted,
            cycles_checked=checked,
        )

    # Order matters here, and getting it wrong is how "we have not looked yet"
    # becomes "we looked and found nothing".
    horizon_elapsed = (
        bool(later) and (later[-1].at - stated_at).total_seconds() >= horizon_seconds
    )
    if not horizon_elapsed:
        # No cycles at all, or the newest is still inside the window. Nothing
        # is settled either way yet, whether or not a figure has been seen.
        verdict = Verdict.PENDING
    elif not saw_a_reading:
        # Time has passed and the figure this trigger names was unavailable on
        # every cycle inside the window — including the case where the loop was
        # down and resumed after the horizon. Nothing can be concluded, which
        # is not the same as "it did not fire".
        verdict = Verdict.UNKNOWN
    else:
        verdict = Verdict.NOT_FIRED

    return WatchOutcome(
        symbol=symbol,
        stated_at=stated_at,
        trigger=trigger,
        waiting_for=waiting_for,
        verdict=verdict,
        cycles_checked=checked,
    )


def render(report: WatchReport) -> list[str]:
    """Plain lines carrying the caveats the counts do not."""
    lines: list[str] = []

    if not report.can_grade_anything:
        lines.append(
            "Nothing to grade: no watch in this window named a checkable "
            "trigger and no cycle recorded numeric readings. That means the "
            "evidence is absent, NOT that every watch was honoured."
        )
        if report.ungradeable:
            lines.append(
                f"{report.ungradeable} watch(es) were seen but could not be "
                "scored. Records written before structured triggers shipped "
                "look identical to a watch that skipped the field, so this "
                "should fall as older cycles age out of the window."
            )
        return lines

    lines.append(
        f"{report.graded} watch(es) graded. "
        f"{len(report.fired)} fired, {len(report.missed)} of those with no "
        "proposal following."
    )
    lines.append(
        "This measures whether the bot did what it said it would, not what a "
        "trade would have made. No profit is estimated here, because a fill, a "
        "size and an intraday path would all have to be assumed."
    )

    if report.watches_naming_nothing:
        lines.append(
            f"{report.watches_naming_nothing} watch(es) named no condition at "
            "all — no level, no figure, nothing. Those cannot be scored either "
            "way, and a watch with no trigger is not a plan."
        )
    if report.watches_with_prose_only:
        lines.append(
            f"{report.watches_with_prose_only} watch(es) described a condition "
            "in prose but gave nothing checkable, so they are not scored "
            "above. Cycles predating structured triggers are indistinguishable "
            "from a skipped field and are counted here too."
        )
    if report.cycles_without_numeric_readings:
        lines.append(
            f"{report.cycles_without_numeric_readings} cycle(s) recorded no "
            "numeric readings, so any trigger needing them over that span is "
            "unknown rather than unfired."
        )

    unknown = sum(1 for o in report.outcomes if o.verdict is Verdict.UNKNOWN)
    if unknown:
        lines.append(
            f"{unknown} watch(es) came back unknown: the figure named was "
            "unavailable every time it was checked."
        )

    return lines
