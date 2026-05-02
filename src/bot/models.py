"""Domain models — orders, positions, market snapshots, Claude decisions."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Direction(StrEnum):
    BUY = "buy"
    SELL = "sell"


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
    ticket: int
    symbol: str
    direction: Direction
    size_lots: float
    open_price: float
    open_time: datetime
    stop_loss: float | None = None
    take_profit: float | None = None
    current_pnl_usd: float = 0.0


class AccountSnapshot(BaseModel):
    equity_usd: float
    balance_usd: float
    margin_used_usd: float
    free_margin_usd: float
    open_positions: list[Position]
    realised_pnl_today_usd: float = 0.0


class OrderProposal(BaseModel):
    """What Claude proposes — must pass the risk gate before execution."""

    symbol: str
    direction: Direction
    size_lots: float = Field(gt=0)
    stop_loss_price: float = Field(gt=0)
    take_profit_price: float = Field(gt=0)
    rationale: str = Field(min_length=10, max_length=500)


class OrderResult(BaseModel):
    accepted: bool
    ticket: int | None = None
    error: str | None = None
    filled_price: float | None = None


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
    proposals: list[OrderProposal]
    verdicts: list[RiskVerdict]
    executed: list[OrderResult]
    claude_input_tokens: int
    claude_output_tokens: int
    claude_cached_tokens: int = 0
    estimated_cost_usd: float = 0.0
    notes: str = ""
