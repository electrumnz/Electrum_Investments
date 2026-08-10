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
from bot.market_clock import MarketPhase, market_state

WINDOWS = load_rules().instruments["us_equity"].windows_by_day


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

    assert state.phase is MarketPhase.PRE       # Alpaca: open
    assert state.bot_window_open is False       # rules.yaml: shut
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
    assert state.bot_window_next == at(8, 17, 14, 0)


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
