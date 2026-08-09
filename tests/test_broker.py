from __future__ import annotations

import pytest

from bot.broker import Broker, MockBroker, is_crypto_symbol
from bot.models import Direction, OrderProposal


def _proposal(
    symbol: str = "SPY",
    direction: Direction = Direction.BUY,
    qty: float = 10,
    rationale: str = "Mock order used to exercise the broker interface.",
) -> OrderProposal:
    if direction == Direction.BUY:
        sl, tp = 575.00, 590.00
    else:
        sl, tp = 585.00, 570.00
    return OrderProposal(
        symbol=symbol,
        direction=direction,
        qty=qty,
        limit_price=580.00,
        stop_loss_price=sl,
        take_profit_price=tp,
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
