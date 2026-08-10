from __future__ import annotations

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


def _alpaca_with(trading: _CapturingTrading) -> Any:
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
