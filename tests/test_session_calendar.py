"""The trading calendar, and the failure it exists to catch.

A market holiday reads as a suspicious silence — nothing fills, no bars arrive,
something eventually looks wrong. An **early close** does not. On the Friday
after Thanksgiving the market shuts at 13:00 New York; `market_clock._phase_at`
says 16:00 for three more hours; the session was genuinely open and every figure
stays plausible. That is the confident-partial-answer failure this repository is
built around, arriving through the calendar rather than through the model.

Nothing here reaches the risk gate. It is a network-derived cache, and the gate
has to stay deterministic and must not fail open on one.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from bot.broker import MockBroker
from bot.market_clock import NY, TradingDay, render_sessions
from bot.session_calendar import REFRESH_AFTER, RETRY_AFTER, SessionCalendar

WINDOWS = {d: [(8, 24)] for d in range(5)}


def day(d: date, close_hour: int = 16, close_minute: int = 0) -> TradingDay:
    def ny(h: int, m: int) -> datetime:
        return datetime(d.year, d.month, d.day, h, m, tzinfo=NY).astimezone(UTC)

    return TradingDay(
        date=d, open_utc=ny(9, 30), close_utc=ny(close_hour, close_minute)
    )


# Thanksgiving 2026 is Thursday 26 November. The market is shut that day and
# closes at 13:00 the Friday after — a real, dated example rather than a made-up
# one, so the arithmetic can be checked against a published calendar.
THANKSGIVING = date(2026, 11, 26)
BLACK_FRIDAY = date(2026, 11, 27)

NOVEMBER = [
    day(date(2026, 11, 24)),
    day(date(2026, 11, 25)),
    # 26th absent: holiday.
    day(BLACK_FRIDAY, close_hour=13),
    day(date(2026, 11, 30)),
]


def loaded(days: list[TradingDay], now: datetime) -> SessionCalendar:
    broker = MockBroker()
    broker.set_calendar(days)
    cal = SessionCalendar()
    cal.refresh(broker, now)
    return cal


# --------------------------------------------------------------- the half day


def test_an_early_close_is_recognised_from_its_hours():
    """13:00 is not a shorter version of 16:00, it is a different session."""
    assert day(BLACK_FRIDAY, close_hour=13).is_early_close is True
    assert day(BLACK_FRIDAY).is_early_close is False
    assert "EARLY CLOSE" in day(BLACK_FRIDAY, close_hour=13).render()


def test_the_early_close_is_announced_on_the_day():
    """The whole reason for holding a calendar. Nothing else in the codebase can
    see this: the phase computation says OPEN until 16:00, the quote is live,
    the bars arrive, and every figure is plausible."""
    noon = datetime(2026, 11, 27, 16, 0, tzinfo=UTC)     # 11:00 New York
    lines = "\n".join(loaded(NOVEMBER, noon).render(BLACK_FRIDAY))

    assert "EARLY CLOSE TODAY" in lines
    assert "13:00 New York, not 16:00" in lines


def test_past_an_early_close_the_session_block_says_the_order_will_rest():
    """14:00 on a half day. `_phase_at` still reads OPEN for two more hours, so
    without the calendar the model would be told its order fills now."""
    after = datetime(2026, 11, 27, 19, 0, tzinfo=UTC)    # 14:00 New York
    text = "\n".join(
        render_sessions(
            after,
            windows_by_class={"us_equity": WINDOWS},
            calendar=loaded(NOVEMBER, after),
        )
    )

    assert "Out-of-hours mechanics" in text
    assert "RESTS" in text


def test_before_an_early_close_the_order_still_fills_normally():
    """The counterpart, so the warning keeps meaning something. 11:00 on a half
    day is an ordinary open market."""
    noon = datetime(2026, 11, 27, 16, 0, tzinfo=UTC)
    text = "\n".join(
        render_sessions(
            noon,
            windows_by_class={"us_equity": WINDOWS},
            calendar=loaded(NOVEMBER, noon),
        )
    )

    assert "EARLY CLOSE TODAY" in text
    assert "Out-of-hours mechanics" not in text


def test_a_half_day_ahead_is_flagged_before_it_arrives():
    """Knowing on the day is late. A position opened Wednesday afternoon expects
    Friday's usual close."""
    tuesday = datetime(2026, 11, 24, 16, 0, tzinfo=UTC)
    lines = "\n".join(loaded(NOVEMBER, tuesday).render(date(2026, 11, 24)))

    assert "Non-standard hours coming up" in lines
    assert "EARLY CLOSE" in lines


# ----------------------------------------------------------------- a holiday


def test_a_holiday_is_named_with_the_date_trading_resumes():
    thursday = datetime(2026, 11, 26, 16, 0, tzinfo=UTC)
    lines = "\n".join(loaded(NOVEMBER, thursday).render(THANKSGIVING))

    assert "NOT a trading day" in lines
    assert "Trading resumes Fri 27 Nov" in lines


def test_a_holiday_forces_the_mechanics_without_a_live_clock():
    """Two independent routes to the same fact, on purpose. `get_clock` catches
    a holiday only while standing in it and only if the call succeeds; the
    cached calendar answers offline, so one failing does not take both down."""
    thursday = datetime(2026, 11, 26, 16, 0, tzinfo=UTC)
    text = "\n".join(
        render_sessions(
            thursday,
            windows_by_class={"us_equity": WINDOWS},
            broker_clock=None,
            calendar=loaded(NOVEMBER, thursday),
        )
    )

    assert "Out-of-hours mechanics" in text


# ------------------------------------------------- unknown is not "no holiday"


def test_an_unloaded_calendar_says_so_rather_than_reporting_a_clear_run():
    """`FinnhubCalendar.is_degraded` again. An empty holiday list from a
    calendar nobody fetched is indistinguishable from a quarter with no
    holidays, and only one of those should be acted on."""
    cal = SessionCalendar()

    assert cal.is_loaded is False
    assert cal.is_degraded is True
    assert cal.is_trading_day(BLACK_FRIDAY) is None
    assert "NOT loaded" in "\n".join(cal.render(BLACK_FRIDAY))


def test_a_failed_refresh_keeps_the_days_it_already_had():
    """A calendar published months ago does not stop being true because one
    fetch timed out. Clearing it would turn a transient failure into "every day
    is unknown", which is worse than a slightly old answer and is exactly when
    the answer is most needed."""
    now = datetime(2026, 11, 24, 16, 0, tzinfo=UTC)
    broker = MockBroker()
    broker.set_calendar(NOVEMBER)
    cal = SessionCalendar()
    cal.refresh(broker, now)

    broker.set_calendar(None)                      # the fetch now fails
    cal.refresh(broker, now + REFRESH_AFTER + timedelta(minutes=1))

    assert cal.is_trading_day(THANKSGIVING) is False       # still known
    assert cal.is_degraded is True                          # but flagged
    assert "CALENDAR STALE" in "\n".join(cal.render(date(2026, 11, 24)))


def test_a_date_outside_the_fetched_range_is_unknown_not_a_holiday():
    """The distinction `covers` exists for. Absence from the dict means "not a
    trading day" only inside the range that was actually fetched."""
    now = datetime(2026, 11, 24, 16, 0, tzinfo=UTC)
    cal = loaded(NOVEMBER, now)

    assert cal.is_trading_day(THANKSGIVING) is False        # in range, shut
    assert cal.is_trading_day(date(2030, 1, 2)) is None      # out of range


# ------------------------------------------------------------------ refreshing


def test_a_refresh_is_a_no_op_until_the_cache_goes_stale():
    """96 cycles a day against a calendar published a year ahead. The cadence is
    the point: this must not become a per-cycle network call."""
    now = datetime(2026, 11, 24, 16, 0, tzinfo=UTC)
    broker = MockBroker()
    broker.set_calendar(NOVEMBER)
    cal = SessionCalendar()

    assert cal.refresh(broker, now) is True                 # first, always
    assert cal.refresh(broker, now + timedelta(minutes=15)) is False
    assert cal.refresh(broker, now + timedelta(hours=4)) is False
    assert cal.refresh(broker, now + REFRESH_AFTER) is True


def test_a_broker_with_no_calendar_method_is_survived():
    """The Protocol carries `get_calendar`, but a hand-rolled double in some
    other test need not, and a calendar refresh must never be the thing that
    ends a cycle."""
    cal = SessionCalendar()

    assert cal.refresh(object(), datetime(2026, 11, 24, tzinfo=UTC)) is False
    assert cal.is_degraded is True


def test_a_failed_fetch_backs_off_instead_of_retrying_every_call():
    """Found by running the dashboard, not by the suite.

    A failure leaves `_fetched_at` unset, so "have we ever fetched" stayed false
    and every caller re-attempted. The web poller reads every five seconds
    against a `MockBroker` with no calendar — five failed attempts in ten
    seconds, and it would have carried on all day. Against real Alpaca that is a
    hot retry loop aimed at an endpoint that has just shown it is unhappy.
    """
    now = datetime(2026, 11, 24, 16, 0, tzinfo=UTC)
    broker = MockBroker()                       # no calendar seeded: fetch fails
    cal = SessionCalendar()

    assert cal.refresh(broker, now) is False                    # attempted
    assert cal.refresh(broker, now + timedelta(seconds=5)) is False
    assert cal._attempted_at == now                             # not re-attempted

    # And it does try again, so a transient failure heals rather than sticking.
    broker.set_calendar(NOVEMBER)
    assert cal.refresh(broker, now + RETRY_AFTER) is True
    assert cal.is_degraded is False
