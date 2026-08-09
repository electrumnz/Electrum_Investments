"""Tests for the Marketaux and Finnhub adapters.

The two feeds are not equally important and the tests reflect that. Headlines
are context: losing them costs Claude some information. The earnings calendar
feeds `RiskGate._news_blackout`, so losing it silently disarms a risk rule —
most of the Finnhub tests are about that failure being visible rather than
about parsing.

Nothing here touches the network: every adapter takes its JSON getter as an
argument and the tests pass a stub.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

import pytest

from bot.data.finnhub import FinnhubCalendar
from bot.data.finnhub import _parse as parse_earnings
from bot.data.marketaux import MarketauxNews


class _StubGetter:
    """Records what it was asked for, returns what the test supplied."""

    def __init__(self, payload: Any = None, *, raises: Exception | None = None) -> None:
        self.payload = payload
        self.raises = raises
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, params: dict[str, str]) -> Any:
        self.calls.append((url, params))
        if self.raises is not None:
            raise self.raises
        return self.payload


# ------------------------------------------------------------------ marketaux


def _article(
    title: str, symbols: list[str], published: str = "2026-08-09T12:00:00Z"
) -> dict[str, Any]:
    return {
        "title": title,
        "published_at": published,
        "entities": [{"symbol": s} for s in symbols],
    }


def test_headlines_carry_their_tickers():
    getter = _StubGetter({"data": [_article("Apple beats on services", ["AAPL"])]})
    feed = MarketauxNews(api_key="k", getter=getter)

    assert feed.recent_headlines(["AAPL"]) == [
        "[AAPL] Apple beats on services (2026-08-09T12:00)"
    ]


def test_crypto_pairs_are_not_sent_to_marketaux():
    """`BTC/USD` is not a ticker Marketaux knows, and it spoils the whole query."""
    getter = _StubGetter({"data": []})
    MarketauxNews(api_key="k", getter=getter).recent_headlines(["AAPL", "BTC/USD", "MSFT"])

    _, params = getter.calls[0]
    assert params["symbols"] == "AAPL,MSFT"


def test_a_marketaux_outage_costs_headlines_and_nothing_else():
    """It must return empty rather than raise: this runs inside the trading loop."""
    getter = _StubGetter(raises=RuntimeError("connection reset"))
    assert MarketauxNews(api_key="k", getter=getter).recent_headlines(["AAPL"]) == []


def test_a_failed_fetch_is_not_cached():
    """A transient failure should be retried next cycle, not held for half an hour."""
    getter = _StubGetter(raises=RuntimeError("boom"))
    feed = MarketauxNews(api_key="k", getter=getter)

    feed.recent_headlines(["AAPL"])
    feed.recent_headlines(["AAPL"])

    assert len(getter.calls) == 2


def test_headlines_are_cached_between_cycles():
    """The free tier allows 100 requests/day against 96 cycles, so this is required."""
    getter = _StubGetter({"data": [_article("Something happened", ["SPY"])]})
    feed = MarketauxNews(api_key="k", getter=getter)

    feed.recent_headlines(["SPY"])
    feed.recent_headlines(["SPY"])
    feed.recent_headlines(["SPY"])

    assert len(getter.calls) == 1


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"data": "not a list"}, {"data": [None, 7, {"no_title": True}]}],
)
def test_malformed_payloads_yield_no_headlines_rather_than_raising(payload):
    getter = _StubGetter(payload)
    assert MarketauxNews(api_key="k", getter=getter).recent_headlines(["SPY"]) == []


# -------------------------------------------------------------------- finnhub


def _earnings(symbol: str, day: str, hour: str) -> dict[str, Any]:
    return {"earningsCalendar": [{"symbol": symbol, "date": day, "hour": hour}]}


def test_before_market_open_lands_on_the_open():
    """15 July is EDT (UTC-4), so 09:30 ET is 13:30 UTC."""
    windows = parse_earnings(_earnings("AAPL", "2026-07-15", "bmo"), wanted={"AAPL"})

    assert len(windows) == 1
    assert windows[0].timestamp == datetime(2026, 7, 15, 13, 30, tzinfo=UTC)
    assert windows[0].affected_symbols == frozenset({"AAPL"})


def test_after_market_close_lands_on_the_close():
    windows = parse_earnings(_earnings("MSFT", "2026-07-15", "amc"), wanted={"MSFT"})

    assert len(windows) == 1
    assert windows[0].timestamp == datetime(2026, 7, 15, 20, 0, tzinfo=UTC)


def test_daylight_saving_is_respected():
    """The same wall-clock announcement is an hour later in UTC during winter.

    Hard-coding a UTC offset would put every January window 60 minutes wrong,
    which is exactly wide enough to slip past a 15-minute blackout.
    """
    summer = parse_earnings(_earnings("KO", "2026-07-15", "bmo"), wanted={"KO"})[0]
    winter = parse_earnings(_earnings("KO", "2026-01-15", "bmo"), wanted={"KO"})[0]

    assert summer.timestamp.hour == 13  # EDT, UTC-4
    assert winter.timestamp.hour == 14  # EST, UTC-5


def test_unknown_timing_covers_the_whole_session():
    """`dmh` means "sometime during the day" — so the whole day is blacked out.

    Guessing a midpoint would invent precision; skipping it would fail open on
    a risk control. Over-blocking one symbol for one session is the cheap error.
    """
    windows = parse_earnings(_earnings("JNJ", "2026-07-15", "dmh"), wanted={"JNJ"})

    assert len(windows) == 14  # 09:30 to 16:00 ET at half-hour spacing
    assert windows[0].timestamp == datetime(2026, 7, 15, 13, 30, tzinfo=UTC)
    assert windows[-1].timestamp == datetime(2026, 7, 15, 20, 0, tzinfo=UTC)

    # Half-hour spacing against a +/-15 minute gate leaves no uncovered gap.
    gaps = [
        (b.timestamp - a.timestamp).total_seconds() / 60 for a, b in pairwise(windows)
    ]
    assert all(g <= 30 for g in gaps)


def test_a_missing_hour_field_is_treated_as_unknown_not_skipped():
    assert len(parse_earnings(_earnings("SPY", "2026-07-15", ""), wanted={"SPY"})) == 14


def test_symbols_we_do_not_trade_are_ignored():
    payload = {
        "earningsCalendar": [
            {"symbol": "AAPL", "date": "2026-07-15", "hour": "bmo"},
            {"symbol": "TSLA", "date": "2026-07-15", "hour": "bmo"},
        ]
    }
    windows = parse_earnings(payload, wanted={"AAPL"})

    assert [w.affected_symbols for w in windows] == [frozenset({"AAPL"})]


def test_calendar_reports_itself_degraded_when_the_fetch_fails():
    """The heartbeat reads this. Zero windows and "could not ask" must differ.

    Without it, a Finnhub outage silently disarms the news blackout and the
    logs look identical to a quiet week with no announcements.
    """
    getter = _StubGetter(raises=RuntimeError("502 Bad Gateway"))
    calendar = FinnhubCalendar(api_key="k", symbols=["AAPL"], getter=getter)

    assert calendar.upcoming_windows(lookahead_minutes=60) == []
    assert calendar.is_degraded is True


def test_a_healthy_calendar_is_not_degraded():
    getter = _StubGetter({"earningsCalendar": []})
    calendar = FinnhubCalendar(api_key="k", symbols=["AAPL"], getter=getter)

    calendar.upcoming_windows(lookahead_minutes=60)

    assert calendar.is_degraded is False


def test_windows_outside_the_lookahead_are_filtered_out():
    """Both directions: yesterday's announcement and next week's are both excluded."""
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    next_week = (datetime.now(UTC) + timedelta(days=6)).date().isoformat()
    payload = {
        "earningsCalendar": [
            {"symbol": "AAPL", "date": yesterday, "hour": "bmo"},
            {"symbol": "AAPL", "date": next_week, "hour": "bmo"},
        ]
    }
    calendar = FinnhubCalendar(api_key="k", symbols=["AAPL"], getter=_StubGetter(payload))

    assert calendar.upcoming_windows(lookahead_minutes=60) == []


def test_the_calendar_is_cached():
    getter = _StubGetter({"earningsCalendar": []})
    calendar = FinnhubCalendar(api_key="k", symbols=["AAPL"], getter=getter)

    calendar.upcoming_windows(lookahead_minutes=60)
    calendar.upcoming_windows(lookahead_minutes=60)

    assert len(getter.calls) == 1


def test_a_failed_calendar_fetch_is_retried_not_cached():
    getter = _StubGetter(raises=RuntimeError("timeout"))
    calendar = FinnhubCalendar(api_key="k", symbols=["AAPL"], getter=getter)

    calendar.upcoming_windows(lookahead_minutes=60)
    calendar.upcoming_windows(lookahead_minutes=60)

    assert len(getter.calls) == 2
