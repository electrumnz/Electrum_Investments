from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from bot.config import Rules
from bot.models import (
    AccountSnapshot,
    Direction,
    OrderProposal,
    Tick,
    TradingActivity,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = REPO_ROOT / "config" / "rules.yaml"

# Alpaca paper accounts start at $100,000, and config/rules.yaml is calibrated
# to that balance. Tests use the same figure so the fixtures and the shipped
# rules stay consistent.
PAPER_EQUITY = 100_000.0

# A Monday, 15:00 UTC — inside the [14, 21) session window in rules.yaml.
INSIDE_SESSION = datetime(2026, 5, 4, 15, 0, tzinfo=UTC)


@pytest.fixture
def rules() -> Rules:
    return Rules.load(RULES_PATH)


@pytest.fixture
def now() -> datetime:
    return INSIDE_SESSION


@pytest.fixture
def account() -> AccountSnapshot:
    return AccountSnapshot(
        equity_usd=PAPER_EQUITY,
        cash_usd=PAPER_EQUITY,
        buying_power_usd=PAPER_EQUITY,
        open_positions=[],
    )


@pytest.fixture
def spy_tick() -> Tick:
    return Tick(symbol="SPY", bid=579.98, ask=580.02, timestamp=INSIDE_SESSION)


@pytest.fixture
def buy_proposal() -> OrderProposal:
    """A well-formed proposal that passes every gate against the `account` fixture.

    10 shares at $580 is $5,800 notional (5.8% of equity) risking $50 if the
    stop fills (0.05% of equity) — comfortably inside every cap in rules.yaml.
    """
    return OrderProposal(
        symbol="SPY",
        direction=Direction.BUY,
        qty=10,
        limit_price=580.00,
        stop_loss_price=575.00,
        take_profit_price=590.00,
        rationale="Reclaimed the prior day high on rising volume; invalidated below 575.",
    )


@pytest.fixture
def quiet_activity() -> TradingActivity:
    return TradingActivity()
