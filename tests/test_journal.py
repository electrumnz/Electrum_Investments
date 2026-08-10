"""Tests for the SQLite trade journal.

The important ones here are persistence (a stand-down that vanishes on restart
is worthless) and the scratch handling (a counter that punished small losses
would push the bot toward holding losers).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.journal import Journal
from bot.models import (
    AssetClass,
    Direction,
    ExecutionMode,
    StandDownState,
    Trade,
    TradeOutcome,
)

ENTRY = datetime(2026, 5, 4, 15, 0, tzinfo=UTC)


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "journal.db")


def _trade(
    symbol: str = "SPY",
    qty: float = 10,
    entry_price: float = 580.0,
    stop: float = 575.0,
    strategy: str = "mean_reversion",
    asset_class: AssetClass = AssetClass.EQUITY,
    entry_time: datetime = ENTRY,
) -> Trade:
    return Trade(
        symbol=symbol,
        asset_class=asset_class,
        strategy=strategy,
        direction=Direction.BUY,
        qty=qty,
        entry_time=entry_time,
        entry_price=entry_price,
        planned_stop=stop,
        planned_target=590.0,
        rationale="Test trade.",
        execution_mode=ExecutionMode.PAPER,
    )


# ------------------------------------------------------------ round trip


def test_entry_then_exit_round_trip(journal):
    trade_id = journal.record_entry(_trade())
    assert trade_id > 0
    assert len(journal.open_trades()) == 1

    journal.record_exit(
        trade_id,
        exit_time=ENTRY + timedelta(hours=2),
        exit_price=590.0,
        realised_pnl_usd=100.0,
        fees_usd=1.0,
    )

    assert journal.open_trades() == []
    closed = journal.closed_trades()
    assert len(closed) == 1
    assert closed[0].net_pnl_usd == pytest.approx(99.0)


def test_r_multiple_uses_planned_risk(journal):
    # 10 shares, $5 of stop distance = $50 planned risk.
    trade_id = journal.record_entry(_trade())
    journal.record_exit(
        trade_id,
        exit_time=ENTRY + timedelta(hours=1),
        exit_price=590.0,
        realised_pnl_usd=100.0,
    )
    assert journal.closed_trades()[0].r_multiple == pytest.approx(2.0)


def test_fees_reduce_r_multiple(journal):
    trade_id = journal.record_entry(_trade())
    journal.record_exit(
        trade_id,
        exit_time=ENTRY + timedelta(hours=1),
        exit_price=590.0,
        realised_pnl_usd=100.0,
        fees_usd=25.0,
    )
    # (100 - 25) / 50 = 1.5R, not 2R.
    assert journal.closed_trades()[0].r_multiple == pytest.approx(1.5)


def test_open_trade_has_no_r_multiple(journal):
    journal.record_entry(_trade())
    assert journal.open_trades()[0].r_multiple is None
    assert journal.open_trades()[0].is_open


# --------------------------------------------------------------- MAE/MFE


def test_excursion_widens_in_both_directions(journal):
    trade_id = journal.record_entry(_trade())

    journal.update_excursion(trade_id, -30.0)
    journal.update_excursion(trade_id, 80.0)
    journal.update_excursion(trade_id, -10.0)  # narrower, must not overwrite
    journal.update_excursion(trade_id, 20.0)   # narrower, must not overwrite

    trade = journal.open_trades()[0]
    assert trade.mae_usd == pytest.approx(-30.0)
    assert trade.mfe_usd == pytest.approx(80.0)


def test_excursion_ignores_closed_trades(journal):
    trade_id = journal.record_entry(_trade())
    journal.record_exit(
        trade_id, exit_time=ENTRY + timedelta(hours=1), exit_price=585.0,
        realised_pnl_usd=50.0,
    )
    journal.update_excursion(trade_id, -999.0)
    assert journal.closed_trades()[0].mae_usd == pytest.approx(0.0)


# --------------------------------------------------------------- outcomes


@pytest.mark.parametrize(
    "pnl, expected",
    [
        (100.0, TradeOutcome.WIN),      # +2.0R
        (-100.0, TradeOutcome.LOSS),    # -2.0R
        (5.0, TradeOutcome.SCRATCH),    # +0.1R, inside the threshold
        (-5.0, TradeOutcome.SCRATCH),   # -0.1R, inside the threshold
    ],
)
def test_outcome_classification(journal, pnl, expected):
    trade_id = journal.record_entry(_trade())
    journal.record_exit(
        trade_id, exit_time=ENTRY + timedelta(hours=1), exit_price=580.0,
        realised_pnl_usd=pnl,
    )
    assert journal.closed_trades()[0].outcome(0.25) == expected


# ------------------------------------------------------- loss streak


def _close(journal, pnl: float, minutes: int) -> None:
    trade_id = journal.record_entry(_trade(entry_time=ENTRY + timedelta(minutes=minutes)))
    journal.record_exit(
        trade_id,
        exit_time=ENTRY + timedelta(minutes=minutes + 30),
        exit_price=580.0,
        realised_pnl_usd=pnl,
    )


def test_consecutive_losses_counts_a_streak(journal):
    for i in range(3):
        _close(journal, -100.0, i * 60)
    assert journal.consecutive_losses(0.25) == 3


def test_a_win_breaks_the_streak(journal):
    _close(journal, -100.0, 0)
    _close(journal, -100.0, 60)
    _close(journal, 100.0, 120)   # win resets
    _close(journal, -100.0, 180)
    assert journal.consecutive_losses(0.25) == 1


def test_scratches_neither_count_nor_reset(journal):
    """A run of real losses interrupted by a scratch is still a streak."""
    _close(journal, -100.0, 0)
    _close(journal, 2.0, 60)      # scratch: skipped entirely
    _close(journal, -100.0, 120)
    assert journal.consecutive_losses(0.25) == 2


def test_small_losses_alone_never_build_a_streak(journal):
    for i in range(5):
        _close(journal, -5.0, i * 60)   # all inside the threshold
    assert journal.consecutive_losses(0.25) == 0


# ------------------------------------------------------ stand-down state


def test_stand_down_defaults_to_inactive(journal):
    state = journal.get_stand_down()
    assert state.stage == 0
    assert not state.is_active(datetime.now(UTC))


def test_stand_down_survives_a_restart(journal, tmp_path):
    """The whole point: restarting the process must not clear a stand-down."""
    ends = datetime.now(UTC) + timedelta(days=3)
    journal.save_stand_down(
        StandDownState(
            stage=1,
            started_at=datetime.now(UTC),
            ends_at=ends,
            consecutive_losses=3,
            last_triggered_at=datetime.now(UTC),
        )
    )

    # A completely fresh Journal against the same file, as after a restart.
    reopened = Journal(tmp_path / "journal.db")
    state = reopened.get_stand_down()

    assert state.stage == 1
    assert state.consecutive_losses == 3
    assert state.is_active(datetime.now(UTC))
    assert state.days_remaining(datetime.now(UTC)) == pytest.approx(3.0, abs=0.01)


def test_stand_down_expires_on_time(journal):
    journal.save_stand_down(
        StandDownState(stage=1, ends_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    assert not journal.get_stand_down().is_active(datetime.now(UTC))


def test_saving_stand_down_twice_updates_rather_than_duplicates(journal):
    journal.save_stand_down(StandDownState(stage=1, consecutive_losses=3))
    journal.save_stand_down(StandDownState(stage=2, consecutive_losses=6))
    state = journal.get_stand_down()
    assert state.stage == 2
    assert state.consecutive_losses == 6


# --------------------------------------------------------------- filters


def test_closed_trades_filter_by_strategy_and_class(journal):
    for strategy, asset_class, symbol in [
        ("mean_reversion", AssetClass.EQUITY, "SPY"),
        ("momentum", AssetClass.CRYPTO, "BTC/USD"),
    ]:
        trade_id = journal.record_entry(
            _trade(symbol=symbol, strategy=strategy, asset_class=asset_class)
        )
        journal.record_exit(
            trade_id, exit_time=ENTRY + timedelta(hours=1), exit_price=585.0,
            realised_pnl_usd=50.0,
        )

    assert len(journal.closed_trades(strategy="momentum")) == 1
    assert len(journal.closed_trades(asset_class=AssetClass.CRYPTO)) == 1
    assert len(journal.closed_trades()) == 2


def test_open_trade_lookup_by_symbol(journal):
    journal.record_entry(_trade(symbol="SPY"))
    journal.record_entry(_trade(symbol="QQQ"))
    assert journal.open_trade_for("SPY") is not None
    assert journal.open_trade_for("AAPL") is None


# ---------------------------------------------------------------- equity


def test_equity_is_one_row_per_day(journal):
    day = datetime(2026, 5, 4, 15, 0, tzinfo=UTC)
    journal.record_equity(100_000.0, when=day)
    journal.record_equity(101_000.0, when=day.replace(hour=20))
    journal.record_equity(99_000.0, when=day + timedelta(days=1))

    curve = journal.equity_curve()
    assert len(curve) == 2
    assert curve[0] == ("2026-05-04", 101_000.0)  # last write for the day wins


def test_an_old_journal_is_migrated_to_allow_a_trade_with_no_target(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` does NOTHING to a table that already exists.

    So editing `SCHEMA` changes what a fresh database gets and nothing about the
    one on the box — and the suite cannot see the gap, because every test builds
    its journal from scratch in a `tmp_path` and therefore always gets the new
    shape.

    This is the expensive one. `take_profit_price` became optional, the model
    and `SCHEMA` were both updated, the suite was green, and the first real
    no-target order reached Alpaca, rested there, and then failed to journal on
    `NOT NULL constraint failed: trades.planned_target`. A live order the
    journal had never heard of, which is the `14b88c8` bug exactly:
    `open_risk_usd` cannot count it, so the 2% cap was blind to it.

    So this test builds the OLD schema deliberately, which is the only way to
    exercise a migration at all.
    """
    import sqlite3

    from bot.journal import SCHEMA

    path = tmp_path / "legacy.db"
    old = SCHEMA.replace("planned_target    REAL,", "planned_target    REAL    NOT NULL,")
    conn = sqlite3.connect(path)
    conn.executescript(old)
    conn.execute(
        "INSERT INTO trades (symbol, asset_class, strategy, direction, qty, "
        "entry_time, entry_price, planned_stop, planned_target) VALUES "
        "('QQQ','us_equity','manual','buy',5,'2026-08-01T00:00:00+00:00',500,490,520)"
    )
    conn.commit()
    conn.close()

    journal = Journal(path)          # the migration runs on open

    # The existing row survived. A migration that dropped history to fix a
    # constraint would be a far worse trade than the constraint.
    assert [t.symbol for t in journal.open_trades()] == ["QQQ"]

    # And the write that used to fail now works.
    journal.record_entry(
        Trade(
            symbol="SPY",
            strategy="manual",
            direction=Direction.SELL,
            qty=21,
            entry_time=datetime(2026, 8, 10, 13, 23, tzinfo=UTC),
            entry_price=772.84,
            planned_stop=820.0,
            planned_target=None,
            rationale="Short with a hard stop and no target, which is a normal trade.",
        )
    )
    assert {t.symbol for t in journal.open_trades()} == {"QQQ", "SPY"}


def test_the_migration_is_idempotent(tmp_path):
    """It runs on every open. A database already in the right shape must pay one
    PRAGMA and stop, not rebuild the table on every start."""
    path = tmp_path / "fresh.db"
    Journal(path)
    journal = Journal(path)          # second open, already migrated

    journal.record_entry(
        Trade(
            symbol="SPY",
            strategy="manual",
            direction=Direction.SELL,
            qty=1,
            entry_time=datetime(2026, 8, 10, 13, 23, tzinfo=UTC),
            entry_price=772.84,
            planned_stop=820.0,
            planned_target=None,
            rationale="Proves a repeated open leaves a working table behind.",
        )
    )
    assert len(journal.open_trades()) == 1
