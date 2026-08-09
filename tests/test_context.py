"""The market context, and the bars and indicators that now feed it.

The load-bearing assertions here are the negative ones: a symbol whose history
could not be fetched must be NAMED in the prompt, not quietly dropped. A symbol
that disappears from the indicators block is indistinguishable from one nobody
asked about, and the model would then reason about its live quote with no
history and nothing to say so.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.broker import MockBroker
from bot.context import build_market_context, fetch_indicators, fetch_market_ticks
from bot.models import AccountSnapshot, Bar

START = datetime(2026, 1, 5, tzinfo=UTC)


def daily_bars(symbol: str, count: int, *, start_price: float = 100.0) -> list[Bar]:
    """A gently rising series, oldest first, as every Alpaca bars endpoint returns."""
    return [
        Bar(
            symbol=symbol,
            timestamp=START + timedelta(days=i),
            open=start_price + i * 0.1,
            high=start_price + i * 0.1 + 1.0,
            low=start_price + i * 0.1 - 1.0,
            close=start_price + i * 0.1,
            volume=1_000_000.0,
        )
        for i in range(count)
    ]


@pytest.fixture
def account() -> AccountSnapshot:
    return AccountSnapshot(
        equity_usd=100_000.0, cash_usd=100_000.0, buying_power_usd=100_000.0
    )


# ------------------------------------------------------------------- the mock


def test_mock_broker_refuses_to_invent_bars_for_an_unseeded_symbol():
    """An empty list would be indistinguishable from "this symbol has no history"."""
    with pytest.raises(KeyError):
        MockBroker().get_daily_bars("SPY")


def test_mock_broker_returns_the_most_recent_bars_within_the_lookback():
    broker = MockBroker()
    broker.set_bars("SPY", daily_bars("SPY", 300))

    bars = broker.get_daily_bars("SPY", 50)

    assert len(bars) == 50
    assert bars[-1].timestamp == START + timedelta(days=299)


# ------------------------------------------------------------------- fetching


def test_fetch_indicators_names_the_symbols_it_could_not_price():
    broker = MockBroker()
    broker.set_bars("SPY", daily_bars("SPY", 250))
    # QQQ is deliberately left unseeded.

    indicators, missing = fetch_indicators(broker, ["SPY", "QQQ"])

    assert set(indicators) == {"SPY"}
    assert missing == ["QQQ"]


def test_a_symbol_with_no_history_is_called_out_in_the_prompt(account):
    """The whole point of the second return value from fetch_indicators."""
    broker = MockBroker()
    broker.set_bars("SPY", daily_bars("SPY", 250))
    broker.set_price("QQQ", 500.0, 500.04)

    indicators, missing = fetch_indicators(broker, ["SPY", "QQQ"])
    context = build_market_context(
        account=account,
        ticks=fetch_market_ticks(broker, ["QQQ"]),
        headlines=[],
        news_windows=[],
        indicators=indicators,
        symbols_without_history=missing,
    )

    assert "NO PRICE HISTORY AVAILABLE for: QQQ" in context
    assert "propose nothing on them" in context


# -------------------------------------------------------------------- context


def test_context_carries_computed_indicators_rather_than_raw_bars(account):
    """The model is handed answers. Bars would invite it to do the arithmetic."""
    broker = MockBroker()
    broker.set_bars("SPY", daily_bars("SPY", 250))
    indicators, _ = fetch_indicators(broker, ["SPY"])

    context = build_market_context(
        account=account,
        ticks={},
        headlines=[],
        news_windows=[],
        indicators=indicators,
    )

    assert "## Indicators (computed from daily bars, not estimated)" in context
    assert "20-day average:" in context
    assert "200-day average:" in context
    assert "ATR(14):" in context
    assert "distance from the 20-day average:" in context
    # Nothing that looks like a bar series, which is what would prompt the model
    # to start averaging things itself.
    assert "open" not in context.lower().split("## indicators")[1].split("##")[0]


def test_context_still_renders_with_no_indicators_at_all(account):
    """The loop must survive a total bars outage, saying so rather than crashing."""
    context = build_market_context(
        account=account, ticks={}, headlines=[], news_windows=[]
    )

    assert "## Indicators (computed from daily bars, not estimated)" in context
    assert "- (none)" in context


def test_short_history_is_rendered_as_unavailable_not_omitted(account):
    broker = MockBroker()
    broker.set_bars("SPY", daily_bars("SPY", 30))
    indicators, missing = fetch_indicators(broker, ["SPY"])

    context = build_market_context(
        account=account,
        ticks={},
        headlines=[],
        news_windows=[],
        indicators=indicators,
        symbols_without_history=missing,
    )

    assert missing == []
    assert "200-day average: unavailable" in context
    assert "NOT AVAILABLE for this symbol" in context
