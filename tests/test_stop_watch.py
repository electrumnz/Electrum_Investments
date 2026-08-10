"""The out-of-hours stop backstop.

The bracket at the broker is the primary protection and covers the regular
session. This covers what it structurally cannot: a stop leg cannot fire
outside regular hours, Alpaca does not accept brackets on crypto, and a
position adopted from the broker has a journalled stop with no order behind it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from bot import stop_watch
from bot.models import Direction, Tick, Trade

NOW = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)


def _trade(direction: Direction, entry: float, stop: float, qty: float = 10) -> Trade:
    return Trade(
        symbol="SPY",
        direction=direction,
        qty=qty,
        entry_time=NOW,
        entry_price=entry,
        planned_stop=stop,
        planned_target=entry + (entry - stop) * 2,
    )


def _tick(mid: float) -> Tick:
    return Tick(symbol="SPY", bid=mid - 0.01, ask=mid + 0.01, timestamp=NOW)


def test_a_long_through_its_stop_is_reported():
    breaches = stop_watch.check([_trade(Direction.BUY, 580.0, 570.0)], {"SPY": _tick(568.0)})

    assert len(breaches) == 1
    assert breaches[0].distance_usd == 2.0
    # Closing here costs more than the trade was sized to lose, which is the
    # figure the stop existed to prevent.
    assert breaches[0].loss_if_closed_now_usd == 120.0


def test_a_short_through_its_stop_is_reported():
    """A short's stop sits ABOVE entry, so the comparison inverts. Getting this
    backwards would report every healthy short as breached and every breached
    one as fine."""
    breaches = stop_watch.check(
        [_trade(Direction.SELL, 790.0, 820.0, qty=33)], {"SPY": _tick(825.0)}
    )

    assert len(breaches) == 1
    assert breaches[0].distance_usd == 5.0
    assert breaches[0].loss_if_closed_now_usd == 33 * 35.0


def test_a_position_inside_its_stop_is_not_reported():
    assert stop_watch.check([_trade(Direction.BUY, 580.0, 570.0)], {"SPY": _tick(575.0)}) == []
    assert (
        stop_watch.check([_trade(Direction.SELL, 790.0, 820.0)], {"SPY": _tick(800.0)}) == []
    )


def test_the_boundary_is_not_a_breach():
    """Price AT the stop has not gone through it. Reporting the touch would cry
    wolf on every position that grazes its level intraday."""
    assert stop_watch.check([_trade(Direction.BUY, 580.0, 570.0)], {"SPY": _tick(570.0)}) == []
    assert (
        stop_watch.check([_trade(Direction.SELL, 790.0, 820.0)], {"SPY": _tick(820.0)}) == []
    )


def test_a_missing_quote_is_skipped_rather_than_assumed_safe():
    """`fetch_market_ticks` drops a symbol whose fetch failed, so an absent tick
    means "not checked", never "fine".

    Treating a missing price as unbreached would let a feed outage read as an
    all-clear on the exact cycle somebody needed to know otherwise. Same rule as
    `calendar_degraded`: report the weaker fact rather than imply the stronger.
    """
    trades = [_trade(Direction.BUY, 580.0, 570.0)]

    assert stop_watch.check(trades, {}) == []
    # And it says so, separately, rather than being silently absent.
    assert stop_watch.unchecked(trades, {}) == ["SPY"]
    assert stop_watch.unchecked(trades, {"SPY": _tick(575.0)}) == []


def test_the_mid_is_used_rather_than_the_touch():
    """A wide out-of-hours spread would otherwise trip a long on the bid and a
    short on the ask, reporting a breach the traded price never reached."""
    wide = Tick(symbol="SPY", bid=565.0, ask=575.0, timestamp=NOW)  # mid 570

    # Bid is through 570 but the mid is not.
    assert stop_watch.check([_trade(Direction.BUY, 580.0, 569.0)], {"SPY": wide}) == []


def test_the_description_names_the_figures_a_reader_needs():
    breach = stop_watch.check(
        [_trade(Direction.SELL, 790.0, 820.0, qty=33)], {"SPY": _tick(825.0)}
    )[0]
    text = breach.describe()

    assert "through its stop" in text
    assert "820.00" in text          # the level that was planned
    assert "825.00" in text          # where it actually is
    assert "above" in text           # which side, for a short
