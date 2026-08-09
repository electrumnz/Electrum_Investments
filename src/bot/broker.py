"""Broker interface plus an Alpaca implementation and a MockBroker for tests.

`AlpacaBroker` defers its `alpaca` imports until construction so the rest of the
codebase — and the whole test suite — loads without the SDK installed.

Two Alpaca behaviours differ from a typical FX/CFD broker and shape this module:
positions are aggregated to one per symbol rather than one per fill, and orders
are identified by UUID strings rather than integer tickets.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .config import Env
from .models import (
    AccountSnapshot,
    AssetClass,
    Direction,
    OrderProposal,
    OrderResult,
    Position,
    Tick,
    TradingActivity,
)

if TYPE_CHECKING:
    pass


def is_crypto_symbol(symbol: str) -> bool:
    """Alpaca writes crypto pairs with a slash (BTC/USD); equities never have one."""
    return "/" in symbol


@runtime_checkable
class Broker(Protocol):
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def get_account(self) -> AccountSnapshot: ...
    def get_tick(self, symbol: str) -> Tick: ...
    def get_activity(self) -> TradingActivity: ...
    def place_order(self, proposal: OrderProposal) -> OrderResult: ...
    def close_position(self, symbol: str) -> OrderResult: ...


class MockBroker:
    """In-memory broker for tests and for local development without credentials."""

    def __init__(
        self,
        *,
        starting_equity: float = 100_000.0,
        starting_cash: float | None = None,
    ) -> None:
        self._equity = starting_equity
        self._cash = starting_cash if starting_cash is not None else starting_equity
        self._positions: dict[str, Position] = {}
        self._prices: dict[str, Tick] = {}
        self._connected = False
        self._order_seq = 0
        self._fills: list[tuple[datetime, str]] = []

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
            cash_usd=self._cash,
            buying_power_usd=self._cash,
            open_positions=list(self._positions.values()),
        )

    def get_activity(self) -> TradingActivity:
        now = datetime.now(UTC)
        day_ago = now - timedelta(days=1)
        week_ago = now - timedelta(days=7)
        last_by_symbol: dict[str, datetime] = {}
        for ts, symbol in self._fills:
            last_by_symbol[symbol] = max(last_by_symbol.get(symbol, ts), ts)
        return TradingActivity(
            trades_today=sum(1 for ts, _ in self._fills if ts >= day_ago),
            trades_this_week=sum(1 for ts, _ in self._fills if ts >= week_ago),
            last_trade_at_by_symbol=last_by_symbol,
        )

    def place_order(self, proposal: OrderProposal) -> OrderResult:
        if not self._connected:
            return OrderResult(accepted=False, error="not connected")
        try:
            tick = self.get_tick(proposal.symbol)
        except KeyError as e:
            return OrderResult(accepted=False, error=str(e))

        fill_price = tick.ask if proposal.direction == Direction.BUY else tick.bid
        self._order_seq += 1
        order_id = f"mock-{self._order_seq:06d}"
        now = datetime.now(UTC)

        self._positions[proposal.symbol] = Position(
            symbol=proposal.symbol,
            asset_class=proposal.asset_class,
            direction=proposal.direction,
            qty=proposal.qty,
            entry_price=fill_price,
            opened_at=now,
            current_price=fill_price,
        )
        self._cash -= proposal.qty * fill_price
        self._fills.append((now, proposal.symbol))
        return OrderResult(
            accepted=True,
            order_id=order_id,
            filled_price=fill_price,
            filled_qty=proposal.qty,
        )

    def close_position(self, symbol: str) -> OrderResult:
        position = self._positions.pop(symbol, None)
        if position is None:
            return OrderResult(accepted=False, error=f"no open position in {symbol}")
        try:
            tick = self.get_tick(symbol)
            exit_price = tick.bid if position.direction == Direction.BUY else tick.ask
        except KeyError:
            exit_price = position.entry_price
        self._cash += position.qty * exit_price
        self._order_seq += 1
        return OrderResult(
            accepted=True,
            order_id=f"mock-{self._order_seq:06d}",
            filled_price=exit_price,
            filled_qty=position.qty,
        )


class AlpacaBroker:
    """Real broker wired to Alpaca.

    Paper-only: the constructor calls `Env.assert_paper_only()`, so pointing this
    at a live account fails loudly here as well as at startup.
    """

    def __init__(self, env: Env) -> None:
        env.assert_paper_only()

        # Deferred so the package stays importable without the SDK installed.
        from alpaca.data.historical import (
            CryptoHistoricalDataClient,
            StockHistoricalDataClient,
        )
        from alpaca.trading.client import TradingClient

        if not env.alpaca_api_key or not env.alpaca_secret_key:
            raise RuntimeError(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set. "
                "Create paper-trading keys at https://app.alpaca.markets/paper/dashboard/overview"
            )

        self._env = env
        self._trading = TradingClient(
            api_key=env.alpaca_api_key,
            secret_key=env.alpaca_secret_key,
            paper=True,
        )
        self._stock_data = StockHistoricalDataClient(
            api_key=env.alpaca_api_key, secret_key=env.alpaca_secret_key
        )
        self._crypto_data = CryptoHistoricalDataClient(
            api_key=env.alpaca_api_key, secret_key=env.alpaca_secret_key
        )
        self._connected = False

    def connect(self) -> None:
        # Alpaca is stateless HTTP; "connecting" means proving the keys work.
        account: Any = self._trading.get_account()
        status = getattr(account, "status", None)
        if status != "ACTIVE":
            raise RuntimeError(f"Alpaca account is not ACTIVE (status={status})")
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def get_account(self) -> AccountSnapshot:
        # The SDK's return type is a union with a raw-dict variant; we always get
        # the model back, so treat it as Any rather than narrowing at every field.
        raw: Any = self._trading.get_account()
        return AccountSnapshot(
            equity_usd=float(raw.equity or 0),
            cash_usd=float(raw.cash or 0),
            buying_power_usd=float(raw.buying_power or 0),
            open_positions=self._positions(),
        )

    def _positions(self) -> list[Position]:
        out: list[Position] = []
        raw_positions: Any = self._trading.get_all_positions()
        for p in raw_positions:
            qty = float(p.qty)
            out.append(
                Position(
                    symbol=p.symbol,
                    asset_class=(
                        AssetClass.CRYPTO
                        if is_crypto_symbol(p.symbol)
                        else AssetClass.EQUITY
                    ),
                    # Alpaca signs qty for direction; the model keeps qty positive.
                    direction=Direction.BUY if qty >= 0 else Direction.SELL,
                    qty=abs(qty),
                    entry_price=float(p.avg_entry_price),
                    # Alpaca does not return an open time on positions; the audit
                    # log is the source of truth for when we entered.
                    opened_at=datetime.now(UTC),
                    current_price=float(p.current_price) if p.current_price else None,
                    unrealised_pnl_usd=float(p.unrealized_pl or 0),
                )
            )
        return out

    def get_tick(self, symbol: str) -> Tick:
        if is_crypto_symbol(symbol):
            from alpaca.data.requests import CryptoLatestQuoteRequest

            quotes = self._crypto_data.get_crypto_latest_quote(
                CryptoLatestQuoteRequest(symbol_or_symbols=symbol)
            )
        else:
            from alpaca.data.requests import StockLatestQuoteRequest

            quotes = self._stock_data.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=symbol)
            )

        quote = quotes.get(symbol)
        if quote is None:
            raise RuntimeError(f"No quote returned for {symbol}")
        return Tick(
            symbol=symbol,
            bid=float(quote.bid_price),
            ask=float(quote.ask_price),
            timestamp=quote.timestamp,
        )

    def get_activity(self) -> TradingActivity:
        """Derive recent trade counts from Alpaca's filled-order history."""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        now = datetime.now(UTC)
        week_ago = now - timedelta(days=7)
        day_ago = now - timedelta(days=1)

        raw_orders: Any = self._trading.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=week_ago, limit=500)
        )

        # Only orders that actually filled count as trades; the SDK's return type
        # is loose, so pull the two fields defensively.
        filled: list[tuple[str, datetime]] = []
        for order in raw_orders:
            filled_at = getattr(order, "filled_at", None)
            symbol = getattr(order, "symbol", None)
            if filled_at is None or symbol is None:
                continue
            filled.append((str(symbol), filled_at))

        last_by_symbol: dict[str, datetime] = {}
        for symbol, filled_at in filled:
            prior = last_by_symbol.get(symbol)
            if prior is None or filled_at > prior:
                last_by_symbol[symbol] = filled_at

        return TradingActivity(
            trades_today=sum(1 for _, ts in filled if ts >= day_ago),
            trades_this_week=len(filled),
            last_trade_at_by_symbol=last_by_symbol,
        )

    def place_order(self, proposal: OrderProposal) -> OrderResult:
        """Submit a limit order.

        Limit only, deliberately. Market orders were a documented source of
        slippage loss in LLM trading experiments, and `OrderProposal` has no way
        to express one.
        """
        if not self._connected:
            return OrderResult(accepted=False, error="not connected")

        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        crypto = is_crypto_symbol(proposal.symbol)
        request = LimitOrderRequest(
            symbol=proposal.symbol,
            qty=proposal.qty,
            side=OrderSide.BUY if proposal.direction == Direction.BUY else OrderSide.SELL,
            # Crypto trades around the clock and rejects DAY.
            time_in_force=TimeInForce.GTC if crypto else TimeInForce.DAY,
            limit_price=proposal.limit_price,
        )

        try:
            order: Any = self._trading.submit_order(request)
        except Exception as e:  # surface any SDK/API failure as a rejection
            return OrderResult(accepted=False, error=f"submit_order failed: {e}")

        filled_price = getattr(order, "filled_avg_price", None)
        filled_qty = getattr(order, "filled_qty", None)
        return OrderResult(
            accepted=True,
            order_id=str(order.id),
            filled_price=float(filled_price) if filled_price else None,
            filled_qty=float(filled_qty) if filled_qty else None,
        )

    def close_position(self, symbol: str) -> OrderResult:
        try:
            order: Any = self._trading.close_position(symbol)
        except Exception as e:  # surface any SDK/API failure as a rejection
            return OrderResult(accepted=False, error=f"close_position failed: {e}")

        filled_price = getattr(order, "filled_avg_price", None)
        filled_qty = getattr(order, "filled_qty", None)
        return OrderResult(
            accepted=True,
            order_id=str(order.id),
            filled_price=float(filled_price) if filled_price else None,
            filled_qty=float(filled_qty) if filled_qty else None,
        )
