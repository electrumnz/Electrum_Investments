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
    ExitReason,
    FillState,
    PositionAction,
    PositionActionRecord,
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
    dream_id          INTEGER,
    -- The stop actually in force, once it is no longer the one the position was
    -- sized against. NULL means it has never moved. `planned_stop` above stays
    -- the sizing figure so R keeps meaning what it meant at entry — see
    -- `Trade.current_stop`. Added to an existing table by
    -- `_add_current_stop_column`, for the same reason `dream_id` needs one.
    current_stop      REAL,
    -- What was ORDERED, kept beside what filled. `qty` and `entry_price` above
    -- are written from the proposal at submission and corrected by `reconcile`
    -- once the entry order is terminal, so without these two the correction
    -- erases the only record of what was actually asked for. NULL on a row
    -- written before they existed — absent, never zero.
    submitted_qty     REAL,
    submitted_price   REAL,
    -- How much of that is KNOWN to have happened: see `models.FillState`. NULL
    -- reads back as `unconfirmed`, which is not a migration artefact wearing an
    -- optimistic label — it is literally true of a legacy row, because nothing
    -- was ever read back from the broker for it.
    fill_state        TEXT,
    -- Why the position closed: see `models.ExitReason`. NULL means nothing was
    -- recorded, which is deliberately NOT the same as `unknown`, where the
    -- question was asked and could not be answered.
    exit_reason       TEXT,
    -- Whether `exit_price` is a real reading or the fallback to entry price.
    -- NULL means it was not recorded; 0 and 1 are the two answers. Not defaulted
    -- to 0 — the good outcome must not be what an absence looks like, and an
    -- exit review reading an estimated price as evidence would report a
    -- confident `closed_early` off nothing.
    --
    -- All five added to an existing table by `_add_fill_and_exit_columns`.
    exit_price_estimated INTEGER
);

CREATE INDEX IF NOT EXISTS idx_trades_open   ON trades (exit_time);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades (symbol);
CREATE INDEX IF NOT EXISTS idx_trades_entry  ON trades (entry_time);

-- Every intentional move on an OPEN position, with the reason it was made.
--
-- A NEW TABLE needs no migration and that is worth stating rather than leaving
-- somebody to work out: `CREATE TABLE IF NOT EXISTS` is a no-op only on a table
-- that ALREADY EXISTS, so this statement does reach the database on the
-- droplet and does create the table there. The trap that has bitten this
-- repository twice is editing an existing table's definition, which is why
-- `current_stop` above gets a migration and this does not.
--
-- Append-only by convention: nothing here updates or deletes a row. The whole
-- value of the record is that it cannot be tidied afterwards, which is the same
-- argument the JSONL audit log rests on.
--
-- `reason` is NOT NULL and is refused when blank at two layers above this. The
-- absence of a reason is what the Board's `unexplained-move` tag exists to
-- show, so a row carrying an empty one would silence the tag while explaining
-- nothing.
CREATE TABLE IF NOT EXISTS position_actions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    -- NULL for a move on a position the journal has never seen. Absent, never
    -- zero; the same fact as a symbol in `symbols_with_unknown_risk`.
    trade_id        INTEGER,
    symbol          TEXT    NOT NULL,
    action          TEXT    NOT NULL,
    actor           TEXT    NOT NULL,
    at              TEXT    NOT NULL,
    reason          TEXT    NOT NULL,
    before_stop     REAL,
    after_stop      REAL,
    before_qty      REAL,
    after_qty       REAL,
    -- Alpaca REPLACES a stop leg rather than editing it, so this is the id of
    -- the NEW order and not the one that was resting before the move.
    broker_order_id TEXT,
    -- Whether the move reached the broker at all. 0 is an ordinary state —
    -- Alpaca accepts no bracket on crypto, so a crypto stop has always been a
    -- journal figure — and it is stored because "the stop moved" and "the stop
    -- moved at the broker" are different claims.
    reached_broker  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_position_actions_symbol ON position_actions (symbol);
CREATE INDEX IF NOT EXISTS idx_position_actions_at     ON position_actions (at);
CREATE INDEX IF NOT EXISTS idx_position_actions_trade  ON position_actions (trade_id);

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


def _add_current_stop_column(conn: sqlite3.Connection) -> None:
    """Give a `trades` table that predates stop moves its `current_stop` column.

    **The third time this exact migration has been needed, and the reason it is
    needed a third time is unchanged.** `CREATE TABLE IF NOT EXISTS` does
    nothing to a table that already exists, so adding `current_stop` to `SCHEMA`
    changes what a fresh journal gets and nothing whatever about
    `data/journal.db` on the droplet — the file holding the one live position.
    The suite cannot see that gap on its own: every other test here builds its
    journal from scratch in a `tmp_path` and therefore always gets the new
    shape. That is how 866 green tests once sat over a database that could not
    store the row the models had just been changed to allow, found by placing a
    real order and watching the journal write fail AFTER the broker call.

    Three properties, the same three the two migrations above establish:

    - **One statement, so there is no half-migrated state.** SQLite's
      `ALTER TABLE ... ADD COLUMN` is atomic on its own, and nothing is dropped,
      so there is no table rebuild to tear.
    - **Additive only.** A nullable column with no default touches no existing
      row, and NULL is exactly the right answer for every trade already open:
      none of their stops has been moved, so the stop in force is still the stop
      they were sized against. `Trade.effective_stop` reads it that way.
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
    if "current_stop" in columns:
        return

    log.warning(
        "journal_migrating_current_stop",
        detail=(
            "This journal predates recorded stop moves. Adding the current_stop "
            "column in place; no existing row is modified, and NULL is correct "
            "for every position whose stop has never been moved."
        ),
    )
    conn.execute("ALTER TABLE trades ADD COLUMN current_stop REAL")


#: The columns that carry what was ORDERED versus what happened, and why the
#: position closed. Declared once, so `SCHEMA` above and the migration below
#: cannot drift apart about which columns exist or what type they are.
_FILL_AND_EXIT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("submitted_qty", "REAL"),
    ("submitted_price", "REAL"),
    ("fill_state", "TEXT"),
    ("exit_reason", "TEXT"),
    ("exit_price_estimated", "INTEGER"),
)


def _add_fill_and_exit_columns(conn: sqlite3.Connection) -> None:
    """Give a `trades` table the five columns fill correction and exit review need.

    **The fourth time this migration has been needed, and the reason has not
    changed once.** `CREATE TABLE IF NOT EXISTS` does nothing to a table that
    already exists, so adding a column to `SCHEMA` changes what a FRESH journal
    gets and nothing whatever about `data/journal.db` on the droplet — the file
    holding the one live position. The suite cannot see that gap on its own:
    every other test here builds its journal from scratch in a `tmp_path` and
    therefore always gets the new shape. That is how 866 green tests once sat
    over a database that could not store the row the models had just been
    changed to allow, found by placing a real order and watching the journal
    write fail AFTER the broker call — a live order the journal had never heard
    of, with the 2% cap blind to it.

    **One function for five columns rather than five functions**, and that is
    safe for the same reason each of the three before it was safe on its own:

    - **Each `ALTER TABLE ... ADD COLUMN` is atomic by itself**, and nothing is
      dropped, so there is no table to rebuild and no half-rebuilt state to be
      left in. A run interrupted between two of them leaves a table with some of
      the columns, which is a perfectly ordinary table; the next open adds the
      rest. That is a weaker guarantee than one transaction and it is the
      RIGHT one here, because the alternative — the rebuild in
      `_drop_planned_target_not_null` — copies every row and is only needed when
      a constraint has to be dropped.
    - **Additive only.** Every column is nullable with no default, so no
      existing row is touched, and `NULL` is the correct answer for all of them
      on a legacy row: nothing was read back from the broker about that entry,
      and nothing classified its exit.
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
    missing = [(n, t) for n, t in _FILL_AND_EXIT_COLUMNS if n not in columns]
    if not missing:
        return

    log.warning(
        "journal_migrating_fill_and_exit_columns",
        columns=[n for n, _ in missing],
        detail=(
            "This journal predates fill confirmation and the exit review. "
            "Adding the columns in place; no existing row is modified, and NULL "
            "is correct for every one of them on a trade whose fill was never "
            "read back and whose exit was never classified."
        ),
    )
    for name, sql_type in missing:
        conn.execute(f"ALTER TABLE trades ADD COLUMN {name} {sql_type}")


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
            _add_current_stop_column(conn)
            _add_fill_and_exit_columns(conn)

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
                    entry_order_id, dream_id,
                    submitted_qty, submitted_price, fill_state
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    trade.submitted_qty,
                    trade.submitted_price,
                    trade.fill_state.value,
                ),
            )
            return int(cursor.lastrowid or 0)

    def confirm_fill(
        self,
        trade_id: int,
        *,
        fill_state: FillState,
        qty: float | None = None,
        entry_price: float | None = None,
    ) -> None:
        """Replace what the PROPOSAL said with what the broker actually did.

        Called by `reconcile`, which is the only thing in a position to know:
        `record_fill` runs immediately after `broker.place_order` and writes the
        proposal's quantity and limit price, because waiting for a terminal
        order state would leave a live position unjournalled in the meantime —
        the `14b88c8` hole, where an order rests at the broker and the 2% cap is
        blind to it.

        `qty` and `entry_price` are optional because the state moves on its own
        for an order that is merely RESTING: there is a fact to record and no
        figures to correct yet.

        **Only an OPEN trade is touched.** Rewriting the entry of a closed trade
        would move the denominator of an R-multiple that has already been
        reported, so a correction that arrives late is dropped rather than
        applied to history.
        """
        sets = ["fill_state = ?"]
        params: list[Any] = [fill_state.value]
        if qty is not None:
            sets.append("qty = ?")
            params.append(qty)
        if entry_price is not None:
            sets.append("entry_price = ?")
            params.append(entry_price)
        params.append(trade_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE trades SET {', '.join(sets)} "
                "WHERE id = ? AND exit_time IS NULL",
                tuple(params),
            )

    def record_exit(
        self,
        trade_id: int,
        *,
        exit_time: datetime,
        exit_price: float,
        realised_pnl_usd: float,
        fees_usd: float | None = None,
        exit_order_id: str | None = None,
        exit_reason: ExitReason | None = None,
        exit_price_estimated: bool | None = None,
    ) -> None:
        """Close out a trade. Fees are added to whatever entry already charged.

        `exit_reason` is WHY it closed and `exit_price_estimated` says whether
        the price above is a reading or a fallback. Both default to `None`,
        which stores NULL and means "not recorded" — deliberately not
        `ExitReason.UNKNOWN`, which means the question was asked and could not be
        answered, and deliberately not `False`, which would let an unrecorded
        estimate read as a confirmed fill.
        """
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
                       exit_order_id = ?, exit_reason = ?,
                       exit_price_estimated = ?
                 WHERE id = ?
                """,
                (
                    _iso(exit_time),
                    exit_price,
                    realised_pnl_usd,
                    exit_order_id,
                    exit_reason.value if exit_reason is not None else None,
                    None if exit_price_estimated is None else int(exit_price_estimated),
                    trade_id,
                ),
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
        """Combined risk IN FORCE across every open trade.

        Feeds `AccountSnapshot.open_risk_usd`, which the total-risk cap counts
        against. Alpaca cannot supply this — it holds stop-losses as separate
        orders, not as attributes of a position — so the journal is the only
        place that knows what each open trade stands to lose.

        `current_risk_usd` rather than `planned_risk_usd`, because the cap is a
        statement about what the stops would cost if they filled, and a stop
        that has been tightened would cost less. The two are identical for every
        trade whose stop has never moved, which is all of them until one is.
        """
        return sum(t.current_risk_usd for t in self.open_trades())

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
            # Same again. NULL means the stop has never moved, so the stop in
            # force is still `planned_stop` — which `effective_stop` answers.
            current_stop=row["current_stop"],
            submitted_qty=row["submitted_qty"],
            submitted_price=row["submitted_price"],
            # NULL reads back as UNCONFIRMED, which is not a default standing in
            # for a missing value — it is what the row actually is. Nothing was
            # ever read back from the broker about a trade written before this
            # column existed, and `UNCONFIRMED` says exactly that.
            fill_state=(
                FillState(row["fill_state"])
                if row["fill_state"]
                else FillState.UNCONFIRMED
            ),
            # NULL stays None here, because "nothing classified this exit" and
            # "the exit was classified and could not be established" are
            # different findings and `ExitReason.UNKNOWN` is only the second.
            exit_reason=(
                ExitReason(row["exit_reason"]) if row["exit_reason"] else None
            ),
            exit_price_estimated=(
                None
                if row["exit_price_estimated"] is None
                else bool(row["exit_price_estimated"])
            ),
        )

    # --------------------------------------------------- position actions
    #
    # The record of every intentional move on a position already open, and the
    # reason it was made. `record_exit` takes a price, a time and a realised
    # figure, so stop-hit, target-hit, closed-by-hand and expiry are
    # indistinguishable afterwards; this captures the reason at the moment of
    # the move, which is the only moment anybody knows it.

    def record_position_action(self, action: PositionActionRecord) -> int:
        """Store one move. Returns its id. **A blank reason is refused here too.**

        `PositionActionRecord` already refuses one, so this is a second guard on
        the same rule rather than the only one. It is worth the duplication: the
        absence of a reason is precisely what the Board's `unexplained-move`
        state exists to make visible, so a row with an empty reason would be an
        explained move that explains nothing — and it would silence the tag
        while doing it, which is worse than never having recorded the move at
        all.
        """
        if not action.reason.strip():
            raise ValueError(
                f"a position action on {action.symbol} needs a reason. A move "
                "with no reason recorded is indistinguishable from a move "
                "nobody made deliberately, which is the state the "
                "unexplained-move tag exists to report."
            )
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO position_actions (
                    trade_id, symbol, action, actor, at, reason,
                    before_stop, after_stop, before_qty, after_qty,
                    broker_order_id, reached_broker
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    action.trade_id,
                    action.symbol,
                    action.action.value,
                    action.actor,
                    _iso(action.at),
                    action.reason,
                    action.before_stop,
                    action.after_stop,
                    action.before_qty,
                    action.after_qty,
                    action.broker_order_id,
                    1 if action.reached_broker else 0,
                ),
            )
            return int(cursor.lastrowid or 0)

    def record_stop_move(self, action: PositionActionRecord) -> int:
        """Move a trade's stop AND record why, in one transaction.

        Two writes that must not come apart. Updating the trade without the
        record produces exactly the state this feature exists to expose — a
        stop that changed with no reason on file — and writing the record
        without the update leaves the caps counting a stop that is no longer
        there. One connection, one commit, so the pair is atomic.

        The trade row is only touched when `trade_id` and `after_stop` are both
        present. A move on a position the journal has never seen is still worth
        recording; there is simply no row to update.
        """
        if not action.reason.strip():
            raise ValueError(
                f"a stop move on {action.symbol} needs a reason; nothing was "
                "written."
            )
        with self._connect() as conn:
            if action.trade_id is not None and action.after_stop is not None:
                conn.execute(
                    "UPDATE trades SET current_stop = ? WHERE id = ? AND exit_time IS NULL",
                    (action.after_stop, action.trade_id),
                )
            cursor = conn.execute(
                """
                INSERT INTO position_actions (
                    trade_id, symbol, action, actor, at, reason,
                    before_stop, after_stop, before_qty, after_qty,
                    broker_order_id, reached_broker
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    action.trade_id,
                    action.symbol,
                    action.action.value,
                    action.actor,
                    _iso(action.at),
                    action.reason,
                    action.before_stop,
                    action.after_stop,
                    action.before_qty,
                    action.after_qty,
                    action.broker_order_id,
                    1 if action.reached_broker else 0,
                ),
            )
            return int(cursor.lastrowid or 0)

    def position_actions(
        self, *, symbol: str | None = None, limit: int | None = None
    ) -> list[PositionActionRecord]:
        """Recorded moves, NEWEST FIRST.

        Newest first on purpose, and it is the ordering `closed_trades` gets
        wrong: that query sorts ascending before its LIMIT, so a limited call
        drops the newest rows while leaving the earliest bucket full. A limited
        read of an action log has to be the most recent moves or it answers a
        question nobody asked.
        """
        sql = "SELECT * FROM position_actions"
        params: list[Any] = []
        if symbol:
            sql += " WHERE symbol = ?"
            params.append(symbol)
        sql += " ORDER BY at DESC, id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._to_action(r) for r in rows]

    @staticmethod
    def _to_action(row: sqlite3.Row) -> PositionActionRecord:
        at = _dt(row["at"])
        assert at is not None  # NOT NULL in schema
        return PositionActionRecord(
            id=row["id"],
            trade_id=row["trade_id"],
            symbol=row["symbol"],
            action=PositionAction(row["action"]),
            actor=row["actor"],
            at=at,
            reason=row["reason"],
            before_stop=row["before_stop"],
            after_stop=row["after_stop"],
            before_qty=row["before_qty"],
            after_qty=row["after_qty"],
            broker_order_id=row["broker_order_id"],
            reached_broker=bool(row["reached_broker"]),
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
