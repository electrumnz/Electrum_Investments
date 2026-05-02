from __future__ import annotations

import pytest

from bot.broker import Broker, MockBroker
from bot.models import Direction, OrderProposal


def test_mock_broker_satisfies_protocol():
    broker = MockBroker()
    assert isinstance(broker, Broker)


def test_place_order_requires_seeded_price():
    broker = MockBroker()
    broker.connect()
    proposal = OrderProposal(
        symbol="EURUSD",
        direction=Direction.BUY,
        size_lots=0.01,
        stop_loss_price=1.0820,
        take_profit_price=1.0900,
        rationale="No price seeded — should fail cleanly.",
    )
    result = broker.place_order(proposal)
    assert not result.accepted
    assert "EURUSD" in (result.error or "")


def test_round_trip_order_lifecycle():
    broker = MockBroker(starting_equity=10_000)
    broker.connect()
    broker.set_price("EURUSD", bid=1.0850, ask=1.0852)
    proposal = OrderProposal(
        symbol="EURUSD",
        direction=Direction.BUY,
        size_lots=0.01,
        stop_loss_price=1.0820,
        take_profit_price=1.0900,
        rationale="Mock order; round-trip exercise.",
    )
    placed = broker.place_order(proposal)
    assert placed.accepted
    assert placed.ticket is not None
    assert placed.filled_price == 1.0852  # ask for buys

    account = broker.get_account()
    assert len(account.open_positions) == 1

    closed = broker.close_position(placed.ticket)
    assert closed.accepted
    assert broker.get_account().open_positions == []


def test_close_unknown_ticket_fails():
    broker = MockBroker()
    broker.connect()
    result = broker.close_position(ticket=999)
    assert not result.accepted


def test_orders_blocked_when_disconnected():
    broker = MockBroker()
    broker.set_price("EURUSD", bid=1.0850, ask=1.0852)
    proposal = OrderProposal(
        symbol="EURUSD",
        direction=Direction.BUY,
        size_lots=0.01,
        stop_loss_price=1.0820,
        take_profit_price=1.0900,
        rationale="Disconnected; should be rejected.",
    )
    result = broker.place_order(proposal)
    assert not result.accepted
    assert "not connected" in (result.error or "")


@pytest.mark.parametrize(
    "direction, expected_fill",
    [(Direction.BUY, 1.0852), (Direction.SELL, 1.0850)],
)
def test_fill_side_correct(direction, expected_fill):
    broker = MockBroker()
    broker.connect()
    broker.set_price("EURUSD", bid=1.0850, ask=1.0852)

    if direction == Direction.BUY:
        sl, tp = 1.0820, 1.0900
    else:
        sl, tp = 1.0900, 1.0820

    proposal = OrderProposal(
        symbol="EURUSD",
        direction=direction,
        size_lots=0.01,
        stop_loss_price=sl,
        take_profit_price=tp,
        rationale="Side check — buy fills at ask, sell fills at bid.",
    )
    result = broker.place_order(proposal)
    assert result.accepted
    assert result.filled_price == expected_fill
