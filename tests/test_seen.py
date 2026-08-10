"""The "what changed since I last looked" marker.

Two things are being defended here and they pull against each other.

The marker must **not** advance while the operator is reading, or the answer is
empty in the one sitting it was wanted for — and empty renders identically to a
quiet market, so the failure is silent. And it must **not** advance past
something that was never on screen, or the only notice an item would ever get
is spent while nobody is looking.

Everything else is the third-state rule this repository keeps restating: a
first visit is not an empty delta, a damaged cookie is not a first visit, and a
count that could not be taken is `None` rather than `0`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.audit import AuditLog, DecisionEntry
from bot.journal import Journal
from bot.models import (
    Decision,
    Direction,
    OrderProposal,
    RiskVerdict,
    Trade,
)
from bot.web import seen

MONDAY = datetime(2026, 5, 4, 9, 0, tzinfo=UTC)


def at(**delta: float) -> datetime:
    return MONDAY + timedelta(**delta)


def _entry(when: datetime, *, rejected: int = 0, approved: int = 0) -> DecisionEntry:
    proposal = OrderProposal(
        symbol="SPY",
        direction=Direction.BUY,
        qty=3,
        limit_price=580.0,
        stop_loss_price=575.0,
        take_profit_price=590.0,
        rationale="Reclaimed the prior day high; invalidated below 575.",
    )
    verdicts = [RiskVerdict.reject("risk 1,131.00 exceeds the per-trade cap")] * rejected
    verdicts += [RiskVerdict.approve()] * approved
    return DecisionEntry(
        timestamp=when,
        decision=Decision(
            timestamp=when,
            proposals=[proposal] * (rejected + approved),
            verdicts=verdicts,
        ),
    )


def _trade(*, entered: datetime, exited: datetime | None) -> Trade:
    return Trade(
        symbol="SPY",
        direction=Direction.BUY,
        qty=3,
        entry_time=entered,
        entry_price=580.0,
        planned_stop=575.0,
        planned_target=590.0,
        exit_time=exited,
        exit_price=585.0 if exited else None,
        realised_pnl_usd=15.0 if exited else None,
    )


# ------------------------------------------------------- when the marker moves


def test_the_marker_holds_still_across_one_sitting():
    """The crux. Board, then Decisions, then a refresh, all inside a few minutes.

    If the marker advanced on each of those, the second page of every visit
    would report that nothing had happened — which is also what a genuinely
    quiet morning looks like.
    """
    first = seen.observe(None, now=at(hours=0))
    second = seen.observe(first.cookie_value, now=at(minutes=2))
    third = seen.observe(second.cookie_value, now=at(minutes=9))

    assert not second.is_new_visit
    assert not third.is_new_visit
    assert second.marker.at == third.marker.at


def test_a_gap_ends_the_sitting_and_the_marker_becomes_the_last_request():
    """It advances to when they were last SHOWN something, never to now.

    Stamping `now` would mark as seen everything recorded between the previous
    render and this one — items that were never on screen.
    """
    opening = seen.observe(None, now=at(hours=0))
    closing = seen.observe(opening.cookie_value, now=at(minutes=6))

    afternoon = seen.observe(closing.cookie_value, now=at(hours=5))

    assert afternoon.is_new_visit
    assert afternoon.marker.state is seen.MarkerState.MARKED
    assert afternoon.marker.at == at(minutes=6)


def test_the_marker_never_swallows_something_recorded_after_the_last_render():
    """The specific loss the rule above prevents, asserted end to end."""
    morning = seen.observe(None, now=at(hours=0))
    last_render = seen.observe(morning.cookie_value, now=at(minutes=5))

    # A cycle rejects a proposal one minute after that page was drawn.
    rejection = _entry(at(minutes=6), rejected=1)

    afternoon = seen.observe(last_render.cookie_value, now=at(hours=6))
    summary = seen.summarise(afternoon.marker, decisions=[rejection])

    assert summary.new_rejections == 1
    assert summary.needs_attention


def test_a_short_pause_inside_a_sitting_does_not_end_it():
    start = seen.observe(None, now=at(hours=0))

    resumed = seen.observe(start.cookie_value, now=at(minutes=29))

    assert not resumed.is_new_visit


def test_two_visits_in_a_day_each_get_their_own_window():
    morning = seen.observe(None, now=at(hours=0))
    morning_end = seen.observe(morning.cookie_value, now=at(minutes=4))
    midday = seen.observe(morning_end.cookie_value, now=at(hours=4))
    midday_end = seen.observe(midday.cookie_value, now=at(hours=4, minutes=3))

    evening = seen.observe(midday_end.cookie_value, now=at(hours=9))

    assert midday.marker.at == at(minutes=4)
    assert evening.marker.at == at(hours=4, minutes=3)


# ----------------------------------------------------------- the three states


def test_a_first_visit_is_not_an_empty_delta():
    """The whole reason `MarkerState` exists.

    "Nothing has happened since you last looked" is a claim about the world.
    A visitor with no cookie has no last look, and saying it anyway is the
    confident partial answer this repository is built around refusing.
    """
    visit = seen.observe(None, now=MONDAY)
    summary = seen.summarise(visit.marker, decisions=[_entry(at(hours=-2))])

    assert visit.marker.state is seen.MarkerState.FIRST_VISIT
    assert not summary.is_reportable
    assert summary.new_decisions is None
    assert "nothing to compare" in summary.headline()


def test_the_whole_first_sitting_keeps_reading_as_a_first_visit():
    """The cookie written on arrival carries no marker, so nothing invents one."""
    first = seen.observe(None, now=MONDAY)

    second = seen.observe(first.cookie_value, now=at(minutes=3))

    assert second.marker.state is seen.MarkerState.FIRST_VISIT
    assert second.marker.at is None


def test_counts_are_none_rather_than_zero_when_nothing_can_be_counted():
    """A zero would be read as a measurement by whoever renders it next."""
    summary = seen.summarise(seen.Marker(seen.MarkerState.FIRST_VISIT))

    assert (summary.new_decisions, summary.new_trades_closed, summary.new_rejections) == (
        None,
        None,
        None,
    )
    assert not summary.needs_attention


def test_a_marked_visitor_with_a_quiet_window_says_so_plainly():
    summary = seen.summarise(
        seen.Marker(seen.MarkerState.MARKED, at(hours=-3)),
        decisions=[_entry(at(hours=-5))],
        now=MONDAY,
    )

    assert summary.is_reportable
    assert summary.new_decisions == 0
    assert not summary.anything_new
    assert "Nothing new since you last looked" in summary.headline()


# --------------------------------------------------------- degrading safely


def test_a_malformed_cookie_degrades_to_no_marker():
    for junk in ("", "nonsense", "1:abc:def", "1:100", "1:100:200:300", "9:100:200", "::"):
        visit = seen.observe(junk, now=MONDAY)

        assert visit.marker.at is None, junk
        assert not visit.marker.known, junk


def test_a_damaged_cookie_is_distinguished_from_never_having_visited():
    """We know there WAS a previous visit and cannot say when. Different facts."""
    visit = seen.observe("1:not-a-number:12", now=MONDAY)

    assert visit.marker.state is seen.MarkerState.UNUSABLE
    assert "could not be read" in seen.summarise(visit.marker).headline()


def test_a_cookie_from_the_future_is_refused_rather_than_believed():
    """We wrote it, so a future value means this box's clock moved.

    Trusting it would leave the marker ahead of every record, so the delta
    would be empty on every visit for as long as the cookie lived — silently.
    """
    ahead = seen.observe(None, now=at(days=2))

    visit = seen.observe(ahead.cookie_value, now=MONDAY)

    assert visit.marker.state is seen.MarkerState.UNUSABLE


def test_a_clock_going_backwards_repairs_itself_on_the_next_request():
    ahead = seen.observe(None, now=at(days=2))
    corrected = seen.observe(ahead.cookie_value, now=MONDAY)

    later = seen.observe(corrected.cookie_value, now=at(hours=6))

    assert later.marker.state is seen.MarkerState.MARKED
    assert later.marker.at == MONDAY


def test_an_incoherent_pair_is_not_trusted():
    """A marker stamped after the activity it is supposed to precede."""
    forged = f"{seen.COOKIE_VERSION}:{int(at(hours=5).timestamp())}:{int(MONDAY.timestamp())}"

    assert seen.observe(forged, now=at(hours=9)).marker.state is seen.MarkerState.UNUSABLE


def test_an_old_format_degrades_instead_of_being_misread():
    """A version-less format would parse an old value as a new one, quietly."""
    assert seen.observe("2:100:200", now=MONDAY).marker.state is seen.MarkerState.UNUSABLE


def test_a_naive_timestamp_is_read_as_utc_rather_than_raising():
    """The audit log is read tolerantly and old journal rows predate the
    timezone, so a naive value must cost nothing rather than take a page down."""
    marker = seen.Marker(seen.MarkerState.MARKED, at(hours=0))

    assert marker.is_new(datetime(2026, 5, 4, 8, 30)) is False
    assert marker.is_new(datetime(2026, 5, 4, 9, 30)) is True


# ------------------------------------------------------------- partitioning


def test_partition_cuts_strictly_after_the_marker():
    marker = at(hours=0)
    entries = [_entry(at(hours=1)), _entry(marker), _entry(at(hours=-1))]

    split = seen.partition_decisions(entries, since=marker)

    assert len(split.new) == 1
    assert len(split.earlier) == 2


def test_partition_preserves_the_order_it_was_given():
    """The audit view is newest-first and the page renders it that way."""
    entries = [_entry(at(hours=3)), _entry(at(hours=2)), _entry(at(hours=1))]

    split = seen.partition_decisions(entries, since=at(hours=0))

    assert [e.timestamp for e in split.new] == [at(hours=3), at(hours=2), at(hours=1)]


def test_an_open_trade_is_undated_rather_than_old():
    """It has not happened yet. Sweeping it into `earlier` would count it as read."""
    trades = [
        _trade(entered=at(hours=-9), exited=at(hours=1)),
        _trade(entered=at(hours=-9), exited=None),
    ]

    split = seen.partition_trades(trades, since=at(hours=0))

    assert len(split.new) == 1
    assert len(split.undated) == 1
    assert split.earlier == []


def test_trades_are_split_on_when_they_closed_not_when_they_opened():
    old_trade_closed_recently = _trade(entered=at(days=-4), exited=at(minutes=30))

    split = seen.partition_trades([old_trade_closed_recently], since=at(hours=0))

    assert len(split.new) == 1


def test_is_new_is_three_valued():
    """`None` means unanswerable. A renderer must badge such a row neither way."""
    unknown = seen.Marker(seen.MarkerState.FIRST_VISIT)
    known = seen.Marker(seen.MarkerState.MARKED, at(hours=0))

    assert unknown.is_new(at(hours=1)) is None
    assert known.is_new(None) is None
    assert known.is_new(at(hours=1)) is True
    assert known.is_new(at(hours=-1)) is False


# ---------------------------------------------------------------- the summary


def test_the_summary_counts_cycles_closes_and_rejections():
    summary = seen.summarise(
        seen.Marker(seen.MarkerState.MARKED, at(hours=0)),
        decisions=[
            _entry(at(hours=3), rejected=2),
            _entry(at(hours=2), approved=1),
            _entry(at(hours=-1), rejected=5),
        ],
        closed_trades=[
            _trade(entered=at(hours=-9), exited=at(hours=1)),
            _trade(entered=at(hours=-9), exited=at(hours=-2)),
        ],
        now=at(hours=4),
    )

    assert summary.new_decisions == 2
    assert summary.new_rejections == 2
    assert summary.new_trades_closed == 1
    assert summary.needs_attention


def test_quiet_cycles_alone_do_not_ask_for_attention():
    """Doing nothing is frequently correct, and flagging it teaches people to
    ignore the flag."""
    summary = seen.summarise(
        seen.Marker(seen.MarkerState.MARKED, at(hours=0)),
        decisions=[_entry(at(hours=1)), _entry(at(hours=2))],
        now=at(hours=3),
    )

    assert summary.anything_new
    assert not summary.needs_attention


def test_counts_are_named_as_floors_when_the_history_stops_short():
    """`AuditLog.read` takes a limit, so a long absence outruns the window."""
    summary = seen.summarise(
        seen.Marker(seen.MarkerState.MARKED, at(days=-30)),
        decisions=[_entry(at(hours=-1)), _entry(at(hours=-2))],
        now=MONDAY,
    )

    assert summary.counts_are_lower_bounds
    assert any("floors rather than totals" in c for c in summary.caveats())


def test_a_window_reaching_past_the_marker_is_reported_as_a_total():
    summary = seen.summarise(
        seen.Marker(seen.MarkerState.MARKED, at(hours=-2)),
        decisions=[_entry(at(hours=-1)), _entry(at(hours=-5))],
        now=MONDAY,
    )

    assert not summary.counts_are_lower_bounds
    assert summary.caveats() == []


def test_silence_over_a_long_window_is_named_without_guessing_the_cause():
    """A shut market and a stopped loop look identical from here.

    Note the explicit empty list: that is "I read the audit log and it held
    nothing", which is the finding this caveat reports. See the test below for
    the case that must NOT produce it.
    """
    summary = seen.summarise(
        seen.Marker(seen.MarkerState.MARKED, at(hours=-6)), decisions=[], now=MONDAY
    )

    caveat = " ".join(summary.caveats())
    assert "may have been shut" in caveat
    assert "may have stopped" in caveat


def test_a_caller_that_never_read_the_log_is_not_told_the_loop_may_have_stopped():
    """"I was handed nothing" is not "nothing was recorded".

    A page that renders trades and never opens the audit log used to get an
    unprompted alarm about the trading loop. Same rule as `has_cycles` in
    `news_history` and `can_grade_anything` in `triggers`: a caller handed only
    empty lists reaches for the wrong reading every time.
    """
    summary = seen.summarise(
        seen.Marker(seen.MarkerState.MARKED, at(hours=-6)), now=MONDAY
    )

    assert summary.new_decisions is None
    assert summary.new_rejections is None
    assert not summary.decisions_were_read
    assert "may have stopped" not in " ".join(summary.caveats())


def test_a_long_sitting_still_gets_a_marker():
    """A gap ends a sitting that STOPPED. Nothing ended one that never stops.

    A browser on a refresh timer, or an EventSource reconnecting on a flaky
    link, requests more often than VISIT_GAP forever — so without a ceiling the
    first sitting never establishes a marker at all and the delta stays empty
    for as long as the tab lives.
    """
    raw: str | None = None
    moment = MONDAY
    # Ten hours of a page touching the server every twenty minutes.
    for _ in range(30):
        visit = seen.observe(raw, now=moment)
        raw = visit.cookie_value
        moment += timedelta(minutes=20)

    final = seen.observe(raw, now=moment)

    assert final.marker.known, "a permanently-connected browser never got a marker"
    assert final.marker.at is not None


def test_a_sitting_inside_the_ceiling_still_holds_its_marker():
    """The ceiling must not break the thing the gap is for: paging around the
    deck within one visit has to keep reporting the same delta."""
    first = seen.observe(None, now=MONDAY)
    second = seen.observe(first.cookie_value, now=MONDAY + timedelta(hours=3))
    third = seen.observe(second.cookie_value, now=MONDAY + timedelta(hours=3, minutes=2))

    assert second.marker.state is third.marker.state
    assert second.marker.at == third.marker.at


def test_a_version_one_cookie_degrades_rather_than_being_misread():
    """The old three-part encoding must not be read as a four-part one."""
    stale = seen.observe("1:1746370800:1746370800", now=MONDAY)

    assert stale.marker.state is seen.MarkerState.UNUSABLE
    assert stale.marker.at is None


@pytest.mark.parametrize("raw", ["1:\u0663\u0664:\u0665\u0666", "2:\uff11\uff12:\uff13\uff14:\uff11\uff12"])
def test_non_ascii_digits_are_refused(raw):
    """`str.isdigit()` alone is True for Arabic-Indic and fullwidth digits, and
    `int()` accepts them — so a crafted cookie decoded to a 1970 marker."""
    assert seen.observe(raw, now=MONDAY).marker.at is None


def test_a_short_quiet_window_does_not_cry_wolf():
    """Cycles are 15 minutes apart, so ten minutes holding nothing is normal."""
    summary = seen.summarise(seen.Marker(seen.MarkerState.MARKED, at(minutes=-10)), now=MONDAY)

    assert summary.caveats() == []


def test_no_caveat_is_offered_when_there_are_no_figures_to_qualify():
    for state in (seen.MarkerState.FIRST_VISIT, seen.MarkerState.UNUSABLE):
        assert seen.summarise(seen.Marker(state)).caveats() == []


def test_the_headline_states_the_age_of_the_window():
    summary = seen.summarise(
        seen.Marker(seen.MarkerState.MARKED, at(hours=-5)),
        decisions=[_entry(at(hours=-1), rejected=1)],
        now=MONDAY,
    )

    assert "5 hours ago" in summary.headline()


# ------------------------------------------------------------- the cookie wire


def test_the_cookie_is_not_writable_by_a_script():
    """Not secrecy — the marker is no credential. A script that could write it
    could advance the marker on page load, which is the design this avoids."""
    kwargs = seen.cookie_for(seen.observe(None, now=MONDAY), secure=True)

    assert kwargs["httponly"] is True
    assert kwargs["samesite"] == "lax"
    assert kwargs["key"] == seen.COOKIE_NAME
    assert kwargs["path"] == "/"


def test_secure_is_the_callers_to_decide_so_loopback_over_http_still_works():
    visit = seen.observe(None, now=MONDAY)

    assert seen.cookie_for(visit, secure=False)["secure"] is False
    assert seen.cookie_for(visit, secure=True)["secure"] is True


def test_the_marker_outlives_a_session_cookie():
    """`SessionStore` is in-memory, so a deploy empties it — and a deploy is
    exactly when somebody opens the dashboard to see what changed."""
    from bot.web.auth import SESSION_TTL_SECONDS

    assert seen.COOKIE_MAX_AGE_SECONDS > SESSION_TTL_SECONDS


def test_the_marker_is_its_own_cookie_not_the_session_one():
    from bot.web.auth import COOKIE_NAME as SESSION_COOKIE

    assert seen.COOKIE_NAME != SESSION_COOKIE


def test_sub_second_precision_survives_the_round_trip_by_being_dropped():
    """A floored marker shows a borderline item twice rather than never."""
    precise = MONDAY.replace(microsecond=750_000)
    first = seen.observe(None, now=precise)

    later = seen.observe(first.cookie_value, now=precise + timedelta(hours=2))

    assert later.marker.at == MONDAY  # floored, so the item at .75s reads as new
    assert later.marker.is_new(precise) is True


def test_from_cookies_takes_a_plain_jar():
    """Typed as a mapping so nothing here needs FastAPI to be testable."""
    stamped = seen.observe(None, now=MONDAY)

    visit = seen.from_cookies({seen.COOKIE_NAME: stamped.cookie_value}, now=at(hours=4))

    assert visit.marker.at == MONDAY
    assert seen.from_cookies({}, now=MONDAY).marker.state is seen.MarkerState.FIRST_VISIT


# ------------------------------------------------- against the real readers


def test_it_fits_what_the_dashboard_actually_reads(tmp_path):
    """Proof the surface lines up with `AuditLog.read()` and `closed_trades()`.

    Both stores get a `tmp_path`: nothing in the suite may leave a file in
    `data/` or `audit/`, and `tests/conftest.py` fails the run if it does.
    """
    log = AuditLog(tmp_path / "audit")
    log.record(_entry(at(hours=-4), rejected=1).decision)
    log.record(_entry(at(hours=2), rejected=3).decision)

    journal = Journal(tmp_path / "journal.db")
    trade_id = journal.record_entry(_trade(entered=at(hours=-9), exited=None))
    journal.record_exit(
        trade_id, exit_time=at(hours=1), exit_price=585.0, realised_pnl_usd=15.0
    )

    marker = seen.Marker(seen.MarkerState.MARKED, MONDAY)
    summary = seen.summarise(
        marker,
        decisions=log.read().decisions,
        closed_trades=journal.closed_trades(),
        now=at(hours=3),
    )

    assert summary.new_decisions == 1
    assert summary.new_rejections == 3
    assert summary.new_trades_closed == 1
    assert summary.needs_attention
