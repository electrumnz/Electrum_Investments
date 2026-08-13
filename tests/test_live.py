"""Tests for the live layer: one poller, many browsers, honest ages.

Three things matter here and the rest is plumbing.

- **The four states stay four.** A cold start, a slow broker and a broker error
  must not collapse into "no figures", because only some of them mean anybody
  should do something. Most of this file is that distinction.
- **Open risk comes from the journal.** The broker cannot report it, so a
  snapshot that skipped it would show a portfolio at zero risk. There is a test
  that the wire even exists, and one that it cannot be forgotten.
- **A blocked broker does not block the event loop.** That is the whole reason
  the module exists, and it is asserted rather than assumed.

Everything runs against `MockBroker`. Nothing here touches the network, `data/`
or `audit/` — the journal fixture is a `tmp_path` file.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from bot.broker import MockBroker
from bot.journal import Journal
from bot.market_clock import TradingDay
from bot.models import (
    AccountSnapshot,
    Direction,
    OrderStatus,
    Position,
    Trade,
    WorkingOrder,
)
from bot.reconcile import apply_journal_state
from bot.web import live
from bot.web.live import LivePoller, LiveSnapshot, LiveState, LiveStatus

pytest.importorskip("fastapi")
from fastapi import FastAPI

NOW = datetime(2026, 5, 4, 15, 0, tzinfo=UTC)


class _StubBroker(MockBroker):
    """A `MockBroker` that can be made to misbehave, and counts what it was asked.

    Subclassed rather than hand-rolled so the happy path is the real mock's
    behaviour and only the failure being tested is fake.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.account_reads = 0
        self.connects = 0
        self.disconnects = 0
        self.fail_account_with: Exception | None = None
        self.fail_orders_with: Exception | None = None
        self.block_for_seconds = 0.0

    def connect(self) -> None:
        self.connects += 1
        super().connect()

    def disconnect(self) -> None:
        self.disconnects += 1
        super().disconnect()

    def set_position(self, position: Position) -> None:
        """`MockBroker` builds positions from fills and has no public setter.

        Seeding one directly is the only way to give it an unrealised P&L, which
        is the figure under test. It belongs on this subclass rather than in a
        test body, so nothing outside the mock family reaches into it.
        """
        self._positions[position.symbol] = position

    def get_account(self) -> AccountSnapshot:
        self.account_reads += 1
        if self.block_for_seconds:
            # Deliberately a BLOCKING sleep: this stands in for a slow HTTP call
            # into Alpaca, and an `await` here would test nothing.
            time.sleep(self.block_for_seconds)
        if self.fail_account_with is not None:
            raise self.fail_account_with
        return super().get_account()

    def get_open_orders(self) -> list[WorkingOrder]:
        if self.fail_orders_with is not None:
            raise self.fail_orders_with
        return super().get_open_orders()


@pytest.fixture
def broker() -> _StubBroker:
    return _StubBroker(starting_equity=100_000.0)


@pytest.fixture
def journal(tmp_path) -> Journal:
    return Journal(tmp_path / "journal.db")


def _with_risk(snapshot: AccountSnapshot, open_risk: float) -> AccountSnapshot:
    snapshot.open_risk_usd = open_risk
    return snapshot


def _poller(broker: MockBroker, *, open_risk: float = 0.0, **kwargs: Any) -> LivePoller:
    return LivePoller(
        broker_factory=lambda: broker,
        apply_journal_state=lambda s: _with_risk(s, open_risk),
        **kwargs,
    )


def _open_trade(journal: Journal, *, risk_per_share: float = 5.0, qty: float = 10) -> None:
    journal.record_entry(
        Trade(
            symbol="SPY",
            direction=Direction.BUY,
            qty=qty,
            entry_time=NOW,
            entry_price=580.0,
            planned_stop=580.0 - risk_per_share,
            planned_target=600.0,
        )
    )


# --------------------------------------------------------------- cold start


def test_cold_start_reports_nothing_rather_than_zero(broker):
    """No read yet is not a read of zero.

    The failure this guards is a Board showing $0.00 equity while the poller is
    still warming up, which an operator has no way to tell from an account that
    has actually been emptied.
    """
    poller = _poller(broker)

    assert poller.latest() is None
    payload = poller.payload()
    assert payload["status"] == LiveStatus.STARTING.value
    assert payload["account"] is None
    assert payload["positions"] is None
    assert payload["orders"] is None
    assert payload["as_of"] is None
    assert payload["age_seconds"] is None
    assert payload["error"] == ""
    assert "not zeroes" in payload["headline"]


def test_the_payload_is_json_serialisable(broker):
    """It travels down an SSE frame, so anything unserialisable is a dead stream."""
    poller = _poller(broker)
    poller.poll_once()
    assert json.loads(json.dumps(poller.payload()))["status"] == LiveStatus.LIVE.value


# ------------------------------------------------------------ a good read


def test_a_completed_read_is_dated_and_live(broker):
    poller = _poller(broker)
    poller.poll_once()

    snapshot = poller.latest()
    assert snapshot is not None
    assert snapshot.account.equity_usd == 100_000.0

    payload = poller.payload()
    assert payload["status"] == LiveStatus.LIVE.value
    assert payload["as_of"] == snapshot.taken_at.isoformat()
    # The age travels with the figure, always. A number with no age is the
    # thing this module exists to stop.
    assert payload["age_seconds"] is not None
    assert payload["age_seconds"] < 5
    assert payload["stale"] is False


def test_positions_reach_the_payload_with_their_unrealised(broker):
    broker.set_position(
        Position(
            symbol="SPY",
            direction=Direction.BUY,
            qty=10,
            entry_price=575.0,
            opened_at=NOW,
            current_price=580.0,
            unrealised_pnl_usd=50.0,
        )
    )
    poller = _poller(broker)
    poller.poll_once()

    payload = poller.payload()
    assert payload["account"]["position_count"] == 1
    assert payload["account"]["unrealised_pnl_usd"] == 50.0
    assert payload["positions"][0]["symbol"] == "SPY"
    assert payload["positions"][0]["current_price"] == 580.0


# ------------------------------------------------------------- open risk


def test_open_risk_comes_from_the_journal_not_the_broker(broker, journal):
    """The load-bearing wire.

    Alpaca holds stop-losses as separate orders, so the broker cannot report
    what a position was designed to lose — `MockBroker.get_account` returns
    `open_risk_usd` at its default of zero. Only the journal knows, and every
    path that hands out a snapshot has to say so.
    """
    _open_trade(journal, risk_per_share=5.0, qty=10)
    assert broker.get_account().open_risk_usd == 0.0  # the broker's view

    poller = LivePoller(
        broker_factory=lambda: broker,
        apply_journal_state=lambda s: apply_journal_state(s, journal),
    )
    poller.poll_once()

    snapshot = poller.latest()
    assert snapshot is not None
    assert snapshot.account.open_risk_usd == pytest.approx(50.0)
    assert poller.payload()["account"]["open_risk_usd"] == pytest.approx(50.0)


def test_the_poller_fills_every_figure_the_journal_owns(broker, journal):
    """One journal read, four figures, and the other three used to stay empty.

    The poller called `journal.open_risk_usd` and assigned the total alone,
    leaving `open_risk_by_symbol`, `planned_stop_by_symbol` and
    `symbols_with_unknown_risk` at their defaults. Nothing on the web side reads
    them yet, so it was not a live fault — it becomes one the first time a
    surface renders a class's risk, because an empty breakdown reads as *this
    class risks nothing*. That is the missing-versus-zero rule, and the fix is
    structural: the poller hands over the whole snapshot, so it cannot fill one
    figure and forget the rest.
    """
    _open_trade(journal, risk_per_share=5.0, qty=10)

    poller = LivePoller(
        broker_factory=lambda: broker,
        apply_journal_state=lambda s: apply_journal_state(s, journal),
    )
    poller.poll_once()

    snapshot = poller.latest()
    assert snapshot is not None
    account = snapshot.account
    assert account.open_risk_by_symbol == {"SPY": pytest.approx(50.0)}
    assert account.planned_stop_by_symbol == {"SPY": pytest.approx(575.0)}
    # The total is the sum of the breakdown rather than a second reading of the
    # journal, so the two can never describe different sets of open trades.
    assert account.open_risk_usd == pytest.approx(sum(account.open_risk_by_symbol.values()))


def test_the_open_risk_source_cannot_be_forgotten(broker):
    """No default, so a caller that omits it fails at wiring time.

    A default of zero would mean the total-risk cap silently had nothing to
    count, which is the bug fixed in `14b88c8` arriving in a new place. The
    constructor refusing is the cheapest possible place to find out.
    """
    with pytest.raises(TypeError):
        LivePoller(broker_factory=lambda: broker)  # type: ignore[call-arg]


def test_an_unreadable_journal_fails_the_whole_read_rather_than_showing_no_risk(broker):
    """Deliberately harsher than the orders degradation, and for a reason.

    `open_risk_usd` is a plain float with no way to say "unknown", so degrading
    would mean writing a zero and trusting every renderer to check a flag beside
    it. A board reporting no risk against a portfolio that has plenty is exactly
    the invented figure this repository exists to prevent; no board, clearly
    labelled, is the better failure.
    """

    def locked(snapshot: AccountSnapshot) -> AccountSnapshot:
        raise RuntimeError("database is locked")

    poller = LivePoller(broker_factory=lambda: broker, apply_journal_state=locked)
    poller.poll_once()

    payload = poller.payload()
    assert payload["status"] == LiveStatus.FAILING.value
    assert payload["account"] is None
    assert "database is locked" in payload["error"]
    # The wording must not send anybody hunting a broker outage.
    assert "broker" not in payload["headline"]


def test_build_poller_wires_the_journal(journal, tmp_path):
    """The factory `build_app` calls must carry the journal through."""
    from bot.config import Env

    _open_trade(journal, risk_per_share=2.0, qty=25)
    poller = live.build_poller(
        journal=journal,
        env=Env(_env_file=None),  # type: ignore[call-arg]
        force_mock=True,
    )
    poller.poll_once()

    snapshot = poller.latest()
    assert snapshot is not None
    assert snapshot.account.open_risk_usd == pytest.approx(50.0)


# ----------------------------------------------------------- broker errors


def test_a_broker_error_keeps_the_last_good_figures_and_names_it(broker):
    """Failing is not the same as having nothing, and both keep the age."""
    poller = _poller(broker)
    poller.poll_once()
    good = poller.latest()
    assert good is not None

    broker.fail_account_with = RuntimeError("alpaca 503")
    poller.poll_once()

    payload = poller.payload()
    assert payload["status"] == LiveStatus.FAILING.value
    assert "alpaca 503" in payload["error"]
    assert payload["consecutive_failures"] == 1
    # The previous reading is still there, still dated, and the headline says
    # plainly that it has stopped moving.
    assert payload["account"]["equity_usd"] == 100_000.0
    assert payload["as_of"] == good.taken_at.isoformat()
    assert "not being updated" in payload["headline"]


def test_a_broker_error_before_any_success_has_no_figures(broker):
    """Failing with nothing behind it must not read as failing with data."""
    broker.fail_account_with = RuntimeError("bad credentials")
    poller = _poller(broker)
    poller.poll_once()

    payload = poller.payload()
    assert payload["status"] == LiveStatus.FAILING.value
    assert payload["account"] is None
    assert "No account read has succeeded yet" in payload["headline"]


def test_a_failure_does_not_end_the_poller(broker):
    """Same rule as `fetch_market_ticks`: degrade the cycle, never end the loop."""
    poller = _poller(broker)
    broker.fail_account_with = ValueError("json decode")
    poller.poll_once()
    poller.poll_once()
    assert poller.state().consecutive_failures == 2

    broker.fail_account_with = None
    poller.poll_once()

    payload = poller.payload()
    assert payload["status"] == LiveStatus.LIVE.value
    assert payload["error"] == ""
    assert payload["consecutive_failures"] == 0


def test_an_arbitrary_exception_is_caught_too(broker):
    """The catch is broad on purpose. An unanticipated exception is exactly the
    case where ending the poller would be worst."""

    class Exotic(Exception):
        pass

    broker.fail_account_with = Exotic("something nobody predicted")
    poller = _poller(broker)
    poller.poll_once()  # must not raise
    assert poller.state().status() is LiveStatus.FAILING


def test_the_broker_is_rebuilt_after_a_failure(broker):
    """One session in the steady state, a fresh one after an error.

    Reconnecting every poll would double the account calls for nothing; never
    reconnecting would strand the poller on a session that has gone.
    """
    poller = _poller(broker)
    poller.poll_once()
    poller.poll_once()
    assert broker.connects == 1

    broker.fail_account_with = RuntimeError("connection reset")
    poller.poll_once()
    broker.fail_account_with = None
    poller.poll_once()
    assert broker.connects == 2

    # And the failed session is CLOSED, not merely unreferenced. Dropping the
    # last reference to a broker is not the same as closing it, and a broker
    # whose read just failed is the one most likely to be holding something.
    assert broker.disconnects == 1


# ------------------------------------------------- slow, and stale, and fine


def test_a_slow_read_is_not_a_failure():
    """An outstanding read reports `slow`, keeps the previous figures, and names
    no error. Only one of "slow" and "failing" needs anybody to do anything."""
    snapshot = LiveSnapshot(
        taken_at=NOW - timedelta(seconds=2),
        account=AccountSnapshot(equity_usd=1.0, cash_usd=1.0, buying_power_usd=1.0),
    )
    state = LiveState(
        snapshot=snapshot,
        poll_started_at=NOW - timedelta(seconds=9),
        slow_after_seconds=3.0,
    )

    assert state.status(now=NOW) is LiveStatus.SLOW
    payload = state.payload(now=NOW)
    assert payload["error"] == ""
    assert payload["read_running_for_seconds"] == pytest.approx(9.0)
    assert payload["account"]["equity_usd"] == 1.0
    assert "has not answered for 9 seconds" in payload["headline"]


def test_a_slow_first_read_is_still_a_cold_start_underneath():
    state = LiveState(poll_started_at=NOW - timedelta(seconds=8), slow_after_seconds=3.0)
    assert state.status(now=NOW) is LiveStatus.SLOW
    assert state.payload(now=NOW)["account"] is None


def test_a_read_in_progress_is_not_slow_immediately():
    state = LiveState(poll_started_at=NOW - timedelta(seconds=1), slow_after_seconds=3.0)
    assert state.status(now=NOW) is LiveStatus.STARTING


def test_a_recorded_error_outranks_an_outstanding_read():
    """Recovery is declared when a read comes back, never while one is running."""
    state = LiveState(
        error="RuntimeError: down",
        poll_started_at=NOW - timedelta(seconds=30),
        slow_after_seconds=3.0,
    )
    assert state.status(now=NOW) is LiveStatus.FAILING


def test_a_stale_reading_says_so_even_though_nothing_failed():
    """The `TailnetStatus.is_stale` rule: a reading that stopped being taken
    reports that it stopped, rather than sitting there looking healthy."""
    snapshot = LiveSnapshot(
        taken_at=NOW - timedelta(minutes=4),
        account=AccountSnapshot(equity_usd=1.0, cash_usd=1.0, buying_power_usd=1.0),
    )
    state = LiveState(snapshot=snapshot, stale_after_seconds=20.0)

    payload = state.payload(now=NOW)
    assert payload["status"] == LiveStatus.LIVE.value
    assert payload["stale"] is True
    assert payload["age_seconds"] == pytest.approx(240.0)
    assert "may have stopped" in payload["headline"]


# ------------------------------------------------------ degraded sub-reads


def test_orders_that_could_not_be_read_are_not_an_empty_order_book(broker):
    """`FinnhubCalendar.is_degraded` in a new costume.

    An empty list from a failed fetch looks exactly like "nothing is resting",
    and only one of those means the page is complete.
    """
    broker.fail_orders_with = RuntimeError("orders endpoint 500")
    poller = _poller(broker)
    poller.poll_once()

    payload = poller.payload()
    # The equity figure survives: a hiccup costs the pending list, not the
    # number beside it.
    assert payload["status"] == LiveStatus.LIVE.value
    assert payload["orders"] == []
    assert payload["orders_unavailable"] is True


def test_a_readable_but_empty_order_book_is_not_flagged(broker):
    poller = _poller(broker)
    poller.poll_once()
    assert poller.payload()["orders_unavailable"] is False


def test_a_broker_that_swallows_its_own_order_failure_still_reports_degraded(broker):
    """The test above pinned a path the real broker never takes.

    `_StubBroker.fail_orders_with` raises, and the poller's `except` catches it
    — so the flag looked exercised. `AlpacaBroker.get_open_orders` does not
    raise: it catches its own SDK errors and returns `[]`, because it feeds a
    display beside positions and risk. So on the only broker that can genuinely
    fail, `orders_unavailable` was unreachable and an outage rendered as an
    account with nothing resting.

    The broker reports the degradation on itself now, the same way
    `FinnhubCalendar` does, and this is the shape that actually happens in
    production: an empty list AND a flag.
    """
    broker.set_orders_degraded(True)
    poller = _poller(broker)
    poller.poll_once()

    payload = poller.payload()
    assert payload["orders"] == []
    assert payload["orders_unavailable"] is True


def test_the_degraded_flag_clears_once_the_orders_come_back(broker):
    """A flag that only ever goes up would leave the page claiming an outage
    for the rest of the process's life."""
    broker.set_orders_degraded(True)
    poller = _poller(broker)
    poller.poll_once()
    assert poller.payload()["orders_unavailable"] is True

    broker.set_orders_degraded(False)
    poller.poll_once()
    assert poller.payload()["orders_unavailable"] is False


def test_a_calendar_that_raises_costs_the_strip_and_never_the_equity(broker):
    """The last unbounded sub-read in `_read`, found by making one raise.

    `_read` said "never raises" over the calendar block, quoting
    `SessionCalendar.refresh`, which says the same thing over a claim about the
    BROKER: `get_calendar` is documented to catch its own errors and answer
    `None`. Nothing in the `Broker` protocol makes that structural.
    `AlpacaBroker` holds it inside one `try`, with the deferred
    `from alpaca.trading.requests import GetCalendarRequest` sitting outside it.

    Measured before the fix: an exception out of `get_calendar` took the WHOLE
    poll down — `status=failing`, `snapshot=None`, no equity, no positions, no
    orders — every five seconds, for a holiday strip. The order book above it
    and the tape below it both degrade alone; this was the one that did not.
    """

    class _NoCalendar(MockBroker):
        def get_calendar(self, start: date, end: date) -> list[TradingDay] | None:
            raise RuntimeError("calendar endpoint 500")

    poller = _poller(_NoCalendar(starting_equity=100_000.0))
    poller.poll_once()

    payload = poller.payload()
    # The figure this page exists for survives.
    assert payload["status"] == LiveStatus.LIVE.value
    assert payload["account"] is not None
    assert payload["account"]["equity_usd"] == pytest.approx(100_000.0)

    snapshot = poller.latest()
    assert snapshot is not None
    # And the strip says it is unknown rather than saying there are no sessions.
    assert snapshot.sessions_ahead == []
    assert snapshot.calendar_loaded is False
    assert snapshot.calendar_degraded is True


def test_a_missing_quote_is_named_and_its_distance_stays_unknown(broker):
    """No quote means the distance to fill is unknown. Zero would read as
    "already there", which is the opposite of the truth."""
    broker.set_open_orders(
        [
            WorkingOrder(
                order_id="a1",
                symbol="SPY",
                direction=Direction.BUY,
                qty=3,
                limit_price=575.0,
                status=OrderStatus.NEW,
            ),
            WorkingOrder(
                order_id="b2",
                symbol="QQQ",
                direction=Direction.BUY,
                qty=2,
                limit_price=480.0,
                status=OrderStatus.NEW,
            ),
        ]
    )
    broker.set_price("SPY", 579.98, 580.02)  # QQQ deliberately unseeded

    poller = _poller(broker)
    poller.poll_once()
    payload = poller.payload()

    rows = {row["symbol"]: row for row in payload["orders"]}
    assert rows["SPY"]["distance_pct"] is not None
    assert rows["QQQ"]["distance_pct"] is None
    assert payload["quotes_unavailable"] == ["QQQ"]


# ------------------------------------------------------------ subscribers


@pytest.mark.asyncio
async def test_one_poller_serves_every_subscriber(broker):
    """The point of the module. Three browsers, one broker conversation.

    A ten-second interval means exactly one read happens inside the window, so
    the assertion is on the count rather than on a race.
    """
    poller = _poller(broker, interval_seconds=10.0, idle_stop_after_seconds=None)
    queues = [poller.subscribe() for _ in range(3)]
    assert poller.subscriber_count == 3

    poller.ensure_running()
    try:
        await asyncio.sleep(0.05)
        assert broker.account_reads == 1
        for queue in queues:
            assert queue.qsize() == 1
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_a_subscriber_that_fell_behind_gets_no_backlog(broker):
    """The queue holds one wake-up. A browser that fell behind should see the
    CURRENT state on its next turn, not a queue of superseded ones."""
    poller = _poller(broker, interval_seconds=0.0, idle_stop_after_seconds=None)
    queue = poller.subscribe()
    poller.ensure_running()
    try:
        await asyncio.sleep(0.05)
        assert broker.account_reads > 1
        assert queue.qsize() == 1
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_unsubscribing_stops_the_wake_ups(broker):
    poller = _poller(broker, interval_seconds=0.0, idle_stop_after_seconds=None)
    queue = poller.subscribe()
    poller.unsubscribe(queue)
    poller.ensure_running()
    try:
        await asyncio.sleep(0.02)
        assert broker.account_reads >= 1
        assert queue.qsize() == 0
        assert poller.subscriber_count == 0
    finally:
        await poller.stop()


# ---------------------------------------------------------------- the loop


@pytest.mark.asyncio
async def test_a_blocking_broker_does_not_block_the_event_loop(broker):
    """The reason `_run` uses `asyncio.to_thread`.

    The broker sleeps for two tenths of a second inside `get_account`. If that
    ran on the event loop, nothing else in the process could run while it did —
    which is the page-hangs-on-a-slow-Alpaca bug this module was built to close.
    Because it does not, this coroutine wakes up mid-read and finds a state that
    says a read is outstanding.
    """
    broker.block_for_seconds = 0.2
    poller = _poller(broker, interval_seconds=0.01, slow_after_seconds=0.01)
    poller.ensure_running()
    try:
        await asyncio.sleep(0.08)
        state = poller.state()
        assert state.status() is LiveStatus.SLOW
        assert state.poll_running_for() is not None
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_the_loop_keeps_reading_after_a_failure(broker):
    broker.fail_account_with = RuntimeError("down")
    poller = _poller(broker, interval_seconds=0.0)
    poller.ensure_running()
    try:
        await asyncio.sleep(0.05)
        assert broker.account_reads > 1
        assert poller.state().status() is LiveStatus.FAILING
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_ensure_running_is_idempotent(broker):
    """Two browsers connecting must not mean two pollers."""
    poller = _poller(broker, interval_seconds=0.0, idle_stop_after_seconds=None)
    queue = poller.subscribe()
    poller.ensure_running()
    poller.ensure_running()
    poller.ensure_running()
    try:
        await asyncio.sleep(0.02)
        first = broker.account_reads
        await asyncio.sleep(0.02)
        # One loop advances the count; three would advance it in lockstep with
        # three times the broker traffic, which is what this guards.
        assert broker.connects == 1
        assert broker.account_reads > first
    finally:
        poller.unsubscribe(queue)
        await poller.stop()


@pytest.mark.asyncio
async def test_the_poller_stops_when_nobody_is_watching(broker):
    """No subscribers means no reason to be talking to the broker."""
    poller = _poller(broker, interval_seconds=0.0, idle_stop_after_seconds=0.0)
    poller.ensure_running()
    await asyncio.sleep(0.05)
    reads = broker.account_reads
    await asyncio.sleep(0.05)
    assert reads >= 1
    assert broker.account_reads == reads


@pytest.mark.asyncio
async def test_the_idle_stop_lets_the_broker_session_go(broker):
    """Stopping the reads but keeping the session open is half an idle stop.

    The point of the idle stop is that a box nobody is watching holds nothing
    open. It used to return out of the loop with `self._broker` still set and
    connected, so the reads stopped and the session did not.
    """
    poller = _poller(broker, interval_seconds=0.0, idle_stop_after_seconds=0.0)
    poller.ensure_running()
    await asyncio.sleep(0.05)

    assert broker.connects == 1
    assert broker.disconnects == 1


@pytest.mark.asyncio
async def test_a_stopped_poller_cannot_authenticate_again(broker):
    """`stop()` cancels an await, and cancelling an await does not stop a thread.

    `asyncio.to_thread` hands the poll to an executor; cancellation abandons the
    future while the worker runs on. So an abandoned poll could reach
    `_connected_broker`, build and authenticate a NEW session, and store it
    after `stop()` had already cleared and disconnected the old one — a
    connected broker with nobody left to close it.

    Driven here by calling `poll_once` directly after `stop()`, which is exactly
    what that abandoned worker does.
    """
    poller = _poller(broker)
    poller.poll_once()
    assert broker.connects == 1

    await poller.stop()
    assert broker.disconnects == 1

    poller.poll_once()

    # Refused rather than reconnected, and recorded rather than raised: the
    # poll is caught by `poll_once` and lands on a state nothing will render.
    assert broker.connects == 1
    assert "poller is stopped" in poller.state().error


@pytest.mark.asyncio
async def test_a_stopped_poller_does_not_start_itself_again(broker):
    """A stream opening during shutdown would otherwise put the loop back,
    behind `stop()`: a background task and a broker session outliving the
    application that owned them."""
    poller = _poller(broker, interval_seconds=0.0, idle_stop_after_seconds=None)
    await poller.stop()

    poller.ensure_running()
    await asyncio.sleep(0.02)

    assert broker.account_reads == 0


@pytest.mark.asyncio
async def test_stop_is_safe_when_nothing_is_running(broker):
    await _poller(broker).stop()


def test_an_injected_clock_is_used_for_ages_not_only_for_stamps(broker):
    """Half-honouring an injectable clock is worse than not offering one.

    `LiveState` falls back to the wall clock because it is a value object with
    no clock of its own. That was fine until the poller took one: readings were
    stamped with the injected clock and their ages measured against the real
    one, so a test that set the clock to last year got an age in the tens of
    millions of seconds and every reading reported stale. Figures that look
    like measurements and are not are the thing this repository refuses.
    """
    moment = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    poller = _poller(broker, clock=lambda: moment)
    poller.poll_once()

    payload = poller.payload()
    assert payload["age_seconds"] == pytest.approx(0.0, abs=0.001)
    assert payload["stale"] is False
    assert poller.reading_is_stale() is False


# ------------------------------------------------------------ the SSE frame


@pytest.mark.asyncio
async def test_the_stream_sends_the_current_state_before_anything_happens(broker):
    """A browser connecting during a cold start is TOLD it is a cold start,
    rather than left on a blank panel until the first read lands."""
    poller = _poller(broker, interval_seconds=0.0, idle_stop_after_seconds=None)
    frames = live.stream(poller)
    try:
        assert await anext(frames) == f"retry: {live.RECONNECT_DELAY_MS}\n\n"
        first = await anext(frames)
        assert first.startswith("data: ")
        assert first.endswith("\n\n")
        # Unnamed event, so `EventSource.onmessage` receives it. A named event
        # would silently reach nobody.
        assert "event:" not in first
        assert json.loads(first[len("data: ") :])["status"] in {
            LiveStatus.STARTING.value,
            LiveStatus.LIVE.value,
        }
    finally:
        await frames.aclose()
        await poller.stop()


@pytest.mark.asyncio
async def test_the_stream_unsubscribes_when_the_client_goes(broker):
    """Starlette closes the generator on disconnect. A subscriber left behind
    would keep the poller believing somebody is watching."""
    poller = _poller(broker, interval_seconds=0.0, idle_stop_after_seconds=None)
    frames = live.stream(poller)
    await anext(frames)
    assert poller.subscriber_count == 1
    await frames.aclose()
    assert poller.subscriber_count == 0
    await poller.stop()


@pytest.mark.asyncio
async def test_the_stream_pushes_each_new_reading(broker):
    poller = _poller(broker, interval_seconds=0.0, idle_stop_after_seconds=None)
    frames = live.stream(poller)
    try:
        await anext(frames)  # retry directive
        await anext(frames)  # the state as found
        payload = json.loads((await anext(frames))[len("data: ") :])
        assert payload["status"] == LiveStatus.LIVE.value
        assert payload["account"]["equity_usd"] == 100_000.0
    finally:
        await frames.aclose()
        await poller.stop()


# ------------------------------------------------------------- the route


async def _drive(app: Any, path: str, want_chunks: int) -> tuple[int, dict[str, str], list[str]]:
    """Call an ASGI app directly and hang up after `want_chunks` body chunks.

    Written by hand because no in-process HTTP client can read an endless
    response: both `TestClient` and `httpx.ASGITransport` await the application
    to completion before handing back a response object, so an SSE stream would
    simply never return. Driving the ASGI callable is what proves the events
    arrive INCREMENTALLY, which is the property under test.
    """
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"host", b"testserver"), (b"accept", b"text/event-stream")],
        "scheme": "http",
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
        "root_path": "",
    }
    chunks: list[str] = []
    status = 0
    headers: dict[str, str] = {}
    hang_up = asyncio.Event()

    async def receive() -> dict[str, Any]:
        await hang_up.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status, headers
        if message["type"] == "http.response.start":
            status = message["status"]
            headers = {k.decode(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body:
                chunks.append(body.decode())
            if len(chunks) >= want_chunks:
                hang_up.set()

    await asyncio.wait_for(app(scope, receive, send), timeout=10)
    return status, headers, chunks


@pytest.mark.asyncio
async def test_the_installed_route_streams_events_and_ends_on_disconnect(broker):
    """End to end through FastAPI, against the version actually installed."""
    app = FastAPI()
    poller = _poller(broker, interval_seconds=0.0, idle_stop_after_seconds=None)
    live.install(app, poller)

    status, headers, chunks = await _drive(app, live.DEFAULT_PATH, want_chunks=3)

    assert status == 200
    assert headers["content-type"].startswith("text/event-stream")
    assert headers["cache-control"] == "no-store"
    assert chunks[0].startswith("retry: ")
    events = [json.loads(c[len("data: ") :]) for c in chunks[1:] if c.startswith("data: ")]
    assert events
    assert {"status", "headline", "as_of", "age_seconds", "stale"} <= set(events[0])

    # The disconnect closed the generator, which unsubscribed.
    assert poller.subscriber_count == 0
    await poller.stop()


@pytest.mark.asyncio
async def test_install_stops_the_poller_on_shutdown(broker):
    """The background task must not outlive the process it belongs to."""
    app = FastAPI()
    poller = _poller(broker, interval_seconds=0.0, idle_stop_after_seconds=None)
    live.install(app, poller)

    assert poller.stop in app.router.on_shutdown

    poller.ensure_running()
    await asyncio.sleep(0.01)
    for handler in app.router.on_shutdown:
        await handler()
    assert broker.account_reads >= 1
    reads = broker.account_reads
    await asyncio.sleep(0.02)
    assert broker.account_reads == reads
