"""Broker interface plus an MT5 implementation and a MockBroker for tests.

`MetaTrader5` is Windows-only. The MT5Broker module-level import is guarded so
the rest of the codebase (and tests on Linux/Mac) can still load.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .config import Env
from .models import (
    AccountSnapshot,
    Direction,
    OrderProposal,
    OrderResult,
    Position,
    Tick,
)

if TYPE_CHECKING:
    pass


@runtime_checkable
class Broker(Protocol):
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def get_account(self) -> AccountSnapshot: ...
    def get_tick(self, symbol: str) -> Tick: ...
    def place_order(self, proposal: OrderProposal) -> OrderResult: ...
    def close_position(self, ticket: int) -> OrderResult: ...


class MockBroker:
    """In-memory broker for tests and local development on non-Windows machines."""

    def __init__(self, *, starting_equity: float = 5000.0) -> None:
        self._equity = starting_equity
        self._positions: dict[int, Position] = {}
        self._next_ticket = 1
        self._prices: dict[str, Tick] = {}
        self._connected = False

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def set_price(self, symbol: str, bid: float, ask: float) -> None:
        self._prices[symbol] = Tick(
            symbol=symbol, bid=bid, ask=ask, timestamp=datetime.now(UTC)
        )

    def get_tick(self, symbol: str) -> Tick:
        if symbol not in self._prices:
            raise KeyError(f"No price seeded for {symbol}; call set_price first")
        return self._prices[symbol]

    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(
            equity_usd=self._equity,
            balance_usd=self._equity,
            margin_used_usd=0.0,
            free_margin_usd=self._equity,
            open_positions=list(self._positions.values()),
        )

    def place_order(self, proposal: OrderProposal) -> OrderResult:
        if not self._connected:
            return OrderResult(accepted=False, error="not connected")
        try:
            tick = self.get_tick(proposal.symbol)
        except KeyError as e:
            return OrderResult(accepted=False, error=str(e))
        fill_price = tick.ask if proposal.direction == Direction.BUY else tick.bid
        ticket = self._next_ticket
        self._next_ticket += 1
        self._positions[ticket] = Position(
            ticket=ticket,
            symbol=proposal.symbol,
            direction=proposal.direction,
            size_lots=proposal.size_lots,
            open_price=fill_price,
            open_time=datetime.now(UTC),
            stop_loss=proposal.stop_loss_price,
            take_profit=proposal.take_profit_price,
        )
        return OrderResult(accepted=True, ticket=ticket, filled_price=fill_price)

    def close_position(self, ticket: int) -> OrderResult:
        if ticket not in self._positions:
            return OrderResult(accepted=False, error=f"unknown ticket {ticket}")
        del self._positions[ticket]
        return OrderResult(accepted=True, ticket=ticket)


class MT5Broker:
    """Real broker wired to MetaTrader 5 via the official Python package.

    Only usable on Windows where the MetaTrader5 package can install.
    """

    def __init__(self, env: Env) -> None:
        if sys.platform != "win32":
            raise RuntimeError(
                "MT5Broker only runs on Windows. Use MockBroker for local development."
            )
        import MetaTrader5  # noqa: PLC0415 — Windows-only import deliberately deferred

        self._mt5 = MetaTrader5
        self._env = env
        self._connected = False

    def connect(self) -> None:
        if not self._mt5.initialize(
            login=self._env.mt5_login,
            password=self._env.mt5_password,
            server=self._env.mt5_server,
        ):
            error = self._mt5.last_error()
            raise RuntimeError(f"MT5 initialize failed: {error}")
        self._connected = True

    def disconnect(self) -> None:
        if self._connected:
            self._mt5.shutdown()
            self._connected = False

    def get_tick(self, symbol: str) -> Tick:
        info = self._mt5.symbol_info_tick(symbol)
        if info is None:
            raise RuntimeError(f"No tick for {symbol}: {self._mt5.last_error()}")
        return Tick(
            symbol=symbol,
            bid=float(info.bid),
            ask=float(info.ask),
            timestamp=datetime.fromtimestamp(info.time, tz=UTC),
        )

    def get_account(self) -> AccountSnapshot:
        info = self._mt5.account_info()
        if info is None:
            raise RuntimeError(f"account_info failed: {self._mt5.last_error()}")
        positions_raw = self._mt5.positions_get() or []
        positions = [
            Position(
                ticket=p.ticket,
                symbol=p.symbol,
                direction=Direction.BUY if p.type == self._mt5.ORDER_TYPE_BUY else Direction.SELL,
                size_lots=float(p.volume),
                open_price=float(p.price_open),
                open_time=datetime.fromtimestamp(p.time, tz=UTC),
                stop_loss=float(p.sl) if p.sl else None,
                take_profit=float(p.tp) if p.tp else None,
                current_pnl_usd=float(p.profit),
            )
            for p in positions_raw
        ]
        return AccountSnapshot(
            equity_usd=float(info.equity),
            balance_usd=float(info.balance),
            margin_used_usd=float(info.margin),
            free_margin_usd=float(info.margin_free),
            open_positions=positions,
        )

    def place_order(self, proposal: OrderProposal) -> OrderResult:
        if not self._connected:
            return OrderResult(accepted=False, error="not connected")
        tick = self.get_tick(proposal.symbol)
        is_buy = proposal.direction == Direction.BUY
        order_type = self._mt5.ORDER_TYPE_BUY if is_buy else self._mt5.ORDER_TYPE_SELL
        price = tick.ask if is_buy else tick.bid
        request = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "symbol": proposal.symbol,
            "volume": proposal.size_lots,
            "type": order_type,
            "price": price,
            "sl": proposal.stop_loss_price,
            "tp": proposal.take_profit_price,
            "deviation": 20,
            "magic": 24061,
            "comment": "electrum-bot",
            "type_filling": self._mt5.ORDER_FILLING_IOC,
        }
        result = self._mt5.order_send(request)
        if result is None or result.retcode != self._mt5.TRADE_RETCODE_DONE:
            return OrderResult(
                accepted=False,
                error=f"order_send retcode={getattr(result, 'retcode', 'None')}",
            )
        return OrderResult(
            accepted=True,
            ticket=int(result.order),
            filled_price=float(result.price),
        )

    def close_position(self, ticket: int) -> OrderResult:
        positions = self._mt5.positions_get(ticket=ticket)
        if not positions:
            return OrderResult(accepted=False, error=f"unknown ticket {ticket}")
        p = positions[0]
        is_buy = p.type == self._mt5.ORDER_TYPE_BUY
        # Closing a buy = sell at bid; closing a sell = buy at ask.
        close_type = self._mt5.ORDER_TYPE_SELL if is_buy else self._mt5.ORDER_TYPE_BUY
        tick = self.get_tick(p.symbol)
        price = tick.bid if is_buy else tick.ask
        request = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "symbol": p.symbol,
            "volume": float(p.volume),
            "type": close_type,
            "position": ticket,
            "price": price,
            "deviation": 20,
            "magic": 24061,
            "comment": "electrum-bot-close",
            "type_filling": self._mt5.ORDER_FILLING_IOC,
        }
        result = self._mt5.order_send(request)
        if result is None or result.retcode != self._mt5.TRADE_RETCODE_DONE:
            return OrderResult(
                accepted=False,
                error=f"close retcode={getattr(result, 'retcode', 'None')}",
            )
        return OrderResult(accepted=True, ticket=ticket, filled_price=float(result.price))
