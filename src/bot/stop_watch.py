"""Is any open position trading through the stop that is PROTECTING it?

## Which of the two stop figures this module reads, and why it is that one

A `Trade` carries two stop levels on purpose and they answer different
questions:

- **`planned_stop`** is the SIZING figure. The quantity was computed from
  `|entry - planned_stop|`, so it is what one R means and what `r_multiple` and
  `metrics.py` grade the result against. It never moves, because redefining it
  after the fact would make a trade that lost half what it risked read as a full
  stop-out.
- **`effective_stop`** is the level actually in force — the tightened one where
  `position_actions.tighten_stop` has moved it, the planned one everywhere else.
  It is what would fill, so it is what the risk caps count and what the model is
  shown.

**This module wants the protective one**, and the distinction is not academic.
Short 21 SPY from 773.32 with the stop pulled in from 820 to 800, price at 810:
against `planned_stop` there is no breach, against `effective_stop` the position
is 10.00 a share through the level that is meant to be holding it. The question
here is "is anything about to be — or already — losing more than the stop in
force allows", not "what was this sized against", so `effective_stop` is the
only figure that answers it. Reverting this to `planned_stop` silently
under-reports every position whose stop has ever been tightened.

## Why this exists when the bracket already rests at the broker

`AlpacaBroker.place_order` submits a GTC bracket, so the stop is a real order
sitting at Alpaca rather than a number in SQLite. That covers the regular
session completely, and it is the primary protection.

It does not cover everything, and the gaps are structural rather than
oversights:

- **A stop cannot fire outside the regular session.** A stop is a trigger that
  becomes a MARKET order, and extended-hours venues accept limit orders only.
  So between 16:00 and 09:30 New York the leg rests, eligible again only when
  the session reopens. No broker offers anything different.
- **Crypto brackets are not supported by Alpaca at all**, so a crypto position
  has no broker-side stop by construction.
- **A position adopted from the broker**, or one whose bracket was cancelled by
  hand, has a journalled stop and no order behind it.

This module answers the one question those leave open: is price already through
the level, with nothing at the broker about to act on it?

## It reports. It does not close.

Nothing here submits an order, and that is deliberate rather than unfinished.
Closing out of hours needs a marketable limit order, which is a new execution
path, and an execution path that fires unattended at 3am is a different
proposition from one an operator watches. The honest intermediate is to make
the breach loud — in the loop's log, in the audit trail and on the Board — and
let the decision to automate it be taken on its own.

**A monitor believed in but not running is worse than no monitor**, which is
the other reason for keeping this narrow: it runs on the decision loop's
fifteen-minute pulse, so it sees what that cycle sees and nothing more. It is a
backstop with a stated resolution, not a guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Direction, Tick, Trade

__all__ = ["Breach", "check"]


@dataclass(frozen=True)
class Breach:
    """One open trade that is trading through the stop in force on it.

    `distance_usd` is how far beyond the stop price has gone, per unit, so a
    reader can tell a one-cent brush from a genuine gap without doing the
    arithmetic themselves. It is measured against `trade.effective_stop` — see
    the module docstring for why that rather than `planned_stop`.
    """

    trade: Trade
    price: float
    distance_usd: float

    @property
    def stop(self) -> float:
        """The level this breach was measured against. The one in force.

        Named on the breach rather than left for a caller to re-derive, because
        a caller reaching for `planned_stop` to print beside `distance_usd`
        would produce two figures that do not add up on a position whose stop
        has been moved.
        """
        return self.trade.effective_stop

    @property
    def loss_if_closed_now_usd(self) -> float:
        """What closing at this price costs against entry.

        Beyond the risk currently in force by exactly `distance_usd * qty`,
        which is the figure the stop existed to prevent. Compare it against
        `Trade.current_risk_usd` rather than `planned_risk_usd`: the second is
        what the position was built to lose and says nothing about what the
        stop protecting it now allows.
        """
        if self.trade.direction == Direction.BUY:
            return (self.trade.entry_price - self.price) * self.trade.qty
        return (self.price - self.trade.entry_price) * self.trade.qty

    def describe(self) -> str:
        side = "below" if self.trade.direction == Direction.BUY else "above"
        # "in force" rather than "planned", and the two are named apart when
        # they differ. A breach reported against 800 on a trade whose journal
        # row says 820 reads as an error unless the sentence says the stop was
        # moved.
        moved = (
            f" (moved in from the planned {self.trade.planned_stop:,.2f})"
            if self.trade.stop_has_moved
            else ""
        )
        return (
            f"{self.trade.symbol} {self.trade.direction.value} is through its stop: "
            f"{self.price:,.2f} is {self.distance_usd:,.2f} {side} the stop in "
            f"force at {self.stop:,.2f}{moved}. Closing here costs "
            f"{self.loss_if_closed_now_usd:,.2f} against entry."
        )


def check(open_trades: list[Trade], ticks: dict[str, Tick]) -> list[Breach]:
    """Which open trades are through the stop in force on them, at these quotes.

    Pure, so the loop can call it and a test can drive it without a broker.

    **Measured against `Trade.effective_stop`, never `planned_stop`.** The
    module docstring has the worked example; the short version is that a stop
    tightened from 820 to 800 is protecting at 800, and checking the level it
    was moved away from would report a position 10 a share through its
    protection as fine.

    **A symbol with no quote is skipped rather than assumed safe**, and that is
    the distinction worth keeping: `fetch_market_ticks` drops a symbol whose
    fetch failed, so an absent tick means "not checked", not "fine". Treating a
    missing price as unbreached would let a feed outage read as an all-clear on
    the exact cycle somebody needed to know. The loop reports what it could not
    check alongside what it did.
    """
    breaches: list[Breach] = []
    for trade in open_trades:
        tick = ticks.get(trade.symbol)
        if tick is None:
            continue
        price = tick.mid
        # The stop PROTECTING the position, which is the tightened level where
        # one has been recorded. `planned_stop` is the sizing figure and is a
        # different question; see the module docstring.
        stop = trade.effective_stop
        # Long stops sit below entry, short stops above. Compared against the
        # MID rather than the bid or the ask: a wide out-of-hours spread would
        # otherwise trip a long on the bid and a short on the ask, reporting a
        # breach that the traded price never reached.
        if trade.direction == Direction.BUY and price < stop:
            breaches.append(Breach(trade, price, stop - price))
        elif trade.direction == Direction.SELL and price > stop:
            breaches.append(Breach(trade, price, price - stop))
    return breaches


def unchecked(open_trades: list[Trade], ticks: dict[str, Tick]) -> list[str]:
    """Open trades whose stop could not be checked, because there is no quote.

    Reported separately and never folded into "no breaches". Same rule as
    `calendar_degraded` and `symbols_without_history`: say the answer is
    unknown rather than let its absence imply the safe one.
    """
    return sorted(t.symbol for t in open_trades if t.symbol not in ticks)
