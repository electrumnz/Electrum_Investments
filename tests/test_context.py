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
from bot.models import AccountSnapshot, Bar, Tick

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


class ExplodingBroker(MockBroker):
    """A broker that fails the way the Alpaca SDK actually fails.

    `APIError` and `httpx.ReadTimeout` are neither `KeyError` nor
    `RuntimeError`, which is the whole point: the narrow catch these two
    functions started with would have let them through.
    """

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    def get_daily_bars(self, symbol: str, lookback: int = 260) -> list[Bar]:
        raise self._error

    def get_tick(self, symbol: str) -> Tick:
        raise self._error


@pytest.mark.parametrize(
    "error",
    [
        Exception("APIError: rate limit exceeded"),
        TimeoutError("read timed out"),
        ValueError("Expecting value: line 1 column 1 (char 0)"),
    ],
    ids=["api_error", "timeout", "bad_json"],
)
def test_a_broker_failure_degrades_the_cycle_rather_than_ending_the_loop(error, account):
    """A trading loop that dies quietly is worse than one that trades badly.

    When the bars endpoint has a bad minute the journal stops being reconciled
    and open positions stop being watched, with nothing on screen to say so.
    So a failed fetch has to come back as "no history for this symbol", which
    is already the honest description of what the model has.
    """
    broker = ExplodingBroker(error)

    indicators, missing = fetch_indicators(broker, ["SPY", "QQQ"])
    ticks = fetch_market_ticks(broker, ["SPY", "QQQ"])

    assert indicators == {}
    assert missing == ["SPY", "QQQ"]
    assert ticks == {}

    context = build_market_context(
        account=account,
        ticks=ticks,
        headlines=[],
        news_windows=[],
        indicators=indicators,
        symbols_without_history=missing,
    )
    assert "NO PRICE HISTORY AVAILABLE for: QQQ, SPY" in context


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


def test_watched_posts_lead_the_headlines_in_the_prompt(account):
    """A post moves the price before the wire carries it, so it is read first."""
    context = build_market_context(
        account=account,
        ticks={},
        headlines=["Reuters: steel makers rally"],
        news_windows=[],
        social_posts=["[@realDonaldTrump 14:31] Tariffs on steel imports"],
    )

    posts_at = context.index("Posts from watched accounts")
    headlines_at = context.index("## Recent headlines")

    assert posts_at < headlines_at
    assert "Tariffs on steel imports" in context
    assert "gates nothing" in context


def test_a_degraded_social_feed_says_so_in_the_prompt(account):
    """An empty list from a dead token must not read as a quiet morning."""
    context = build_market_context(
        account=account,
        ticks={},
        headlines=[],
        news_windows=[],
        social_posts=[],
        social_degraded=True,
    )

    assert "FEED DEGRADED" in context
    assert "does NOT mean nothing was posted" in context


# ---------------------------------------------------------------- the session


def test_the_session_block_precedes_the_quotes_it_qualifies(account):
    """Ordering is the point, not decoration. Every figure below the session
    block is a reading; the block says what an order built on those readings
    would actually become. A model that reads the snapshot first has anchored on
    a fill price it will not get."""
    from bot.config import load_rules

    context = build_market_context(
        account=account,
        ticks={},
        headlines=[],
        news_windows=[],
        instruments=load_rules().instruments,
        now=datetime(2026, 8, 10, 8, 45, tzinfo=UTC),   # 04:45 ET, pre-market
    )

    assert "## Session" in context
    assert context.index("## Session") < context.index("## Market snapshot")
    assert "PRE-MARKET" in context
    assert "RESTS" in context


def test_a_disabled_class_is_not_described_as_shut(account):
    """Crypto ships disabled. Listing it here would invite a proposal for a
    class the gate refuses on membership, and `build_system_prompt` already
    omits disabled classes for exactly that reason."""
    from bot.config import load_rules

    rules = load_rules()
    assert rules.instruments["crypto"].enabled is False

    context = build_market_context(
        account=account,
        ticks={},
        headlines=[],
        news_windows=[],
        instruments=rules.instruments,
        now=datetime(2026, 8, 10, 8, 45, tzinfo=UTC),
    )

    assert "crypto" not in context


def test_no_instruments_means_no_session_block_rather_than_a_guessed_one(account):
    """A caller that supplied no rules described no market. Computing a session
    from nothing would be a confident statement about hours nobody configured —
    the same reason `market_state` reports the gate window shut without them."""
    context = build_market_context(
        account=account, ticks={}, headlines=[], news_windows=[]
    )

    assert "## Session" not in context


# --------------------------------------------- the position the agent manages
#
# `claude_client` asks for a `position_plan` on every open position, with an
# action of hold, close or TIGHTEN_STOP. The context block used to render
# direction, quantity, entry, current price and P&L and no stop at all, so the
# model was asked whether to tighten a level it had never been shown, and
# whether a thesis still held without the figure that says what being wrong
# costs. These pin the fix.


def _short_spy() -> AccountSnapshot:
    """The live position's own shape: SHORT 21 SPY, stop 820, risk $980.19."""
    from bot.models import Direction, Position

    return AccountSnapshot(
        equity_usd=100_000.0,
        cash_usd=116_239.81,
        buying_power_usd=200_000.0,
        open_positions=[
            Position(
                symbol="SPY",
                direction=Direction.SELL,
                qty=21,
                entry_price=773.324285,
                opened_at=START,
                current_price=774.18,
                unrealised_pnl_usd=-18.39,
            )
        ],
    )


def test_a_position_shows_the_stop_the_agent_is_asked_to_manage():
    account = _short_spy()
    account.planned_stop_by_symbol = {"SPY": 820.0}
    account.open_risk_by_symbol = {"SPY": 980.19}

    text = build_market_context(account=account, ticks={}, headlines=[], news_windows=[])

    assert "stop 820.0000" in text
    assert "980.19" in text
    # And it is framed as the agent's own position, not a thing it comments on.
    assert "yours to manage" in text


def test_a_position_with_no_journal_row_says_UNKNOWN_rather_than_nothing():
    """The failure this guards is a blank that reads like "no stop needed".

    A held position whose journal row is missing has real exposure and unknown
    protection. Those are different facts from "flat" and from "unprotected",
    and the prompt has to say which one it is. Same rule as
    `symbols_with_unknown_risk` refusing to be counted as zero.
    """
    account = _short_spy()  # no planned_stop_by_symbol, no open_risk_by_symbol

    text = build_market_context(account=account, ticks={}, headlines=[], news_windows=[])

    assert "STOP UNKNOWN" in text
    # Never a plausible wrong figure in place of the missing one.
    assert "stop 0" not in text
    assert "risking $0.00" not in text


def test_the_journalled_stop_is_not_presented_as_the_brokers():
    """planned_stop and the resting leg's trigger are two different facts.

    `WorkingOrder.stop_price` is the other half of the pair, and the whole
    reason both exist is that they can disagree. The prompt must not let the
    model assume they agree.
    """
    account = _short_spy()
    account.planned_stop_by_symbol = {"SPY": 820.0}

    text = build_market_context(account=account, ticks={}, headlines=[], news_windows=[])

    assert "the JOURNAL planned" in text
