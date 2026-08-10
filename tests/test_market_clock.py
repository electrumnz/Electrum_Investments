"""The session phases, and the distinction the whole module exists for.

Written after a real confusion: "the market should be open right now", at 04:49
New York time on a Monday. Alpaca *was* open — pre-market starts at 04:00 — so
the answer was not simply no. It was "open for a session this bot does not
trade, four and a half hours before the one it does", and only two clocks read
together give you that.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bot.config import load_rules
from bot.market_clock import (
    BrokerClock,
    MarketPhase,
    is_continuous,
    market_state,
    render_sessions,
)

WINDOWS = load_rules().instruments["us_equity"].windows_by_day
CONTINUOUS = load_rules().instruments["crypto"].windows_by_day


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
