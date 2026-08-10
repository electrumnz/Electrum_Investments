"""Trade journal — the queryable record of what the bot actually did.

SQLite rather than Postgres: one user, one machine, no server to run, and the
whole thing is a single file that can be copied or deleted. The JSONL audit log
in `audit.py` stays exactly as it is — append-only evidence. This is the layer
you can ask questions of.

It also holds the stand-down state, deliberately. Keeping that in memory on the
risk gate would mean a stand-down evaporated whenever the process restarted, and
restarting the process is exactly what somebody tilting would do.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from .models import (
    AssetClass,
    Direction,
    ExecutionMode,
    StandDownState,
    Trade,
    TradeOutcome,
)

log = structlog.get_logger()

DEFAULT_DB_PATH = Path("data") / "journal.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol            TEXT    NOT NULL,
    asset_class       TEXT    NOT NULL,
    strategy          TEXT    NOT NULL DEFAULT 'unspecified',
    direction         TEXT    NOT NULL,
    qty               REAL    NOT NULL,
    entry_time        TEXT    NOT NULL,
    entry_price       REAL    NOT NULL,
    planned_stop      REAL    NOT NULL,
    planned_target    REAL,            -- NULL when opened with no target
    exit_time         TEXT,
    exit_price        REAL,
    realised_pnl_usd  REAL,
    fees_usd          REAL    NOT NULL DEFAULT 0,
    mae_usd           REAL    NOT NULL DEFAULT 0,
    mfe_usd           REAL    NOT NULL DEFAULT 0,
    execution_mode    TEXT    NOT NULL DEFAULT 'paper',
    rationale         TEXT    NOT NULL DEFAULT '',
    entry_order_id    TEXT,
    exit_order_id     TEXT,
    -- The adopted dream whose grant permitted this symbol. NULL for a trade in
    -- a symbol config/rules.yaml already allowed, which is every trade so far.
    -- Provenance, never endorsement — see `Trade.dream_id`.
    --
    -- Listed here so a FRESH journal is built with it, and added to an existing
    -- one by `_add_dream_id_column`. Both halves are needed: `CREATE TABLE IF
    -- NOT EXISTS` is a no-op on the table already on the droplet.
    dream_id          INTEGER
);

CREATE INDEX IF NOT EXISTS idx_trades_open   ON trades (exit_time);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades (symbol);
CREATE INDEX IF NOT EXISTS idx_trades_entry  ON trades (entry_time);

-- Single row, id enforced to 1. Small enough that a row-per-change history
-- would be noise; changes worth auditing land in the JSONL log instead.
CREATE TABLE IF NOT EXISTS stand_down_state (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    stage               INTEGER NOT NULL DEFAULT 0,
    started_at          TEXT,
    ends_at             TEXT,
    consecutive_losses  INTEGER NOT NULL DEFAULT 0,
    last_triggered_at   TEXT
);

CREATE TABLE IF NOT EXISTS daily_equity (
    day         TEXT PRIMARY KEY,
    equity_usd  REAL NOT NULL,
    recorded_at TEXT NOT NULL
);
"""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    # Rows written before a timezone was attached should still compare safely.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _drop_planned_target_not_null(conn: sqlite3.Connection) -> None:
    """Make `planned_target` nullable on a database that predates the change.

    **`CREATE TABLE IF NOT EXISTS` is a no-op on a table that already exists**,
    so editing `SCHEMA` changes what a FRESH database gets and nothing at all
    about the one already on the box. That gap is invisible from the test suite,
    which builds every journal from scratch in a `tmp_path` and therefore always
    gets the new shape.

    Found the expensive way. `take_profit_price` became optional, the model and
    `SCHEMA` were both updated, 866 tests passed — and the first real
    no-target order reached Alpaca, rested there, and then failed to journal on
    `NOT NULL constraint failed: trades.planned_target`. The order was live and
    unrecorded, which is precisely the `14b88c8` bug: `open_risk_usd` cannot
    count a position the journal has never heard of, so the 2% total-risk cap
    was blind to it.

    SQLite cannot drop a NOT NULL with `ALTER`, so the table is rebuilt. The
    whole thing runs in one transaction: either the new table replaces the old
    with every row intact, or nothing changes. A half-migrated journal would be
    worse than an unmigrated one.

    Idempotent, and cheap to re-run: `PRAGMA table_info` is a lookup, so a
    database that is already correct pays one query at startup and stops.
    """
    columns = conn.execute("PRAGMA table_info(trades)").fetchall()
    target = next((c for c in columns if c["name"] == "planned_target"), None)
    if target is None or not target["notnull"]:
        return

    log.warning(
        "journal_migrating_planned_target_nullable",
        detail=(
            "This journal predates optional take-profit. Rebuilding the trades "
            "table so a trade with no target can be recorded."
        ),
    )
    names = ", ".join(c["name"] for c in columns)
    conn.executescript(
        "PRAGMA foreign_keys=OFF;\n"
        "BEGIN;\n"
        "ALTER TABLE trades RENAME TO trades_legacy;\n"
        + SCHEMA
        + f"INSERT INTO trades ({names}) SELECT {names} FROM trades_legacy;\n"
        "DROP TABLE trades_legacy;\n"
        "COMMIT;\n"
        "PRAGMA foreign_keys=ON;\n"
    )


def _add_dream_id_column(conn: sqlite3.Connection) -> None:
    """Give a `trades` table that predates dream adoption its `dream_id` column.

    **The same trap as `_drop_planned_target_not_null`, and it has already been
    paid for once.** `CREATE TABLE IF NOT EXISTS` does nothing to a table that
    already exists, so editing `SCHEMA` changes what a fresh journal gets and
    nothing whatever about `data/journal.db` on the droplet. The suite is
    structurally blind to it — every test builds its journal from scratch in a
    `tmp_path` and therefore always gets the new shape — which is how 866 green
    tests once sat over a database that could not store the row the models had
    just been changed to allow. That was found by placing a real order and
    watching the journal write fail AFTER the broker call, leaving a live
    position the journal had never heard of and the 2% cap blind to it.

    Three properties, and they are the ones the two existing migrations here and
    in `dreaming.py` already established:

    - **One statement, so there is no half-migrated state to be left in.**
      SQLite's `ALTER TABLE ... ADD COLUMN` is atomic on its own; there is no
      table rebuild to tear, because nothing is dropped. The rebuild in
      `_drop_planned_target_not_null` was only needed because SQLite cannot drop
      a NOT NULL with `ALTER`.
    - **Additive only.** A nullable column with no default touches no existing
      row, and `NULL` is exactly the right answer for every trade placed before
      grants existed: none of them came from a dream.
    - **Idempotent and cheap.** It runs on every open rather than behind a
      version flag, because a version flag is one more piece of bookkeeping that
      can be wrong. `PRAGMA table_info` is a lookup, so a correct database pays
      one query and stops.

    Any future change to `SCHEMA` needs a migration beside it and a test that
    starts from the old shape. The suite will not warn you.
    """
    columns = {str(c["name"]) for c in conn.execute("PRAGMA table_info(trades)")}
    if not columns:  # pragma: no cover - SCHEMA has just created the table
        return
    if "dream_id" in columns:
        return

    log.warning(
        "journal_migrating_dream_id",
        detail=(
            "This journal predates dream adoption. Adding the dream_id column "
            "in place; no existing row is modified, and NULL is the correct "
            "answer for every trade placed before grants existed."
        ),
    )
    conn.execute("ALTER TABLE trades ADD COLUMN dream_id INTEGER")


class Journal:
    def __init__(self, path: Path = DEFAULT_DB_PATH) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            # Order matters in one direction only: the rebuild copies the OLD
            # table's column list, so running it first means a legacy journal
            # comes out of it already carrying `dream_id` from SCHEMA and the
            # second migration is a no-op. The reverse order also works; this
            # one keeps the expensive migration reading the shape it was
            # written against.
            _drop_planned_target_not_null(conn)
            _add_dream_id_column(conn)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------- trades

    def record_entry(self, trade: Trade) -> int:
        """Persist a newly opened trade. Returns its id."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO trades (
                    symbol, asset_class, strategy, direction, qty,
                    entry_time, entry_price, planned_stop, planned_target,
                    fees_usd, mae_usd, mfe_usd, execution_mode, rationale,
                    entry_order_id, dream_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    trade.symbol,
                    trade.asset_class.value,
                    trade.strategy,
                    trade.direction.value,
                    trade.qty,
                    _iso(trade.entry_time),
                    trade.entry_price,
                    trade.planned_stop,
                    trade.planned_target,
                    trade.fees_usd,
                    trade.mae_usd,
                    trade.mfe_usd,
                    trade.execution_mode.value,
                    trade.rationale,
                    trade.entry_order_id,
                    trade.dream_id,
                ),
            )
            return int(cursor.lastrowid or 0)

    def record_exit(
        self,
        trade_id: int,
        *,
        exit_time: datetime,
        exit_price: float,
        realised_pnl_usd: float,
        fees_usd: float | None = None,
        exit_order_id: str | None = None,
    ) -> None:
        """Close out a trade. Fees are added to whatever entry already charged."""
        with self._connect() as conn:
            if fees_usd is not None:
                conn.execute(
                    "UPDATE trades SET fees_usd = fees_usd + ? WHERE id = ?",
                    (fees_usd, trade_id),
                )
            conn.execute(
                """
                UPDATE trades
                   SET exit_time = ?, exit_price = ?, realised_pnl_usd = ?,
                       exit_order_id = ?
                 WHERE id = ?
                """,
                (_iso(exit_time), exit_price, realised_pnl_usd, exit_order_id, trade_id),
            )

    def update_excursion(self, trade_id: int, unrealised_pnl_usd: float) -> None:
        """Widen MAE/MFE from a fresh mark. Called once per decision cycle.

        Sampled, not tick-accurate: a spike between two polls is invisible, so
        both figures understate the true excursion. That is a real limitation
        and is surfaced in the UI rather than papered over.
        """
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE trades
                   SET mae_usd = MIN(mae_usd, ?),
                       mfe_usd = MAX(mfe_usd, ?)
                 WHERE id = ? AND exit_time IS NULL
                """,
                (unrealised_pnl_usd, unrealised_pnl_usd, trade_id),
            )

    def open_trades(self) -> list[Trade]:
        return self._query("SELECT * FROM trades WHERE exit_time IS NULL ORDER BY entry_time")

    def open_risk_usd(self) -> float:
        """Combined planned risk across every open trade.

        Feeds `AccountSnapshot.open_risk_usd`, which the total-risk cap counts
        against. Alpaca cannot supply this — it holds stop-losses as separate
        orders, not as attributes of a position — so the journal is the only
        place that knows what each open trade was designed to lose.
        """
        return sum(t.planned_risk_usd for t in self.open_trades())

    def open_trade_for(self, symbol: str) -> Trade | None:
        rows = self._query(
            "SELECT * FROM trades WHERE exit_time IS NULL AND symbol = ? "
            "ORDER BY entry_time DESC LIMIT 1",
            (symbol,),
        )
        return rows[0] if rows else None

    def closed_trades(
        self,
        *,
        since: datetime | None = None,
        strategy: str | None = None,
        asset_class: AssetClass | None = None,
        limit: int | None = None,
    ) -> list[Trade]:
        sql = "SELECT * FROM trades WHERE exit_time IS NOT NULL"
        params: list[Any] = []
        if since:
            sql += " AND exit_time >= ?"
            params.append(_iso(since))
        if strategy:
            sql += " AND strategy = ?"
            params.append(strategy)
        if asset_class:
            sql += " AND asset_class = ?"
            params.append(asset_class.value)
        sql += " ORDER BY exit_time"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self._query(sql, tuple(params))

    def recent_closed(self, count: int) -> list[Trade]:
        """Most recently closed trades, oldest first — the order a streak reads in."""
        rows = self._query(
            "SELECT * FROM trades WHERE exit_time IS NOT NULL "
            "ORDER BY exit_time DESC LIMIT ?",
            (count,),
        )
        return list(reversed(rows))

    def consecutive_losses(self, scratch_threshold_r: float) -> int:
        """Count losses back from the most recent close until a non-loss.

        Scratches neither count nor reset — they are simply skipped, so a run of
        real losses separated by a scratch still reads as a streak.
        """
        streak = 0
        for trade in reversed(self.recent_closed(50)):
            outcome = trade.outcome(scratch_threshold_r)
            if outcome == TradeOutcome.LOSS:
                streak += 1
            elif outcome == TradeOutcome.WIN:
                break
        return streak

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[Trade]:
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._to_trade(r) for r in rows]

    @staticmethod
    def _to_trade(row: sqlite3.Row) -> Trade:
        entry_time = _dt(row["entry_time"])
        assert entry_time is not None  # NOT NULL in schema
        return Trade(
            id=row["id"],
            symbol=row["symbol"],
            asset_class=AssetClass(row["asset_class"]),
            strategy=row["strategy"],
            direction=Direction(row["direction"]),
            qty=row["qty"],
            entry_time=entry_time,
            entry_price=row["entry_price"],
            planned_stop=row["planned_stop"],
            planned_target=row["planned_target"],
            exit_time=_dt(row["exit_time"]),
            exit_price=row["exit_price"],
            realised_pnl_usd=row["realised_pnl_usd"],
            fees_usd=row["fees_usd"],
            mae_usd=row["mae_usd"],
            mfe_usd=row["mfe_usd"],
            execution_mode=ExecutionMode(row["execution_mode"]),
            rationale=row["rationale"],
            entry_order_id=row["entry_order_id"],
            exit_order_id=row["exit_order_id"],
            # Read directly rather than defensively: the migration runs on every
            # open, so a `Journal` that exists has this column. A row written
            # before grants existed carries NULL, which is the right answer.
            dream_id=row["dream_id"],
        )

    # --------------------------------------------------------- stand-down

    def get_stand_down(self) -> StandDownState:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM stand_down_state WHERE id = 1").fetchone()
        if row is None:
            return StandDownState()
        return StandDownState(
            stage=row["stage"],
            started_at=_dt(row["started_at"]),
            ends_at=_dt(row["ends_at"]),
            consecutive_losses=row["consecutive_losses"],
            last_triggered_at=_dt(row["last_triggered_at"]),
        )

    def save_stand_down(self, state: StandDownState) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO stand_down_state
                    (id, stage, started_at, ends_at, consecutive_losses, last_triggered_at)
                VALUES (1,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    stage = excluded.stage,
                    started_at = excluded.started_at,
                    ends_at = excluded.ends_at,
                    consecutive_losses = excluded.consecutive_losses,
                    last_triggered_at = excluded.last_triggered_at
                """,
                (
                    state.stage,
                    _iso(state.started_at),
                    _iso(state.ends_at),
                    state.consecutive_losses,
                    _iso(state.last_triggered_at),
                ),
            )

    # ------------------------------------------------------------- equity

    def record_equity(self, equity_usd: float, *, when: datetime | None = None) -> None:
        """One row per day; later calls overwrite that day's figure."""
        moment = when or datetime.now(UTC)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO daily_equity (day, equity_usd, recorded_at) VALUES (?,?,?)
                ON CONFLICT(day) DO UPDATE SET
                    equity_usd = excluded.equity_usd,
                    recorded_at = excluded.recorded_at
                """,
                (moment.date().isoformat(), equity_usd, _iso(moment)),
            )

    def equity_curve(self) -> list[tuple[str, float]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT day, equity_usd FROM daily_equity ORDER BY day"
            ).fetchall()
        return [(r["day"], r["equity_usd"]) for r in rows]
