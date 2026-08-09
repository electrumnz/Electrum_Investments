"""Domain models — orders, positions, market snapshots, Claude decisions.

Terminology note: these models describe an **Alpaca** account (US equities and
crypto), not an FX/CFD account. Quantities are shares or coin units, never
"lots"; order identifiers are Alpaca UUID strings, never integer tickets; and
Alpaca aggregates all exposure to one symbol into a single position, so
positions are keyed by symbol rather than by individual fill.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Direction(StrEnum):
    BUY = "buy"
    SELL = "sell"


class AssetClass(StrEnum):
    """Alpaca asset classes we support.

    The distinction matters for risk: PDT rules bind on equities but not on
    crypto, and crypto trades 24/7 while equities respect the market calendar.
    """

    EQUITY = "us_equity"
    CRYPTO = "crypto"


class Tick(BaseModel):
    symbol: str
    bid: float
    ask: float
    timestamp: datetime

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid


class Bar(BaseModel):
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class Position(BaseModel):
    """An open position. Alpaca reports one aggregated position per symbol."""

    symbol: str
    asset_class: AssetClass = AssetClass.EQUITY
    direction: Direction
    qty: float = Field(gt=0, description="Shares or coin units; always positive")
    entry_price: float
    opened_at: datetime
    current_price: float | None = None
    unrealised_pnl_usd: float = 0.0

    @property
    def notional_usd(self) -> float:
        """Current market value of the position, falling back to entry price."""
        return self.qty * (self.current_price or self.entry_price)


class AccountSnapshot(BaseModel):
    """Account state as reported by Alpaca.

    `daytrade_count` and `pattern_day_trader` come straight from Alpaca rather
    than being recomputed locally — the broker is the authority on what counts
    as a day trade, and getting it wrong risks a 90-day account restriction.
    """

    equity_usd: float
    cash_usd: float
    buying_power_usd: float
    open_positions: list[Position] = Field(default_factory=list)
    realised_pnl_today_usd: float = 0.0
    daytrade_count: int = Field(default=0, ge=0)
    pattern_day_trader: bool = False

    @property
    def gross_exposure_usd(self) -> float:
        return sum(p.notional_usd for p in self.open_positions)

    @property
    def cash_pct(self) -> float:
        if self.equity_usd <= 0:
            return 0.0
        return self.cash_usd / self.equity_usd * 100

    def position_for(self, symbol: str) -> Position | None:
        return next((p for p in self.open_positions if p.symbol == symbol), None)


class TradingActivity(BaseModel):
    """Recent trading history, used by the frequency and cooldown gates.

    Overtrading — not stock picking — was the dominant loss driver in the
    Alpha Arena LLM trading competition, where fees consumed P&L. These counts
    are what let the risk gate enforce a hard ceiling on churn.
    """

    trades_today: int = Field(default=0, ge=0)
    trades_this_week: int = Field(default=0, ge=0)
    last_trade_at_by_symbol: dict[str, datetime] = Field(default_factory=dict)

    def seconds_since_last_trade(self, symbol: str, now: datetime) -> float | None:
        last = self.last_trade_at_by_symbol.get(symbol)
        if last is None:
            return None
        return (now - last).total_seconds()


class OrderProposal(BaseModel):
    """What Claude proposes — must pass the risk gate before execution."""

    symbol: str
    asset_class: AssetClass = AssetClass.EQUITY
    direction: Direction
    qty: float = Field(gt=0, description="Shares or coin units")
    limit_price: float = Field(
        gt=0,
        description="Limit orders only. Market orders are refused — they were a "
        "documented source of slippage loss in LLM trading experiments.",
    )
    stop_loss_price: float = Field(gt=0)
    take_profit_price: float = Field(gt=0)
    rationale: str = Field(min_length=10, max_length=500)

    @property
    def notional_usd(self) -> float:
        return self.qty * self.limit_price


class OrderResult(BaseModel):
    accepted: bool
    order_id: str | None = None
    error: str | None = None
    filled_price: float | None = None
    filled_qty: float | None = None


class RiskVerdict(BaseModel):
    approved: bool
    reasons: list[str] = Field(default_factory=list)

    @classmethod
    def approve(cls) -> RiskVerdict:
        return cls(approved=True)

    @classmethod
    def reject(cls, *reasons: str) -> RiskVerdict:
        return cls(approved=False, reasons=list(reasons))


class Decision(BaseModel):
    """One full pass of the loop — what we asked, what Claude said, what we did."""

    timestamp: datetime
    proposals: list[OrderProposal] = Field(default_factory=list)
    verdicts: list[RiskVerdict] = Field(default_factory=list)
    executed: list[OrderResult] = Field(default_factory=list)
    claude_input_tokens: int = 0
    claude_output_tokens: int = 0
    claude_cached_tokens: int = 0
    estimated_cost_usd: float = 0.0
    notes: str = ""
