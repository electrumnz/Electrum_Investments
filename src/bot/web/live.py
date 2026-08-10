"""Live account state for the dashboard: one poller, many browsers.

## The two problems this closes

Every page in `src/bot/web/` renders once and then freezes. Equity, unrealised
P&L and the open-position table are whatever they were when the operator last
pressed reload, and nothing on screen says how old that is. On a page whose
whole argument is that its figures are measured rather than plausible, a figure
with no age is the weaker half of that claim.

Worse, the read that produces those figures happens **during the render**.
`_account_orders_prices` opens a broker session, calls Alpaca several times and
returns; a slow Alpaca is therefore a page that does not arrive, with nothing on
screen while it does not arrive. The one surface whose job is making problems
visible is the surface that disappears when the broker has a bad minute.

Both are the same fix. A single background task owns the broker conversation and
holds the latest reading; pages render from whatever it has, immediately,
including nothing; and browsers are pushed each new reading over Server-Sent
Events.

## One poller, not one per browser

The task belongs to the application, not to a request. Polling per connected
browser would multiply broker load by however many tabs are open — and the
budget being spent is shared with the decision loop, which is the thing that
actually trades. Every subscriber reads the same state.

## The three ways there can be no fresh figure, and why they are not one thing

`LiveStatus` has four values because a cold start, a slow broker and a broker
error are different facts and only some of them need anybody to do anything:

- **starting** — nothing has come back yet. There are no figures. They are not
  zero; they are unknown, and the payload sends `null` rather than `0.00`.
- **slow** — a read is outstanding and has been for a while. Nothing has
  failed. The figures on screen are the previous ones and are dated.
- **failing** — the last read raised. The error is named, and the figures on
  screen are the last good ones, dated, and are not being updated.
- **live** — the last read succeeded.

Collapsing those into one spinner is the mistake `FinnhubCalendar.is_degraded`
exists to prevent, arriving through the interface: an outage that looks like a
quiet start is an outage nobody acts on. `is_stale` sits alongside for the case
where nothing failed and nothing is running either — the same reasoning as
`TailnetStatus.is_stale`, where a reading that quietly stopped must report that
it stopped rather than read as healthy.

## The age travels with the figure

Every payload carries `as_of`, `age_seconds` and `stale`, computed at send time
rather than at read time. A stale figure presented as current is the failure
this repository exists to prevent, and a page that has to work out for itself
how old its numbers are will eventually get it wrong.

## Open risk still comes from the journal

`AccountSnapshot.open_risk_usd` is what the 2% total-risk cap counts against and
the broker cannot report it — Alpaca holds stop-losses as separate orders, so it
does not know what a position was designed to lose. Every path that hands out a
snapshot has to fill it in from the journal, and this is now one of those paths.
`LivePoller` therefore takes its source as a **required** argument with no
default, because a default of zero is precisely the bug the argument exists to
prevent.

## A failed read degrades, it never ends the loop

Same rule as `fetch_market_ticks`: the catch is broad because the Alpaca SDK
raises `APIError`, `httpx` raises timeouts and JSON decoding raises
`ValueError`, none of which is a `KeyError`. A narrow catch would end the poller
on the first unanticipated exception, and a dashboard whose figures silently
stopped updating is worse than one saying the broker is refusing.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import structlog

# Module scope, matching `app.py`, and for the same reason: `from __future__
# import annotations` turns every annotation into a string that FastAPI
# resolves against MODULE globals. A `StreamingResponse` imported inside a
# function is invisible to it, and the route's return annotation stops being
# recognised as a Response — at which point FastAPI tries to build a response
# model out of it. Nothing warns.
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..broker import Broker
from ..config import Env
from ..journal import Journal
from ..models import AccountSnapshot, WorkingOrder

log = structlog.get_logger()


# Five seconds between reads of the broker.
#
# This is one operator watching one page, not a market-data feed. The fastest
# thing on the Board is unrealised P&L across a handful of positions, and a
# figure that lands inside five seconds reads as live to a person; below that
# the eye cannot tell the difference and the cost is real.
#
# The cost is Alpaca's request budget, which the **decision loop** also spends.
# One poll is one account call, one positions call, one orders call and one
# quote per symbol carrying a resting order — so twelve polls a minute is tens
# of requests against a per-minute allowance in the hundreds, with room left for
# the loop. Halving this interval buys nothing anybody can see and spends the
# allowance of the process that actually trades.
POLL_INTERVAL_SECONDS = 5.0

# How long a read must be outstanding before it is reported as slow rather than
# merely in progress. A healthy Alpaca round trip is well under a second, so
# three seconds is unambiguous, and it lands before the next poll would have
# started rather than after.
SLOW_AFTER_SECONDS = 3.0

# The age at which a reading stops being presentable as current. Four poll
# intervals: long enough that one slow read is not an alarm, short enough that
# nobody reads a minute-old equity figure as this minute's.
STALE_AFTER_SECONDS = 20.0

# The stream re-sends the current state this often even when nothing new has
# arrived. Two jobs, and the second is why it re-sends the PAYLOAD rather than
# an SSE comment: a read that never returns produces no new reading, so without
# this the page would sit on a figure quietly getting older with nothing to say
# so. The heartbeat is how a browser learns a read has been outstanding for a
# while. It also keeps a proxy from closing the connection as idle.
HEARTBEAT_SECONDS = 15.0

# How long the poller keeps going after the last browser disconnects. Long
# enough that a page reload finds it warm — a cold poller means the next render
# has no figures at all — and short enough that a closed laptop stops talking to
# the broker. Nobody watching is the normal overnight state. `None` disables the
# idle stop for a deployment that would rather keep it warm permanently.
IDLE_STOP_AFTER_SECONDS = 120.0

# What the browser is told to wait before reconnecting a dropped stream, in
# milliseconds. `EventSource` reconnects by itself; this only sets the pace, so
# a restart of the web process is not answered by a reconnect storm.
RECONNECT_DELAY_MS = 3000

# Where `install` mounts the stream. Deliberately NOT in `app.py`'s `OPEN_PATHS`
# allowlist: this serves equity, positions and resting orders, so it belongs
# behind the same password as the pages that render them.
DEFAULT_PATH = "/live"


def _now() -> datetime:
    return datetime.now(UTC)


def _ago(seconds: float | None) -> str:
    """Age as a person would say it. `None` means it has never happened."""
    if seconds is None:
        return "never"
    if seconds < 1:
        return "a moment ago"
    if seconds < 90:
        return f"{seconds:.0f} seconds ago"
    return f"{seconds / 60:.0f} minutes ago"


class LiveStatus(StrEnum):
    """Why the figures on screen are what they are.

    Four values rather than a boolean, because "nothing yet", "the broker is
    taking its time" and "the broker refused" are three different findings and
    a reader handed only a spinner reaches for the wrong one every time.
    """

    STARTING = "starting"  # nothing has come back yet — not zero, unknown
    LIVE = "live"          # the last read succeeded
    SLOW = "slow"          # a read is outstanding; figures shown are the previous ones
    FAILING = "failing"    # the last read raised; `error` names it


class LiveSnapshot(BaseModel):
    """One successful read of the account, dated.

    Shaped so it can stand in for `_account_orders_prices`' return value: an
    `AccountSnapshot`, the resting orders and a quote per order symbol. A page
    render can take `snapshot.account, snapshot.orders, snapshot.prices` and
    hand them straight to `render.board`, which is the whole point — the live
    layer replaces the blocking read rather than duplicating what it produced.
    """

    taken_at: datetime
    account: AccountSnapshot
    orders: list[WorkingOrder] = Field(default_factory=list)
    prices: dict[str, float] = Field(default_factory=dict)

    # An empty order book and an order book that could not be read look
    # identical on screen, and only one of them means "nothing is waiting".
    # Same lesson as `FinnhubCalendar.is_degraded`: say the figure is unknown
    # rather than let its absence imply zero. `_account_orders_prices` swallows
    # this today; carrying the flag is the difference between the page being
    # able to say so and not.
    orders_unavailable: bool = False

    # Symbols whose quote could not be fetched. Named rather than dropped: with
    # no quote the distance-to-fill on that order is unknown, and a row silently
    # missing one reads as an order needing no move at all.
    quotes_unavailable: list[str] = Field(default_factory=list)

    @property
    def unrealised_pnl_usd(self) -> float:
        return sum(p.unrealised_pnl_usd for p in self.account.open_positions)


@dataclass(frozen=True)
class LiveState:
    """Everything a subscriber is told, as raw facts plus the thresholds.

    Frozen and self-contained on purpose, in the same shape as `TailnetStatus`:
    every question that depends on the current time takes `now`, so the whole
    thing is testable without a clock, a loop or a broker.
    """

    snapshot: LiveSnapshot | None = None
    # Non-empty only when the most recent read failed. Cleared by a success, so
    # "the broker is refusing" cannot outlive the refusal.
    error: str = ""
    consecutive_failures: int = 0
    # Set while a read is in flight, cleared when it finishes either way.
    poll_started_at: datetime | None = None

    slow_after_seconds: float = SLOW_AFTER_SECONDS
    stale_after_seconds: float = STALE_AFTER_SECONDS

    def age_seconds(self, *, now: datetime | None = None) -> float | None:
        """How old the figures are. `None` when there are none."""
        if self.snapshot is None:
            return None
        return ((now or _now()) - self.snapshot.taken_at).total_seconds()

    def poll_running_for(self, *, now: datetime | None = None) -> float | None:
        """How long the outstanding read has been outstanding, if one is."""
        if self.poll_started_at is None:
            return None
        return ((now or _now()) - self.poll_started_at).total_seconds()

    def is_stale(self, *, now: datetime | None = None) -> bool:
        """Too old to present as current, whatever the status says.

        Separate from `status` because the interesting case is the one where
        nothing failed and nothing is running: a poller that quietly stopped
        leaves a perfectly healthy-looking `live` state getting older. A reading
        that has stopped being taken must report that, not read as fine.
        """
        age = self.age_seconds(now=now)
        return age is not None and age > self.stale_after_seconds

    def status(self, *, now: datetime | None = None) -> LiveStatus:
        """Which of the four states this is.

        A recorded error wins over an outstanding read: "the broker is
        refusing" is the diagnosis and "this attempt is also taking a while" is
        a symptom of it. Recovery is only declared once a read has actually come
        back, never while one is merely in progress.
        """
        if self.error:
            return LiveStatus.FAILING
        running = self.poll_running_for(now=now)
        if running is not None and running > self.slow_after_seconds:
            return LiveStatus.SLOW
        if self.snapshot is None:
            return LiveStatus.STARTING
        return LiveStatus.LIVE

    def headline(self, *, now: datetime | None = None) -> str:
        """One sentence, written for somebody who has forgotten all of this."""
        moment = now or _now()
        status = self.status(now=moment)
        age = self.age_seconds(now=moment)

        if status is LiveStatus.FAILING:
            if self.snapshot is None:
                return (
                    f"The broker has not answered successfully yet ({self.error}). "
                    "There are no figures to show — they are unknown rather than zero."
                )
            return (
                f"The last read of the broker failed ({self.error}). The figures "
                f"below were read {_ago(age)} and are not being updated."
            )

        if status is LiveStatus.SLOW:
            running = self.poll_running_for(now=moment) or 0.0
            if self.snapshot is None:
                return (
                    f"The broker has not answered the first read yet, "
                    f"{running:.0f} seconds in. Nothing has failed."
                )
            return (
                f"The broker has not answered for {running:.0f} seconds. Nothing "
                f"has failed; the figures below were read {_ago(age)}."
            )

        if status is LiveStatus.STARTING:
            return (
                "Waiting for the first read of the account. Nothing has been read "
                "yet, so there are no figures — not zeroes."
            )

        if self.is_stale(now=moment):
            return (
                f"The last successful read was {_ago(age)}, which is older than "
                "this page expects. Nothing failed, so the poller may have stopped."
            )
        return f"Live. The account was read {_ago(age)}."

    def payload(self, *, now: datetime | None = None) -> dict[str, Any]:
        """The JSON one SSE event carries.

        Built by hand rather than dumped off the models, because a wire format
        is a decision: the browser gets the derived figures the Board actually
        renders (unrealised P&L, distance to fill) computed here in Python,
        which is the same rule `indicators.py` follows for the model. A page
        that had to derive them from raw fields would eventually derive one of
        them differently from the server-rendered version of the same page.

        **Everything that depends on a reading is `null` when there is no
        reading**, never zero. A cold start that rendered 0.00 equity would be
        a lie the page has no way to walk back.
        """
        moment = now or _now()
        snapshot = self.snapshot
        return {
            "status": self.status(now=moment).value,
            "headline": self.headline(now=moment),
            "error": self.error,
            "consecutive_failures": self.consecutive_failures,
            "as_of": snapshot.taken_at.isoformat() if snapshot else None,
            "age_seconds": self.age_seconds(now=moment),
            "stale": self.is_stale(now=moment),
            "read_running_for_seconds": self.poll_running_for(now=moment),
            "account": None if snapshot is None else _account_json(snapshot),
            "positions": None if snapshot is None else _positions_json(snapshot),
            "orders": None if snapshot is None else _orders_json(snapshot),
            "orders_unavailable": None if snapshot is None else snapshot.orders_unavailable,
            "quotes_unavailable": None if snapshot is None else snapshot.quotes_unavailable,
        }


def _account_json(snapshot: LiveSnapshot) -> dict[str, Any]:
    account = snapshot.account
    return {
        "equity_usd": account.equity_usd,
        "cash_usd": account.cash_usd,
        "buying_power_usd": account.buying_power_usd,
        # From the journal, never the broker. See the module docstring.
        "open_risk_usd": account.open_risk_usd,
        "realised_pnl_today_usd": account.realised_pnl_today_usd,
        "unrealised_pnl_usd": snapshot.unrealised_pnl_usd,
        "gross_exposure_usd": account.gross_exposure_usd,
        "position_count": len(account.open_positions),
    }


def _positions_json(snapshot: LiveSnapshot) -> list[dict[str, Any]]:
    return [
        {
            "symbol": p.symbol,
            "direction": p.direction.value,
            "qty": p.qty,
            "entry_price": p.entry_price,
            "current_price": p.current_price,
            "unrealised_pnl_usd": p.unrealised_pnl_usd,
            "notional_usd": p.notional_usd,
        }
        for p in snapshot.account.open_positions
    ]


def _orders_json(snapshot: LiveSnapshot) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for order in snapshot.orders:
        price = snapshot.prices.get(order.symbol)
        rows.append(
            {
                "order_id": order.order_id,
                "symbol": order.symbol,
                "direction": order.direction.value,
                "qty": order.qty,
                "remaining_qty": order.remaining_qty,
                "limit_price": order.limit_price,
                "status": order.status.value,
                "submitted_at": order.submitted_at.isoformat() if order.submitted_at else None,
                "current_price": price,
                # `None` when the quote is missing, and the symbol is also in
                # `quotes_unavailable`. Zero would read as "already there".
                "distance_pct": None if price is None else order.distance_to_fill(price),
            }
        )
    return rows


class LivePoller:
    """The one background reader of the broker, and the state every page shares.

    Constructed per application, never as a module global: `build_app` is
    called several times over in the test suite, and a shared poller would let
    one test's broker answer another test's page.
    """

    def __init__(
        self,
        *,
        broker_factory: Callable[[], Broker],
        open_risk_usd: Callable[[], float],
        interval_seconds: float = POLL_INTERVAL_SECONDS,
        slow_after_seconds: float = SLOW_AFTER_SECONDS,
        stale_after_seconds: float = STALE_AFTER_SECONDS,
        idle_stop_after_seconds: float | None = IDLE_STOP_AFTER_SECONDS,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        """`open_risk_usd` has no default, and that is the point.

        It is `Journal.open_risk_usd` in every real deployment. Giving it a
        default would mean a caller that forgot it still worked, producing
        snapshots whose open risk was silently zero — which is the bug fixed in
        `14b88c8` arriving in a new place. With no default the constructor
        raises instead, at import-and-wire time, where it is cheap to notice.
        """
        self._broker_factory = broker_factory
        self._open_risk_usd = open_risk_usd
        self._interval_seconds = interval_seconds
        self._idle_stop_after_seconds = idle_stop_after_seconds
        self._clock = clock

        self._state = LiveState(
            slow_after_seconds=slow_after_seconds,
            stale_after_seconds=stale_after_seconds,
        )
        self._broker: Broker | None = None
        self._task: asyncio.Task[None] | None = None
        self._subscribers: set[asyncio.Queue[None]] = set()
        self._last_subscriber_at = clock()

    # ------------------------------------------------------------- reading

    def state(self) -> LiveState:
        """The current state. Never blocks, never touches the network.

        This is what a page render calls, and why it can no longer hang: the
        broker conversation happens somewhere else entirely.
        """
        return self._state

    def latest(self) -> LiveSnapshot | None:
        """The most recent successful read, or `None` if there has never been one.

        `None` on a cold start is the design, not a gap. The page renders its
        shell, the stream fills it in a moment later, and nothing invents a
        figure in between.
        """
        return self._state.snapshot

    def payload(self, *, now: datetime | None = None) -> dict[str, Any]:
        return self._state.payload(now=now)

    def poll_once(self) -> LiveState:
        """Read the broker once and fold the result into the state.

        **Synchronous on purpose.** This is the blocking work, and `_run` is
        what pushes it onto a worker thread; keeping it an ordinary function is
        also what lets a test drive the poller with no event loop at all.

        Never raises. A failure is recorded and the previous reading is kept,
        with its age attached — the loop must survive a broker that is having a
        bad minute, because the alternative is a dashboard that silently stops
        being live while looking exactly as it did.
        """
        # Recorded before the work starts so a reader on the event loop can see
        # that a read is outstanding and for how long. Assigning one immutable
        # value in one statement is safe to do from the worker thread, and it is
        # advisory in any case: losing the race costs a "slow" label, never a
        # figure.
        self._state = replace(self._state, poll_started_at=self._clock())
        try:
            snapshot = self._read()
        except Exception as exc:
            # Broad, and for the same reason `fetch_market_ticks` is broad: the
            # Alpaca SDK raises `APIError`, `httpx` raises timeouts and JSON
            # decoding raises `ValueError`, none of which is a `KeyError` or a
            # `RuntimeError`. A narrow catch lets the unanticipated one through
            # and ends the poller, which is exactly the case where ending it is
            # worst.
            detail = f"{type(exc).__name__}: {exc}"
            failures = self._state.consecutive_failures + 1
            # Dropped so the next attempt reconnects. The cheapest explanation
            # for a failed read is a session that has gone.
            self._broker = None
            log.warning("live_poll_failed", error=detail, consecutive=failures)
            self._state = replace(
                self._state,
                error=detail,
                consecutive_failures=failures,
                poll_started_at=None,
            )
        else:
            self._state = replace(
                self._state,
                snapshot=snapshot,
                error="",
                consecutive_failures=0,
                poll_started_at=None,
            )
        return self._state

    def _read(self) -> LiveSnapshot:
        """One broker session's worth of everything the Board shows.

        A failure of `get_account` propagates and fails the whole read, which is
        correct: equity is the figure the page exists for. Orders and quotes
        degrade instead, because a broker hiccup should cost the pending list
        rather than the equity beside it — but each degradation is **recorded**,
        not swallowed.
        """
        broker = self._connected_broker()
        account = broker.get_account()

        # The one figure on this snapshot that cannot come from the broker.
        # Alpaca holds stop-losses as separate orders, so it does not know what
        # an open position was designed to lose; the journal recorded the
        # planned stop and is the only place that does. Left unset, the 2%
        # total-risk cap has nothing to count and the page reports a risk of
        # zero against a portfolio that has plenty.
        account.open_risk_usd = self._open_risk_usd()

        orders: list[WorkingOrder] = []
        orders_unavailable = False
        try:
            orders = broker.get_open_orders()
        except Exception as exc:
            orders_unavailable = True
            log.warning("live_orders_fetch_failed", error=f"{type(exc).__name__}: {exc}")

        prices: dict[str, float] = {}
        quotes_unavailable: list[str] = []
        for symbol in sorted({o.symbol for o in orders}):
            try:
                prices[symbol] = broker.get_tick(symbol).mid
            except Exception:
                quotes_unavailable.append(symbol)

        return LiveSnapshot(
            taken_at=self._clock(),
            account=account,
            orders=orders,
            prices=prices,
            orders_unavailable=orders_unavailable,
            quotes_unavailable=quotes_unavailable,
        )

    def _connected_broker(self) -> Broker:
        """One broker for the life of the poller, rebuilt after a failure.

        Reconnecting on every poll would add an account call per read for
        nothing — Alpaca is stateless HTTP and `connect()` is a credential check
        rather than a socket. Rebuilding only after a failure keeps the steady
        state cheap and still recovers from a session that has genuinely gone.
        """
        if self._broker is None:
            broker = self._broker_factory()
            broker.connect()
            self._broker = broker
        return self._broker

    # ----------------------------------------------------------- the loop

    def ensure_running(self) -> None:
        """Start the background read if it is not already going. Idempotent.

        Called from the stream rather than from application startup, so a box
        with nobody watching is not talking to the broker every five seconds all
        night. The cost is that the first render after a quiet spell shows
        `starting` and fills in a moment later, which is the honest thing for it
        to do. Call it from a startup hook instead if a permanently warm poller
        is wanted; it is safe to call from both.
        """
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="live-poller")

    async def _run(self) -> None:
        try:
            while True:
                # The whole point of this module: the blocking broker call
                # happens on a worker thread, so a slow Alpaca costs a stale
                # figure rather than a page that never arrives. Doing it on the
                # event loop would block every other request in the process,
                # which is the bug being fixed wearing a different hat.
                await asyncio.to_thread(self.poll_once)
                # Waking subscribers happens back on the loop thread. Asyncio
                # queues are not thread-safe and this is the only place that
                # touches them.
                self._publish()
                await asyncio.sleep(self._interval_seconds)
                if self._idle_long_enough():
                    log.info("live_poller_idle_stop")
                    return
        finally:
            # Cleared here rather than by whoever cancelled, so `ensure_running`
            # can tell a finished poller from a running one without having to
            # inspect the task object.
            self._task = None

    def _idle_long_enough(self) -> bool:
        if self._idle_stop_after_seconds is None or self._subscribers:
            return False
        idle = (self._clock() - self._last_subscriber_at).total_seconds()
        return idle > self._idle_stop_after_seconds

    async def stop(self) -> None:
        """Cancel the background read. Safe to call when it is not running.

        Wired to application shutdown by `install`. A read already handed to a
        worker thread finishes on its own and its result is discarded, which is
        harmless: the process is going away.
        """
        task = self._task
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        broker = self._broker
        self._broker = None
        if broker is not None:
            with suppress(Exception):
                broker.disconnect()

    # ---------------------------------------------------------- subscribers

    def subscribe(self) -> asyncio.Queue[None]:
        """A wake-up channel for one browser.

        The queue carries a signal rather than the payload, and holds at most
        one. A subscriber that fell behind should get the **current** state on
        its next turn, never a backlog of superseded ones — and rendering the
        payload at send time is also what keeps `age_seconds` honest, since a
        queued payload would carry the age it had when it was queued.
        """
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        self._last_subscriber_at = self._clock()
        return queue

    def unsubscribe(self, queue: asyncio.Queue[None]) -> None:
        self._subscribers.discard(queue)
        self._last_subscriber_at = self._clock()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def _publish(self) -> None:
        for queue in list(self._subscribers):
            # A full queue is already holding an unread wake-up, and nothing is
            # lost by dropping this one: the reader renders the state as it
            # finds it, not as it was when the signal was posted.
            with suppress(asyncio.QueueFull):
                queue.put_nowait(None)


def _event(payload: dict[str, Any]) -> str:
    """One SSE message, in the default (unnamed) event.

    Unnamed on purpose. A named event only reaches a listener registered for
    that exact name, so `EventSource.onmessage` — the obvious thing for the page
    to write — would silently receive nothing, which is the worst kind of
    broken. The status travels inside the payload, where it cannot be missed.

    `json.dumps` is also what keeps the framing intact: a newline inside a value
    would end the message early, and it escapes them.
    """
    return f"data: {json.dumps(payload)}\n\n"


async def stream(poller: LivePoller) -> AsyncGenerator[str, None]:
    """The body of the SSE response: one subscriber's view of the poller.

    Typed as a generator rather than an iterator so callers keep `aclose()`.
    Starlette calls it on disconnect, and it is what makes the `finally` below
    run rather than leaking a subscriber for every browser that ever connected.
    """
    poller.ensure_running()
    queue = poller.subscribe()
    try:
        # How long `EventSource` waits before reconnecting a dropped stream.
        yield f"retry: {RECONNECT_DELAY_MS}\n\n"
        # The current state goes out before anything is awaited, so a browser
        # connecting during a cold start is TOLD it is a cold start rather than
        # left on a blank panel until the first read lands.
        yield _event(poller.payload())
        while True:
            # A timeout means nothing new arrived, and the state goes out
            # anyway — see HEARTBEAT_SECONDS. A read that never returns produces
            # no reading, so this is the only way a browser hears that its
            # figures are getting older.
            with suppress(TimeoutError):
                await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            yield _event(poller.payload())
    finally:
        # Runs on client disconnect too: Starlette cancels the generator, and a
        # subscriber that was never removed would keep the poller believing
        # somebody is still watching, so it would never idle-stop.
        poller.unsubscribe(queue)


def sse_response(poller: LivePoller) -> StreamingResponse:
    """A ready-to-return SSE response. No dependencies beyond FastAPI itself."""
    return StreamingResponse(
        stream(poller),
        media_type="text/event-stream",
        headers={
            # A cached snapshot of a live stream is a stale figure with nothing
            # to say so.
            "Cache-Control": "no-store",
            # nginx buffers proxied responses by default, which holds every
            # event back until the buffer fills. Ignored by everything else.
            "X-Accel-Buffering": "no",
        },
    )


def build_poller(
    *,
    journal: Journal,
    env: Env,
    force_mock: bool = False,
    interval_seconds: float = POLL_INTERVAL_SECONDS,
) -> LivePoller:
    """The wiring `build_app` uses. The journal is not optional here either."""

    def factory() -> Broker:
        # Deferred exactly as in `_account_orders_prices`: `..main` pulls in the
        # Anthropic client and the data feeds, none of which a web process needs
        # at import time.
        from ..main import build_broker

        return build_broker(env, force_mock=force_mock)

    return LivePoller(
        broker_factory=factory,
        open_risk_usd=journal.open_risk_usd,
        interval_seconds=interval_seconds,
    )


def install(app: FastAPI, poller: LivePoller, *, path: str = DEFAULT_PATH) -> None:
    """Mount the stream and make sure the poller stops with the process.

    The route is deliberately left out of `app.py`'s `OPEN_PATHS`: it serves
    equity, positions and resting orders, so it belongs behind the same password
    as the pages that render them. A same-origin `EventSource` sends the session
    cookie by itself, so nothing extra is needed for it to work signed in.
    """

    @app.get(path)
    def live_stream() -> StreamingResponse:
        return sse_response(poller)

    # A shutdown hook rather than `@app.on_event`, which is deprecated, and
    # rather than a lifespan, which would have to be handed to the `FastAPI`
    # constructor in `build_app` and is therefore not something this module can
    # add from outside.
    app.router.on_shutdown.append(poller.stop)
