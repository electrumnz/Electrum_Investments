"""A queryable index over the audit log, derived and disposable.

`get_recent_news` and `get_recent_decisions` answer "what happened lately" by
scanning dated files newest-first. That is the right shape for a window and the
wrong shape for a question. "What did we decide about AAPL in March", "which
rejection reason fires most often", "how many times did we watch SPY without
naming a trigger" all mean walking every file and parsing every line, and the
chat surface cannot do that in a turn.

This module builds a SQLite index so those are one query.

## The audit log stays the source of truth

**Nothing here is authoritative and nothing in the trading path reads it.** The
JSONL files are the record; `insight.db` is a cache of them that can be deleted
at any moment and rebuilt from scratch. Three things follow, and all three are
the point rather than side effects:

- **The schema can change freely.** `SCHEMA_VERSION` is checked on open and a
  mismatch drops every table and replays the log. The audit log itself is
  append-only and never migrated — a reader that rejected yesterday's format
  would throw away the history it exists to preserve — and this is how you get
  a schema you can evolve without ever touching that guarantee.
- **A bug in here cannot cost a trade.** The index is downstream of everything.
  The risk gate, the loop and the journal neither read nor write it.
- **It must never be backed up.** `deploy/backup-journal.sh` covers the journal
  and the audit log because those are irreplaceable. A derived file in a backup
  is a way to restore a stale index over a fresh one.

## Indexing is incremental, and idempotent so the increment cannot corrupt it

A dated file is finished once the UTC day rolls over; only today's grows. So
each file's byte offset is remembered and the next pass reads from there.

Two details that are easy to get wrong:

- **The offset is taken after the last COMPLETE line**, never after the last
  byte. The log is written by a long-running process, so the final line can be
  a partial write — `audit._parse` already tolerates exactly that. Recording
  the file size instead would skip the rest of that line forever once the
  append completed it.
- **Every row uses a natural primary key and `INSERT OR REPLACE`**, so
  indexing the same line twice changes nothing. That makes the offset an
  optimisation rather than a correctness requirement: if it is ever wrong, the
  worst outcome is repeated work. Correctness that depends on bookkeeping being
  right is correctness waiting to break.

News is stored as raw per-cycle **sightings** rather than as a deduped table
with a running count, for the same reason. A count incremented during indexing
would double on a re-read; `GROUP BY` at query time cannot.

## What is deliberately not parsed

`MarketInputs.indicators` and `.intraday` hold the *rendered* line —
`"close 580.12, sma20 574.30, atr 6.41, ..."` — because that is what the loop
recorded. They are indexed as searchable text and no attempt is made to pull
numbers back out of them.

Reversing prose into figures would produce a number nobody can check, which is
the failure `indicators.py` exists to prevent, arriving from the other
direction. If numeric history is wanted, the fix is to record the numbers in
`MarketInputs` — a change to what gets written, which no amount of cleverness
here can substitute for, and which only starts paying from the day it ships.

## Full-text search, and why it is FTS5 rather than embeddings

`docs/HUGGINGFACE.md` measured both on this box. FTS5 is already compiled into
the stdlib `sqlite3`, indexes 50,000 documents in 0.30 s and answers a BM25
query in 23.9 ms at **30 MB RSS**; the obvious embedding default measured 97
documents a second at **477 MB peak RSS**, on a 2 GB droplet that is also
running the trading loop, the web unit and a Hermes process per chat message.
So this is full text and there is no model. The trigger for revisiting is a
*recorded* question that FTS5 answered badly, which the audit log makes
checkable.

`documents` is the corpus and `search` is an FTS5 index over it with
`content='documents'`, so the text is stored once and the virtual table holds
only the inverted index.

**An FTS5 table is not naturally idempotent, and that is the trap here.** The
rest of this module survives a re-read because every row has a natural key and
`INSERT OR REPLACE` lands on it. An FTS5 index has no natural key: insert the
same document twice and it is in there twice, ranked twice, counted twice.
Three things hold that shut and none of them is decoration:

- **`documents` carries the natural key** (`doc_id`) and `search` is derived
  from it by trigger, so there is exactly one FTS entry per document by
  construction rather than by care.
- **The upsert is `ON CONFLICT ... DO UPDATE`, never `INSERT OR REPLACE`.**
  `REPLACE` resolves a conflict by deleting the old row, and a `BEFORE`/`AFTER
  DELETE` trigger **does not fire** for that delete unless
  `PRAGMA recursive_triggers` is on. The old FTS entry would survive, the new
  one would be added beside it, and the count would double silently. This is
  the one place in this module where the usual idiom is wrong.
- **The update is skipped when the body is unchanged**, which is every re-read,
  so a repeated pass costs one failed insert per document and no index churn.

## What is searchable, and what deliberately is not

The corpus is the text somebody could remember a PHRASE from: the model's
rationales, its assessment and position-plan prose, the gate's rejection
reasons, cycle notes, headlines, social posts, blackout windows and audit-event
payloads.

**`readings.summary` is excluded**, and it is the interesting exclusion. It is
searchable text and always was — nothing here parses it back into numbers, that
rule is untouched — but it is a TEMPLATE with figures substituted, so every row
carries the same words and none of them tells one row from another. It is also
the largest thing in the index by volume: two rendered lines per symbol per
cycle against roughly one prose document per symbol per cycle. Indexing it
would spend most of the corpus on rows that discriminate nothing, and the
questions it answers — "when was the close below 641.20", "when was sma200
unavailable" — are answered EXACTLY by `numeric_readings`, and approximately by
searching prose. That is the same finding `docs/HUGGINGFACE.md` reached about
embeddings, one layer down: where SQL answers a question exactly, a ranked
search is a worse tool for it. `readings` is still queryable with `LIKE`
through `run_query`.

Dreams are not here either, and cannot be: this index is derived from
`audit/*.jsonl` and nothing else, and a dream lives in `data/dreams.db`. What
DOES reach the corpus is a consideration, because `raise_consideration` writes
an audit event — which is the whole point of it being an event.

## What it costs, measured against the same log without it

14 days of 96-cycle days — 1,344 cycles, 13,376 documents — with the corpus
switched off and on, so the comparison is this feature and nothing else:

| | without | with |
|---|---:|---:|
| cold build (full replay) | 422 ms | **1,111 ms** |
| no-op refresh | 1.15 ms | 0.81 ms |
| delta, one cycle appended | 3.11 ms | 4.87 ms |
| database | 11.0 MB | 16.8 MB |

Searches over that corpus run 1.1-10.9 ms, which is the band the existing SQL
queries already sit in; the 10.9 ms is a term appearing in nearly every
document, and it is the worst case rather than the normal one. At 90 days and
77,132 documents a search is 2.7-65 ms and the one-cycle delta is unchanged at
5.9 ms — the number that matters, because every tool call pays it and a full
replay happens only on `electrum-bot reindex` or a `SCHEMA_VERSION` bump.

**The cold build is the regression, at 2.6x, and there is a measured fix that
is deliberately not taken.** Populating `documents` with the triggers dropped
and then issuing FTS5's bulk `INSERT INTO search(search) VALUES('rebuild')` is
**4x faster** on 77k documents (2,509 ms against 594 ms) and produces a
byte-comparable index, because both routes derive `search` from `documents`.
It is not here because it is a second population path for a cost paid on an
explicit command, and a second path is a second thing that can disagree with
the first. Revisit if a replay ever runs inside something a person is waiting
on. (`automerge` was tried and is worth about 10%: not worth a knob.)
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from .models import (
    AssessmentTrigger,
    Decision,
    IndicatorSnapshot,
    TriggerField,
    TriggerOp,
)
from .triggers import DEFAULT_HORIZON_DAYS as TRIGGER_HORIZON_DAYS
from .triggers import CycleReadings, WatchReport, grade_watch

log = structlog.get_logger(__name__)

DEFAULT_DB_PATH = Path("data/insight.db")
DEFAULT_AUDIT_DIR = Path("audit")

# Bump this on any schema change. There is no migration path and there is not
# meant to be one: a mismatch drops every table and replays the audit log,
# which is cheap and cannot lose anything the log still holds.
SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Byte offset per source file. `bytes_indexed` is the offset AFTER the last
-- complete line, never the file size, so a torn final line is re-read and
-- completed on the next pass instead of being skipped forever.
CREATE TABLE IF NOT EXISTS source_files (
    name           TEXT PRIMARY KEY,
    bytes_indexed  INTEGER NOT NULL,
    indexed_at     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS cycles (
    ts                 TEXT PRIMARY KEY,
    day                TEXT NOT NULL,
    outcome            TEXT NOT NULL,
    proposal_count     INTEGER NOT NULL DEFAULT 0,
    approved_count     INTEGER NOT NULL DEFAULT 0,
    rejected_count     INTEGER NOT NULL DEFAULT 0,
    acted              INTEGER NOT NULL DEFAULT 0,
    notes              TEXT    NOT NULL DEFAULT '',
    has_inputs         INTEGER NOT NULL DEFAULT 0,
    calendar_degraded  INTEGER NOT NULL DEFAULT 0,
    social_degraded    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cycles_day ON cycles (day);

CREATE TABLE IF NOT EXISTS assessments (
    ts           TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    stance       TEXT NOT NULL,
    reasoning    TEXT NOT NULL DEFAULT '',
    waiting_for  TEXT NOT NULL DEFAULT '',
    -- The machine-checkable half of a watch. NULL means the model named no
    -- condition, or the record predates structured triggers; `triggers.py`
    -- counts those two separately because they are different failures.
    trigger_field TEXT,
    trigger_op    TEXT,
    trigger_value REAL,
    PRIMARY KEY (ts, symbol)
);
CREATE INDEX IF NOT EXISTS idx_assess_symbol ON assessments (symbol, ts);
CREATE INDEX IF NOT EXISTS idx_assess_stance ON assessments (stance, ts);

CREATE TABLE IF NOT EXISTS proposals (
    ts                TEXT    NOT NULL,
    idx               INTEGER NOT NULL,
    symbol            TEXT    NOT NULL,
    direction         TEXT    NOT NULL,
    qty               REAL    NOT NULL,
    limit_price       REAL    NOT NULL,
    stop_loss_price   REAL    NOT NULL,
    take_profit_price REAL,            -- NULL when no target was set
    risk_usd          REAL    NOT NULL DEFAULT 0,
    notional_usd      REAL    NOT NULL DEFAULT 0,
    rationale         TEXT    NOT NULL DEFAULT '',
    -- NULL when the verdict list could not be paired with the proposal list.
    -- The loop skips a proposal whose symbol has no tick, so the two can differ
    -- in length; guessing an alignment would attribute one trade's rejection to
    -- another. Same reasoning as `DecisionEntry.verdict_for`.
    approved          INTEGER,
    PRIMARY KEY (ts, idx)
);
CREATE INDEX IF NOT EXISTS idx_proposals_symbol ON proposals (symbol, ts);

CREATE TABLE IF NOT EXISTS rejections (
    ts            TEXT    NOT NULL,
    idx           INTEGER NOT NULL,
    reason_idx    INTEGER NOT NULL,
    symbol        TEXT    NOT NULL DEFAULT '',
    reason        TEXT    NOT NULL,
    PRIMARY KEY (ts, idx, reason_idx)
);
CREATE INDEX IF NOT EXISTS idx_rejections_symbol ON rejections (symbol, ts);

CREATE TABLE IF NOT EXISTS position_plans (
    ts            TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    action        TEXT NOT NULL,
    thesis_intact INTEGER NOT NULL DEFAULT 1,
    reasoning     TEXT NOT NULL DEFAULT '',
    waiting_for   TEXT NOT NULL DEFAULT '',
    invalidation  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (ts, symbol)
);

-- The rendered summary line, stored as text. Never parsed back into numbers;
-- see the module docstring.
CREATE TABLE IF NOT EXISTS readings (
    ts      TEXT NOT NULL,
    symbol  TEXT NOT NULL,
    kind    TEXT NOT NULL,   -- 'daily' | 'intraday'
    summary TEXT NOT NULL,
    PRIMARY KEY (ts, symbol, kind)
);
CREATE INDEX IF NOT EXISTS idx_readings_symbol ON readings (symbol, ts);

-- The same daily figures as numbers. Every column is nullable because every
-- one of them can genuinely be unavailable, and a stored zero would be read
-- back as a real reading. This is what a trigger is graded against and what a
-- chart is drawn from; `readings.summary` above is what a person reads.
CREATE TABLE IF NOT EXISTS numeric_readings (
    ts       TEXT NOT NULL,
    symbol   TEXT NOT NULL,
    close    REAL,
    sma_20   REAL,
    sma_200  REAL,
    atr_14   REAL,
    volume_ratio REAL,
    distance_from_sma_20_atr REAL,
    swing_high REAL,
    swing_low  REAL,
    highest_close REAL,
    lowest_close  REAL,
    PRIMARY KEY (ts, symbol)
);
CREATE INDEX IF NOT EXISTS idx_numeric_symbol ON numeric_readings (symbol, ts);

-- One row per (cycle, item). Deduping happens at query time with GROUP BY, so
-- re-indexing a line can never inflate a count.
CREATE TABLE IF NOT EXISTS news_sightings (
    ts   TEXT NOT NULL,
    kind TEXT NOT NULL,      -- 'headline' | 'social' | 'window'
    text TEXT NOT NULL,
    PRIMARY KEY (ts, kind, text)
);
CREATE INDEX IF NOT EXISTS idx_news_kind ON news_sightings (kind, ts);

CREATE TABLE IF NOT EXISTS events (
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (ts, kind)
);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events (kind, ts);

-- Deduped news with the age attached, so callers cannot accidentally present a
-- headline cached since this morning as something that just broke.
CREATE VIEW IF NOT EXISTS news AS
SELECT kind,
       text,
       MIN(ts) AS first_seen,
       MAX(ts) AS last_seen,
       COUNT(*) AS cycles
FROM news_sightings
GROUP BY kind, text;

-- One row per searchable document, with a natural key in `doc_id` exactly like
-- every other table here. `body` is a search COPY rather than a source: its
-- whitespace is normalised and several fields may be joined into one document.
-- The authoritative text is in the table the `kind` names.
CREATE TABLE IF NOT EXISTS documents (
    id     INTEGER PRIMARY KEY,
    doc_id TEXT NOT NULL UNIQUE,
    kind   TEXT NOT NULL,
    ts     TEXT NOT NULL,
    symbol TEXT NOT NULL DEFAULT '',
    body   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_kind ON documents (kind, ts);
CREATE INDEX IF NOT EXISTS idx_documents_symbol ON documents (symbol, ts);

-- EXTERNAL CONTENT, so the text lives once in `documents` and this holds only
-- the inverted index. Porter stemming because the corpus is English prose and
-- somebody looking for "reclaimed" means "reclaim" too; the metadata columns
-- are UNINDEXED because they are filters rather than text, and an exact filter
-- beats a token match for a ticker.
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
    body,
    doc_id UNINDEXED,
    kind   UNINDEXED,
    ts     UNINDEXED,
    symbol UNINDEXED,
    content='documents',
    content_rowid='id',
    tokenize='porter unicode61'
);

-- The documented external-content maintenance pattern. `documents` is written
-- with an upsert and NEVER with INSERT OR REPLACE: REPLACE resolves a conflict
-- by deleting the old row without firing this DELETE trigger, which would
-- strand the old index entry beside the new one and double the document.
CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
    INSERT INTO search (rowid, body, doc_id, kind, ts, symbol)
    VALUES (new.id, new.body, new.doc_id, new.kind, new.ts, new.symbol);
END;

CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
    INSERT INTO search (search, rowid, body, doc_id, kind, ts, symbol)
    VALUES ('delete', old.id, old.body, old.doc_id, old.kind, old.ts, old.symbol);
END;

CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
    INSERT INTO search (search, rowid, body, doc_id, kind, ts, symbol)
    VALUES ('delete', old.id, old.body, old.doc_id, old.kind, old.ts, old.symbol);
    INSERT INTO search (rowid, body, doc_id, kind, ts, symbol)
    VALUES (new.id, new.body, new.doc_id, new.kind, new.ts, new.symbol);
END;
"""

_TABLES = [
    "meta",
    "source_files",
    "cycles",
    "assessments",
    "proposals",
    "rejections",
    "position_plans",
    "readings",
    "numeric_readings",
    "news_sightings",
    "events",
    "documents",
]

# Dropped separately, because an FTS5 table owns four shadow tables of its own
# and a `DROP TABLE documents` leaves every one of them behind. The triggers
# need no list: SQLite drops a table's triggers with the table.
_VIRTUAL_TABLES = ["search"]

# The `kind` of every document in the corpus. Named rather than inferred, so a
# search for an unknown kind can be REFUSED instead of silently matching
# nothing — an empty result must never be the way a caller learns it typed the
# wrong word. `headline`, `social` and `window` deliberately reuse the
# `news_sightings` vocabulary rather than inventing a second one.
SEARCHABLE_KINDS: tuple[str, ...] = (
    "rationale",
    "rejection",
    "assessment",
    "position_plan",
    "note",
    "event",
    "headline",
    "social",
    "window",
)


@dataclass
class IndexReport:
    """What a pass over the log actually did."""

    files_scanned: int = 0
    files_updated: int = 0
    lines_read: int = 0
    decisions_indexed: int = 0
    events_indexed: int = 0
    malformed: int = 0
    unreadable: list[str] = field(default_factory=list)
    rebuilt: bool = False

    @property
    def changed(self) -> bool:
        return self.files_updated > 0 or self.rebuilt


def _add_document(
    conn: sqlite3.Connection,
    *,
    doc_id: str,
    kind: str,
    ts: str,
    symbol: str,
    parts: tuple[str | None, ...],
) -> None:
    """Put one searchable document into the corpus, idempotently.

    `parts` are joined and their whitespace normalised, so a body is one tidy
    passage rather than several fields with ragged formatting. Empty parts are
    dropped, and a document with nothing left to say is not stored at all —
    an empty row would be a hit that carries no information.

    **The upsert is deliberately not `INSERT OR REPLACE`.** REPLACE deletes the
    conflicting row without firing the DELETE trigger, so the old FTS entry
    would survive beside the new one and the document would be in the index
    twice. `ON CONFLICT ... DO UPDATE` fires the UPDATE trigger, which removes
    the old entry before adding the new one.

    The `WHERE` on the update is what makes a re-read cheap: an unchanged body
    is the normal case — the audit log is append-only, so a line re-read says
    exactly what it said before — and skipping the update skips the trigger,
    the delete and the re-index with it.
    """
    cleaned = [" ".join(part.split()) for part in parts if part]
    body = "\n".join(p for p in cleaned if p)
    if not body:
        return
    conn.execute(
        "INSERT INTO documents (doc_id, kind, ts, symbol, body) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(doc_id) DO UPDATE SET "
        "kind = excluded.kind, ts = excluded.ts, symbol = excluded.symbol, "
        "body = excluded.body "
        "WHERE documents.body <> excluded.body",
        (doc_id, kind, ts, symbol, body),
    )


class InsightIndex:
    """A derived SQLite index over `audit/*.jsonl`."""

    def __init__(
        self,
        db_path: Path = DEFAULT_DB_PATH,
        audit_dir: Path = DEFAULT_AUDIT_DIR,
    ) -> None:
        self._path = db_path
        self._audit = audit_dir
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._prepare()

    # ------------------------------------------------------------- plumbing

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # WAL and a busy timeout because the MCP server and the CLI can both
        # reach this. Same reasoning as the journal's busy timeout: without it
        # a concurrent write fails at exactly the busy moment worth indexing.
        conn = sqlite3.connect(self._path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _prepare(self) -> None:
        """Create the schema, dropping everything first if the version moved."""
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            current = int(row["value"]) if row else None

        if current == SCHEMA_VERSION:
            return

        if current is not None:
            log.info("insight_schema_changed", was=current, now=SCHEMA_VERSION)
        self._drop_all()
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def _drop_all(self) -> None:
        with self._connect() as conn:
            conn.execute("DROP VIEW IF EXISTS news")
            # The FTS index before the corpus underneath it. Leaving `search`
            # standing is not a tidiness problem: `documents.id` restarts at 1
            # after a rebuild, so a surviving entry is filed against a REUSED
            # rowid and the old term answers with the new document's text.
            # `tests/test_insight.py` pins it, and FTS5's own integrity-check
            # does not catch it.
            for virtual in _VIRTUAL_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {virtual}")
            for table in _TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table}")

    # ------------------------------------------------------------- indexing

    def rebuild(self) -> IndexReport:
        """Throw the index away and replay the whole audit log."""
        self._drop_all()
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        report = self.refresh()
        report.rebuilt = True
        return report

    def refresh(self) -> IndexReport:
        """Index whatever has been appended since the last pass.

        Cheap when nothing changed: one `stat` per dated file and no read. On a
        quiet call this is the only work done, which is what lets the query
        tools call it on every request instead of relying on a cron.
        """
        report = IndexReport()
        if not self._audit.exists():
            return report

        with self._connect() as conn:
            offsets = {
                r["name"]: r["bytes_indexed"]
                for r in conn.execute("SELECT name, bytes_indexed FROM source_files")
            }

            for path in sorted(self._audit.glob("*.jsonl")):
                report.files_scanned += 1
                try:
                    size = path.stat().st_size
                except OSError as exc:
                    report.unreadable.append(f"{path.name}: {exc}")
                    continue

                start = offsets.get(path.name, 0)
                # A file that shrank was truncated or replaced, so the offset
                # is meaningless and it is read from the top. Idempotent keys
                # make that a no-op for rows already present.
                if size < start:
                    start = 0
                if size == start:
                    continue

                try:
                    consumed, lines = self._index_file(conn, path, start, report)
                except OSError as exc:
                    report.unreadable.append(f"{path.name}: {exc}")
                    continue

                report.files_updated += 1
                report.lines_read += lines
                conn.execute(
                    "INSERT OR REPLACE INTO source_files "
                    "(name, bytes_indexed, indexed_at) VALUES (?, ?, ?)",
                    (path.name, start + consumed, datetime.now(UTC).isoformat()),
                )

        return report

    def _index_file(
        self, conn: sqlite3.Connection, path: Path, start: int, report: IndexReport
    ) -> tuple[int, int]:
        """Index complete lines from `start`. Returns (bytes consumed, lines)."""
        with path.open("rb") as fh:
            fh.seek(start)
            blob = fh.read()

        # Everything after the last newline is a partial write. Leaving it
        # unconsumed is what makes a torn line recoverable rather than lost.
        cut = blob.rfind(b"\n")
        if cut < 0:
            return 0, 0
        complete = blob[: cut + 1]

        lines = 0
        for raw in complete.split(b"\n"):
            if not raw.strip():
                continue
            lines += 1
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                report.malformed += 1
                continue
            if not isinstance(payload, dict):
                report.malformed += 1
                continue

            if "kind" in payload:
                if self._index_event(conn, payload):
                    report.events_indexed += 1
                else:
                    report.malformed += 1
                continue

            try:
                decision = Decision.model_validate(payload)
            except Exception:
                # Written by an older build with a field that has since changed
                # shape. Counted, never raised — same contract as audit._parse.
                report.malformed += 1
                continue
            self._index_decision(conn, decision)
            report.decisions_indexed += 1

        return cut + 1, lines

    @staticmethod
    def _index_event(conn: sqlite3.Connection, payload: dict[str, Any]) -> bool:
        stamp = payload.get("timestamp")
        if not isinstance(stamp, str):
            return False
        body = payload.get("payload")
        kind = str(payload["kind"])
        rendered = json.dumps(body) if body is not None else ""
        conn.execute(
            "INSERT OR REPLACE INTO events (ts, kind, payload) VALUES (?, ?, ?)",
            (stamp, kind, rendered),
        )
        # The kind goes into the body as well as into its own column: an event
        # is often all name and no payload (`loop_end`), and a corpus that could
        # not find one by name would be missing the only word it has.
        _add_document(
            conn,
            doc_id=f"event:{stamp}:{kind}",
            kind="event",
            ts=stamp,
            symbol="",
            parts=(kind, rendered),
        )
        return True

    @staticmethod
    def _index_decision(conn: sqlite3.Connection, decision: Decision) -> None:
        ts = decision.timestamp.isoformat()
        day = decision.timestamp.date().isoformat()

        verdicts = decision.verdicts
        # Only trust positional pairing when the lists line up; see the schema.
        paired = len(verdicts) == len(decision.proposals)
        approved = sum(1 for v in verdicts if v.approved)
        rejected = sum(1 for v in verdicts if not v.approved)
        acted = any(r.accepted for r in decision.executed)

        if not decision.proposals:
            outcome = "held"
        elif acted:
            outcome = "executed"
        elif decision.executed:
            outcome = "refused"
        elif approved:
            outcome = "approved"
        else:
            outcome = "rejected"

        inputs = decision.inputs
        conn.execute(
            "INSERT OR REPLACE INTO cycles (ts, day, outcome, proposal_count, "
            "approved_count, rejected_count, acted, notes, has_inputs, "
            "calendar_degraded, social_degraded) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts, day, outcome, len(decision.proposals), approved, rejected,
                int(acted), decision.notes, int(inputs is not None),
                int(bool(inputs and inputs.calendar_degraded)),
                int(bool(inputs and inputs.social_degraded)),
            ),
        )

        _add_document(
            conn,
            doc_id=f"note:{ts}",
            kind="note",
            ts=ts,
            symbol="",
            parts=(decision.notes,),
        )

        for a in decision.assessments:
            trigger = a.trigger
            _add_document(
                conn,
                doc_id=f"assessment:{ts}:{a.symbol}",
                kind="assessment",
                ts=ts,
                symbol=a.symbol,
                parts=(a.reasoning, a.waiting_for),
            )
            conn.execute(
                "INSERT OR REPLACE INTO assessments (ts, symbol, stance, "
                "reasoning, waiting_for, trigger_field, trigger_op, trigger_value) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts, a.symbol, a.stance.value, a.reasoning, a.waiting_for,
                    trigger.field.value if trigger else None,
                    trigger.op.value if trigger else None,
                    trigger.value if trigger else None,
                ),
            )

        for i, p in enumerate(decision.proposals):
            verdict = verdicts[i] if paired and i < len(verdicts) else None
            _add_document(
                conn,
                doc_id=f"rationale:{ts}:{i}",
                kind="rationale",
                ts=ts,
                symbol=p.symbol,
                parts=(p.rationale,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO proposals (ts, idx, symbol, direction, qty, "
                "limit_price, stop_loss_price, take_profit_price, risk_usd, "
                "notional_usd, rationale, approved) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts, i, p.symbol, p.direction.value, p.qty, p.limit_price,
                    p.stop_loss_price, p.take_profit_price, p.risk_usd,
                    p.notional_usd, p.rationale,
                    None if verdict is None else int(verdict.approved),
                ),
            )
            if verdict is not None and not verdict.approved:
                for j, reason in enumerate(verdict.reasons):
                    conn.execute(
                        "INSERT OR REPLACE INTO rejections "
                        "(ts, idx, reason_idx, symbol, reason) VALUES (?, ?, ?, ?, ?)",
                        (ts, i, j, p.symbol, reason),
                    )
                    _add_document(
                        conn,
                        doc_id=f"rejection:{ts}:{i}:{j}",
                        kind="rejection",
                        ts=ts,
                        symbol=p.symbol,
                        parts=(reason,),
                    )

        # A verdict list that could not be paired still carries reasons, and
        # they are the only record of why the gate refused. Filed against the
        # cycle with no symbol rather than dropped or guessed at.
        if not paired:
            for i, verdict in enumerate(verdicts):
                if verdict.approved:
                    continue
                for j, reason in enumerate(verdict.reasons):
                    conn.execute(
                        "INSERT OR REPLACE INTO rejections "
                        "(ts, idx, reason_idx, symbol, reason) VALUES (?, ?, ?, ?, ?)",
                        (ts, -1 - i, j, "", reason),
                    )
                    _add_document(
                        conn,
                        doc_id=f"rejection:{ts}:{-1 - i}:{j}",
                        kind="rejection",
                        ts=ts,
                        symbol="",
                        parts=(reason,),
                    )

        for plan in decision.position_plans:
            _add_document(
                conn,
                doc_id=f"position_plan:{ts}:{plan.symbol}",
                kind="position_plan",
                ts=ts,
                symbol=plan.symbol,
                parts=(plan.reasoning, plan.waiting_for, plan.invalidation),
            )
            conn.execute(
                "INSERT OR REPLACE INTO position_plans (ts, symbol, action, "
                "thesis_intact, reasoning, waiting_for, invalidation) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    ts, plan.symbol, plan.action.value, int(plan.thesis_intact),
                    plan.reasoning, plan.waiting_for, plan.invalidation,
                ),
            )

        if inputs is None:
            return

        for symbol, summary in inputs.indicators.items():
            conn.execute(
                "INSERT OR REPLACE INTO readings (ts, symbol, kind, summary) "
                "VALUES (?, ?, 'daily', ?)",
                (ts, symbol, summary),
            )
        for symbol, snap in inputs.readings.items():
            conn.execute(
                "INSERT OR REPLACE INTO numeric_readings (ts, symbol, close, "
                "sma_20, sma_200, atr_14, volume_ratio, "
                "distance_from_sma_20_atr, swing_high, swing_low, "
                "highest_close, lowest_close) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts, symbol, snap.close, snap.sma_20, snap.sma_200,
                    snap.atr_14, snap.volume_ratio,
                    snap.distance_from_sma_20_atr, snap.swing_high,
                    snap.swing_low, snap.highest_close, snap.lowest_close,
                ),
            )
        for symbol, summary in inputs.intraday.items():
            conn.execute(
                "INSERT OR REPLACE INTO readings (ts, symbol, kind, summary) "
                "VALUES (?, ?, 'intraday', ?)",
                (ts, symbol, summary),
            )
        for kind, items in (
            ("headline", inputs.headlines),
            ("social", inputs.social_posts),
            ("window", inputs.news_windows),
        ):
            for text in items:
                cleaned = text.strip()
                if not cleaned:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO news_sightings (ts, kind, text) "
                    "VALUES (?, ?, ?)",
                    (ts, kind, cleaned),
                )
                # Keyed on the TEXT and not on the cycle, unlike the sighting
                # above. The 30-minute cache re-serves a headline every cycle,
                # so a per-cycle document would put the same story in the index
                # ninety-six times over — ranked ninety-six times, and crowding
                # out everything said once. `ts` therefore stays at the FIRST
                # sighting, because an unchanged body skips the update.
                _add_document(
                    conn,
                    doc_id=f"news:{kind}:{' '.join(cleaned.split())}",
                    kind=kind,
                    ts=ts,
                    symbol="",
                    parts=(cleaned,),
                )

    # -------------------------------------------------------------- reading

    def watch_report(
        self, *, days: int = 30, horizon_days: float = TRIGGER_HORIZON_DAYS
    ) -> WatchReport:
        """Grade every watch in the window against the cycles that followed it.

        The reading side is a plain query; the grading is `triggers.py`, which
        is pure. Nothing here estimates a price, a fill or a profit.
        """
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        report = WatchReport()

        with self._connect() as conn:
            watches = conn.execute(
                "SELECT ts, symbol, waiting_for, trigger_field, trigger_op, "
                "trigger_value FROM assessments "
                "WHERE stance = 'watch' AND ts >= ? ORDER BY ts",
                (since,),
            ).fetchall()
            if not watches:
                return report

            # Everything from the oldest watch onward, since a trigger is
            # graded against what came after it.
            oldest = str(watches[0]["ts"])
            cycles = _cycle_readings(conn, oldest)
            report.cycles_with_numeric_readings = sum(1 for c in cycles if c.readings)
            report.cycles_without_numeric_readings = sum(
                1 for c in cycles if not c.readings
            )

        for row in watches:
            stated_at = _aware(str(row["ts"]))
            if row["trigger_field"] is None:
                # Split on what the row actually shows: whether any condition
                # was described at all. Naming nothing is a quality problem;
                # prose without a checkable form is a coverage gap. The cause
                # of the second — an old record or a skipped field — is not
                # recoverable from here, so it is not claimed.
                if str(row["waiting_for"] or "").strip():
                    report.watches_with_prose_only += 1
                else:
                    report.watches_naming_nothing += 1
                continue

            try:
                trigger = AssessmentTrigger(
                    field=TriggerField(str(row["trigger_field"])),
                    op=TriggerOp(str(row["trigger_op"])),
                    value=float(row["trigger_value"]),
                )
            except (ValueError, TypeError):
                report.watches_with_prose_only += 1
                continue

            later = [c for c in cycles if c.at > stated_at]
            report.outcomes.append(
                grade_watch(
                    symbol=str(row["symbol"]),
                    stated_at=stated_at,
                    trigger=trigger,
                    waiting_for=str(row["waiting_for"]),
                    later=later,
                    horizon_days=horizon_days,
                )
            )

        return report

    def describe(self) -> dict[str, Any]:
        """Schema and row counts, so a caller can write a query without guessing."""
        with self._connect() as conn:
            tables: dict[str, list[str]] = {}
            for name in [*_TABLES, *_VIRTUAL_TABLES, "news"]:
                if name in {"meta", "source_files"}:
                    continue
                cols = conn.execute(f"PRAGMA table_info({name})").fetchall()
                if cols:
                    tables[name] = [str(c["name"]) for c in cols]
            counts = {
                name: int(conn.execute(f"SELECT COUNT(*) c FROM {name}").fetchone()["c"])
                for name in tables
                # `search` is external-content, so counting it means
                # reconstructing every row out of `documents` — the same number
                # `documents` answers with an index lookup.
                if name not in _VIRTUAL_TABLES
            }
            span = conn.execute(
                "SELECT MIN(day) lo, MAX(day) hi FROM cycles"
            ).fetchone()

        return {
            "tables": tables,
            "row_counts": counts,
            "covers_days": {"first": span["lo"], "last": span["hi"]},
            "searchable_kinds": list(SEARCHABLE_KINDS),
            "note": (
                "Derived from audit/*.jsonl and rebuilt from it, so it is a "
                "cache rather than a source. `readings.summary` is the rendered "
                "text the loop recorded, not parseable numbers. Use the `news` "
                "view for deduped items with first_seen/last_seen. For prose, "
                "prefer the full-text search tool over LIKE: `documents` is the "
                "corpus and `search` is an FTS5 index over it. `readings` is "
                "deliberately NOT in that corpus — it is a rendered template, "
                "so every row shares its words; ask `numeric_readings` for a "
                "figure instead."
            ),
        }


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


_SNAPSHOT_FIELDS = (
    "close",
    "sma_20",
    "sma_200",
    "atr_14",
    "volume_ratio",
    "distance_from_sma_20_atr",
    "swing_high",
    "swing_low",
    "highest_close",
    "lowest_close",
)


def _cycle_readings(conn: sqlite3.Connection, since: str) -> list[CycleReadings]:
    """Every cycle from `since` onward, with its numeric figures and proposals.

    Ascending, because a trigger is graded forward in time from the moment it
    was written.
    """
    readings: dict[str, dict[str, IndicatorSnapshot]] = {}
    for row in conn.execute(
        "SELECT * FROM numeric_readings WHERE ts >= ? ORDER BY ts", (since,)
    ):
        ts = str(row["ts"])
        readings.setdefault(ts, {})[str(row["symbol"])] = IndicatorSnapshot(
            **{name: row[name] for name in _SNAPSHOT_FIELDS}
        )

    proposed: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT ts, symbol FROM proposals WHERE ts >= ?", (since,)
    ):
        proposed.setdefault(str(row["ts"]), set()).add(str(row["symbol"]))

    # Driven by the cycle list rather than by the readings, so a cycle that
    # recorded no numeric figures still appears — as one that could not settle
    # a trigger, which is a different fact from one where the trigger failed.
    stamps = [
        str(r["ts"])
        for r in conn.execute("SELECT ts FROM cycles WHERE ts >= ? ORDER BY ts", (since,))
    ]
    return [
        CycleReadings(
            at=_aware(ts),
            readings=readings.get(ts, {}),
            proposed_symbols=proposed.get(ts, set()),
        )
        for ts in stamps
    ]


# ------------------------------------------------------------- open querying


@dataclass(frozen=True)
class QueryResult:
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


# Statements that are not a read. The connection is opened read-only, which is
# the actual guarantee — SQLite refuses the write at the file layer — so this
# is the second lock, catching the ones that reach outside the file rather than
# write to it. ATTACH is the one that matters: read-only on this database says
# nothing about a second one a query brings along.
_FORBIDDEN = re.compile(
    r"\b(attach|detach|pragma|vacuum|insert|update|delete|drop|alter|create|"
    r"replace|reindex|analyze)\b",
    re.IGNORECASE,
)
_LEADING_COMMENT = re.compile(r"^\s*(?:--[^\n]*\n|/\*.*?\*/|\s)+", re.DOTALL)

MAX_ROWS = 200
# VM instructions, not milliseconds. A cartesian join over a year of cycles
# should come back as an error rather than as a hung chat turn.
QUERY_STEP_LIMIT = 5_000_000

# The largest string or blob one query may build, in bytes.
#
# **The progress handler bounds STEPS and a step can be arbitrarily large**,
# which is the gap this closes. Measured through `run_query` with the guard in
# place: `SELECT length(randomblob(1000000000))` returned `ok` after 6.8 seconds
# having taken the process to 988 MB RSS — one VM instruction, so the handler
# never got a chance to abort it. SQLite's own default cap on a value is 1 GB,
# and the droplet has 2 GB total with the trading loop and the web unit already
# on it, so "bounded runtime" was true of the clock and false of the memory.
#
# 16 MiB is far above anything this index actually holds — the widest column is
# a rationale capped at a few hundred characters — while leaving room for a
# `group_concat` over a real result set. A query that wants more comes back as
# an error, which is the same answer a query that ran too long gets.
QUERY_VALUE_LIMIT_BYTES = 16 * 1024 * 1024


def _read_only_connection(db_path: Path) -> sqlite3.Connection:
    """A connection that cannot write and cannot run forever.

    `mode=ro` is the actual guarantee — SQLite refuses the write at the file
    layer, whatever a statement says — and the progress handler bounds runtime
    so a bad query returns an error instead of hanging a chat turn. Both
    reading paths share this, so neither can be given one lock and not the
    other.

    The value limit is the third, and it is there because the second does not
    cover it: the handler counts VM instructions and one instruction can
    allocate a gigabyte. See `QUERY_VALUE_LIMIT_BYTES`.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, QUERY_VALUE_LIMIT_BYTES)
    steps = {"n": 0}

    def _guard() -> int:
        steps["n"] += 1
        return 1 if steps["n"] > QUERY_STEP_LIMIT // 1000 else 0

    # Fires every 1000 VM instructions; returning non-zero aborts the query.
    conn.set_progress_handler(_guard, 1000)
    return conn


def run_query(
    db_path: Path,
    sql: str,
    *,
    params: tuple[Any, ...] | list[Any] | None = None,
    limit: int = MAX_ROWS,
) -> QueryResult:
    """Run one read-only SELECT against the index.

    Open querying is the point — a fixed set of tools cannot anticipate the
    question — and it is safe here for a reason that would not hold elsewhere:
    this database is derived, rebuildable, holds no credentials and is read by
    nothing in the trading path. The worst a bad query can do is return a bad
    answer.

    `params` is for callers that build a query from user input, which is every
    caller in this repo other than the open SQL tool. Binding beats
    interpolating: a ticker with a quote in it becomes a value that matches
    nothing rather than a syntax error, and the guard below never has to be the
    thing standing between a search box and the database.
    """
    text = sql.strip().rstrip(";").strip()
    if not text:
        return QueryResult(error="empty query")

    bare = _LEADING_COMMENT.sub("", text)
    if not re.match(r"(?is)^(select|with)\b", bare):
        return QueryResult(error="only SELECT (or WITH ... SELECT) queries are allowed")
    if ";" in text:
        return QueryResult(error="one statement per query")
    if _FORBIDDEN.search(text):
        return QueryResult(error="only read-only SELECT queries are allowed")

    if not db_path.exists():
        return QueryResult(error=f"no index at {db_path}; run `electrum-bot reindex`")

    capped = max(1, min(limit, MAX_ROWS))
    try:
        conn = _read_only_connection(db_path)
    except sqlite3.Error as exc:
        return QueryResult(error=f"could not open the index: {exc}")

    try:
        cursor = conn.execute(text, tuple(params or ()))
        fetched = cursor.fetchmany(capped + 1)
        columns = [d[0] for d in cursor.description or []]
    except sqlite3.OperationalError as exc:
        detail = str(exc)
        if "interrupted" in detail.lower():
            detail = "query took too long and was stopped"
        return QueryResult(error=detail)
    except sqlite3.Error as exc:
        return QueryResult(error=str(exc))
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()

    truncated = len(fetched) > capped
    return QueryResult(
        columns=columns,
        rows=[dict(r) for r in fetched[:capped]],
        truncated=truncated,
    )


# ---------------------------------------------------------- full-text search


@dataclass(frozen=True)
class SearchHit:
    """One matching document, with where it came from attached.

    `body` is the search copy — whitespace normalised, several fields possibly
    joined — so quote `snippet` or `body` as an extract and go to the table
    `kind` names for the authoritative text.
    """

    doc_id: str
    kind: str
    ts: str
    symbol: str
    body: str
    snippet: str
    score: float


@dataclass(frozen=True)
class SearchResult:
    hits: list[SearchHit] = field(default_factory=list)
    # How many documents the non-text filters left for the search to run over.
    # No hits in a corpus of 12,000 and no hits in a corpus of 0 are opposite
    # findings, and a caller handed only an empty list reaches for the wrong
    # one every time. Same rule as `has_cycles` and `can_grade_anything`.
    documents_searched: int = 0
    kinds_searched: list[str] = field(default_factory=list)
    # What was actually handed to FTS5 after the terms were quoted, so an
    # unexpected result can be explained rather than guessed at.
    match_expression: str = ""
    truncated: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


DEFAULT_HITS = 20
MAX_HITS = 100
# 12 tokens of context. Long enough to read as a sentence, short enough that a
# page of hits is still a page.
SNIPPET_TOKENS = 12


def _match_expression(query: str) -> tuple[str, str]:
    """Turn ordinary words into an FTS5 expression. Returns (expression, error).

    Every term is quoted, so a colon, a hyphen or a stray bracket is searched
    for rather than read as syntax — the same reasoning as binding a parameter
    instead of interpolating it. Terms are ANDed, and a trailing `*` survives
    as a prefix search because that is the one operator worth having by
    default.

    A term with no letter or digit in it is dropped: it tokenises to nothing,
    and FTS5 answers an empty phrase with a syntax error that tells a reader
    nothing about what they typed.
    """
    terms: list[str] = []
    for token in query.split():
        prefix = token.endswith("*")
        core = token[:-1] if prefix else token
        if not any(ch.isalnum() for ch in core):
            continue
        escaped = core.replace('"', '""')
        terms.append(f'"{escaped}"*' if prefix else f'"{escaped}"')
    if not terms:
        return "", "no searchable words in that query"
    return " ".join(terms), ""


def search_history(
    db_path: Path,
    query: str,
    *,
    kinds: tuple[str, ...] | list[str] = (),
    symbol: str = "",
    days: int = 0,
    limit: int = DEFAULT_HITS,
    raw: bool = False,
) -> SearchResult:
    """Full-text search over everything the bot has written down.

    `run_query` answers a question that can be written as SQL — a symbol, a
    date range, a GROUP BY. This answers the other kind: "where did we ever
    talk about a supply chain", "what did the gate say about buying power",
    where the wanted thing is a phrase and the table it lives in is the part
    nobody knows.

    Safe for the same reasons open SQL is: the index is derived, rebuildable,
    holds no credentials and is read by nothing that trades, and the connection
    is opened read-only with a step limit. This builds its own SQL with bound
    parameters, so a search term never reaches the statement guard — searching
    for the word "delete" is a search, not a write.

    Args:
        query: Words to look for. Quoted and ANDed unless `raw`.
        kinds: Restrict to these document kinds. An unknown one is an ERROR
            rather than an empty result — a typo must not look like silence.
        symbol: Restrict to documents filed against this symbol. Documents with
            no symbol (headlines, cycle notes, events) are excluded by it.
        days: Only documents stamped within the last N days. 0 means all
            history, which is the point of the index existing.
        limit: Maximum hits (default 20, capped at 100).
        raw: Hand `query` to FTS5 as written, for `OR`, `NEAR` and explicit
            phrases. A malformed expression comes back as an error.
    """
    if raw:
        expression, problem = query.strip(), ""
        if not expression:
            problem = "empty search"
    else:
        expression, problem = _match_expression(query)
    if problem:
        return SearchResult(error=problem)

    wanted = [k.strip().lower() for k in kinds if k.strip()]
    unknown = [k for k in wanted if k not in SEARCHABLE_KINDS]
    if unknown:
        return SearchResult(
            error=(
                f"unknown document kind(s): {', '.join(unknown)}. "
                f"Known kinds: {', '.join(SEARCHABLE_KINDS)}"
            )
        )

    if not db_path.exists():
        return SearchResult(error=f"no index at {db_path}; run `electrum-bot reindex`")

    where: list[str] = []
    params: list[Any] = []
    if wanted:
        where.append(f"d.kind IN ({','.join('?' * len(wanted))})")
        params.extend(wanted)
    if symbol.strip():
        where.append("d.symbol = ?")
        params.append(symbol.strip().upper())
    if days > 0:
        where.append("d.ts >= ?")
        params.append((datetime.now(UTC) - timedelta(days=days)).isoformat())
    clause = f" AND {' AND '.join(where)}" if where else ""

    capped = max(1, min(limit, MAX_HITS))
    try:
        conn = _read_only_connection(db_path)
    except sqlite3.Error as exc:
        return SearchResult(error=f"could not open the index: {exc}")

    try:
        # bm25() is negative and more negative is a better match, which reads
        # backwards to everyone. Negated here so `score` means what a reader
        # assumes it means, and the ordering stays FTS5's own.
        rows = conn.execute(
            "SELECT d.doc_id, d.kind, d.ts, d.symbol, d.body, "
            f"snippet(search, 0, '[', ']', '…', {SNIPPET_TOKENS}) AS snippet, "
            "-bm25(search) AS score "
            "FROM search JOIN documents d ON d.id = search.rowid "
            f"WHERE search MATCH ?{clause} "
            "ORDER BY score DESC LIMIT ?",
            (expression, *params, capped + 1),
        ).fetchall()
        corpus = int(
            conn.execute(
                "SELECT COUNT(*) c FROM documents d"
                + (f" WHERE {' AND '.join(where)}" if where else ""),
                tuple(params),
            ).fetchone()["c"]
        )
    except sqlite3.OperationalError as exc:
        detail = str(exc)
        if "interrupted" in detail.lower():
            detail = "search took too long and was stopped"
        elif detail.startswith("fts5:"):
            detail = f"{detail} — see the FTS5 query syntax, or set raw=False"
        return SearchResult(error=detail, kinds_searched=wanted or list(SEARCHABLE_KINDS))
    except sqlite3.Error as exc:
        return SearchResult(error=str(exc))
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()

    return SearchResult(
        hits=[
            SearchHit(
                doc_id=str(r["doc_id"]),
                kind=str(r["kind"]),
                ts=str(r["ts"]),
                symbol=str(r["symbol"]),
                body=str(r["body"]),
                snippet=str(r["snippet"]),
                score=float(r["score"]),
            )
            for r in rows[:capped]
        ],
        documents_searched=corpus,
        kinds_searched=wanted or list(SEARCHABLE_KINDS),
        match_expression=expression,
        truncated=len(rows) > capped,
    )
