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
)


REPO_ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = REPO_ROOT / "config" / "rules.yaml"


@pytest.fixture
def rules() -> Rules:
    return Rules.load(RULES_PATH)


@pytest.fixture
def session_start_dt() -> datetime:
    return datetime(2026, 5, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def empty_account() -> AccountSnapshot:
    return AccountSnapshot(
        equity_usd=5000.0,
        balance_usd=5000.0,
        margin_used_usd=0.0,
        free_margin_usd=5000.0,
        open_positions=[],
    )


@pytest.fixture
def eurusd_tick() -> Tick:
    return Tick(
        symbol="EURUSD",
        bid=1.0850,
        ask=1.0852,
        timestamp=datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
    )


@pytest.fixture
def buy_proposal() -> OrderProposal:
    return OrderProposal(
        symbol="EURUSD",
        direction=Direction.BUY,
        size_lots=0.01,
        stop_loss_price=1.0820,
        take_profit_price=1.0900,
        rationale="London open momentum, price reclaimed prior day high.",
    )
