"""The session phases, and the distinction the whole module exists for.

Written after a real confusion: "the market should be open right now", at 04:49
New York time on a Monday. Alpaca *was* open — pre-market starts at 04:00 — so
the answer was not simply no. It was "open for a session this bot does not
trade, four and a half hours before the one it does", and only two clocks read
together give you that.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bot.config import load_rules
from bot.market_clock import (
    CLOCKS,
    BrokerClock,
    ClockFace,
    MarketPhase,
    VenueState,
    is_continuous,
    market_state,
    render_sessions,
)

WINDOWS = load_rules().instruments["us_equity"].windows_by_day
CONTINUOUS = load_rules().instruments["crypto"].windows_by_day

#: Whether the OPTIONAL holiday rules for TSE, ASX and NZX are installed.
HAS_RULES = importlib.util.find_spec("exchange_calendars") is not None

#: Why a skip here is not a hole in CI. `exchange_calendars` is in the `dev`
#: extra and CI installs `.[dev]`, so everything below runs there. A local
#: checkout with only the base dependencies skips rather than going red for a
#: reason it cannot fix from a test file — which is also the honest shape,
#: because on such a checkout the feature genuinely is not present. The
#: DEGRADED path is not skipped anywhere: it is simulated, so it is covered
#: either way round.
needs_rules = pytest.mark.skipif(
    not HAS_RULES,
    reason="exchange_calendars is optional; the dev extra installs it and CI runs that",
)


def at(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


# ----------------------------------------------------------------- phases


@pytest.mark.parametrize(
    ("moment", "phase"),
    [
        # August, so New York is on EDT (UTC-4).
        (at(8, 10, 7, 0), MarketPhase.OVERNIGHT),  # 03:00 ET, before pre opens
        (at(8, 10, 8, 0), MarketPhase.PRE),        # 04:00 ET exactly, pre opens
        (at(8, 10, 8, 45), MarketPhase.PRE),       # 04:45 ET, the real case
        (at(8, 10, 14, 0), MarketPhase.OPEN),      # 10:00 ET
        (at(8, 10, 20, 30), MarketPhase.POST),     # 16:30 ET
        (at(8, 11, 1, 0), MarketPhase.OVERNIGHT),  # 21:00 ET Monday
        (at(8, 15, 12, 0), MarketPhase.WEEKEND),   # Saturday noon UTC
    ],
)
def test_the_phase_at_a_known_moment(moment, phase):
    assert market_state(moment, windows_by_day=WINDOWS).phase is phase


def test_the_boundaries_are_inclusive_at_the_start_and_exclusive_at_the_end():
    """09:30 ET is open, 16:00 ET is not. A session that counted its own close
    as open would approve an order into an auction."""
    assert market_state(at(8, 10, 13, 30)).phase is MarketPhase.OPEN
    assert market_state(at(8, 10, 13, 29)).phase is MarketPhase.PRE
    assert market_state(at(8, 10, 20, 0)).phase is MarketPhase.POST
    assert market_state(at(8, 10, 19, 59)).phase is MarketPhase.OPEN


def test_friday_evening_runs_into_the_weekend_not_into_an_overnight():
    """The weekend is a shape, not a pair of days. Trading stops Friday 8pm ET
    and resumes Sunday 8pm ET, so Saturday has no sessions at all while Sunday
    has one that only starts in the evening."""
    friday_late = at(8, 15, 0, 30)          # Fri 20:30 ET
    assert market_state(friday_late).phase is MarketPhase.WEEKEND
    assert market_state(friday_late).next_phase is MarketPhase.OVERNIGHT

    sunday_evening = at(8, 17, 0, 30)       # Sun 20:30 ET
    assert market_state(sunday_evening).phase is MarketPhase.OVERNIGHT


def test_fridays_after_hours_session_names_the_weekend_as_what_comes_next():
    """The test above only ever checked Friday AFTER 20:00, and the hole was
    the four hours before it.

    Every other weekday's after-hours session rolls into an overnight one at
    8pm. Friday's does not — 20:00 Friday is already `WEEKEND` and nothing
    trades again until Sunday 20:00. `_phase_at` still promised OVERNIGHT for
    the whole of Friday's after-hours, so `render_sessions` put "Next:
    overnight at 20:00 Fri" into the model's own market-context block: the
    market reopens in two and a half hours, when it actually reopens in
    fifty-two.

    The phase and the countdown were both right. Only the NAME of what comes
    next was invented, which is the plausible-wrong-figure shape this whole
    module exists to refuse.
    """
    friday_after_hours = at(8, 14, 21, 30)  # Fri 14 Aug, 17:30 ET

    state = market_state(friday_after_hours)
    assert state.phase is MarketPhase.POST
    assert state.next_phase is MarketPhase.WEEKEND

    # The promise is checkable: step to the moment it names and read it back.
    assert market_state(state.next_change).phase is MarketPhase.WEEKEND

    # Thursday's after-hours genuinely does roll into an overnight session, so
    # the fix must be Friday-shaped rather than a blanket change.
    assert market_state(at(8, 13, 21, 30)).next_phase is MarketPhase.OVERNIGHT


def test_every_promised_next_phase_is_the_phase_that_actually_arrives():
    """Swept minute by minute, because this bug survived a hand-written case.

    Two invariants, checked at every minute of a full week and of both 2026 and
    2027 daylight-saving weekends: `next_change` is in the future, and stepping
    to it lands in `next_phase`. The Friday hole above was 1,200 minutes wide
    and no single-timestamp test found it.

    The DST spans are here because `_at` rebuilds a wall-clock time on a day
    whose offset changes; the sweep is what shows that every boundary this
    module names — 04:00, 09:30, 16:00, 20:00 — sits outside the ambiguous and
    non-existent hours, rather than that being assumed.
    """
    spans = [
        (datetime(2026, 8, 10, tzinfo=UTC), 8),   # an ordinary week
        (datetime(2026, 3, 6, tzinfo=UTC), 4),    # spring forward
        (datetime(2026, 11, 1, tzinfo=UTC), 4),   # fall back
        (datetime(2027, 3, 12, tzinfo=UTC), 4),
        (datetime(2027, 11, 5, tzinfo=UTC), 4),
    ]

    for start, days in spans:
        now = start
        end = start + timedelta(days=days)
        while now < end:
            state = market_state(now, windows_by_day=WINDOWS)
            assert state.next_change > now, f"{now} promised a boundary in the past"
            landed = market_state(state.next_change, windows_by_day=WINDOWS)
            assert landed.phase is state.next_phase, (
                f"{now} (New York {now.astimezone(ZoneInfo('America/New_York'))}) "
                f"is {state.phase} and promised {state.next_phase} at "
                f"{state.next_change}, which is actually {landed.phase}"
            )
            now += timedelta(minutes=1)


def test_the_countdown_points_at_the_next_boundary():
    state = market_state(at(8, 10, 8, 45), windows_by_day=WINDOWS)

    assert state.next_phase is MarketPhase.OPEN
    # 08:45 UTC to 13:30 UTC.
    assert state.seconds_to_change == pytest.approx(4 * 3600 + 45 * 60)


# ------------------------------------------------- the venue is not the gate


def test_the_market_being_open_is_not_the_bot_being_willing():
    """The distinction this module exists for.

    Pre-market at 04:45 ET is a real, tradeable Alpaca session. The gate's
    window does not open for hours. Reporting one green light would answer the
    wrong question.
    """
    state = market_state(at(8, 10, 8, 45), windows_by_day=WINDOWS)

    assert state.phase is MarketPhase.PRE       # Alpaca: pre-market
    # The operator has since widened the window to cover pre-market, so the
    # gate is open here. That does NOT make the two the same question — which
    # is the whole point of the module. `is_tradeable_by_bot` requires the
    # REGULAR session as well, so it still says no, and it says no for a
    # reason a reader can act on.
    assert state.bot_window_open is True
    assert state.is_tradeable_by_bot is False


def test_the_gate_window_can_be_open_while_the_session_is_shut():
    """And this is a REAL defect in `config/rules.yaml`, pinned deliberately.

    `sessions_utc: [[14, 21]]` is the winter (EST) window applied all year. In
    August the regular session is 13:30-20:00 UTC, so the configured window
    runs a full hour past the close and the gate would approve into after-hours
    — where an order without `extended_hours` rests until the next open and
    fills at a price nobody looked at.

    `is_tradeable_by_bot` requires BOTH, which is what keeps the display honest
    while the window itself is still wrong. If the window is ever made
    daylight-saving aware, this test should start failing and that is the
    signal to delete it, not to loosen it.
    """
    state = market_state(at(8, 10, 20, 30), windows_by_day=WINDOWS)

    assert state.phase is MarketPhase.POST
    assert state.bot_window_open is True
    assert state.is_tradeable_by_bot is False


def test_no_rules_means_the_gate_reports_shut_rather_than_open():
    """A caller that did not supply the rules gets the cautious reading. A
    cheerful default here would be a green light nobody configured."""
    state = market_state(at(8, 10, 15, 0))

    assert state.phase is MarketPhase.OPEN
    assert state.bot_window_open is False
    assert state.bot_window_next is None


def test_the_next_gate_opening_is_found_across_a_weekend():
    """Saturday's answer is Monday, and finding it must not loop forever."""
    state = market_state(at(8, 15, 12, 0), windows_by_day=WINDOWS)

    assert state.bot_window_open is False
    assert state.bot_window_next == at(8, 17, 8, 0)


def test_an_empty_window_map_gives_up_rather_than_spinning():
    """A config with no windows at all is a real possibility. `None` says 'not
    from here' instead of searching to the heat death of the universe."""
    state = market_state(at(8, 10, 15, 0), windows_by_day={})

    assert state.bot_window_next is None


# ----------------------------------------------------------- daylight saving


def test_the_phases_follow_new_york_across_a_daylight_saving_change():
    """The boundaries are defined in New York time and converted, never stored
    as fixed UTC hours. January is EST, so the same UTC hour is a different
    session — which is exactly the trap the gate's own window fell into."""
    # 14:30 UTC: 10:30 EST in January (open), 10:30 EDT in August (open too).
    assert market_state(at(1, 12, 14, 30)).phase is MarketPhase.OPEN
    assert market_state(at(8, 10, 14, 30)).phase is MarketPhase.OPEN

    # 13:45 UTC: 08:45 EST in January (pre-market), 09:45 EDT in August (open).
    assert market_state(at(1, 12, 13, 45)).phase is MarketPhase.PRE
    assert market_state(at(8, 10, 13, 45)).phase is MarketPhase.OPEN


# ------------------------------------------- what the model is actually told


def scheduled(moment: datetime, clock: BrokerClock | None = None) -> str:
    return "\n".join(
        render_sessions(
            moment, windows_by_class={"us_equity": WINDOWS}, broker_clock=clock
        )
    )


MECHANICS = "Out-of-hours mechanics"


def test_the_out_of_hours_mechanics_are_stated_when_the_session_is_shut():
    """The model has no memory and no way to discover this by experiment: an
    entry proposed in the pre-market does NOT trade there. It is a bracket or an
    OTO, Alpaca refuses `extended_hours` on both, so it rests and fills at the
    open at a price shown nowhere in the context."""
    text = scheduled(at(8, 10, 8, 45))          # 04:45 ET, pre-market

    assert "PRE-MARKET" in text
    assert MECHANICS in text
    assert "RESTS" in text
    assert "CANNOT FIRE" in text


def test_the_mechanics_are_omitted_during_the_regular_session():
    """Tokens on every cycle for a paragraph that does not apply, and worse, a
    standing warning that an order will rest when it will not."""
    text = scheduled(at(8, 10, 15, 0))          # 11:00 ET

    assert "OPEN" in text
    assert MECHANICS not in text


def test_a_holiday_still_gets_the_mechanics_even_though_the_hours_read_open():
    """The case this whole broker-clock path exists for, and the one that was
    wrong first time round.

    Labor Day is a Monday and reads as an ordinary trading day to arithmetic
    over New York time. `any_shut` was computed from the phase alone, so at the
    one moment the model most needs telling that its order will rest, it was
    told nothing.
    """
    holiday = datetime(2026, 9, 7, 15, 0, tzinfo=UTC)
    shut = BrokerClock(
        is_open=False,
        next_open=datetime(2026, 9, 8, 13, 30, tzinfo=UTC),
        next_close=datetime(2026, 9, 8, 20, 0, tzinfo=UTC),
    )

    text = scheduled(holiday, shut)

    assert "reports it CLOSED" in text
    assert "market holiday" in text
    assert MECHANICS in text


def test_a_broker_clock_that_agrees_adds_no_noise():
    open_now = BrokerClock(
        is_open=True,
        next_open=at(8, 11, 13, 30),
        next_close=at(8, 10, 20, 0),
    )
    text = scheduled(at(8, 10, 15, 0), open_now)

    assert "NOTE:" not in text


def test_the_broker_saying_open_while_the_hours_say_otherwise_is_named_too():
    """The mirror case: an early-close day put back, or a session Alpaca has
    extended. Rarer than a holiday and reported the same way, because the point
    is to surface a disagreement rather than to pick a winner."""
    open_now = BrokerClock(
        is_open=True,
        next_open=at(8, 11, 13, 30),
        next_close=at(8, 10, 20, 0),
    )
    text = scheduled(at(8, 10, 8, 45), open_now)     # computed: pre-market

    assert "reports the regular session OPEN" in text
    assert "The broker is authoritative" in text


def test_neither_source_read_says_so_rather_than_implying_an_ordinary_day():
    """`FinnhubCalendar.is_degraded` in another costume. Silence about holidays
    reads as "no holiday" unless it is stated, and on Thanksgiving that is a
    confident wrong answer.

    Fires only when BOTH the live clock and the cached calendar are absent. With
    either one present the caveat would be false, and a warning that is
    sometimes wrong is one an operator learns to skip.
    """
    text = scheduled(at(8, 10, 15, 0), None)

    assert "Neither Alpaca's clock nor its trading calendar" in text
    assert "market holiday" in text


def test_a_continuous_market_is_never_described_as_shut():
    """"Out of hours" is a property of the moment AND the instrument. Crypto
    trades through Sunday 03:00, and telling the model the market is closed
    beside a crypto symbol would simply be false."""
    text = "\n".join(
        render_sessions(
            at(8, 16, 3, 0),                    # Sunday, deep in the weekend
            windows_by_class={"crypto": CONTINUOUS},
        )
    )

    assert "CONTINUOUS" in text
    assert "WEEKEND" not in text
    assert MECHANICS not in text
    # No sessions means no holiday question, so the caveat would be noise.
    assert "NOT read this cycle" not in text


def test_a_mixed_config_answers_each_class_on_its_own_terms():
    text = "\n".join(
        render_sessions(
            at(8, 16, 3, 0),
            windows_by_class={"crypto": CONTINUOUS, "us_equity": WINDOWS},
        )
    )

    assert "crypto: CONTINUOUS" in text
    assert "us_equity: WEEKEND" in text
    assert MECHANICS in text


def test_is_continuous_recognises_only_a_genuinely_unbroken_week():
    assert is_continuous(CONTINUOUS) is True
    assert is_continuous(WINDOWS) is False
    assert is_continuous({}) is False
    # Six days of 24 hours is not a 24/7 market, and the missing day is exactly
    # the one somebody would trade into.
    assert is_continuous({d: [(0, 24)] for d in range(6)}) is False


# ------------------------------------------------ daylight saving and holidays


def test_every_exchange_opens_at_its_own_local_hour_across_daylight_saving():
    """The property the whole module exists for, applied to four exchanges.

    Sessions are declared in each exchange's LOCAL time and converted, never
    stored as fixed UTC hours — so the southern hemisphere's daylight saving,
    which runs opposite to the northern one, needs no diary entry either. This
    is exactly what `sessions_utc` in `config/rules.yaml` cannot do, and why
    that field is an hour out for half the year.
    """
    from bot.market_clock import CLOCKS

    face = {f.code: f for f in CLOCKS}

    # ASX at 10:00 Sydney: August is AEST (UTC+10), January is AEDT (UTC+11).
    assert face["SYD"].is_open(datetime(2026, 8, 11, 0, 0, tzinfo=UTC)) is True
    assert face["SYD"].is_open(datetime(2026, 1, 13, 23, 0, tzinfo=UTC)) is True

    # NYSE at 09:35 New York: August is EDT (UTC-4), January is EST (UTC-5).
    assert face["NY"].is_open(datetime(2026, 8, 11, 13, 35, tzinfo=UTC)) is True
    assert face["NY"].is_open(datetime(2026, 1, 13, 14, 35, tzinfo=UTC)) is True

    # Japan observes no daylight saving at all, so JST is UTC+9 year round.
    #
    # The August date used to be the 11th, and that was WRONG in a way nothing
    # could see: 11 August is Mountain Day, a Japanese national holiday, and the
    # TSE is shut. The assertion passed because `is_open` was weekday-shaped, so
    # the suite pinned the badge claiming Tokyo was trading on a public holiday
    # — the exact failure this item was raised to fix, sitting inside the test
    # that was supposed to be checking the hours. It was found by installing the
    # holiday rules and watching this line go red.
    assert face["TYO"].is_open(datetime(2026, 8, 12, 1, 0, tzinfo=UTC)) is True
    assert face["TYO"].is_open(datetime(2026, 1, 13, 1, 0, tzinfo=UTC)) is True


def test_a_holiday_shuts_the_new_york_badge_despite_the_clock():
    """Christmas Day 2026 is a Friday and `_phase_at` reads it as an ordinary
    trading day. Without the calendar the badge says NYSE is trading on 25
    December, which is the plausible-wrong-figure failure on the one strip
    whose only job is orientation."""
    from bot.market_clock import CLOCKS, MarketPhase, VenueState

    ny = next(f for f in CLOCKS if f.code == "NY")
    xmas = datetime(2026, 12, 25, 15, 0, tzinfo=UTC)     # 10:00 New York

    assert ny.is_open(xmas) is True                       # the clock alone
    assert ny.state(xmas, MarketPhase.OPEN) is VenueState.LIVE
    # With the calendar's answer, the holiday wins.
    assert (
        ny.state(xmas, MarketPhase.OPEN, trades_today=False) is VenueState.CLOSED
    )


def test_an_unknown_calendar_falls_through_rather_than_reading_as_a_holiday():
    """`None` means "could not say". Same three-valued rule as everywhere else:
    an unavailable check must not become the pessimistic answer any more than
    it becomes the cheerful one."""
    from bot.market_clock import CLOCKS, MarketPhase, VenueState

    ny = next(f for f in CLOCKS if f.code == "NY")
    ordinary = datetime(2026, 8, 11, 15, 0, tzinfo=UTC)

    assert ny.state(ordinary, MarketPhase.OPEN, trades_today=None) is VenueState.LIVE


@needs_rules
def test_every_exchange_on_the_strip_claims_a_holiday_source():
    """Two sources, and the claim has to be true of both.

    New York is `session_calendar`, which is Alpaca's US equity calendar and
    speaks for nothing else — handing it to Sydney would assert the ASX keeps
    NYSE's holidays. TSE, ASX and NZX are `exchange_calendars`, which ships
    their real rules and computes them offline.

    Hardcoding three lists remains the wrong repair: they go stale in silence,
    and a stale list still looks answered.

    This test runs where the optional package IS installed — the `dev` extra
    pulls it in, so CI covers this path. The other path has its own test below,
    and both are needed: a suite that only ever saw one of them would be making
    the weaker claim.
    """
    from bot.market_clock import CLOCKS

    tracked = {f.code for f in CLOCKS if f.tracks_holidays}

    assert tracked == {"NY", "TYO", "SYD", "AKL"}


def test_new_york_is_not_answered_twice():
    """Alpaca's calendar is the one that counts for the market this bot trades.

    `exchange_calendars` ships XNYS and it would be one line to use it. It is
    deliberately not used: a second source for the same fact is a second fact
    that can disagree with the first, and the broker's answer is the one that
    decides whether an order fills. Same reasoning that keeps `Adoption.is_live`
    computed rather than stored.
    """
    from bot.market_clock import CLOCKS

    claimed = {f.code: f.calendar_code for f in CLOCKS}

    assert claimed["NY"] == ""
    assert claimed["TYO"] == "XTKS"
    assert claimed["SYD"] == "XASX"
    assert claimed["AKL"] == "XNZE"


# ------------------------------------- holidays at the exchanges Alpaca cannot see


def local(zone: str, y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    """A moment expressed in an exchange's OWN time, handed over as UTC.

    Written this way round because every assertion below is about a local
    trading hour, and doing the offset arithmetic in the test is how a date
    quietly becomes the day before across the international date line.
    """
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(zone)).astimezone(UTC)


def face(code: str) -> ClockFace:
    return next(f for f in CLOCKS if f.code == code)


@needs_rules
def test_boxing_day_shuts_the_asx_badge():
    """The bug this item was raised for, named in `TODO.md` in these words.

    Boxing Day 2026 falls on a Saturday, so the ASX observes it on Monday 28
    December — which is precisely the case a weekday-shaped check cannot see.
    It is an ordinary Monday to arithmetic over Sydney time, and the badge said
    the ASX was trading.
    """
    sydney = face("SYD")

    assert sydney.is_open(local("Australia/Sydney", 2026, 12, 28, 11)) is False
    assert (
        sydney.state(local("Australia/Sydney", 2026, 12, 28, 11)) is VenueState.CLOSED
    )
    # The Wednesday before is an ordinary session, so this is a holiday rule
    # rather than the whole week going dark.
    assert sydney.is_open(local("Australia/Sydney", 2026, 12, 23, 11)) is True


@needs_rules
def test_each_exchange_keeps_its_own_holidays_rather_than_a_shared_list():
    """The property that makes three separate calendars worth having.

    Japan does not observe Christmas, so the TSE trades on 25 December while the
    ASX and the NZX are shut. One list applied to all three — which is what
    hardcoding would drift towards — gets one of them wrong every time, and the
    strip's only job is orientation.
    """
    assert face("TYO").is_open(local("Asia/Tokyo", 2026, 12, 25, 10)) is True
    assert face("SYD").is_open(local("Australia/Sydney", 2026, 12, 25, 11)) is False
    assert face("AKL").is_open(local("Pacific/Auckland", 2026, 12, 25, 11)) is False

    # And the reverse: Mountain Day is a Japanese national holiday on 11 August
    # and means nothing to the other two.
    assert face("TYO").is_open(local("Asia/Tokyo", 2026, 8, 11, 10)) is False
    assert face("SYD").is_open(local("Australia/Sydney", 2026, 8, 11, 11)) is True


@needs_rules
def test_an_early_close_shuts_the_badge_at_the_hour_it_actually_shuts():
    """The half of this that `session_calendar` says is the more dangerous one.

    A holiday reads as a suspicious silence; an early close does not. The ASX
    shuts at 14:10 Sydney on Christmas Eve and the session was genuinely open
    all morning, so nothing looks wrong at 15:00 while the badge says trading.

    Nothing had to be told this day is unusual. The windows come from the
    calendar rather than from the hand-written pair, so the real close is simply
    the close.
    """
    sydney = face("SYD")

    assert sydney.is_open(local("Australia/Sydney", 2026, 12, 24, 13)) is True
    assert sydney.is_open(local("Australia/Sydney", 2026, 12, 24, 15)) is False
    # An ordinary day still runs to 16:00, so this is that date's close and not
    # the hours being clipped everywhere.
    assert sydney.is_open(local("Australia/Sydney", 2026, 12, 23, 15)) is True


@needs_rules
def test_the_lunch_break_survives_the_calendar_taking_over_the_hours():
    """Tokyo breaks 11:30-12:30 JST, and that is now the LIBRARY's break.

    The windows for a date come from `exchange_calendars`, so a break it did not
    report would silently paint the TSE open through an hour it is shut — the
    exact figure `ClockFace.sessions` grew a tuple to avoid. Worth pinning
    separately from the hours, because a break is the thing a calendar is most
    likely to model as one long session.
    """
    tokyo = face("TYO")

    assert tokyo.is_open(local("Asia/Tokyo", 2026, 8, 12, 11)) is True
    assert tokyo.is_open(local("Asia/Tokyo", 2026, 8, 12, 12)) is False
    assert tokyo.is_open(local("Asia/Tokyo", 2026, 8, 12, 13)) is True
    assert tokyo.is_open(local("Asia/Tokyo", 2026, 8, 12, 15, 45)) is False


@needs_rules
def test_the_hand_written_hours_still_agree_with_the_shipped_rules():
    """The drift guard, and the reason `ClockFace.sessions` is kept at all.

    Those tuples are the fallback for a box without the optional package, so
    they have to stay true — and once the library supersedes them nothing would
    notice them going wrong. A prose note saying "these agree" is not a
    guarantee; this is.

    Checked on an ordinary mid-week session at each exchange, so a holiday or a
    half-day cannot be what makes it pass or fail.
    """
    from bot import exchange_hours

    ordinary = {"TYO": (2026, 8, 12), "SYD": (2026, 8, 12), "AKL": (2026, 8, 12)}

    for code, (y, m, d) in ordinary.items():
        clock = face(code)
        shipped = exchange_hours.session_windows(clock.calendar_code, date(y, m, d))
        assert shipped == clock.sessions, (
            f"{clock.exchange}: the hours in market_clock.py and the ones in "
            f"exchange_calendars have drifted apart — {clock.sessions} against "
            f"{shipped}. One of them is now wrong on every box, and which one "
            "depends on whether the optional package is installed."
        )


@needs_rules
def test_the_time_zone_on_the_face_agrees_with_the_calendars():
    """Two names for one zone, and only one of them is used for the hours.

    `ClockFace.zone` converts the clock; `exchange_hours` converts the session
    boundaries using the calendar's own zone. If those ever disagreed the badge
    would compare a Sydney clock against a Melbourne session and be an hour out
    for half the year, with nothing to say so.
    """
    import exchange_calendars

    for clock in CLOCKS:
        if not clock.calendar_code:
            continue
        calendar = exchange_calendars.get_calendar(clock.calendar_code)
        assert str(calendar.tz) == clock.zone


def test_without_the_rules_the_badges_are_exactly_what_they_were():
    """The degradation, and it has to be EXACTLY the old behaviour.

    This is a package on the box that runs the trading loop, added to colour a
    badge for three markets the bot does not trade. It has to be droppable — so
    an absent import answers "could not ask", the weekday-shaped computation
    takes over, and the claim is withdrawn with it. `tracks_holidays` going
    False is the load-bearing half: a badge that kept claiming to track
    holidays while answering from weekday hours is worse than the gap this
    replaced, because the limit would have stopped travelling with the claim.

    The absence is simulated at the real boundary — `sys.modules` — so this is
    the same `ImportError` a box without the package raises, not a stub of one.
    """
    from bot import exchange_hours

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(exchange_hours, "_SOURCES", {})
        patch.setattr(exchange_hours, "_REASONS", {})
        patch.setitem(sys.modules, "exchange_calendars", None)

        sydney = face("SYD")
        assert sydney.tracks_holidays is False
        assert exchange_hours.session_windows("XASX", date(2026, 12, 28)) is None
        # Boxing Day reads open again, which is the old bug — correctly, because
        # nothing here can see it any more and the badge now says so.
        assert sydney.is_open(local("Australia/Sydney", 2026, 12, 28, 11)) is True
        # Weekends were never a holiday question and still are not.
        assert sydney.is_open(local("Australia/Sydney", 2026, 12, 26, 11)) is False
        # New York is unaffected: its answer comes from Alpaca, not from here.
        assert face("NY").tracks_holidays is True


def test_an_absent_package_says_which_of_the_two_reasons_applies():
    """The Settings-card lesson: a surface that knows it is empty must not guess.

    "The package is not installed" and "this exchange has no rules here" are
    different answers an operator would act on differently, and one of them is
    a one-line fix.
    """
    from bot import exchange_hours

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(exchange_hours, "_SOURCES", {})
        patch.setattr(exchange_hours, "_REASONS", {})
        patch.setitem(sys.modules, "exchange_calendars", None)

        assert "not installed" in (exchange_hours.unavailable_reason("XASX") or "")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(exchange_hours, "_SOURCES", {})
        patch.setattr(exchange_hours, "_REASONS", {})

        # A clock that names no exchange calendar at all — New York's case.
        assert "no exchange calendar" in (exchange_hours.unavailable_reason("") or "")


def test_an_exchange_nobody_has_rules_for_makes_no_claim_and_falls_back():
    """A code the library does not recognise is "could not ask", not a holiday.

    Same three-valued rule as `SessionCalendar.is_trading_day` and
    `Broker.orders_degraded`. The badge keeps working on weekday hours and stops
    claiming to know about holidays, which is the pair that has to move
    together.
    """
    from bot import exchange_hours

    invented = ClockFace(
        "Nowhere-on-Sea", "UTC", code="NOS", exchange="NOX",
        sessions=((time(9, 0), time(17, 0)),), calendar_code="XNOPE",
    )

    assert invented.tracks_holidays is False
    assert exchange_hours.session_windows("XNOPE", date(2026, 8, 12)) is None
    assert invented.is_open(datetime(2026, 8, 12, 12, 0, tzinfo=UTC)) is True
    assert invented.is_open(datetime(2026, 8, 15, 12, 0, tzinfo=UTC)) is False


@needs_rules
def test_a_date_outside_the_shipped_rules_is_unknown_rather_than_a_holiday():
    """`exchange_calendars` bounds each calendar to about twenty years back and
    one year ahead. A question past that edge is outside what it ships, not a
    quiet decade — the `SessionCalendar.covers` rule, and the reason `covers` is
    a separate question from "is the answer empty".

    Unreachable for a tape rendering the present moment, which is exactly why it
    is worth pinning: the case that cannot happen is the one nobody notices has
    started happening.
    """
    from bot import exchange_hours

    far_future = date.today().replace(year=date.today().year + 5)

    assert exchange_hours.covers("XASX", date(2026, 8, 12)) is True
    assert exchange_hours.covers("XASX", far_future) is False
    assert exchange_hours.session_windows("XASX", far_future) is None


@needs_rules
def test_a_holiday_is_not_confused_with_a_weekend_or_with_no_answer():
    """Three states that must not collapse, checked on one exchange.

    `()` is "asked, and it does not trade"; a populated tuple is a real session;
    `None` is "could not ask". A caller that read `None` as a closure would
    paint every badge shut the moment the package went missing.
    """
    from bot import exchange_hours

    assert exchange_hours.session_windows("XNZE", date(2026, 2, 6)) == ()   # Waitangi
    assert exchange_hours.session_windows("XNZE", date(2026, 2, 7)) == ()   # Saturday
    assert exchange_hours.session_windows("XNZE", date(2026, 2, 5)) == (
        (time(10, 0), time(16, 45)),
    )
    assert exchange_hours.session_windows("", date(2026, 2, 5)) is None


def test_the_holiday_rules_never_gate_anything():
    """`RiskGate` reads `market_state`, and `market_state` must not reach here.

    The gate has to stay deterministic and must not fail open, which is why
    `session_calendar` and `BrokerClock` are reported BESIDE the computed phase
    rather than inside it. This is a display badge for exchanges the bot holds
    no position on, and it must stay on that side of the line.

    Proved by making the lookup explode: anything on the gate's path that
    consulted it would raise rather than quietly pass.
    """
    from bot import exchange_hours

    def detonate(*args: object, **kwargs: object) -> None:
        raise AssertionError("the risk gate's clock consulted an exchange calendar")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(exchange_hours, "session_windows", detonate)
        patch.setattr(exchange_hours, "is_available", detonate)

        state = market_state(at(8, 10, 15, 0), windows_by_day=WINDOWS)
        assert state.phase is MarketPhase.OPEN
        assert scheduled(at(8, 10, 8, 45)).count(MECHANICS) == 1


def test_the_rules_are_computed_offline_and_cost_the_loop_nothing_to_import():
    """The two claims that have to be MEASURED rather than read.

    Run in a cold subprocess for a reason: by the time the rest of this file has
    run, `exchange_calendars` is imported and its calendars are cached, so an
    in-process check would prove only that a warm lookup makes no call. The
    interesting moment is the first one.

    Two things are asserted there:

    - **No network.** `socket.socket`, `socket.create_connection` and
      `socket.getaddrinfo` are all replaced with explosives BEFORE the import,
      so a release that started fetching a calendar fails the suite rather than
      putting a network call on a render path. Tests may not touch the network
      and this must not become the exception.
    - **Importing `bot.market_clock` does not drag pandas in.** The import is
      lazy, inside a function, so the trading loop pays nothing for a dependency
      it never asks a question of — and a box without the package can still
      import the module at all, which a top-level import would break.
    """
    script = textwrap.dedent(
        """
        import socket, sys

        def explode(*a, **k):
            raise AssertionError("a network call was attempted")

        # A SUBCLASS rather than a function, because `ssl` derives SSLSocket
        # from whatever `socket.socket` is at import time. Swapping in a plain
        # function makes the stdlib fail to import at all, which would look like
        # a passing network check while proving nothing.
        class NoSocket(socket.socket):
            def __init__(self, *a, **k):
                raise AssertionError("a socket was opened")

        socket.socket = NoSocket
        socket.create_connection = explode
        socket.getaddrinfo = explode

        sys.path.insert(0, "src")
        import bot.market_clock as mc
        assert "pandas" not in sys.modules, "market_clock dragged pandas in"
        assert "exchange_calendars" not in sys.modules, "the import is not lazy"

        from datetime import datetime
        from zoneinfo import ZoneInfo

        sydney = next(f for f in mc.CLOCKS if f.code == "SYD")
        when = datetime(2026, 12, 28, 11, tzinfo=ZoneInfo("Australia/Sydney"))
        print("boxing-day-open:", sydney.is_open(when))
        print("tracks:", sydney.tracks_holidays)
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    if HAS_RULES:
        # Built from scratch with every socket disarmed, and still right about
        # Boxing Day.
        assert "boxing-day-open: False" in done.stdout
        assert "tracks: True" in done.stdout
    else:
        assert "boxing-day-open: True" in done.stdout
        assert "tracks: False" in done.stdout
