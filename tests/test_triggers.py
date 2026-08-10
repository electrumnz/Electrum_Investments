"""Grading the watch list.

The interesting assertions are the ones about what the grader refuses to
claim. Reporting a fired trigger is easy; the failures worth guarding are the
three ways a non-answer can be dressed up as an answer — unknown reported as
unfired, a horizon that has not elapsed reported as settled, and a watch that
never fired counted as a follow-through failure.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.models import AssessmentTrigger, IndicatorSnapshot, TriggerField, TriggerOp
from bot.triggers import CycleReadings, Verdict, WatchReport, grade_watch, render

STATED = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)


def below(value: float = 641.20) -> AssessmentTrigger:
    return AssessmentTrigger(field=TriggerField.CLOSE, op=TriggerOp.BELOW, value=value)


def cycles(*specs: tuple[float, float | None, bool]) -> list[CycleReadings]:
    """(hours after STATED, close or None, proposed SPY) -> cycle list."""
    out = []
    for hours, close, proposed in specs:
        out.append(
            CycleReadings(
                at=STATED + timedelta(hours=hours),
                readings={"SPY": IndicatorSnapshot(close=close)} if close is not None else {},
                proposed_symbols={"SPY"} if proposed else set(),
            )
        )
    return out


def grade(later: list[CycleReadings], *, horizon_days: float = 5.0):
    return grade_watch(
        symbol="SPY",
        stated_at=STATED,
        trigger=below(),
        waiting_for="SPY closing below 641.20",
        later=later,
        horizon_days=horizon_days,
    )


# ------------------------------------------------------------------- firing


def test_a_trigger_that_fires_is_recorded_with_the_reading_that_met_it():
    """The value travels so the verdict can be checked, not merely trusted."""
    outcome = grade(cycles((1, 645.0, False), (2, 640.0, False)))

    assert outcome.verdict is Verdict.FIRED
    assert outcome.fired_at == STATED + timedelta(hours=2)
    assert outcome.fired_value == 640.0


def test_a_fired_trigger_with_a_later_proposal_counts_as_acted_on():
    outcome = grade(cycles((1, 640.0, False), (2, 639.0, True)))

    assert outcome.verdict is Verdict.FIRED
    assert outcome.acted is True
    assert outcome.followed_through is True


def test_acting_on_the_firing_cycle_itself_counts():
    """The loop reads indicators and proposes in one pass, so the proposal
    lands on the same cycle the trigger fires."""
    outcome = grade(cycles((1, 640.0, True),))

    assert outcome.verdict is Verdict.FIRED
    assert outcome.acted is True


def test_a_fired_trigger_with_nothing_following_is_the_case_worth_surfacing():
    outcome = grade(cycles((1, 640.0, False), (2, 638.0, False)))

    assert outcome.verdict is Verdict.FIRED
    assert outcome.acted is False
    assert outcome.followed_through is False


def test_a_proposal_before_the_trigger_fires_does_not_count():
    """Acting first and being right later is not following the plan."""
    outcome = grade(cycles((1, 645.0, True), (2, 640.0, False)))

    assert outcome.verdict is Verdict.FIRED
    assert outcome.acted is False


# ------------------------------------------------------- refusing to overclaim


def test_a_missing_figure_is_unknown_rather_than_not_fired():
    """The load-bearing one. Missing data must not become evidence.

    The window has to be covered for the answer to be settled at all — before
    that it is PENDING, which the test below pins separately.
    """
    outcome = grade(cycles((1, None, False), (24 * 6, None, False)))

    assert outcome.verdict is Verdict.UNKNOWN
    assert outcome.followed_through is None


def test_an_unelapsed_horizon_is_pending_rather_than_not_fired():
    """A settled 'no' needs the window to have actually passed."""
    outcome = grade(cycles((1, 645.0, False)), horizon_days=5.0)

    assert outcome.verdict is Verdict.PENDING


def test_no_later_cycles_at_all_is_pending():
    assert grade([]).verdict is Verdict.PENDING


def test_a_settled_no_needs_the_window_covered():
    outcome = grade(cycles((1, 645.0, False), (24 * 6, 646.0, False)), horizon_days=5.0)

    assert outcome.verdict is Verdict.NOT_FIRED
    assert outcome.followed_through is None


def test_a_firing_outside_the_horizon_does_not_count():
    """A trigger that fires three weeks later is not the setup described: the
    reading behind it is long gone."""
    outcome = grade(cycles((1, 645.0, False), (24 * 20, 600.0, False)), horizon_days=5.0)

    assert outcome.verdict is Verdict.NOT_FIRED
    assert outcome.fired_at is None


# ------------------------------------------------------------- the comparison


def test_each_operator_compares_the_way_it_reads():
    reading = 100.0
    assert AssessmentTrigger(
        field=TriggerField.CLOSE, op=TriggerOp.ABOVE, value=99.0
    ).holds(reading) is True
    assert AssessmentTrigger(
        field=TriggerField.CLOSE, op=TriggerOp.ABOVE, value=100.0
    ).holds(reading) is False
    assert AssessmentTrigger(
        field=TriggerField.CLOSE, op=TriggerOp.AT_OR_ABOVE, value=100.0
    ).holds(reading) is True
    assert AssessmentTrigger(
        field=TriggerField.CLOSE, op=TriggerOp.BELOW, value=101.0
    ).holds(reading) is True
    assert AssessmentTrigger(
        field=TriggerField.CLOSE, op=TriggerOp.AT_OR_BELOW, value=100.0
    ).holds(reading) is True


def test_an_unavailable_reading_is_none_not_false():
    assert below().holds(None) is None


def test_a_snapshot_reports_missing_fields_as_none():
    """`sma_200` over 40 bars is unavailable, and a stored zero would be read
    back as a real average."""
    snap = IndicatorSnapshot(close=580.0)

    assert snap.get(TriggerField.CLOSE) == 580.0
    assert snap.get(TriggerField.SMA_200) is None


# ----------------------------------------------------------------- reporting


def test_an_empty_report_says_the_evidence_is_absent_not_that_all_was_well():
    report = WatchReport()

    assert report.can_grade_anything is False
    lines = " ".join(render(report))
    assert "evidence is absent" in lines
    assert "NOT that every watch was honoured" in lines


def test_the_readout_refuses_to_imply_a_profit_figure():
    report = WatchReport(
        outcomes=[grade(cycles((1, 640.0, False), (2, 638.0, False)))],
        cycles_with_numeric_readings=2,
    )

    lines = " ".join(render(report))
    assert "not what a trade would have made" in lines
    assert "No profit is estimated" in lines


def test_watches_that_named_nothing_are_counted_and_called_out():
    report = WatchReport(watches_naming_nothing=4, cycles_with_numeric_readings=1)

    lines = " ".join(render(report))
    assert "named no condition at all" in lines
    assert "not a plan" in lines


def test_prose_only_watches_do_not_claim_to_know_why():
    """An old record and a skipped field look identical, so neither is claimed."""
    report = WatchReport(watches_with_prose_only=3, cycles_with_numeric_readings=1)

    lines = " ".join(render(report))
    assert "indistinguishable" in lines


def test_unknown_outcomes_are_named_in_the_readout():
    report = WatchReport(
        outcomes=[grade(cycles((1, None, False), (24 * 6, None, False)))],
        cycles_with_numeric_readings=1,
    )

    assert "unknown" in " ".join(render(report))


def test_missed_is_only_the_fired_and_unacted():
    fired_acted = grade(cycles((1, 640.0, True)))
    fired_alone = grade(cycles((1, 640.0, False), (24 * 6, 638.0, False)))
    never_fired = grade(cycles((1, 999.0, False), (24 * 6, 999.0, False)))

    report = WatchReport(outcomes=[fired_acted, fired_alone, never_fired])

    assert len(report.fired) == 2
    assert report.missed == [fired_alone]


def test_cycles_that_all_land_beyond_the_horizon_are_unknown():
    """The loop was down through the window and resumed after it. Nothing was
    observed inside the horizon, so nothing can be concluded."""
    outcome = grade(cycles((24 * 9, 645.0, False),), horizon_days=5.0)

    assert outcome.verdict is Verdict.UNKNOWN
    assert outcome.cycles_checked == 0


def test_a_missing_figure_inside_the_horizon_is_pending_not_unknown():
    """Precedence: nothing is settled while the window is still open, whether
    or not a reading has been seen. The figure may yet arrive."""
    outcome = grade(cycles((1, None, False), (2, None, False)), horizon_days=5.0)

    assert outcome.verdict is Verdict.PENDING
