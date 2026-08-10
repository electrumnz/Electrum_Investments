from __future__ import annotations

from collections.abc import Iterator
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


@pytest.fixture(scope="session", autouse=True)
def _runtime_dirs_stay_clean() -> Iterator[None]:
    """No test may leave a file in `data/` or `audit/`.

    Both hold runtime artefacts that are gitignored and shared between runs, so
    a test writing there does two things: it leaves state on the developer's
    machine that the *next* run then reads, and on a real box it puts test rows
    next to the journal that is the only irreplaceable file there.

    Every store in this repository takes its path as a constructor argument for
    exactly this reason, and every fixture passes a `tmp_path`. This catches the
    case that keeps recurring: a new store is added, `build_app` gains a new
    default, and one call site that nobody updated quietly starts writing to the
    real directory. It happened when `DreamStore` landed.

    Session-scoped because it is a guard on the suite rather than on any one
    test, and because the interesting question is "did anything appear", not
    "which test did it" — the answer to the second is almost always the test
    that constructed something without a path.
    """
    watched = [REPO_ROOT / "data", REPO_ROOT / "audit"]

    def snapshot() -> set[Path]:
        return {p for d in watched if d.exists() for p in d.iterdir()}

    before = snapshot()
    yield
    new = sorted(str(p.relative_to(REPO_ROOT)) for p in snapshot() - before)
    assert not new, (
        f"tests wrote to a runtime directory: {new}. Pass a tmp_path to the "
        "store instead of letting it use its production default."
    )


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

    3 shares at $580 is $1,740 notional — 1.74% of equity, just inside the 2%
    total-invested cap, which is the binding constraint on a $100k account.
    Risk if the stop fills is $15.
    """
    return OrderProposal(
        symbol="SPY",
        direction=Direction.BUY,
        qty=3,
        limit_price=580.00,
        stop_loss_price=575.00,
        take_profit_price=590.00,
        rationale="Reclaimed the prior day high on rising volume; invalidated below 575.",
    )


@pytest.fixture
def quiet_activity() -> TradingActivity:
    return TradingActivity()
