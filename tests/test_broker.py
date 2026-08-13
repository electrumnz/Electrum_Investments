from __future__ import annotations

from datetime import UTC, datetime
from types import EllipsisType
from typing import Any

import pytest

from bot.broker import Broker, MockBroker, is_crypto_symbol
from bot.models import Direction, OrderProposal


def _proposal(
    symbol: str = "SPY",
    direction: Direction = Direction.BUY,
    qty: float = 10,
    rationale: str = "Mock order used to exercise the broker interface.",
    limit_price: float = 580.00,
    stop_loss_price: float | None = None,
    take_profit_price: float | EllipsisType | None = ...,
    trail_percent: float | None = None,
) -> OrderProposal:
    """`take_profit_price=None` means NO TARGET; omitting it takes the default.

    A sentinel rather than `None` for "unspecified", because the whole point of
    the change under test is that `None` is now a meaningful value. A helper
    that quietly substituted its default for it would make every no-target test
    pass for the wrong reason — which is exactly what happened first time.
    """
    if direction == Direction.BUY:
        sl, tp = 575.00, 590.00
    else:
        sl, tp = 585.00, 570.00
    return OrderProposal(
        symbol=symbol,
        direction=direction,
        qty=qty,
        limit_price=limit_price,
        stop_loss_price=stop_loss_price if stop_loss_price is not None else sl,
        take_profit_price=tp if take_profit_price is ... else take_profit_price,
        trail_percent=trail_percent,
        rationale=rationale,
    )


def test_mock_broker_satisfies_protocol():
    assert isinstance(MockBroker(), Broker)


@pytest.mark.parametrize(
    "symbol, expected",
    [("BTC/USD", True), ("ETH/USD", True), ("SPY", False), ("AAPL", False)],
)
def test_crypto_symbol_detection(symbol, expected):
    assert is_crypto_symbol(symbol) is expected


def test_place_order_requires_seeded_price():
    broker = MockBroker()
    broker.connect()
    result = broker.place_order(_proposal(rationale="No price seeded; should fail cleanly."))
    assert not result.accepted
    assert "SPY" in (result.error or "")


def test_round_trip_order_lifecycle():
    broker = MockBroker(starting_equity=100_000)
    broker.connect()
    broker.set_price("SPY", bid=579.98, ask=580.02)

    placed = broker.place_order(_proposal())
    assert placed.accepted
    assert placed.order_id is not None
    assert placed.filled_price == 580.02  # buys fill at the ask

    account = broker.get_account()
    assert len(account.open_positions) == 1
    assert account.open_positions[0].symbol == "SPY"

    closed = broker.close_position("SPY")
    assert closed.accepted
    assert broker.get_account().open_positions == []


def test_close_unknown_symbol_fails():
    broker = MockBroker()
    broker.connect()
    result = broker.close_position("NOPE")
    assert not result.accepted
    assert "NOPE" in (result.error or "")


def test_orders_blocked_when_disconnected():
    broker = MockBroker()
    broker.set_price("SPY", bid=579.98, ask=580.02)
    result = broker.place_order(_proposal(rationale="Disconnected; should be rejected."))
    assert not result.accepted
    assert "not connected" in (result.error or "")


@pytest.mark.parametrize(
    "direction, expected_fill",
    [(Direction.BUY, 580.02), (Direction.SELL, 579.98)],
)
def test_fill_side_correct(direction, expected_fill):
    broker = MockBroker()
    broker.connect()
    broker.set_price("SPY", bid=579.98, ask=580.02)
    result = broker.place_order(_proposal(direction=direction))
    assert result.accepted
    assert result.filled_price == expected_fill


def test_cash_moves_with_position():
    broker = MockBroker(starting_equity=100_000)
    broker.connect()
    broker.set_price("SPY", bid=579.98, ask=580.02)

    broker.place_order(_proposal(qty=10))
    after_buy = broker.get_account()
    assert after_buy.cash_usd == pytest.approx(100_000 - 10 * 580.02)

    broker.close_position("SPY")
    after_close = broker.get_account()
    assert after_close.cash_usd == pytest.approx(100_000 - 10 * 580.02 + 10 * 579.98)


def test_activity_tracks_fills():
    broker = MockBroker()
    broker.connect()
    broker.set_price("SPY", bid=579.98, ask=580.02)

    assert broker.get_activity().trades_today == 0

    broker.place_order(_proposal())
    activity = broker.get_activity()
    assert activity.trades_today == 1
    assert activity.trades_this_week == 1
    assert "SPY" in activity.last_trade_at_by_symbol


# --------------------------------------------------- the stop reaches Alpaca


class _CapturingTrading:
    """Stands in for the Alpaca SDK's TradingClient, recording what it was sent.

    Hand-rolled rather than mocked so the assertions are about the REQUEST
    OBJECT the SDK would receive — which is the thing that was wrong before,
    and which a mock returning a canned response would not have shown.
    """

    def __init__(self) -> None:
        # `Any`, because the assertions read fields off the SDK's request
        # models and typing them as `object` would hide exactly what is being
        # checked behind an ignore on every line.
        self.requests: list[Any] = []

    def submit_order(self, request: Any) -> Any:
        self.requests.append(request)

        class _Order:
            id = "order-1"
            filled_avg_price = None
            filled_qty = None

        return _Order()


def _alpaca_with(
    trading: Any, *, rules: Any = None, now: datetime | None = None
) -> Any:
    """An AlpacaBroker with its SDK clients replaced. Built without __init__ so
    no credentials are needed and nothing reaches the network.

    `rules` defaults to `None`, which is what `main.build_broker` constructs
    today and is the second lock on the out-of-hours fill path: with no rules
    there is no instrument class to consult. `now` pins the moment the
    out-of-hours decision is made, so 04:30 New York is reachable from a test
    without waiting until 04:30 New York.
    """
    from bot.broker import AlpacaBroker

    broker: Any = AlpacaBroker.__new__(AlpacaBroker)
    broker._trading = trading
    broker._connected = True
    broker._orders_degraded = False
    broker._rules = rules
    if now is not None:
        broker._entry_moment = lambda: now
    return broker


def test_an_equity_order_sends_the_stop_to_the_broker():
    """The gap this closes: `stop_loss_price` was validated by the gate, used to
    size the position, written to the journal — and never sent to Alpaca.

    The operator's third rule is "hard stops on every trade". It was true at
    sizing time and false at the broker: nothing was resting there that would
    have closed a losing position, ever.
    """
    from alpaca.trading.enums import OrderClass, TimeInForce

    trading = _CapturingTrading()
    result = _alpaca_with(trading).place_order(
        _proposal(symbol="SPY", stop_loss_price=575.0, take_profit_price=590.0)
    )

    assert result.accepted
    request = trading.requests[0]
    assert request.order_class == OrderClass.BRACKET
    assert request.stop_loss.stop_price == 575.0
    assert request.take_profit.limit_price == 590.0

    # GTC, not DAY, and that is the whole point: a DAY bracket's legs expire
    # with the session, so a position held overnight would sit unprotected from
    # 16:00 until somebody noticed.
    assert request.time_in_force == TimeInForce.GTC


def test_a_crypto_order_stays_a_plain_limit():
    """Alpaca does not accept bracket orders on crypto, so sending one would be
    rejected outright and the entry would not happen at all. The loop's stop
    monitor is what covers a crypto position instead."""
    from alpaca.trading.enums import OrderClass, TimeInForce

    trading = _CapturingTrading()
    _alpaca_with(trading).place_order(
        _proposal(symbol="BTC/USD", limit_price=60_000.0,
                  stop_loss_price=57_000.0, take_profit_price=66_000.0)
    )

    request = trading.requests[0]
    assert request.order_class in (None, OrderClass.SIMPLE)
    assert request.stop_loss is None
    assert request.time_in_force == TimeInForce.GTC


def test_a_failed_submit_is_a_rejection_not_an_exception():
    """Same on both paths. A broker refusal must reach the caller as a result
    rather than propagate and end whatever was running."""
    class _Failing(_CapturingTrading):
        def submit_order(self, request: Any) -> Any:
            raise RuntimeError("422 unprocessable")

    result = _alpaca_with(_Failing()).place_order(_proposal(symbol="SPY"))

    assert not result.accepted
    assert "422" in (result.error or "")


# ------------------------------------------------------- moving a resting stop


def test_replacing_a_stop_sends_only_the_new_trigger():
    """One server-side replace, carrying nothing but the stop price.

    Cancel-then-place is the same thing with a WINDOW IN IT — between the two
    calls the position has no stop at all, and a failure on the second leaves it
    that way. That window is exactly what the operator's third rule exists to
    close.

    `qty` and `limit_price` are deliberately unset so Alpaca carries them over.
    A quantity travelling silently with a stop move would be a second,
    unrecorded change to the position.
    """
    class _Replacing:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        def replace_order_by_id(self, order_id: Any, order_data: Any) -> Any:
            self.calls.append((order_id, order_data))

            class _Order:
                id = "replacement-order-id"

            return _Order()

    trading = _Replacing()
    result = _alpaca_with(trading).replace_stop("old-leg", stop_price=800.0)

    assert result.accepted
    # The replacement is a NEW order. Anything holding the old id is holding a
    # reference to something Alpaca has cancelled.
    assert result.order_id == "replacement-order-id"

    order_id, request = trading.calls[0]
    assert order_id == "old-leg"
    assert request.stop_price == 800.0
    assert request.qty is None
    assert request.limit_price is None


def test_a_failed_replace_is_a_rejection_and_says_the_old_stop_is_still_there():
    """The failure direction that matters: the position keeps the stop it had,
    and the caller must be told so rather than left to assume."""
    class _Failing:
        def replace_order_by_id(self, order_id: Any, order_data: Any) -> Any:
            raise RuntimeError("422 order is not replaceable")

    result = _alpaca_with(_Failing()).replace_stop("old-leg", stop_price=800.0)

    assert not result.accepted
    assert "422" in (result.error or "")
    assert "still resting" in (result.error or "")


def test_the_mock_replaces_rather_than_edits_in_place():
    """A double that SUCCEEDS differently from the thing it doubles pins a path
    production never takes, exactly as one that fails differently does. Alpaca
    issues a new order id on a replace, so the mock must too."""
    from bot.broker import MockBroker
    from bot.models import Direction, WorkingOrder

    broker = MockBroker()
    broker.set_open_orders(
        [
            WorkingOrder(
                order_id="leg-1",
                symbol="SPY",
                direction=Direction.BUY,
                qty=21,
                stop_price=820.0,
                order_type="stop",
            )
        ]
    )

    result = broker.replace_stop("leg-1", stop_price=800.0)
    assert result.accepted

    resting = broker.get_open_orders()
    assert len(resting) == 1
    assert resting[0].stop_price == 800.0
    assert resting[0].order_id != "leg-1"
    assert resting[0].order_id == result.order_id

    # And the old id is gone, so a second replace against it cannot succeed.
    assert not broker.replace_stop("leg-1", stop_price=790.0).accepted


# ------------------------------------------------- the half-price quote bug


def test_a_one_sided_quote_is_refused_rather_than_halved():
    """`mid` is `(bid + ask) / 2`, so a missing side returns HALF the real
    price — and half a price looks exactly like a price.

    Observed live against Alpaca's free IEX feed in the pre-market: SPY came
    back `bid=0, ask=771.64`, so `mid` was 385.82 against a true 773.26.
    Nothing raised. That figure fed position sizing, the limit price, the
    gate's own sanity check, the tape and unrealised P&L — all agreeing with
    each other while being twice out.

    802 tests were green over it, because `MockBroker` always seeds both sides.
    """
    from datetime import UTC, datetime

    import pytest

    from bot.models import Tick

    with pytest.raises(ValueError, match="one-sided quote"):
        Tick(symbol="SPY", bid=0.0, ask=771.64, timestamp=datetime.now(UTC))

    # And the mirror case, which halves just as silently.
    with pytest.raises(ValueError, match="one-sided quote"):
        Tick(symbol="SPY", bid=771.64, ask=0.0, timestamp=datetime.now(UTC))


def test_a_two_sided_quote_still_works_and_mids_correctly():
    from datetime import UTC, datetime

    from bot.models import Tick

    tick = Tick(symbol="SPY", bid=773.60, ask=773.98, timestamp=datetime.now(UTC))

    assert tick.mid == pytest.approx(773.79)
    assert tick.spread == pytest.approx(0.38)


def test_an_unusable_quote_reaches_callers_as_a_no_quote_runtime_error():
    """`check_order` catches `(KeyError, RuntimeError)` for "no market price".
    A raw ValidationError would sail past that and crash the tool, turning a
    thin pre-market book into an outage rather than a named missing symbol."""
    import pytest

    class _OneSided:
        bid_price = 0.0
        ask_price = 771.64
        timestamp = None

        def get(self, symbol: str) -> object:
            return self

    broker = _alpaca_with(_CapturingTrading())
    broker._stock_data = type(
        "_D", (), {"get_stock_latest_quote": lambda self, req: _OneSided()}
    )()

    with pytest.raises(RuntimeError, match="Unusable quote"):
        broker.get_tick("SPY")


# --------------------------------------------------- no target is a normal trade


def test_no_take_profit_sends_an_oto_with_the_stop_intact():
    """A trade with no target is a normal trade.

    The operator's rules require a hard stop and have never required an exit.
    `take_profit_price` used to be mandatory, so whoever built a proposal had to
    invent a level to satisfy the validator — survivable while the field was
    only journalled, and not survivable once entries became GTC brackets, where
    an invented target is a live OCO leg resting at the broker at a price
    nobody chose.
    """
    from alpaca.trading.enums import OrderClass, TimeInForce

    trading = _CapturingTrading()
    result = _alpaca_with(trading).place_order(
        _proposal(symbol="SPY", direction=Direction.SELL, limit_price=773.79,
                  stop_loss_price=820.0, take_profit_price=None)
    )

    assert result.accepted
    request = trading.requests[0]
    assert request.order_class == OrderClass.OTO
    assert request.take_profit is None
    # The half that must never go missing.
    assert request.stop_loss.stop_price == 820.0
    assert request.time_in_force == TimeInForce.GTC


def test_a_target_still_produces_a_bracket():
    """The order class follows the proposal rather than the proposal being bent
    to fit one order class."""
    from alpaca.trading.enums import OrderClass

    trading = _CapturingTrading()
    _alpaca_with(trading).place_order(
        _proposal(symbol="SPY", stop_loss_price=575.0, take_profit_price=590.0)
    )

    request = trading.requests[0]
    assert request.order_class == OrderClass.BRACKET
    assert request.take_profit.limit_price == 590.0
    assert request.stop_loss.stop_price == 575.0


# ------------------------------------------- a trail is NOT a leg at the broker
#
# The constraint that shapes all of this, and it is the "a broker-side stop OR
# an out-of-hours fill, never both" trade in a second place: **Alpaca accepts no
# trailing leg on an entry.** `StopLossRequest` — the only stop a bracket or an
# OTO can carry — is a trigger and an optional limit, and a trailing stop is a
# separate order TYPE that can only be submitted against a position that already
# exists.
#
# So a trailing proposal rests a FIXED stop at the initial level. That is the
# safe half rather than a compromise: the operator's third rule is true from the
# first instant, at the level the position was sized against, and a trail can
# only tighten from there. What must not happen is the system claiming a trail
# it is not holding.


def test_a_trailing_exit_still_rests_a_fixed_stop_at_the_initial_level():
    """The guarantee. A trail must not cost the trade its broker-side stop.

    The dangerous version of this change would submit a plain limit order and
    arrange the trail afterwards — which is rule 3 gone for as long as it takes
    somebody to notice the second step never ran.
    """
    from alpaca.trading.enums import OrderClass, TimeInForce

    from bot.models import StopAtBroker

    trading = _CapturingTrading()
    result = _alpaca_with(trading).place_order(
        _proposal(
            symbol="SPY",
            stop_loss_price=575.0,
            take_profit_price=None,
            trail_percent=1.5,
        )
    )

    assert result.accepted
    request = trading.requests[0]
    assert request.order_class == OrderClass.OTO
    assert request.stop_loss.stop_price == 575.0
    assert request.time_in_force == TimeInForce.GTC
    # Whatever else changed, this did not: the leg is a fixed trigger.
    assert getattr(request.stop_loss, "trail_percent", None) is None
    assert result.stop_at_broker is StopAtBroker.FIXED


def test_a_trailing_exit_is_not_reported_as_a_trailing_leg():
    """The half that would be a lie. `stop_at_broker` says what is RESTING.

    A caller reading FIXED here knows the trail is still its own problem. One
    reading TRAILING would believe the broker was moving the stop, and would be
    wrong on every cycle until somebody checked the account by hand.
    """
    from bot.models import StopAtBroker

    trading = _CapturingTrading()
    result = _alpaca_with(trading).place_order(
        _proposal(symbol="SPY", trail_percent=0.75)
    )

    assert result.stop_at_broker is not StopAtBroker.TRAILING


def test_a_crypto_trailing_exit_degrades_the_way_the_crypto_stop_already_does():
    """Crypto takes no bracket at Alpaca, so there is no leg for a trail to be
    attached to and no leg for the STOP either. Both are journal figures watched
    by `stop_watch`, and the result says so rather than looking identical to a
    bracketed equity entry."""
    from alpaca.trading.enums import OrderClass

    from bot.models import StopAtBroker

    trading = _CapturingTrading()
    result = _alpaca_with(trading).place_order(
        _proposal(
            symbol="BTC/USD",
            limit_price=60_000.0,
            stop_loss_price=57_000.0,
            take_profit_price=None,
            trail_percent=3.0,
        )
    )

    request = trading.requests[0]
    assert request.order_class in (None, OrderClass.SIMPLE)
    assert request.stop_loss is None
    assert result.stop_at_broker is StopAtBroker.ABSENT


def test_a_refused_submission_claims_no_protection_at_all():
    """UNSTATED, not ABSENT. Nothing was submitted, so there is no position for
    a claim about protection to be about — the same distinction as `has_cycles`
    and first-visit, arriving on the order path."""
    from bot.models import StopAtBroker

    class _Failing(_CapturingTrading):
        def submit_order(self, request: Any) -> Any:
            raise RuntimeError("422 unprocessable")

    result = _alpaca_with(_Failing()).place_order(
        _proposal(symbol="SPY", trail_percent=2.0)
    )

    assert not result.accepted
    assert result.stop_at_broker is StopAtBroker.UNSTATED


def test_the_mock_reports_the_same_routing_consequence_as_alpaca():
    """A double that answered this differently would pin a path production never
    takes — the `orders_degraded` trap, which was found the same way.

    The mock holds no order objects, so what it is faithful about is the
    ROUTING: an equity entry leaves a stop at the broker and a crypto one does
    not, and that follows from the symbol rather than from anything in memory.
    """
    from bot.models import StopAtBroker

    broker = MockBroker()
    broker.connect()
    broker.set_price("SPY", 579.98, 580.02)
    broker.set_price("BTC/USD", 64_990.0, 65_010.0)

    equity = broker.place_order(_proposal(symbol="SPY", trail_percent=1.0))
    crypto = broker.place_order(
        _proposal(
            symbol="BTC/USD",
            limit_price=65_000.0,
            stop_loss_price=63_000.0,
            take_profit_price=None,
        )
    )

    assert equity.stop_at_broker is StopAtBroker.FIXED
    assert crypto.stop_at_broker is StopAtBroker.ABSENT
    # And it does not invent a resting leg it has no parent for.
    assert broker.get_open_orders() == []


# ------------------------------------------------------------- the broker clock


def test_a_mock_broker_reports_no_clock_rather_than_a_cheerful_open():
    """`None` means "could not ask", and a broker with no calendar behind it
    genuinely cannot answer. Defaulting to open would be an invented fact about
    a market, which is the one thing this repository refuses."""
    assert MockBroker().get_clock() is None


def test_a_clock_that_cannot_be_read_returns_none_instead_of_raising():
    """Same rule as `fetch_market_ticks`: this feeds a context block, and an SDK
    error escaping it would end the decision loop over a nicety. The caller says
    "not read this cycle" in the rendered text, so the failure is visible rather
    than silent."""

    class _Broken:
        def get_clock(self) -> Any:
            raise RuntimeError("alpaca is having a moment")

    assert _alpaca_with(_Broken()).get_clock() is None


def test_the_clock_comes_back_in_utc():
    """Alpaca stamps these in New York time, and this is NOT about arithmetic.

    An aware datetime compares and subtracts correctly whatever its offset, so
    leaving Alpaca's own tzinfo on would give the right answer to every
    comparison — the first version of this test asserted equality and passed
    with the conversion deleted, which is the trap worth recording.

    What the conversion buys is the rendered string. `_broker_disagreement`
    prints the next open into the model's context with `.isoformat()`, and every
    other timestamp in that document is UTC. One line reading `-04:00` among
    them is a figure a reader has to convert in their head, which is how a
    four-hour error gets made.
    """
    from datetime import timedelta, timezone

    ny = timezone(timedelta(hours=-4))

    class _Clock:
        def get_clock(self) -> Any:
            class _C:
                is_open = True
                next_open = datetime(2026, 8, 11, 9, 30, tzinfo=ny)
                next_close = datetime(2026, 8, 10, 16, 0, tzinfo=ny)

            return _C()

    clock = _alpaca_with(_Clock()).get_clock()

    assert clock is not None
    assert clock.is_open is True
    assert clock.next_open.utcoffset() == timedelta(0)
    assert clock.next_close.utcoffset() == timedelta(0)
    assert clock.next_open.isoformat(timespec="minutes") == "2026-08-11T13:30+00:00"


# --------------------------------------------------------------- resting stops
#
# `WorkingOrder` carried `limit_price` and nothing else, so a stop leg resting
# at the broker rendered as `limit_price=None` and no surface in this repository
# could state the level it fires at. That was survivable while nothing sent a
# stop to Alpaca. It stopped being survivable when entries became brackets and
# OTOs: the stop leg IS what the operator's third rule depends on, and the
# journal's `planned_stop` and the broker's real trigger are two different
# facts.
#
# The fixture below is the shape of the live position deliberately — a BUY 21
# SPY stop at 820 protecting a short — so these tests read against the thing
# that exposed the gap.


class _ListingTrading:
    """Stands in for the SDK's TradingClient for `get_orders` only."""

    def __init__(self, orders: list[Any]) -> None:
        self._orders = orders

    def get_orders(self, request: Any) -> Any:
        return self._orders


class _RawOrder:
    """One order as the Alpaca SDK hands it over."""

    def __init__(self, **fields: Any) -> None:
        self.id = "952237ac-d7ec-426e-bb5f-5c6ce7294260"
        self.symbol = "SPY"
        self.side = "buy"
        self.qty = 21
        self.limit_price = None
        self.stop_price = None
        self.order_type = "limit"
        self.status = "new"
        self.submitted_at = None
        self.filled_qty = 0
        for key, value in fields.items():
            setattr(self, key, value)


def test_a_resting_stop_reports_the_level_it_will_trigger_at():
    """The whole point: the leg's trigger is READ BACK, never assumed."""
    trading = _ListingTrading([_RawOrder(order_type="stop", stop_price="820.00")])

    orders = _alpaca_with(trading).get_open_orders()

    assert len(orders) == 1
    assert orders[0].stop_price == 820.00
    assert orders[0].is_stop is True
    assert orders[0].trigger_price_unknown is False


def test_a_limit_order_has_no_stop_and_that_is_not_an_unknown():
    """`stop_price is None` on a limit order is correct and uninteresting.

    The distinction this pins is the reason `order_type` is carried at all: a
    renderer checking only `stop_price is None` would print the same blank here
    as it would for a stop leg whose level could not be read, and only one of
    those is a problem.
    """
    trading = _ListingTrading([_RawOrder(order_type="limit", limit_price="772.84")])

    order = _alpaca_with(trading).get_open_orders()[0]

    assert order.limit_price == 772.84
    assert order.stop_price is None
    assert order.is_stop is False
    assert order.trigger_price_unknown is False


def test_a_stop_leg_with_no_reported_trigger_is_UNKNOWN_not_absent():
    """The state that must be shown loudly rather than rendered as a blank.

    A stop leg protecting a live position whose level the broker did not report
    is most of the way to having no stop, and it must not be indistinguishable
    from an ordinary limit order with nothing to report.
    """
    trading = _ListingTrading([_RawOrder(order_type="stop", stop_price=None)])

    order = _alpaca_with(trading).get_open_orders()[0]

    assert order.stop_price is None
    assert order.is_stop is True
    assert order.trigger_price_unknown is True


def test_an_enum_order_type_is_normalised_rather_than_stringified():
    """`str(OrderType.STOP_LIMIT)` is "OrderType.STOP_LIMIT" on some SDK
    versions, which would fail every equality check written against it. Take
    `.value` when there is one."""

    class _OrderType:
        value = "stop_limit"

    trading = _ListingTrading(
        [_RawOrder(order_type=_OrderType(), stop_price="820.00", limit_price="819.50")]
    )

    order = _alpaca_with(trading).get_open_orders()[0]

    assert order.order_type == "stop_limit"
    assert order.is_stop is True
    assert order.stop_price == 820.00
    assert order.limit_price == 819.50


def test_an_order_type_this_code_has_never_heard_of_still_travels():
    """Raw string rather than an enum, so a new broker order type is displayed
    rather than coerced into the nearest member this code happens to know."""
    trading = _ListingTrading([_RawOrder(order_type="trailing_stop", stop_price="815.00")])

    order = _alpaca_with(trading).get_open_orders()[0]

    assert order.order_type == "trailing_stop"
    assert order.is_stop is True
    assert order.stop_price == 815.00


def test_a_trailing_leg_reads_back_its_trail_and_its_high_water_mark():
    """`stop_price` alone would make a trailing leg look like a fixed stop that
    is quietly moving on its own.

    The level IS correct — it is where the broker has trailed to — but it is a
    reading rather than a level anybody chose, and the same rule applies as to a
    timestamp on the Board: a figure that will be a different number tomorrow
    has to arrive with what determines it. Alpaca reports the trail and the
    high-water mark; without them nothing in this repository could say where the
    stop goes next.
    """
    trading = _ListingTrading(
        [
            _RawOrder(
                order_type="trailing_stop",
                stop_price="815.00",
                trail_percent="2.5",
                hwm="795.12",
            )
        ]
    )

    order = _alpaca_with(trading).get_open_orders()[0]

    assert order.is_stop is True
    assert order.is_trailing is True
    assert order.stop_price == 815.00
    assert order.trail_percent == 2.5
    assert order.high_water_mark == 795.12
    assert order.trail_is_unreadable is False
    assert order.trigger_price_unknown is False


def test_an_absolute_trail_reads_back_too():
    """Alpaca sets `trail_percent` OR `trail_price`, never both. A reader that
    only knew about one would report a perfectly well-described leg as
    undescribed."""
    trading = _ListingTrading(
        [_RawOrder(order_type="trailing_stop", stop_price="815.00", trail_price="5.00")]
    )

    order = _alpaca_with(trading).get_open_orders()[0]

    assert order.trail_price == 5.00
    assert order.trail_percent is None
    assert order.trail_is_unreadable is False


def test_a_trailing_leg_with_no_trail_reported_says_so():
    """`trigger_price_unknown` one level up.

    The current trigger can be read and nothing whatever can be said about where
    it goes next — so the level on screen is a stop that moves for reasons
    nobody can state. That is a different fact from a fixed stop at 815 and must
    not render as one.
    """
    trading = _ListingTrading([_RawOrder(order_type="trailing_stop", stop_price="815.00")])

    order = _alpaca_with(trading).get_open_orders()[0]

    assert order.trail_is_unreadable is True
    # And the trigger itself is perfectly readable, which is why this needs its
    # own question rather than being folded into the existing one.
    assert order.trigger_price_unknown is False


def test_a_fixed_stop_is_not_reported_as_an_unreadable_trail():
    """The absent-versus-unknown rule, on the new field. A plain stop has no
    trail and that is correct and dull, exactly as `stop_price is None` is on a
    limit order."""
    trading = _ListingTrading([_RawOrder(order_type="stop", stop_price="820.00")])

    order = _alpaca_with(trading).get_open_orders()[0]

    assert order.is_trailing is False
    assert order.trail_percent is None
    assert order.trail_is_unreadable is False


# ------------------------------------------- filling an entry OUT OF HOURS
#
# The fork nobody was offered: 21 SPY submitted 09:23:47 New York came back
# `filled_qty=0.0`, rested through the pre-market and filled after 09:30. The
# code cannot satisfy "place the trade now" and "get a position on now" at once,
# because an entry carrying a stop is a bracket or an OTO and Alpaca accepts
# `extended_hours` on neither.
#
# Everything below is about the switch being OFF being the state that holds, and
# about the ON state surrendering exactly one thing and saying so.


def _rules(
    *,
    allow: bool = False,
    enabled: bool = True,
    also_listed_under_equity: str | None = None,
) -> Any:
    """The shipped rules with one field moved, so a test cannot pass because of
    a fixture that looks nothing like production.

    `Rules.load` builds a fresh object per call, so mutating one leaks nowhere.
    """
    from bot.config import Rules

    from .conftest import RULES_PATH

    rules = Rules.load(RULES_PATH)
    equity = rules.instruments["us_equity"]
    equity.allow_extended_hours_fills = allow
    equity.enabled = enabled
    if also_listed_under_equity is not None:
        equity.allowed_symbols = [*equity.allowed_symbols, also_listed_under_equity]
    return rules


def _ny(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """A New York wall-clock moment, as UTC.

    Written this way rather than as a UTC hour because the whole point is that
    the decision follows New York and therefore daylight saving. A fixed UTC
    hour in a test would pass in July and quietly mean a different phase in
    January, which is the trap `sessions_utc` already carries.
    """
    from bot.market_clock import NY

    return datetime(year, month, day, hour, minute, tzinfo=NY).astimezone(UTC)


# A Monday in May 2026. 05:00 is the pre-market, 10:00 the regular session,
# 17:00 after hours, 22:00 overnight; the Saturday is dark.
PREMARKET = _ny(2026, 5, 4, 5, 0)
REGULAR = _ny(2026, 5, 4, 10, 0)
AFTER_HOURS = _ny(2026, 5, 4, 17, 0)
OVERNIGHT = _ny(2026, 5, 4, 22, 0)
WEEKEND = _ny(2026, 5, 9, 12, 0)


def test_the_switch_ships_off_and_nothing_has_surrendered_a_stop():
    """The shipped state, asserted rather than assumed.

    `extended_hours_fill_classes` is the readable form of the question "is any
    class trading without a broker-side stop out of hours", and the answer this
    repository ships with is none.
    """
    from bot.config import load_rules

    rules = load_rules()
    assert rules.extended_hours_fill_classes == []
    assert rules.instruments["us_equity"].allow_extended_hours_fills is False


@pytest.mark.parametrize(
    ("moment", "why"),
    [
        (PREMARKET, "pre-market — the window the operator actually asked for"),
        (AFTER_HOURS, "after hours — the other window extended_hours covers"),
        (REGULAR, "the regular session, where a bracket fills anyway"),
        (OVERNIGHT, "overnight, which extended_hours does not reach"),
        (WEEKEND, "the weekend, where nothing trades at all"),
    ],
)
def test_the_path_is_unreachable_while_the_switch_is_off(moment, why):
    """**The load-bearing test.** With the switch off there is no moment, no
    phase and no symbol that reaches the unbracketed path.

    It is parametrized across every phase rather than tested once in the
    pre-market, because the failure being guarded against is a fallback: some
    later branch that reaches this path for a reason other than the operator
    having chosen it.
    """
    from bot.broker import plan_extended_hours_fill

    plan = plan_extended_hours_fill(_rules(allow=False), "SPY", moment)

    assert not plan.engage, why
    # Not opted in either, so nothing is logged and nothing is decided. The
    # third state exists precisely so this reads differently from "considered
    # and declined".
    assert not plan.opted_in
    assert "allow_extended_hours_fills is off" in plan.reason


def test_the_shipped_config_keeps_the_stop_in_the_pre_market():
    """The same property one layer up, through the real order path.

    The pure function above could be right while `place_order` ignored it, so
    this asserts what actually reaches the SDK: a bracket, GTC, with the stop
    attached and no `extended_hours` anywhere on it.
    """
    from alpaca.trading.enums import OrderClass, TimeInForce

    from bot.models import StopAtBroker

    trading = _CapturingTrading()
    result = _alpaca_with(trading, rules=_rules(allow=False), now=PREMARKET).place_order(
        _proposal(symbol="SPY", stop_loss_price=575.0, take_profit_price=590.0)
    )

    request = trading.requests[0]
    assert request.order_class == OrderClass.BRACKET
    assert request.stop_loss.stop_price == 575.0
    assert request.time_in_force == TimeInForce.GTC
    assert not request.extended_hours
    assert result.stop_at_broker is StopAtBroker.FIXED


def test_a_broker_holding_no_rules_can_never_engage():
    """The second lock, and it is the one that holds today.

    `main.build_broker` constructs `AlpacaBroker(env)` with no rules, so even a
    config that switched this on could not reach the path — there is no
    instrument class to consult. A caller has to hand the rules in deliberately.
    """
    from bot.broker import plan_extended_hours_fill

    plan = plan_extended_hours_fill(None, "SPY", PREMARKET)

    assert not plan.engage
    assert not plan.opted_in
    assert "without rules" in plan.reason


@pytest.mark.parametrize(
    ("moment", "engage", "why"),
    [
        (PREMARKET, True, "04:00-09:30 New York is what extended_hours covers"),
        (AFTER_HOURS, True, "16:00-20:00 is the other half of it"),
        (REGULAR, False, "a bracket fills in the regular session with its stop on"),
        (OVERNIGHT, False, "extended_hours does not reach the overnight venue"),
        (WEEKEND, False, "nothing trades, so the stop would be given up for nothing"),
    ],
)
def test_with_the_switch_on_only_the_two_real_windows_engage(moment, engage, why):
    """Engaging outside PRE and POST would surrender the resting stop and buy no
    fill at all, which is strictly worse than doing nothing.

    The regular-session case is the one worth stating: there the bracket is not
    a compromise, it is the better order.
    """
    from bot.broker import plan_extended_hours_fill

    plan = plan_extended_hours_fill(_rules(allow=True), "SPY", moment)

    assert plan.engage is engage, why
    # Opted in either way, so an operator who threw the switch and saw nothing
    # happen is told which of the two it was.
    assert plan.opted_in


@pytest.mark.parametrize(
    ("moment", "season"),
    [
        (_ny(2026, 1, 5, 5, 0), "winter, when 05:00 New York is 10:00 UTC"),
        (_ny(2026, 7, 6, 5, 0), "summer, when 05:00 New York is 09:00 UTC"),
    ],
)
def test_the_window_follows_new_york_rather_than_a_fixed_utc_hour(moment, season):
    """Daylight saving, which is what `sessions_utc` cannot express.

    Both moments are 05:00 New York and they are different UTC hours. A decision
    written against a fixed UTC hour would engage in one season and not the
    other, silently, for six months at a time.
    """
    from bot.broker import plan_extended_hours_fill

    assert plan_extended_hours_fill(_rules(allow=True), "SPY", moment).engage, season


def test_crypto_is_refused_even_with_the_switch_on():
    """A continuous market has no extended hours to fill in, and crypto is
    already unbracketed at Alpaca — so there is nothing to surrender and nothing
    to buy.

    Refused here rather than by a config-load validator, which would deny at the
    least useful moment. The flag is read, the answer is no, and the reason says
    why rather than the setting being ignored in silence.
    """
    from bot.broker import plan_extended_hours_fill

    rules = _rules(allow=True)
    rules.instruments["crypto"].enabled = True
    rules.instruments["crypto"].allowed_symbols = ["BTC/USD"]
    rules.instruments["crypto"].allow_extended_hours_fills = True

    plan = plan_extended_hours_fill(rules, "BTC/USD", PREMARKET)

    assert not plan.engage
    assert plan.opted_in
    assert "crypto" in plan.reason


def test_a_disabled_class_grants_nothing_however_it_is_configured():
    """Same rule as the dream grant's class hard-block: `enabled:` is what makes
    a switched-off class mean off, rather than off unless some other line says
    otherwise."""
    from bot.broker import plan_extended_hours_fill

    plan = plan_extended_hours_fill(
        _rules(allow=True, enabled=False), "SPY", PREMARKET
    )

    assert not plan.engage
    assert not plan.opted_in
    assert "not enabled" in plan.reason


def test_a_symbol_whose_class_cannot_be_determined_is_refused():
    """`true_class_key` answers `""` when the file and the routing rule disagree,
    and an unknown class is an unknown permission. It fails closed, exactly as
    `grants.resolve_granted_symbols` does on the same question.
    """
    from bot.broker import plan_extended_hours_fill

    # Listed under us_equity while `"/" in symbol` routes it to crypto, so the
    # two sources cannot both be right and neither is guessed at.
    rules = _rules(allow=True, also_listed_under_equity="BTC/USD")

    plan = plan_extended_hours_fill(rules, "BTC/USD", PREMARKET)

    assert not plan.engage
    assert not plan.opted_in
    assert "could not be determined" in plan.reason


def test_an_engaged_entry_carries_no_stop_and_reports_ABSENT():
    """What the switch actually costs, asserted as the request that reaches the
    SDK and the result that reaches the caller.

    `ABSENT` is the whole reporting contract: it is the only way anything
    downstream can tell a position with nothing behind it from one with a leg
    resting at the level it was sized against.
    """
    from alpaca.trading.enums import TimeInForce

    from bot.models import StopAtBroker

    trading = _CapturingTrading()
    result = _alpaca_with(trading, rules=_rules(allow=True), now=PREMARKET).place_order(
        _proposal(symbol="SPY", stop_loss_price=575.0, take_profit_price=590.0)
    )

    request = trading.requests[0]
    assert request.extended_hours is True
    # DAY rather than GTC, which inverts the bracket path's reasoning: Alpaca
    # accepts `extended_hours` only on a DAY limit order.
    assert request.time_in_force == TimeInForce.DAY
    assert request.stop_loss is None
    assert request.take_profit is None
    assert request.order_class in (None,)
    assert request.limit_price == 580.00

    assert result.accepted
    assert result.stop_at_broker is StopAtBroker.ABSENT


def test_a_stop_and_extended_hours_never_travel_on_one_request():
    """The documented half of the wall, defended structurally.

    Alpaca's docs say `extended_hours` is not accepted on a bracket or an OTO,
    and **no extended-hours bracket has ever been sent from this codebase to
    watch it be refused**. If Alpaca turns out to DOWNGRADE one rather than
    reject it, the failure is worse than a rejection — a stop silently missing
    with no error — so the two are separate branches and the combination is
    unrepresentable here rather than merely avoided.
    """
    trading = _CapturingTrading()
    broker = _alpaca_with(trading, rules=_rules(allow=True), now=PREMARKET)
    broker.place_order(_proposal(symbol="SPY"))
    broker._entry_moment = lambda: REGULAR
    broker.place_order(_proposal(symbol="SPY"))

    out_of_hours, in_session = trading.requests
    assert out_of_hours.extended_hours and out_of_hours.stop_loss is None
    assert in_session.stop_loss is not None and not in_session.extended_hours
    for request in trading.requests:
        assert not (request.extended_hours and request.stop_loss)


def test_the_mock_reports_the_absent_stop_the_same_way():
    """A double that answered FIXED here would pin a path production never
    takes, which is the `orders_degraded` trap applied to a double that succeeds
    differently rather than one that fails differently."""
    from bot.models import StopAtBroker

    broker = MockBroker(rules=_rules(allow=True))
    broker.connect()
    broker.set_price("SPY", 579.98, 580.02)

    broker.set_entry_moment(REGULAR)
    assert broker.place_order(_proposal()).stop_at_broker is StopAtBroker.FIXED

    broker.set_entry_moment(PREMARKET)
    assert broker.place_order(_proposal()).stop_at_broker is StopAtBroker.ABSENT


def test_a_mock_with_no_rules_behaves_exactly_as_it_always_did():
    """Every existing caller constructs `MockBroker()`, so the addition has to
    be invisible to them."""
    from bot.models import StopAtBroker

    broker = MockBroker()
    broker.connect()
    broker.set_price("SPY", 579.98, 580.02)
    broker.set_entry_moment(PREMARKET)

    assert broker.place_order(_proposal()).stop_at_broker is StopAtBroker.FIXED


def test_the_risk_gate_still_rejects_what_it_always_rejected():
    """**The gate is not part of this and must not become part of it.**

    The stop is still required, still validated and still what sizes the
    position. What the switch changes is only where the stop LIVES after
    approval — the journal rather than the broker — so an oversized proposal is
    refused with the switch on exactly as it is with the switch off.
    """
    from bot.models import AccountSnapshot, Tick, TradingActivity
    from bot.risk import RiskGate

    from .conftest import PAPER_EQUITY

    oversized = _proposal(symbol="SPY", qty=250)   # |580-575| x 250 = $1,250
    account = AccountSnapshot(
        equity_usd=PAPER_EQUITY,
        cash_usd=PAPER_EQUITY,
        buying_power_usd=PAPER_EQUITY,
        open_positions=[],
    )
    tick = Tick(symbol="SPY", bid=579.98, ask=580.02, timestamp=PREMARKET)

    for allow in (False, True):
        gate = RiskGate(
            _rules(allow=allow),
            equity_at_session_start=PAPER_EQUITY,
            now=PREMARKET,
        )
        verdict = gate.evaluate(
            oversized, account=account, tick=tick, activity=TradingActivity()
        )
        assert not verdict.approved
        assert any("per-trade" in reason.lower() for reason in verdict.reasons), (
            verdict.reasons
        )


def test_a_proposal_cannot_ask_for_an_out_of_hours_fill():
    """It is an execution-layer decision from config plus the clock, not a
    property of a trade, and the model must not be able to choose it.

    Asserted as an absence, in the same shape as
    `test_a_dream_cannot_describe_an_order`: no field on `OrderProposal` names
    extended hours, so there is nothing for a model to fill in.
    """
    from bot.models import OrderProposal

    fields = set(OrderProposal.model_fields)
    assert not [f for f in fields if "extended" in f or "hours" in f]
