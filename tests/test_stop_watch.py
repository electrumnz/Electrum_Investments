"""The out-of-hours stop backstop.

The bracket at the broker is the primary protection and covers the regular
session. This covers what it structurally cannot: a stop leg cannot fire
outside regular hours, Alpaca does not accept brackets on crypto, and a
position adopted from the broker has a journalled stop with no order behind it.

It watches the stop that is PROTECTING the position — `effective_stop` — rather
than the one it was sized against. The last two tests here are what pin that,
and they are written so that reverting either comparison turns them red.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

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
    assert "820.00" in text          # the level in force
    assert "825.00" in text          # where it actually is
    assert "above" in text           # which side, for a short


# ------------------------------------------------- which stop is being watched


def test_a_tightened_stop_is_the_one_watched_on_a_short():
    """The live position's own shape: short 21 SPY at 773.32, stop pulled in
    from 820 to 800, price at 810.

    `planned_stop` says no breach and `effective_stop` says the position is
    10.00 a share through the level that is meant to be holding it. Only the
    second answers the question this module exists to ask, which is whether the
    stop PROTECTING the position has already been passed — `planned_stop` is
    the sizing figure and `r_multiple` keeps it for that.

    Reverting `check` to `planned_stop` makes this fail.
    """
    trade = _trade(Direction.SELL, 773.32, 820.0, qty=21)
    trade.current_stop = 800.0

    # The premise: the two figures genuinely disagree at this price.
    assert trade.planned_stop == 820.0
    assert trade.effective_stop == 800.0
    assert 800.0 < 810.0 < 820.0

    breaches = stop_watch.check([trade], {"SPY": _tick(810.0)})

    assert len(breaches) == 1, "the stop in force at 800 has been passed"
    assert breaches[0].stop == 800.0
    assert breaches[0].distance_usd == 10.0
    assert breaches[0].loss_if_closed_now_usd == pytest.approx(21 * (810.0 - 773.32))

    # And it says which level it means, so a reader looking at a journal row
    # that still reads 820 is not left thinking the figure is wrong.
    text = breaches[0].describe()
    assert "800.00" in text
    assert "in force" in text
    assert "820.00" in text and "moved in from" in text


def test_a_tightened_stop_is_the_one_watched_on_a_long():
    """The same inversion `classify_stop_move` turns on: a long tightens UPWARD.

    Sized against 570 and pulled in to 578, price at 575 is through the stop in
    force while sitting comfortably above the one it was sized against.
    """
    trade = _trade(Direction.BUY, 580.0, 570.0, qty=10)
    trade.current_stop = 578.0

    assert stop_watch.check([trade], {"SPY": _tick(575.0)}), (
        "575 is below the 578 stop in force and is a breach"
    )
    # Same trade, stop never moved: 575 is inside 570 and is not a breach. This
    # is the control, so the test above cannot pass by breaching everything.
    assert stop_watch.check([_trade(Direction.BUY, 580.0, 570.0)], {"SPY": _tick(575.0)}) == []


def test_an_unmoved_stop_still_reads_as_the_planned_one():
    """`effective_stop` falls back to `planned_stop`, which is every trade this
    bot has placed so far. The switch above must not change any of them."""
    trade = _trade(Direction.SELL, 790.0, 820.0, qty=33)

    assert trade.current_stop is None
    breach = stop_watch.check([trade], {"SPY": _tick(825.0)})[0]

    assert breach.stop == 820.0
    assert breach.distance_usd == 5.0
    assert "moved in from" not in breach.describe()
