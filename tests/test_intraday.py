"""Intraday figures, and the one distinction they exist to make.

A close through a level and a wick through it that closed back inside are the
same row on a daily bar, and telling them apart is the whole of `trend_break`.
Every assertion here is about that difference, or about refusing to guess when
the bars cannot support an answer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.broker import MockBroker
from bot.context import build_market_context, fetch_intraday
from bot.intraday import IntradayView, compute, render, summarise
from bot.models import AccountSnapshot, Bar

START = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)


def bar(
    minute: int,
    close: float,
    *,
    high: float | None = None,
    low: float | None = None,
    volume: float = 1_000.0,
    day: int = 4,
) -> Bar:
    return Bar(
        symbol="SPY",
        timestamp=datetime(2026, 5, day, 14, 0, tzinfo=UTC) + timedelta(minutes=minute),
        open=close,
        high=high if high is not None else close,
        low=low if low is not None else close,
        close=close,
        volume=volume,
    )


def prior_session(high: float = 100.0, low: float = 98.0) -> list[Bar]:
    """A quiet earlier day, establishing the levels the next day tests."""
    return [
        bar(i * 5, 99.0, high=high, low=low, day=3) for i in range(12)
    ]


def view_for(bars: list[Bar]) -> IntradayView:
    """`compute` returns None only when handed no bars, which these tests never do."""
    view = compute("SPY", bars, 5)
    assert view is not None
    return view


@pytest.fixture
def account() -> AccountSnapshot:
    return AccountSnapshot(
        equity_usd=100_000.0, cash_usd=100_000.0, buying_power_usd=100_000.0
    )


# --------------------------------------------------- the distinction itself


def test_a_wick_through_a_level_is_not_a_break():
    """The failure mode the whole module exists for.

    Price traded above the prior session high and closed back below it on every
    bar. On a daily bar this is indistinguishable from a break.
    """
    bars = prior_session() + [
        bar(i * 5, 99.5, high=100.6, low=99.0) for i in range(12)
    ]

    view = view_for(bars)
    high_test = next(t for t in view.tests if t.name == "prior session high")

    assert high_test.closes_beyond == 0
    assert high_test.wicks_only == 12
    assert not high_test.is_clean_break
    assert not high_test.last_close_beyond


def test_a_close_through_a_level_is_a_break():
    bars = prior_session() + [
        bar(i * 5, 99.5, high=99.8) for i in range(8)
    ] + [
        bar((8 + i) * 5, 100.8, high=101.0) for i in range(4)
    ]

    view = view_for(bars)
    high_test = next(t for t in view.tests if t.name == "prior session high")

    assert high_test.closes_beyond == 4
    assert high_test.wicks_only == 0
    assert high_test.last_close_beyond
    assert high_test.is_clean_break
    assert not high_test.reclaimed


def test_a_break_that_closed_back_inside_is_reported_as_reclaimed():
    """The most common way to lose money on this strategy, computed not inferred."""
    bars = prior_session() + [
        bar(i * 5, 100.9, high=101.2) for i in range(4)
    ] + [
        bar((4 + i) * 5, 99.2, high=99.9) for i in range(8)
    ]

    view = view_for(bars)
    high_test = next(t for t in view.tests if t.name == "prior session high")

    assert high_test.closes_beyond == 4
    assert high_test.reclaimed
    assert not high_test.last_close_beyond
    # Broke and failed is not a weaker version of broke. It is a reason to stand
    # aside, and `is_clean_break` must not soften into "it did break once".
    assert not high_test.is_clean_break
    assert "RECLAIMED" in high_test.render()


def test_a_downside_break_is_measured_against_the_prior_low():
    bars = prior_session() + [bar(i * 5, 97.4, low=97.0) for i in range(6)]

    view = view_for(bars)
    low_test = next(t for t in view.tests if t.name == "prior session low")

    assert low_test.closes_beyond == 6
    assert low_test.is_clean_break


def test_break_volume_is_a_ratio_against_this_feed_not_an_absolute():
    """Alpaca's free feed is an IEX sample, so only a like-for-like ratio is fair."""
    bars = prior_session() + [
        bar(i * 5, 99.5, volume=1_000.0) for i in range(8)
    ] + [
        bar((8 + i) * 5, 100.9, volume=3_000.0) for i in range(4)
    ]

    view = view_for(bars)
    high_test = next(t for t in view.tests if t.name == "prior session high")

    assert high_test.volume_ratio is not None
    assert high_test.volume_ratio > 1.5
    assert "x the recent average" in high_test.render()


# --------------------------------------------------------- refusing to guess


def test_no_prior_session_means_no_level_and_it_says_so():
    """A symbol with one session of history has nothing to break."""
    view = view_for([bar(i * 5, 99.0) for i in range(12)])

    assert view.prior_session_high is None
    assert view.tests == []
    assert not view.has_levels
    assert "NO PRIOR SESSION" in "\n".join(render(view))


def test_no_bars_at_all_returns_none_rather_than_an_empty_view():
    """Same contract as `indicators.compute`: absent is not zero."""
    assert compute("SPY", [], 5) is None


def test_the_mock_broker_refuses_to_invent_intraday_bars():
    with pytest.raises(KeyError):
        MockBroker().get_intraday_bars("SPY")


def test_fetch_names_the_symbols_with_no_intraday_history():
    broker = MockBroker()
    broker.set_intraday_bars("SPY", [*prior_session(), bar(0, 99.5)])
    # QQQ deliberately unseeded.

    views, missing = fetch_intraday(broker, ["SPY", "QQQ"])

    assert set(views) == {"SPY"}
    assert missing == ["QQQ"]


class ExplodingBroker(MockBroker):
    def get_intraday_bars(
        self, symbol: str, minutes: int = 5, lookback: int = 160
    ) -> list[Bar]:
        raise Exception("APIError: rate limit exceeded")


def test_an_intraday_outage_degrades_the_cycle_rather_than_ending_the_loop():
    """Same rule as every other feed. A bad minute must not stop the bot."""
    views, missing = fetch_intraday(ExplodingBroker(), ["SPY", "QQQ"])

    assert views == {}
    assert missing == ["SPY", "QQQ"]


# ------------------------------------------------------------- into the prompt


def test_a_symbol_without_intraday_bars_is_named_in_the_prompt(account):
    """Silently dropping it would leave the model unable to know it cannot tell."""
    context = build_market_context(
        account=account,
        ticks={},
        headlines=[],
        news_windows=[],
        intraday={},
        symbols_without_intraday=["QQQ", "SPY"],
    )

    assert "NO INTRADAY BARS AVAILABLE for: QQQ, SPY" in context
    assert "cannot tell a close through a level from a wick" in context


def test_the_prompt_carries_counts_rather_than_bars(account):
    bars = prior_session() + [bar(i * 5, 100.9) for i in range(4)]
    view = view_for(bars)

    context = build_market_context(
        account=account,
        ticks={},
        headlines=[],
        news_windows=[],
        intraday={"SPY": view},
    )

    assert "## Intraday (computed from 5-minute bars, not estimated)" in context
    assert "CLOSED beyond" in context
    assert "wicked through without closing beyond" in context


def test_the_context_still_renders_with_no_intraday_at_all(account):
    context = build_market_context(
        account=account, ticks={}, headlines=[], news_windows=[]
    )

    assert "## Intraday (computed from 5-minute bars, not estimated)" in context


def test_the_audit_summary_records_whether_a_break_was_clean():
    bars = prior_session() + [bar(i * 5, 100.9) for i in range(4)]

    line = summarise(view_for(bars))

    assert "prior session high clean break" in line
