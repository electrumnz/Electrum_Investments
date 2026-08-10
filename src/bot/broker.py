"""Broker interface plus an Alpaca implementation and a MockBroker for tests.

`AlpacaBroker` defers its `alpaca` imports until construction so the rest of the
codebase — and the whole test suite — loads without the SDK installed.

Two Alpaca behaviours differ from a typical FX/CFD broker and shape this module:
positions are aggregated to one per symbol rather than one per fill, and orders
are identified by UUID strings rather than integer tickets.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from .config import Env
from .market_clock import BrokerClock, TradingDay
from .models import (
    AccountSnapshot,
    AssetClass,
    Bar,
    Direction,
    OrderProposal,
    OrderResult,
    OrderStatus,
    Position,
    Tick,
    TradingActivity,
    WorkingOrder,
)

if TYPE_CHECKING:
    pass

log = structlog.get_logger()


def _from_ny(naive: datetime) -> datetime:
    """Read a naive Alpaca calendar stamp as New York time and return UTC.

    `.replace(tzinfo=...)` is correct with `zoneinfo` and would not have been
    with `pytz`, which needs `localize` to pick the right offset for the date.
    """
    from .market_clock import NY

    return naive.replace(tzinfo=NY).astimezone(UTC)


def is_crypto_symbol(symbol: str) -> bool:
    """Alpaca writes crypto pairs with a slash (BTC/USD); equities never have one."""
    return "/" in symbol


# Enough daily bars for a 200-day average with room to spare, which is the
# longest period anything in src/bot/strategy.py names. Alpaca charges nothing
# for this data and the request is one call per symbol per cycle.
DEFAULT_BAR_LOOKBACK = 260

# Five-minute bars. Fine enough to tell a close through a level from a wick
# through it, which is the entire question `trend_break` turns on, and coarse
# enough that a session is 78 rows rather than 390.
DEFAULT_INTRADAY_MINUTES = 5

# Roughly two sessions of five-minute bars. Enough for the prior session's high
# and low, which is one of the levels the strategy names, plus today's action.
DEFAULT_INTRADAY_LOOKBACK = 160


@runtime_checkable
class Broker(Protocol):
    @property
    def orders_degraded(self) -> bool:
        """True when the last `get_open_orders` failed and returned nothing.

        Same rule as `FinnhubCalendar.is_degraded`, and it exists for the same
        reason: an empty order list from a refused API call is indistinguishable
        from an account with nothing resting. `get_open_orders` catches its own
        failures — it must, because it feeds a display beside positions and risk
        and an SDK error should not take those down with it — and that catch was
        silently making "we could not ask" look like "there is nothing there".

        A caller that ignores this is asserting there are no working orders,
        which is not what a failed fetch established.
        """
        ...

    def get_clock(self) -> BrokerClock | None:
        """The broker's own reading of whether the regular session is open.

        `None` means the question could not be asked, never that the market is
        shut — same rule as `orders_degraded` and `FinnhubCalendar.is_degraded`.
        `market_clock` computes the phases offline and does it better; this
        exists for the one thing arithmetic cannot know, which is a holiday.
        """
        ...

    def get_calendar(self, start: date, end: date) -> list[TradingDay] | None:
        """Which days trade between `start` and `end`, and until what time.

        `None` means the question could not be asked. It must NOT be an empty
        list on failure: an empty list is a real and different answer — no
        trading days in the range — and conflating them would let an outage
        read as a fortnight-long holiday.
        """
        ...

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def get_account(self) -> AccountSnapshot: ...
    def get_tick(self, symbol: str) -> Tick: ...
    def get_daily_bars(self, symbol: str, lookback: int = ...) -> list[Bar]: ...
    def get_intraday_bars(
        self, symbol: str, minutes: int = ..., lookback: int = ...
    ) -> list[Bar]: ...
    def get_open_orders(self) -> list[WorkingOrder]: ...
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
        self._bars: dict[str, list[Bar]] = {}
        self._intraday: dict[str, list[Bar]] = {}
        self._open_orders: list[WorkingOrder] = []
        self._orders_degraded = False
        self._clock: BrokerClock | None = None
        self._calendar: list[TradingDay] | None = None

    @property
    def orders_degraded(self) -> bool:
        return self._orders_degraded

    def set_orders_degraded(self, degraded: bool) -> None:
        """Test hook. Nothing in memory can fail, so the flag has to be set by
        hand for a caller's degraded path to be exercisable at all."""
        self._orders_degraded = degraded

    def set_clock(self, clock: BrokerClock | None) -> None:
        """Test hook. Defaults to `None`, which is the honest answer for a
        broker with no calendar behind it rather than a cheerful 'open'."""
        self._clock = clock

    def get_clock(self) -> BrokerClock | None:
        return self._clock

    def set_calendar(self, days: list[TradingDay] | None) -> None:
        """Test hook. `None` by default — a broker with no calendar behind it
        cannot answer, and `[]` would claim the range holds no trading days."""
        self._calendar = days

    def get_calendar(self, start: date, end: date) -> list[TradingDay] | None:
        if self._calendar is None:
            return None
        return [d for d in self._calendar if start <= d.date <= end]

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

    def set_bars(self, symbol: str, bars: list[Bar]) -> None:
        """Seed daily history. Oldest first, matching what Alpaca returns."""
        self._bars[symbol] = list(bars)

    def get_daily_bars(self, symbol: str, lookback: int = DEFAULT_BAR_LOOKBACK) -> list[Bar]:
        """Seeded history, or nothing.

        Raises rather than returning `[]` for an unseeded symbol, for the same
        reason `get_tick` does: an empty list is indistinguishable from "this
        symbol genuinely has no history", and the caller needs to be able to
        tell a missing fixture from a missing instrument.
        """
        if symbol not in self._bars:
            raise KeyError(f"No bars seeded for {symbol}; call set_bars first")
        return self._bars[symbol][-lookback:]

    def set_intraday_bars(self, symbol: str, bars: list[Bar]) -> None:
        self._intraday[symbol] = list(bars)

    def get_intraday_bars(
        self,
        symbol: str,
        minutes: int = DEFAULT_INTRADAY_MINUTES,
        lookback: int = DEFAULT_INTRADAY_LOOKBACK,
    ) -> list[Bar]:
        """Seeded intraday history, or nothing. Raises for the same reason.

        `minutes` is accepted and ignored: a fixture is whatever the test seeded,
        and silently resampling it would let a test pass against a bar size it
        never actually provided.
        """
        if symbol not in self._intraday:
            raise KeyError(
                f"No intraday bars seeded for {symbol}; call set_intraday_bars first"
            )
        return self._intraday[symbol][-lookback:]

    def set_open_orders(self, orders: list[WorkingOrder]) -> None:
        """Seed resting orders. The mock fills instantly, so it has none of its own."""
        self._open_orders = list(orders)

    def get_open_orders(self) -> list[WorkingOrder]:
        return list(self._open_orders)

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
        self._orders_degraded = False

    @property
    def orders_degraded(self) -> bool:
        return self._orders_degraded

    def connect(self) -> None:
        # Alpaca is stateless HTTP; "connecting" means proving the keys work.
        account: Any = self._trading.get_account()
        status = getattr(account, "status", None)
        if status != "ACTIVE":
            raise RuntimeError(f"Alpaca account is not ACTIVE (status={status})")
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def get_clock(self) -> BrokerClock | None:
        """Alpaca's calendar-aware reading of the regular session.

        Catches broadly and returns `None`, for the same reason
        `fetch_market_ticks` does: this feeds a context block and a display, and
        an SDK error escaping here would end the decision loop over a nicety.
        `None` says the question could not be asked. It must never be read as
        "the market is shut" — the caller says so in the rendered text.
        """
        try:
            raw: Any = self._trading.get_clock()
            return BrokerClock(
                is_open=bool(raw.is_open),
                next_open=raw.next_open.astimezone(UTC),
                next_close=raw.next_close.astimezone(UTC),
            )
        except Exception as exc:
            log.warning("clock_fetch_failed", error=f"{type(exc).__name__}: {exc}")
            return None

    def get_calendar(self, start: date, end: date) -> list[TradingDay] | None:
        """Alpaca's trading calendar for a date range.

        Deferred import for the same reason the constructor defers its clients:
        the package stays importable without the SDK.

        The open and close come back as NAIVE datetimes on Alpaca's models,
        already carrying the right date but stamped in New York. Attaching UTC
        to them instead would be a four-hour error in summer and five in winter,
        and a silent one — the value stays entirely plausible. So the zone is
        named explicitly and then converted.
        """
        from alpaca.trading.requests import GetCalendarRequest

        try:
            raw: Any = self._trading.get_calendar(
                GetCalendarRequest(start=start, end=end)
            )
        except Exception as exc:
            log.warning(
                "calendar_fetch_failed", error=f"{type(exc).__name__}: {exc}"
            )
            return None

        days: list[TradingDay] = []
        for entry in raw:
            try:
                days.append(
                    TradingDay(
                        date=entry.date,
                        open_utc=_from_ny(entry.open),
                        close_utc=_from_ny(entry.close),
                    )
                )
            except (AttributeError, TypeError, ValueError) as exc:
                # One malformed row must not discard the rest of the year.
                log.warning(
                    "calendar_row_skipped", error=f"{type(exc).__name__}: {exc}"
                )
        return days

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
        try:
            return Tick(
                symbol=symbol,
                bid=float(quote.bid_price),
                ask=float(quote.ask_price),
                timestamp=quote.timestamp,
            )
        except ValueError as exc:
            # `Tick` refuses a one-sided quote — see its docstring for the
            # SPY-at-half-price incident. Re-raised as RuntimeError because
            # that is what every caller already catches for "no usable quote":
            # `check_order` catches `(KeyError, RuntimeError)`, and letting a
            # `ValidationError` past it would turn a thin book into a crashed
            # tool rather than a named missing symbol.
            raise RuntimeError(f"Unusable quote for {symbol}: {exc}") from exc

    def get_daily_bars(self, symbol: str, lookback: int = DEFAULT_BAR_LOOKBACK) -> list[Bar]:
        """Daily bars, oldest first, for the indicators in `src/bot/indicators.py`.

        The calendar window is padded well past `lookback` because weekends and
        holidays are not sessions: asking for 260 calendar days back and
        expecting 260 daily bars leaves the 200-day average permanently short by
        about a third. Crypto trades every day and does not need the padding,
        but the padding costs nothing there either.

        Returns whatever the API gives back rather than raising on a short
        history. `indicators.compute` reports each period it cannot support as
        unavailable, which is the honest handling: a 200-day average over 40
        bars is a 40-day average wearing the wrong label.
        """
        from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        # 1.6x plus a fortnight covers weekends, the nine US market holidays and
        # a long weekend at either end.
        start = datetime.now(UTC) - timedelta(days=int(lookback * 1.6) + 14)

        if is_crypto_symbol(symbol):
            raw: Any = self._crypto_data.get_crypto_bars(
                CryptoBarsRequest(
                    symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start
                )
            )
        else:
            raw = self._stock_data.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start
                )
            )

        rows = raw.data.get(symbol, []) if hasattr(raw, "data") else raw.get(symbol, [])
        bars = [
            Bar(
                symbol=symbol,
                timestamp=row.timestamp,
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=float(row.volume),
            )
            for row in rows
        ]
        return bars[-lookback:]

    def get_intraday_bars(
        self,
        symbol: str,
        minutes: int = DEFAULT_INTRADAY_MINUTES,
        lookback: int = DEFAULT_INTRADAY_LOOKBACK,
    ) -> list[Bar]:
        """Intraday bars, oldest first, so a close through a level beats a wick.

        On a daily bar a close through a level and a wick that closed back
        inside are the same row, and telling those apart is the whole of
        `trend_break`. This is what makes that strategy evaluable.

        Free on Alpaca's IEX feed, same as the daily bars, with the same caveat:
        IEX is a fraction of consolidated volume, so the *shape* of a bar is
        reliable and its volume is a sample rather than the market's. That is
        why `indicators` compares break volume against this feed's own recent
        average rather than against an absolute figure.

        The calendar window is padded past `lookback` the same way
        `get_daily_bars` pads: a session is 78 five-minute bars, so two sessions
        of history spans a weekend more often than not.
        """
        from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        bars_per_session = max(1, (60 // max(1, minutes)) * 7)
        sessions = lookback / bars_per_session
        start = datetime.now(UTC) - timedelta(days=int(sessions * 1.6) + 4)
        timeframe = TimeFrame(amount=minutes, unit=TimeFrameUnit.Minute)

        if is_crypto_symbol(symbol):
            raw: Any = self._crypto_data.get_crypto_bars(
                CryptoBarsRequest(
                    symbol_or_symbols=symbol, timeframe=timeframe, start=start
                )
            )
        else:
            raw = self._stock_data.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=symbol, timeframe=timeframe, start=start
                )
            )

        rows = raw.data.get(symbol, []) if hasattr(raw, "data") else raw.get(symbol, [])
        bars = [
            Bar(
                symbol=symbol,
                timestamp=row.timestamp,
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=float(row.volume),
            )
            for row in rows
        ]
        return bars[-lookback:]

    def get_open_orders(self) -> list[WorkingOrder]:
        """Orders resting at the broker, not yet filled or cancelled.

        The bot submits limit orders only, deliberately, so an order that does
        not reach its price simply sits there. Without this the dashboard shows
        no position and no explanation, when the truth is that an order is
        waiting on a price that has not come.
        """
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        try:
            raw: Any = self._trading.get_orders(
                GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=100)
            )
        except Exception as exc:
            # Same reasoning as the feed fetches: this feeds a display, and an
            # SDK error here must not take down a caller that also renders
            # positions and risk.
            #
            # But an empty list is what an account with nothing resting looks
            # like too, so swallowing the failure quietly turned "we could not
            # ask" into "there is nothing there". The flag is the difference,
            # and it is the `FinnhubCalendar.is_degraded` lesson in a third
            # place: report the weaker fact rather than imply the stronger one.
            self._orders_degraded = True
            log.warning("open_orders_fetch_failed", error=f"{type(exc).__name__}: {exc}")
            return []

        self._orders_degraded = False
        orders: list[WorkingOrder] = []
        for o in raw:
            qty = float(getattr(o, "qty", 0) or 0)
            if qty <= 0:
                continue
            side = str(getattr(o, "side", "buy")).lower()
            limit_price = getattr(o, "limit_price", None)
            # The trigger on a stop or stop-limit leg. Read back rather than
            # assumed: the journal's `planned_stop` says what was intended and
            # this says what is actually resting at the broker, and a stop leg
            # nobody can read the level of is most of the way to no stop.
            stop_price = getattr(o, "stop_price", None)
            # `order_type` on newer SDKs, `type` on older ones. Both are enums
            # that stringify to things like "OrderType.STOP_LIMIT", so take the
            # `.value` when there is one and normalise what is left.
            raw_type = getattr(o, "order_type", None) or getattr(o, "type", None)
            order_type = str(getattr(raw_type, "value", raw_type) or "").lower()
            orders.append(
                WorkingOrder(
                    order_id=str(o.id),
                    symbol=str(o.symbol),
                    direction=Direction.BUY if "buy" in side else Direction.SELL,
                    qty=qty,
                    limit_price=float(limit_price) if limit_price else None,
                    stop_price=float(stop_price) if stop_price else None,
                    order_type=order_type,
                    status=_order_status(str(getattr(o, "status", ""))),
                    submitted_at=getattr(o, "submitted_at", None),
                    filled_qty=float(getattr(o, "filled_qty", 0) or 0),
                )
            )
        return orders

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

        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import (
            LimitOrderRequest,
            StopLossRequest,
            TakeProfitRequest,
        )

        crypto = is_crypto_symbol(proposal.symbol)
        side = OrderSide.BUY if proposal.direction == Direction.BUY else OrderSide.SELL

        if crypto:
            # Crypto trades around the clock and rejects DAY. Alpaca does not
            # accept bracket orders on crypto either, so the stop stays a
            # journal figure here and the loop's monitor is what watches it.
            return self._submit(
                LimitOrderRequest(
                    symbol=proposal.symbol,
                    qty=proposal.qty,
                    side=side,
                    time_in_force=TimeInForce.GTC,
                    limit_price=proposal.limit_price,
                )
            )

        # A BRACKET, so the stop actually rests at the broker.
        #
        # Before this, `stop_loss_price` was validated by the gate, used to size
        # the position, written to the journal — and never sent to Alpaca. The
        # operator's third rule is "hard stops on every trade", and it was true
        # at sizing time and false at the broker: nothing would have closed a
        # losing position, ever, because nothing was resting there to do it.
        #
        # **GTC rather than DAY, and that is the whole point.** A DAY bracket's
        # legs expire with the session, so a position held overnight would sit
        # unprotected from 16:00 until somebody noticed. GTC legs stay active in
        # the system across days, so the stop survives the close, the overnight
        # session, the pre-market and the weekend.
        #
        # What GTC does NOT buy is an out-of-hours exit, and no order type from
        # any broker does. A stop is a trigger that becomes a MARKET order, and
        # extended-hours venues accept limit orders only — so the leg rests
        # through the night and becomes eligible again when the regular session
        # reopens. A gap through the stop therefore fills at the open rather
        # than at the stop price. That is how every retail stop behaves and it
        # is worth knowing rather than being surprised by.
        #
        # The cost of GTC is at the other end: an unfilled ENTRY also rests
        # rather than expiring with the day. That is deliberately visible — an
        # order resting at the broker shows in `get_open_orders` and on the
        # Board's pending panel — rather than silently cancelled.
        # BRACKET when a target was given, OTO when it was not.
        #
        # A trade with no take-profit is a normal trade — the operator's rules
        # require a stop, never an exit — so the order class follows the
        # proposal rather than the proposal being bent to fit one order class.
        # OTO is "one triggers other": the entry fills and the stop becomes
        # active, with no second leg. Both carry the stop, which is the part
        # that must always be there.
        #
        # The alternative, inventing a target to keep the bracket shape, was
        # what this replaces. It was survivable while the field was only
        # journalled; with a real bracket it puts a live OCO leg at the broker
        # at a price nobody chose.
        if proposal.take_profit_price is None:
            exits: dict[str, Any] = {"order_class": OrderClass.OTO}
        else:
            exits = {
                "order_class": OrderClass.BRACKET,
                "take_profit": TakeProfitRequest(
                    limit_price=proposal.take_profit_price
                ),
            }
        return self._submit(
            LimitOrderRequest(
                symbol=proposal.symbol,
                qty=proposal.qty,
                side=side,
                time_in_force=TimeInForce.GTC,
                limit_price=proposal.limit_price,
                stop_loss=StopLossRequest(stop_price=proposal.stop_loss_price),
                **exits,
            )
        )

    def _submit(self, request: Any) -> OrderResult:
        """One place that talks to `submit_order`, so entry and bracket paths
        cannot drift in how they report a failure."""

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


def _order_status(raw: str) -> OrderStatus:
    """Map Alpaca's order status onto ours.

    Alpaca has more statuses than this bot cares about (`pending_new`,
    `accepted`, `done_for_day`, and several stop-related ones). Anything not
    recognised becomes OTHER rather than being guessed at, so a status this
    build has never seen renders as unknown instead of as "new".
    """
    value = raw.lower().strip()
    known = {
        "new": OrderStatus.NEW,
        "accepted": OrderStatus.NEW,
        "pending_new": OrderStatus.NEW,
        "partially_filled": OrderStatus.PARTIALLY_FILLED,
        "filled": OrderStatus.FILLED,
        "canceled": OrderStatus.CANCELED,
        "cancelled": OrderStatus.CANCELED,
        "expired": OrderStatus.EXPIRED,
        "rejected": OrderStatus.REJECTED,
    }
    return known.get(value, OrderStatus.OTHER)
