from __future__ import annotations

from datetime import datetime
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


def _alpaca_with(trading: Any) -> Any:
    """An AlpacaBroker with its SDK clients replaced. Built without __init__ so
    no credentials are needed and nothing reaches the network."""
    from bot.broker import AlpacaBroker

    broker: Any = AlpacaBroker.__new__(AlpacaBroker)
    broker._trading = trading
    broker._connected = True
    broker._orders_degraded = False
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
