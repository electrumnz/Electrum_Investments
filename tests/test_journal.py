"""Tests for the SQLite trade journal.

The important ones here are persistence (a stand-down that vanishes on restart
is worthless) and the scratch handling (a counter that punished small losses
would push the bot toward holding losers).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from bot.journal import SCHEMA, Journal
from bot.models import (
    AssetClass,
    Direction,
    ExecutionMode,
    ExitReason,
    FillState,
    PositionAction,
    PositionActionRecord,
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
    dream_id: int | None = None,
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
        dream_id=dream_id,
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


def test_a_since_in_another_offset_still_finds_the_trade(journal):
    """**Timestamps are TEXT, and SQLite compares TEXT lexically.**

    `'2026-08-12T20:00:00+00:00'` sorts BEFORE `'2026-08-13T00:00:00+13:00'`,
    and the second is nine hours EARLIER. So one caller building a window from
    a non-UTC clock was enough to make `closed_trades(since=...)` silently drop
    rows that fall inside it — reporting "no trades in that window" for a
    window that had one, which is the plausible-wrong-figure failure with no
    exception to notice.

    Not hypothetical as a shape: this repository already had the same string
    comparison bite `grants.py`, and the operator's own clock is New Zealand,
    so "since midnight" is exactly `+13:00`. Measured before the fix: one row
    stored, and `closed_trades` returned zero for a cutoff nine hours before
    the close.
    """
    nz = timezone(timedelta(hours=13))
    trade_id = journal.record_entry(_trade(entry_time=datetime(2026, 8, 12, 18, 0, tzinfo=UTC)))
    closed_at = datetime(2026, 8, 12, 20, 0, tzinfo=UTC)
    journal.record_exit(
        trade_id, exit_time=closed_at, exit_price=585.0, realised_pnl_usd=50.0
    )

    since = datetime(2026, 8, 13, 0, 0, tzinfo=nz)  # 11:00 UTC on the 12th
    assert closed_at > since, "the close is genuinely after the cutoff"
    # The instants say yes and the raw strings say no. That gap is the bug.
    assert not (closed_at.isoformat() >= since.isoformat())

    assert len(journal.closed_trades(since=since)) == 1
    # And a cutoff genuinely after the close still excludes it, so this is not
    # passing because the filter stopped filtering.
    assert journal.closed_trades(since=datetime(2026, 8, 13, 12, 0, tzinfo=nz)) == []


def test_a_timestamp_written_in_another_offset_is_stored_as_utc(journal, tmp_path):
    """Storage is normalised, so lexical order IS chronological order.

    Fixing only the `since` side would leave the ORDER BY clauses — `exit_time`
    on `closed_trades`, `at` on `position_actions` — sorting two rows written
    in different offsets into the wrong sequence, with nothing to say so.
    """
    import sqlite3

    nz = timezone(timedelta(hours=13))
    local = datetime(2026, 8, 13, 9, 0, tzinfo=nz)
    trade_id = journal.record_entry(_trade(entry_time=local))

    conn = sqlite3.connect(tmp_path / "journal.db")
    stored = conn.execute(
        "SELECT entry_time FROM trades WHERE id = ?", (trade_id,)
    ).fetchone()[0]
    conn.close()

    assert stored == "2026-08-12T20:00:00+00:00"
    # The instant survives the round trip unchanged; only its spelling moved.
    assert journal.open_trades()[0].entry_time == local


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


# ------------------------------------------------------------- migrations
#
# Every test above builds its journal from scratch, so every one of them always
# gets the shape `SCHEMA` currently describes. That is exactly why the suite is
# blind to a missing migration, and why these two build the OLD table by hand.


def _schema_without_dream_id() -> str:
    """`journal.SCHEMA` as it stood before trades could come from a dream.

    Derived from the real schema rather than pasted, so the rest of the table
    stays in step with it and only the column under test differs. The comment
    block goes with the column: a stray `-- dream_id` in the DDL would make the
    check below pass on the comment rather than on the column.
    """

    start = SCHEMA.index("    -- The adopted dream whose grant")
    # `dream_id` is no longer the last column, so it now carries the comma that
    # separates it from `current_stop`. Cutting to the end of the LINE rather
    # than to a literal that assumes the trailing punctuation is what keeps this
    # helper working the next time a column is appended after it.
    end = SCHEMA.index("\n", SCHEMA.index("    dream_id          INTEGER")) + 1
    without = SCHEMA[:start] + SCHEMA[end:]
    # The column before it carried the comma that separated the two — but only
    # when removing `dream_id` leaves nothing behind it. With `current_stop`
    # after it there is still a column to separate from, so this is a no-op.
    return without.replace("    exit_order_id     TEXT,\n)", "    exit_order_id     TEXT\n)")


def _schema_without_current_stop() -> str:
    """`journal.SCHEMA` as it stood before a stop could be moved.

    Same construction and same reasoning as the helper above: derived from the
    live schema so only the column under test differs, with its comment block
    removed alongside it so the assertion below cannot pass on a comment.

    `current_stop` IS the last column, so removing it leaves `dream_id` with a
    trailing comma and the DDL has to be repaired.
    """
    start = SCHEMA.index("    -- The stop actually in force")
    end = SCHEMA.index("\n", SCHEMA.index("    current_stop      REAL")) + 1
    without = SCHEMA[:start] + SCHEMA[end:]
    return without.replace(
        "    dream_id          INTEGER,\n)", "    dream_id          INTEGER\n)"
    )


def _columns(conn) -> set[str]:
    return {str(row[1]) for row in conn.execute("PRAGMA table_info(trades)")}


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


def test_an_old_journal_is_migrated_to_carry_a_dream_id(tmp_path):
    """The same trap as the test above, and it has already been paid for once.

    `CREATE TABLE IF NOT EXISTS` does NOTHING to a table that already exists, so
    adding `dream_id` to `SCHEMA` changes what a fresh journal gets and nothing
    whatever about the one on the droplet. The suite cannot see that on its own:
    every other test here builds its journal in a `tmp_path` and therefore always
    gets the new shape, which is how 866 green tests once sat over a database
    that could not store what the models allowed — found by placing a real order
    and having the journal write fail AFTER the broker call.

    So this builds the OLD schema by hand, which is the only way to exercise a
    migration at all.
    """
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(_schema_without_dream_id())
    assert "dream_id" not in _columns(conn), (
        "the hand-built schema still has the column, so this would prove nothing"
    )
    conn.execute(
        "INSERT INTO trades (symbol, asset_class, strategy, direction, qty, "
        "entry_time, entry_price, planned_stop, planned_target) VALUES "
        "('QQQ','us_equity','manual','buy',5,'2026-08-01T00:00:00+00:00',500,490,520)"
    )
    conn.commit()
    conn.close()

    journal = Journal(path)          # the migration runs on open

    # The existing row survived, and reads back with no dream behind it — which
    # is the truth about every trade placed before grants existed.
    held = journal.open_trades()
    assert [t.symbol for t in held] == ["QQQ"]
    assert held[0].dream_id is None

    # And the column is usable, which is the half the models now depend on.
    journal.record_entry(_trade(symbol="TSLA", dream_id=41))
    written = journal.open_trade_for("TSLA")
    assert written is not None
    assert written.dream_id == 41


def test_the_dream_id_migration_is_idempotent(tmp_path):
    """It runs on every open, so a database already in the right shape must pay
    one PRAGMA and stop rather than altering the table again — which SQLite
    would refuse with `duplicate column name`."""
    path = tmp_path / "fresh.db"
    Journal(path)
    journal = Journal(path)          # second open, already migrated

    journal.record_entry(_trade(dream_id=7))
    assert journal.open_trades()[0].dream_id == 7


def test_both_migrations_run_on_a_journal_that_predates_both(tmp_path):
    """The interaction, because the older migration REBUILDS the table.

    `_drop_planned_target_not_null` copies the old table's column list into a
    table freshly created from `SCHEMA`, so it has to leave a journal that then
    satisfies the newer migration rather than one that needs it a second time.
    A database old enough to need both is exactly the one on the droplet.
    """
    import sqlite3

    path = tmp_path / "ancient.db"
    old = _schema_without_dream_id().replace(
        "planned_target    REAL,", "planned_target    REAL    NOT NULL,"
    )
    conn = sqlite3.connect(path)
    conn.executescript(old)
    assert "dream_id" not in _columns(conn)
    conn.execute(
        "INSERT INTO trades (symbol, asset_class, strategy, direction, qty, "
        "entry_time, entry_price, planned_stop, planned_target) VALUES "
        "('SPY','us_equity','manual','sell',21,'2026-08-10T13:37:40+00:00',"
        "773.324285,820,900)"
    )
    conn.commit()
    conn.close()

    journal = Journal(path)

    # History intact. A migration that dropped rows to fix a constraint would be
    # a far worse trade than either constraint.
    survivors = journal.open_trades()
    assert [t.symbol for t in survivors] == ["SPY"]
    assert survivors[0].dream_id is None

    # Both new shapes work: no target, and a dream behind the permission.
    journal.record_entry(
        Trade(
            symbol="TSLA",
            strategy="unspecified",
            direction=Direction.BUY,
            qty=3,
            entry_time=ENTRY,
            entry_price=420.0,
            planned_stop=415.0,
            planned_target=None,
            dream_id=12,
            rationale="Granted by an adopted dream, with no pre-committed target.",
        )
    )
    written = journal.open_trade_for("TSLA")
    assert written is not None
    assert written.dream_id == 12
    assert written.planned_target is None


def test_a_trade_with_no_dream_reads_back_as_none(journal):
    """The ordinary case, and the one that must not become a plausible zero."""
    journal.record_entry(_trade())
    assert journal.open_trades()[0].dream_id is None


def test_an_old_journal_is_migrated_to_carry_a_current_stop(tmp_path):
    """The third time this trap has had to be paid for, on the one file that
    holds a live position.

    `CREATE TABLE IF NOT EXISTS` does NOTHING to a table that already exists, so
    adding `current_stop` to `SCHEMA` changes what a fresh journal gets and
    nothing whatever about `data/journal.db` on the droplet. Every other test
    here builds its journal from scratch in a `tmp_path` and therefore always
    gets the new shape, which is precisely why the suite cannot see the gap.

    Without the migration, the first tightened stop would fail on `no such
    column: current_stop` AFTER the broker had already replaced the leg — a
    stop moved at Alpaca that the journal has no record of, which is the
    `14b88c8` shape arriving on the position-management side.

    So this builds the OLD schema by hand, which is the only way to exercise a
    migration at all, and seeds it with the live position's own row.
    """
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(_schema_without_current_stop())
    assert "current_stop" not in _columns(conn), (
        "the hand-built schema still has the column, so this would prove nothing"
    )
    conn.execute(
        "INSERT INTO trades (symbol, asset_class, strategy, direction, qty, "
        "entry_time, entry_price, planned_stop, planned_target) VALUES "
        "('SPY','us_equity','manual','sell',21,'2026-08-10T13:37:40+00:00',"
        "773.324285,820,NULL)"
    )
    conn.commit()
    conn.close()

    journal = Journal(path)          # the migration runs on open

    # The existing row survived, and reads back with its stop unmoved — which
    # is the truth about every position open before stop moves existed.
    held = journal.open_trades()
    assert [t.symbol for t in held] == ["SPY"]
    assert held[0].current_stop is None
    assert held[0].effective_stop == 820.0
    assert round(held[0].current_risk_usd, 2) == 980.19

    # And the column is usable, which is the half the move path depends on.
    trade_id = held[0].id
    assert trade_id is not None
    journal.record_stop_move(
        PositionActionRecord(
            trade_id=trade_id,
            symbol="SPY",
            action=PositionAction.TIGHTEN_STOP,
            actor="operator",
            at=ENTRY + timedelta(hours=3),
            reason="Pulling the stop in on a journal that predates the column.",
            before_stop=820.0,
            after_stop=800.0,
            reached_broker=True,
        )
    )
    moved = journal.open_trade_for("SPY")
    assert moved is not None
    assert moved.current_stop == 800.0
    assert moved.planned_stop == 820.0          # the sizing figure is untouched
    assert round(journal.open_risk_usd(), 2) == 560.19


def test_the_current_stop_migration_is_idempotent(tmp_path):
    """It runs on every open, so a database already in the right shape must pay
    one PRAGMA and stop rather than altering the table again — which SQLite
    would refuse with `duplicate column name`."""
    path = tmp_path / "fresh.db"
    Journal(path)
    journal = Journal(path)          # second open, already migrated

    journal.record_entry(_trade())
    assert journal.open_trades()[0].current_stop is None


def test_the_action_table_reaches_a_database_that_already_exists(tmp_path):
    """The counter-case to the trap above, stated so nobody has to work it out.

    `CREATE TABLE IF NOT EXISTS` is a no-op only on a table that ALREADY
    EXISTS. A brand new table therefore needs no migration — the statement in
    `SCHEMA` does reach the droplet's database and does create it. This proves
    it against a journal built from the old shape.
    """
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(_schema_without_current_stop())
    conn.execute("DROP TABLE IF EXISTS position_actions")
    conn.commit()
    conn.close()

    journal = Journal(path)
    assert journal.position_actions() == []

    journal.record_position_action(
        PositionActionRecord(
            symbol="SPY",
            action=PositionAction.CLOSE,
            actor="operator",
            at=ENTRY,
            reason="Proves the table exists on a database that predates it.",
            before_qty=21.0,
            after_qty=0.0,
        )
    )
    assert len(journal.position_actions()) == 1


def test_a_stop_move_writes_the_trade_and_the_record_together(journal):
    """Two writes that must not come apart.

    Updating the trade without the record produces exactly the state this
    feature exists to expose — a stop that changed with no reason on file — and
    writing the record without the update leaves the caps counting a stop that
    is no longer there.
    """
    trade_id = journal.record_entry(_trade(stop=575.0))

    journal.record_stop_move(
        PositionActionRecord(
            trade_id=trade_id,
            symbol="SPY",
            action=PositionAction.TIGHTEN_STOP,
            actor="trader",
            at=ENTRY + timedelta(hours=1),
            reason="Held the level twice; taking the room back.",
            before_stop=575.0,
            after_stop=578.0,
            reached_broker=True,
        )
    )

    moved = journal.open_trade_for("SPY")
    assert moved is not None and moved.current_stop == 578.0

    actions = journal.position_actions(symbol="SPY")
    assert len(actions) == 1
    assert actions[0].trade_id == trade_id
    assert actions[0].reached_broker is True


def test_recorded_moves_come_back_newest_first(journal):
    """The ordering `closed_trades` gets wrong: it sorts ascending before its
    LIMIT, so a limited call drops the NEWEST rows. A limited read of an action
    log has to be the most recent moves or it answers a question nobody asked."""
    for hour, stop in ((1, 578.0), (2, 579.0), (3, 579.5)):
        journal.record_position_action(
            PositionActionRecord(
                symbol="SPY",
                action=PositionAction.TIGHTEN_STOP,
                actor="trader",
                at=ENTRY + timedelta(hours=hour),
                reason=f"Move number {hour}.",
                before_stop=575.0,
                after_stop=stop,
            )
        )

    newest = journal.position_actions(limit=2)
    assert [a.after_stop for a in newest] == [579.5, 579.0]


def test_a_blank_reason_is_refused_by_the_journal_as_well_as_the_model(journal):
    """Two guards on one rule, because the absence of a reason is what the
    unexplained-move report exists to show. A row with an empty one would
    silence the report while explaining nothing."""
    record = PositionActionRecord(
        symbol="SPY",
        action=PositionAction.CLOSE,
        actor="operator",
        at=ENTRY,
        reason="real for now",
    )
    record.reason = ""                      # assignment skips validation
    with pytest.raises(ValueError, match="needs a reason"):
        journal.record_position_action(record)
    with pytest.raises(ValueError, match="needs a reason"):
        journal.record_stop_move(record)
    assert journal.position_actions() == []


# ------------------------------------- what was ordered, and why it closed
#
# `record_fill` writes the PROPOSAL's quantity and limit price, so the journal
# recorded an intention and called it an outcome. These cover the columns that
# let `reconcile` correct that without erasing what was actually asked for, and
# the exit reason that `record_exit` could never reconstruct afterwards.


def _schema_without_fill_and_exit_columns() -> str:
    """`journal.SCHEMA` as it stood before fill confirmation and exit review.

    Same construction and same reasoning as the two helpers above: derived from
    the live schema so only the columns under test differ, with their comment
    blocks removed alongside them so the assertions below cannot pass on a
    comment.

    These five ARE the last columns, so removing them leaves `current_stop`
    with a trailing comma and the DDL has to be repaired.
    """
    start = SCHEMA.index("    -- What was ORDERED")
    end = SCHEMA.index("\n", SCHEMA.index("    exit_price_estimated INTEGER")) + 1
    without = SCHEMA[:start] + SCHEMA[end:]
    return without.replace(
        "    current_stop      REAL,\n)", "    current_stop      REAL\n)"
    )


def _legacy_journal(tmp_path, ddl: str):
    """Build a `trades` table from an older DDL and put one row in it."""
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(ddl)
    conn.execute(
        "INSERT INTO trades (symbol, asset_class, strategy, direction, qty, "
        "entry_time, entry_price, planned_stop, planned_target) VALUES "
        "('SPY','us_equity','manual','sell',21,'2026-08-10T13:23:00+00:00',"
        "772.84,820,NULL)"
    )
    conn.commit()
    conn.close()
    return path


def test_an_old_journal_is_migrated_to_carry_the_fill_and_exit_columns(tmp_path):
    """The fourth time this migration has been needed, for the fourth same reason.

    `CREATE TABLE IF NOT EXISTS` is a no-op on a table that already exists, so
    adding a column to `SCHEMA` changes what a fresh journal gets and nothing
    whatever about the one on the droplet — the file holding the live position.
    Every other test in this module builds its journal from scratch in a
    `tmp_path` and therefore always gets the new shape, which is precisely why
    this one builds the OLD shape by hand.
    """
    import sqlite3

    path = _legacy_journal(tmp_path, _schema_without_fill_and_exit_columns())

    conn = sqlite3.connect(path)
    assert "fill_state" not in _columns(conn)      # the old shape really is old
    conn.close()

    journal = Journal(path)                        # the migration runs on open

    conn = sqlite3.connect(path)
    assert {
        "submitted_qty",
        "submitted_price",
        "fill_state",
        "exit_reason",
        "exit_price_estimated",
    } <= _columns(conn)
    conn.close()

    # The existing row survived. A migration that dropped history to add a
    # column would be a far worse trade than the missing column.
    trades = journal.open_trades()
    assert [t.symbol for t in trades] == ["SPY"]

    # And it reads back as what it actually is. Nothing was ever read back from
    # the broker about this entry, so UNCONFIRMED is the truth rather than a
    # default standing in for a missing value.
    assert trades[0].fill_state is FillState.UNCONFIRMED
    assert trades[0].submitted_qty is None
    assert trades[0].fill_is_confirmed is False
    # And "nothing classified this exit" is NOT "the exit could not be
    # established". The second is `ExitReason.UNKNOWN`; this is neither.
    assert trades[0].exit_reason is None
    assert trades[0].exit_price_estimated is None


def test_the_fill_and_exit_migration_is_idempotent(tmp_path):
    """It runs on every open. A database already in the right shape must pay one
    PRAGMA and stop."""
    path = tmp_path / "fresh.db"
    Journal(path)
    journal = Journal(path)                        # second open, already migrated
    journal.record_entry(_trade())
    assert len(journal.open_trades()) == 1


def test_a_half_added_set_of_columns_is_completed_on_the_next_open(tmp_path):
    """Five columns, five independent `ALTER`s, so a torn run finishes later.

    This is the property that makes one migration function safe for five
    columns instead of five functions: each `ALTER TABLE ... ADD COLUMN` is
    atomic by itself and nothing is dropped, so a run interrupted between two of
    them leaves an ordinary table with some of the columns — and the next open
    adds the rest. A migration guarded by a single version flag could not
    recover from that.
    """
    import sqlite3

    ddl = _schema_without_fill_and_exit_columns()
    path = _legacy_journal(tmp_path, ddl)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE trades ADD COLUMN submitted_qty REAL")
    conn.execute("ALTER TABLE trades ADD COLUMN fill_state TEXT")
    conn.commit()
    conn.close()

    Journal(path)

    conn = sqlite3.connect(path)
    assert {
        "submitted_qty",
        "submitted_price",
        "fill_state",
        "exit_reason",
        "exit_price_estimated",
    } <= _columns(conn)
    conn.close()


def test_what_was_ordered_survives_beside_what_was_recorded(journal):
    """The correction must not erase the only record of what was asked for."""
    trade = _trade()
    trade.submitted_qty = 21
    trade.submitted_price = 772.84
    trade_id = journal.record_entry(trade)

    stored = journal.open_trades()[0]
    assert stored.id == trade_id
    assert stored.submitted_qty == 21
    assert stored.submitted_price == 772.84
    assert stored.fill_state is FillState.UNCONFIRMED


def test_confirm_fill_replaces_the_proposal_with_what_the_broker_did(journal):
    """The measured case: limit 772.84, fill 773.324285, and the risk figure
    that followed from believing the limit."""
    trade = Trade(
        symbol="SPY",
        direction=Direction.SELL,
        qty=21,
        entry_time=ENTRY,
        entry_price=772.84,
        planned_stop=820.0,
        submitted_qty=21,
        submitted_price=772.84,
        rationale="Short, journalled from the proposal.",
    )
    trade_id = journal.record_entry(trade)
    assert round(journal.open_trades()[0].current_risk_usd, 2) == 990.36

    journal.confirm_fill(
        trade_id, fill_state=FillState.COMPLETE, qty=21, entry_price=773.324285
    )

    stored = journal.open_trades()[0]
    assert stored.entry_price == 773.324285
    assert stored.fill_state is FillState.COMPLETE
    assert stored.fill_is_confirmed is True
    assert round(stored.current_risk_usd, 2) == 980.19
    # And what was asked for is still on the row.
    assert stored.submitted_price == 772.84


def test_confirm_fill_leaves_a_closed_trade_alone(journal):
    """R has already been reported off this entry. A correction arriving after
    the close would move the denominator of a figure somebody has read."""
    trade_id = journal.record_entry(_trade(entry_price=580.0))
    journal.record_exit(
        trade_id, exit_time=ENTRY, exit_price=590.0, realised_pnl_usd=100.0
    )

    journal.confirm_fill(
        trade_id, fill_state=FillState.COMPLETE, qty=99, entry_price=1.0
    )

    closed = journal.closed_trades()[0]
    assert closed.entry_price == 580.0
    assert closed.qty == 10
    assert closed.fill_state is FillState.UNCONFIRMED


def test_a_partial_fill_is_expressed_as_a_state_rather_than_two_rows(journal):
    """`Trade` has one qty and one entry price, so it cannot hold "3 now, 18
    later". What it can hold is how much of the row anybody has confirmed."""
    trade = Trade(
        symbol="SPY",
        direction=Direction.SELL,
        qty=21,
        entry_time=ENTRY,
        entry_price=772.84,
        planned_stop=820.0,
        submitted_qty=21,
        submitted_price=772.84,
        rationale="Short, journalled from the proposal.",
    )
    trade_id = journal.record_entry(trade)
    journal.confirm_fill(
        trade_id, fill_state=FillState.PARTIAL, qty=3, entry_price=773.10
    )

    stored = journal.open_trades()[0]
    assert stored.fill_state is FillState.PARTIAL
    assert stored.qty == 3
    assert stored.submitted_qty == 21
    assert stored.fill_shortfall_qty == 18


def test_an_unconfirmed_row_reports_no_shortfall_rather_than_zero(journal):
    """Zero would be a claim about a fill nobody has read back."""
    trade = _trade()
    trade.submitted_qty = 10
    journal.record_entry(trade)
    assert journal.open_trades()[0].fill_shortfall_qty is None


def test_an_exit_reason_round_trips_and_an_absent_one_stays_absent(journal):
    """`None` is 'nothing classified this' and UNKNOWN is 'it could not be
    established'. Two different findings, so two different stored values."""
    graded = journal.record_entry(_trade(symbol="SPY"))
    ungraded = journal.record_entry(_trade(symbol="QQQ"))

    journal.record_exit(
        graded,
        exit_time=ENTRY,
        exit_price=575.0,
        realised_pnl_usd=-50.0,
        exit_reason=ExitReason.STOP_HIT,
        exit_price_estimated=False,
    )
    journal.record_exit(
        ungraded, exit_time=ENTRY, exit_price=580.0, realised_pnl_usd=0.0
    )

    by_symbol = {t.symbol: t for t in journal.closed_trades()}
    assert by_symbol["SPY"].exit_reason is ExitReason.STOP_HIT
    assert by_symbol["SPY"].exit_price_estimated is False
    assert by_symbol["QQQ"].exit_reason is None
    assert by_symbol["QQQ"].exit_price_estimated is None
