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

    ETFs are `us_equity` at Alpaca, so they need no separate class. CFDs are
    absent deliberately — they are barred for US residents under CFTC rules,
    which is why this project moved off BlackBull.
    """

    EQUITY = "us_equity"
    CRYPTO = "crypto"


class ExecutionMode(StrEnum):
    """Where an order actually goes.

    A stand-down forces `PAPER` for its duration: the rule is "can't trade
    money, only paper", not "stop trading". Today the whole build is locked to
    PAPER regardless, so this is inert — but the machinery is exercised and
    tested now rather than written in a hurry the day real money is involved.
    """

    PAPER = "paper"
    LIVE = "live"


class TradeOutcome(StrEnum):
    """How a closed trade is classified for the consecutive-loss counter.

    SCRATCH exists so that cutting a trade for a few dollars does not count as
    a loss. A counter that punished scratches would push the bot toward holding
    losers to avoid tripping its own rule, which is exactly backwards.
    """

    WIN = "win"
    LOSS = "loss"
    SCRATCH = "scratch"


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

    @property
    def cost_basis_usd(self) -> float:
        """What the position cost to open, ignoring what it is worth now.

        The total-invested cap is measured on this rather than on market value,
        so a winner drifting upward never retroactively breaches the cap or
        forces a close.
        """
        return self.qty * self.entry_price


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
        """Current market value of all open positions."""
        return sum(p.notional_usd for p in self.open_positions)

    @property
    def total_invested_usd(self) -> float:
        """What all open positions cost to open. Basis for the total-invested cap."""
        return sum(p.cost_basis_usd for p in self.open_positions)

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


class Trade(BaseModel):
    """One trade's full lifecycle, from proposal through to close.

    This is the journal's unit of record. Everything the metrics engine reports
    — R-multiple, expectancy, profit factor, MAE/MFE — is derived from these.
    """

    id: int | None = None

    symbol: str
    asset_class: AssetClass = AssetClass.EQUITY
    strategy: str = "unspecified"
    direction: Direction
    qty: float = Field(gt=0)

    entry_time: datetime
    entry_price: float = Field(gt=0)
    planned_stop: float = Field(gt=0)
    planned_target: float = Field(gt=0)

    exit_time: datetime | None = None
    exit_price: float | None = None

    realised_pnl_usd: float | None = None
    fees_usd: float = 0.0

    # Worst and best unrealised P&L seen while the trade was open, in USD.
    # Sampled at the decision interval rather than from ticks, so both
    # understate the true excursion — see docs.
    mae_usd: float = 0.0
    mfe_usd: float = 0.0

    execution_mode: ExecutionMode = ExecutionMode.PAPER
    rationale: str = ""
    entry_order_id: str | None = None
    exit_order_id: str | None = None

    @property
    def planned_risk_usd(self) -> float:
        """What the trade was designed to lose if the stop filled as planned."""
        return abs(self.entry_price - self.planned_stop) * self.qty

    @property
    def is_open(self) -> bool:
        return self.exit_time is None

    @property
    def net_pnl_usd(self) -> float | None:
        if self.realised_pnl_usd is None:
            return None
        return self.realised_pnl_usd - self.fees_usd

    @property
    def r_multiple(self) -> float | None:
        """Net result as a multiple of the risk actually planned.

        The unit that makes trades of different sizes comparable: +2R is the
        same quality of outcome whether the position was $500 or $5,000.
        """
        net = self.net_pnl_usd
        risk = self.planned_risk_usd
        if net is None or risk <= 0:
            return None
        return net / risk

    def outcome(self, scratch_threshold_r: float) -> TradeOutcome | None:
        """Classify for the consecutive-loss counter. None while still open."""
        r = self.r_multiple
        if r is None:
            return None
        if r > scratch_threshold_r:
            return TradeOutcome.WIN
        if r < -scratch_threshold_r:
            return TradeOutcome.LOSS
        return TradeOutcome.SCRATCH


class StandDownState(BaseModel):
    """Persisted stand-down state.

    Lives in SQLite rather than in memory: a stand-down that vanished when the
    process restarted would be trivially defeated by restarting the process,
    which is precisely what someone tilting would do.
    """

    stage: int = Field(default=0, ge=0, le=2, description="0 = not standing down")
    started_at: datetime | None = None
    ends_at: datetime | None = None
    consecutive_losses: int = Field(default=0, ge=0)
    last_triggered_at: datetime | None = None

    def is_active(self, now: datetime) -> bool:
        return self.ends_at is not None and now < self.ends_at

    def days_remaining(self, now: datetime) -> float:
        if not self.is_active(now) or self.ends_at is None:
            return 0.0
        return (self.ends_at - now).total_seconds() / 86_400


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
