"""Is any open position trading through the stop it was sized against?

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
    """One open trade whose stop level has been passed.

    `distance_usd` is how far beyond the stop price has gone, per unit, so a
    reader can tell a one-cent brush from a genuine gap without doing the
    arithmetic themselves.
    """

    trade: Trade
    price: float
    distance_usd: float

    @property
    def loss_if_closed_now_usd(self) -> float:
        """What closing at this price costs against entry.

        Beyond the planned risk by exactly `distance_usd * qty`, which is the
        figure the stop existed to prevent.
        """
        if self.trade.direction == Direction.BUY:
            return (self.trade.entry_price - self.price) * self.trade.qty
        return (self.price - self.trade.entry_price) * self.trade.qty

    def describe(self) -> str:
        side = "below" if self.trade.direction == Direction.BUY else "above"
        return (
            f"{self.trade.symbol} {self.trade.direction.value} is through its stop: "
            f"{self.price:,.2f} is {self.distance_usd:,.2f} {side} the planned "
            f"{self.trade.planned_stop:,.2f}. Closing here costs "
            f"{self.loss_if_closed_now_usd:,.2f} against entry."
        )


def check(open_trades: list[Trade], ticks: dict[str, Tick]) -> list[Breach]:
    """Which open trades are trading through their stop, given these quotes.

    Pure, so the loop can call it and a test can drive it without a broker.

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
        # Long stops sit below entry, short stops above. Compared against the
        # MID rather than the bid or the ask: a wide out-of-hours spread would
        # otherwise trip a long on the bid and a short on the ask, reporting a
        # breach that the traded price never reached.
        if trade.direction == Direction.BUY and price < trade.planned_stop:
            breaches.append(Breach(trade, price, trade.planned_stop - price))
        elif trade.direction == Direction.SELL and price > trade.planned_stop:
            breaches.append(Breach(trade, price, price - trade.planned_stop))
    return breaches


def unchecked(open_trades: list[Trade], ticks: dict[str, Tick]) -> list[str]:
    """Open trades whose stop could not be checked, because there is no quote.

    Reported separately and never folded into "no breaches". Same rule as
    `calendar_degraded` and `symbols_without_history`: say the answer is
    unknown rather than let its absence imply the safe one.
    """
    return sorted(t.symbol for t in open_trades if t.symbol not in ticks)
