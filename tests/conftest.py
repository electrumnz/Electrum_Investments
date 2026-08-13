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


def runtime_fingerprint(directories: list[Path]) -> dict[str, tuple[int, int]]:
    """Size and mtime of everything directly inside `directories`.

    Module level rather than a closure so the guard below can be tested — a
    guard nobody has watched fail is the thing this repository keeps finding.
    """
    out: dict[str, tuple[int, int]] = {}
    for directory in directories:
        if not directory.exists():
            continue
        for path in directory.iterdir():
            try:
                stat = path.stat()
            except OSError:  # pragma: no cover - vanished mid-scan
                continue
            out[f"{directory.name}/{path.name}"] = (stat.st_size, stat.st_mtime_ns)
    return out


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

    **It compares CONTENTS, not a file listing, and that is the whole fix.**
    Diffing names caught the first offence and went blind to every one after
    it: once `data/dreams.db` existed, a test writing rows into it appeared in
    `before` and in `after` and was reported as nothing at all — so the guard
    was strongest on a clean machine and useless on a developer's, which is the
    inverse of what a guard should be. A size-and-mtime fingerprint catches a
    file that grew as well as one that arrived, and it is cheap: `stat` on a
    handful of paths, once at each end of the run.

    Size and mtime rather than a hash of the bytes because SQLite can rewrite a
    page in place without changing the length, and reading a multi-megabyte
    journal twice per run to notice would cost more than it is worth. mtime has
    a coarse resolution on some filesystems, so this is not a proof — it is a
    tripwire, and one that fires on every way a store has actually been left
    pointed at `data/`.
    """
    watched = [REPO_ROOT / "data", REPO_ROOT / "audit"]

    before = runtime_fingerprint(watched)
    yield
    after = runtime_fingerprint(watched)
    created = sorted(set(after) - set(before))
    changed = sorted(
        name for name, mark in after.items() if name in before and before[name] != mark
    )
    assert not created and not changed, (
        f"tests wrote to a runtime directory: created={created} "
        f"modified={changed}. Pass a tmp_path to the store instead of letting "
        "it use its production default."
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
