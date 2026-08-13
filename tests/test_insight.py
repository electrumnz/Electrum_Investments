"""The derived query index over the audit log.

Two families of assertion matter here and they are different in kind.

The first is that the index cannot corrupt itself. It is built incrementally
from a file that is being appended to by another process, so the awkward cases
— a torn final line, a re-read, a file replaced underneath it — are the normal
cases rather than the exotic ones. Every row uses a natural key so a re-read is
a no-op; these tests are what prove that claim rather than merely stating it.

The second is that open SQL cannot become a write path. The database is
derived and rebuildable, which is what makes handing an agent a SQL prompt
reasonable at all, but "reasonable" is not "unchecked".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from bot.audit import AuditLog
from bot.insight import InsightIndex, run_query, search_history
from bot.models import (
    Decision,
    Direction,
    MarketInputs,
    OrderProposal,
    OrderResult,
    PositionAction,
    PositionPlan,
    RiskVerdict,
    Stance,
    SymbolAssessment,
)

NOW = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)


def proposal(symbol: str = "AAPL", qty: float = 87) -> OrderProposal:
    return OrderProposal(
        symbol=symbol,
        direction=Direction.BUY,
        qty=qty,
        limit_price=580.0,
        stop_loss_price=567.0,
        take_profit_price=600.0,
        rationale="Reclaimed the prior day high; invalidated below 567.",
    )


def cycle(minutes_ago: int = 0, **kwargs) -> Decision:
    return Decision(timestamp=NOW - timedelta(minutes=minutes_ago), **kwargs)


def build(tmp_path, *decisions: Decision) -> InsightIndex:
    log = AuditLog(tmp_path / "audit")
    for decision in decisions:
        log.record(decision)
    index = InsightIndex(tmp_path / "insight.db", audit_dir=tmp_path / "audit")
    index.refresh()
    return index


def rows(index: InsightIndex, sql: str) -> list[dict[str, Any]]:
    result = run_query(index._path, sql)
    assert result.ok, result.error
    return result.rows


# ------------------------------------------------------------------ indexing


def test_a_cycle_becomes_queryable(tmp_path):
    index = build(
        tmp_path,
        cycle(
            proposals=[proposal()],
            verdicts=[RiskVerdict.reject("risk 1,131.00 exceeds the per-trade cap 1,000.00")],
            assessments=[
                SymbolAssessment(
                    symbol="SPY",
                    stance=Stance.WATCH,
                    reasoning="Stretched below the 20-day; volume has not confirmed.",
                    waiting_for="SPY closing above 574.30 on above-average volume",
                )
            ],
            notes="One proposal, refused.",
        ),
    )

    assert rows(index, "SELECT outcome, proposal_count FROM cycles")[0] == {
        "outcome": "rejected",
        "proposal_count": 1,
    }
    assert rows(index, "SELECT symbol, stance, waiting_for FROM assessments")[0] == {
        "symbol": "SPY",
        "stance": "watch",
        "waiting_for": "SPY closing above 574.30 on above-average volume",
    }
    reason = rows(index, "SELECT symbol, reason FROM rejections")[0]
    assert reason["symbol"] == "AAPL"
    assert "exceeds the per-trade cap" in reason["reason"]


def test_every_rejection_reason_is_kept_not_just_the_first(tmp_path):
    """The gate collects all failures; an index that kept one would discard the
    property the gate is built around."""
    index = build(
        tmp_path,
        cycle(
            proposals=[proposal()],
            verdicts=[
                RiskVerdict.reject(
                    "risk exceeds the per-trade cap",
                    "outside the session window",
                    "symbol is not on the allowed list",
                )
            ],
        ),
    )

    assert len(rows(index, "SELECT reason FROM rejections")) == 3


def test_an_executed_cycle_is_distinguished_from_a_broker_refusal(tmp_path):
    """A broker refusal is not an execution; conflating them mislabels the one
    cycle where money did not move."""
    index = build(
        tmp_path,
        cycle(
            minutes_ago=30,
            proposals=[proposal()],
            verdicts=[RiskVerdict.approve()],
            executed=[OrderResult(accepted=True, order_id="ok")],
        ),
        cycle(
            minutes_ago=15,
            proposals=[proposal()],
            verdicts=[RiskVerdict.approve()],
            executed=[OrderResult(accepted=False, error="insufficient buying power")],
        ),
    )

    outcomes = {r["outcome"] for r in rows(index, "SELECT outcome FROM cycles")}
    assert outcomes == {"executed", "refused"}


def test_doing_nothing_is_recorded_as_held(tmp_path):
    index = build(tmp_path, cycle(notes="Nothing met the conditions."))
    assert rows(index, "SELECT outcome FROM cycles")[0]["outcome"] == "held"


def test_unpairable_verdicts_leave_approval_unknown_rather_than_guessed(tmp_path):
    """The loop skips a proposal with no tick, so the lists can differ in
    length. Pairing by position anyway would file one trade's rejection against
    another."""
    index = build(
        tmp_path,
        cycle(
            proposals=[proposal("AAPL"), proposal("MSFT")],
            verdicts=[RiskVerdict.reject("risk exceeds the per-trade cap")],
        ),
    )

    approvals = rows(index, "SELECT approved FROM proposals ORDER BY idx")
    assert [a["approved"] for a in approvals] == [None, None]

    # The reason still survives, filed against the cycle with no symbol rather
    # than attributed to a proposal it may not belong to.
    orphan = rows(index, "SELECT symbol, reason FROM rejections")
    assert len(orphan) == 1
    assert orphan[0]["symbol"] == ""


def test_position_plans_are_indexed(tmp_path):
    index = build(
        tmp_path,
        cycle(
            position_plans=[
                PositionPlan(
                    symbol="SPY",
                    action=PositionAction.HOLD,
                    reasoning="Thesis intact; the stop stays until the 20-day catches up.",
                    invalidation="A daily close below 561.20.",
                )
            ]
        ),
    )
    plan = rows(index, "SELECT symbol, action, invalidation FROM position_plans")[0]
    assert plan["symbol"] == "SPY"
    assert plan["action"] == "hold"


def test_events_are_indexed_alongside_decisions(tmp_path):
    log = AuditLog(tmp_path / "audit")
    log.record(cycle())
    log.record_event("model_call_failed", {"error": "ValidationError: too long"})

    index = InsightIndex(tmp_path / "insight.db", audit_dir=tmp_path / "audit")
    index.refresh()

    event = rows(index, "SELECT kind, payload FROM events")[0]
    assert event["kind"] == "model_call_failed"
    assert "ValidationError" in event["payload"]


# ------------------------------------------------------------------- news


def test_news_is_deduped_across_cycles_with_the_first_sighting_kept(tmp_path):
    """The 30-minute cache re-serves a headline; three rows would be a lie, and
    so would a first_seen taken from the newest sighting."""
    headline = "[SPY] Index closes at a record (2026-08-10)"
    index = build(
        tmp_path,
        cycle(45, inputs=MarketInputs(headlines=[headline])),
        cycle(30, inputs=MarketInputs(headlines=[headline])),
        cycle(15, inputs=MarketInputs(headlines=[headline])),
    )

    item = rows(index, "SELECT text, first_seen, last_seen, cycles FROM news")[0]
    assert item["cycles"] == 3
    assert item["first_seen"] == (NOW - timedelta(minutes=45)).isoformat()
    assert item["last_seen"] == (NOW - timedelta(minutes=15)).isoformat()


def test_the_three_feeds_stay_distinguishable(tmp_path):
    index = build(
        tmp_path,
        cycle(
            inputs=MarketInputs(
                headlines=["a headline"],
                social_posts=["[@someone 14:20] a post"],
                news_windows=["2026-08-11T20:00 affects AAPL"],
            )
        ),
    )
    kinds = {r["kind"] for r in rows(index, "SELECT DISTINCT kind FROM news")}
    assert kinds == {"headline", "social", "window"}


def test_degraded_feeds_are_recorded_per_cycle(tmp_path):
    index = build(
        tmp_path,
        cycle(inputs=MarketInputs(social_degraded=True, calendar_degraded=True)),
    )
    flags = rows(index, "SELECT social_degraded, calendar_degraded FROM cycles")[0]
    assert flags == {"social_degraded": 1, "calendar_degraded": 1}


def test_a_cycle_with_no_inputs_is_flagged_rather_than_looking_empty(tmp_path):
    """Records predating MarketInputs are a gap in the record, not a quiet
    market."""
    index = build(tmp_path, cycle())
    assert rows(index, "SELECT has_inputs FROM cycles")[0]["has_inputs"] == 0


def test_indicator_readings_are_stored_as_the_recorded_text(tmp_path):
    index = build(
        tmp_path,
        cycle(inputs=MarketInputs(indicators={"SPY": "close 580.12, sma20 574.30, atr 6.41"})),
    )
    reading = rows(index, "SELECT symbol, kind, summary FROM readings")[0]
    assert reading["kind"] == "daily"
    assert reading["summary"].startswith("close 580.12")


# ------------------------------------------------------- incremental indexing


def test_appending_indexes_only_the_delta(tmp_path):
    log = AuditLog(tmp_path / "audit")
    log.record(cycle(30))
    index = InsightIndex(tmp_path / "insight.db", audit_dir=tmp_path / "audit")
    assert index.refresh().decisions_indexed == 1

    assert index.refresh().decisions_indexed == 0  # nothing changed

    log.record(cycle(15))
    report = index.refresh()
    assert report.decisions_indexed == 1
    assert len(rows(index, "SELECT ts FROM cycles")) == 2


def test_a_torn_final_line_is_not_consumed_and_completes_later(tmp_path):
    """The load-bearing one for incremental indexing.

    The log is appended to by a running process, so the last line can be a
    partial write. Consuming up to the file SIZE would skip the rest of that
    line forever once the append completed it — the record would be silently
    short by one cycle, with nothing to say so.
    """
    log = AuditLog(tmp_path / "audit")
    log.record(cycle(30))
    path = next(iter((tmp_path / "audit").glob("*.jsonl")))

    torn = cycle(15).model_dump_json()
    with path.open("a", encoding="utf-8") as f:
        f.write(torn[:40])  # no trailing newline: a write in progress

    index = InsightIndex(tmp_path / "insight.db", audit_dir=tmp_path / "audit")
    index.refresh()
    assert len(rows(index, "SELECT ts FROM cycles")) == 1

    # The process finishes its write.
    with path.open("a", encoding="utf-8") as f:
        f.write(torn[40:] + "\n")

    report = index.refresh()
    assert report.decisions_indexed == 1
    assert report.malformed == 0
    assert len(rows(index, "SELECT ts FROM cycles")) == 2


def test_reindexing_the_same_content_changes_nothing(tmp_path):
    """Natural keys everywhere, so the byte offset is an optimisation rather
    than a correctness requirement. A count that could double on a re-read is a
    count that will eventually be wrong."""
    headline = "[SPY] A story seen twice"
    index = build(
        tmp_path,
        cycle(30, inputs=MarketInputs(headlines=[headline])),
        cycle(15, inputs=MarketInputs(headlines=[headline])),
    )
    before = rows(index, "SELECT cycles FROM news")[0]["cycles"]

    # Force a full re-read by forgetting the offsets, keeping the rows.
    with index._connect() as conn:
        conn.execute("DELETE FROM source_files")
    index.refresh()

    assert rows(index, "SELECT cycles FROM news")[0]["cycles"] == before == 2
    assert len(rows(index, "SELECT ts FROM cycles")) == 2


def test_a_truncated_file_is_read_from_the_top_again(tmp_path):
    log = AuditLog(tmp_path / "audit")
    log.record(cycle(30))
    log.record(cycle(15))
    index = InsightIndex(tmp_path / "insight.db", audit_dir=tmp_path / "audit")
    index.refresh()

    path = next(iter((tmp_path / "audit").glob("*.jsonl")))
    path.write_text(cycle(45).model_dump_json() + "\n", encoding="utf-8")

    report = index.refresh()
    assert report.decisions_indexed == 1


def test_malformed_lines_are_counted_and_do_not_stop_the_pass(tmp_path):
    log = AuditLog(tmp_path / "audit")
    log.record(cycle(30))
    path = next(iter((tmp_path / "audit").glob("*.jsonl")))
    with path.open("a", encoding="utf-8") as f:
        f.write("{ not json at all\n")
    log.record(cycle(15))

    index = InsightIndex(tmp_path / "insight.db", audit_dir=tmp_path / "audit")
    report = index.refresh()

    assert report.malformed == 1
    assert report.decisions_indexed == 2


def test_a_schema_version_bump_rebuilds_rather_than_migrates(tmp_path):
    """The audit log is never migrated, so the index must never need to be."""
    index = build(tmp_path, cycle())
    assert len(rows(index, "SELECT ts FROM cycles")) == 1

    with index._connect() as conn:
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")

    reopened = InsightIndex(tmp_path / "insight.db", audit_dir=tmp_path / "audit")
    # Dropped on open; the log is still the source, so a refresh restores it.
    assert rows(reopened, "SELECT COUNT(*) c FROM cycles")[0]["c"] == 0
    reopened.refresh()
    assert rows(reopened, "SELECT COUNT(*) c FROM cycles")[0]["c"] == 1


def test_rebuild_replays_the_whole_log(tmp_path):
    index = build(tmp_path, cycle(30), cycle(15))
    report = index.rebuild()
    assert report.rebuilt is True
    assert report.decisions_indexed == 2
    assert len(rows(index, "SELECT ts FROM cycles")) == 2


def test_a_missing_audit_directory_is_not_an_error(tmp_path):
    """A box that has never run the loop must not fail here."""
    index = InsightIndex(tmp_path / "insight.db", audit_dir=tmp_path / "nothing")
    report = index.refresh()
    assert report.files_scanned == 0
    assert report.changed is False


def test_describe_reports_coverage_and_counts(tmp_path):
    index = build(tmp_path, cycle(30), cycle(15))
    described = index.describe()

    assert described["row_counts"]["cycles"] == 2
    assert "assessments" in described["tables"]
    assert described["covers_days"]["first"] == NOW.date().isoformat()


# -------------------------------------------------------------- query guard


def test_a_select_returns_rows(tmp_path):
    index = build(tmp_path, cycle())
    result = run_query(index._path, "SELECT outcome FROM cycles")
    assert result.ok
    assert result.columns == ["outcome"]


def test_a_cte_is_allowed(tmp_path):
    index = build(tmp_path, cycle())
    result = run_query(index._path, "WITH x AS (SELECT 1 AS n) SELECT n FROM x")
    assert result.ok, result.error
    assert result.rows == [{"n": 1}]


def test_writes_are_refused(tmp_path):
    index = build(tmp_path, cycle())
    for sql in (
        "DELETE FROM cycles",
        "UPDATE cycles SET outcome = 'executed'",
        "INSERT INTO cycles (ts, day, outcome) VALUES ('x', 'y', 'z')",
        "DROP TABLE cycles",
    ):
        result = run_query(index._path, sql)
        assert not result.ok, sql

    # And nothing was written by the attempt.
    assert len(rows(index, "SELECT ts FROM cycles")) == 1


def test_a_write_smuggled_behind_a_select_is_refused(tmp_path):
    index = build(tmp_path, cycle())
    result = run_query(index._path, "SELECT 1; DELETE FROM cycles")
    assert not result.ok
    assert "one statement" in result.error
    assert len(rows(index, "SELECT ts FROM cycles")) == 1


def test_attach_is_refused(tmp_path):
    """Read-only on this database says nothing about a second one a query
    brings along."""
    index = build(tmp_path, cycle())
    result = run_query(index._path, "SELECT 1 FROM cycles WHERE 1=1 -- ATTACH")
    assert not result.ok  # the token is enough to refuse, comment or not

    result = run_query(index._path, "ATTACH DATABASE '/etc/passwd' AS x")
    assert not result.ok


def test_the_connection_itself_is_read_only(tmp_path):
    """Defence in depth: even if the guard above were bypassed, SQLite refuses
    the write at the file layer."""
    import sqlite3

    index = build(tmp_path, cycle())
    conn = sqlite3.connect(f"file:{index._path}?mode=ro", uri=True)
    try:
        raised = False
        try:
            conn.execute("DELETE FROM cycles")
        except sqlite3.OperationalError:
            raised = True
        assert raised
    finally:
        conn.close()


def test_one_instruction_cannot_eat_the_droplet(tmp_path):
    """The progress handler bounds STEPS, and a step can be arbitrarily large.

    Measured through `run_query` with the handler installed:
    `SELECT length(randomblob(1000000000))` came back `ok` after 6.8 seconds
    having taken the process to 988 MB RSS. One VM instruction, so the handler
    was never called between the allocation and the answer, and the docstring's
    claim that runtime was bounded was true of the clock and false of the
    memory. This runs on a 2 GB droplet that is also running the trading loop
    and the web unit.

    A query wanting more than `QUERY_VALUE_LIMIT_BYTES` gets an error, which is
    the same answer a query that ran too long already gets.
    """
    index = build(tmp_path, cycle())

    result = run_query(index._path, "SELECT length(randomblob(1000000000))")
    assert not result.ok
    assert "too big" in result.error

    # And an ordinary value is untouched, or the cap would be a new outage.
    fine = run_query(index._path, "SELECT length(randomblob(1000)) AS n")
    assert fine.ok
    assert fine.rows == [{"n": 1000}]


def test_a_syntax_error_is_reported_rather_than_raised(tmp_path):
    index = build(tmp_path, cycle())
    result = run_query(index._path, "SELECT nope FROM cycles")
    assert not result.ok
    assert "nope" in result.error


def test_an_empty_query_is_refused(tmp_path):
    index = build(tmp_path, cycle())
    assert not run_query(index._path, "   ").ok


def test_results_are_capped_and_say_so(tmp_path):
    index = build(tmp_path, *[cycle(m) for m in range(10)])
    result = run_query(index._path, "SELECT ts FROM cycles", limit=3)
    assert result.ok
    assert len(result.rows) == 3
    assert result.truncated is True


def test_a_missing_index_reports_how_to_build_it(tmp_path):
    result = run_query(tmp_path / "absent.db", "SELECT 1")
    assert not result.ok
    assert "reindex" in result.error


def test_bound_parameters_are_values_not_syntax(tmp_path):
    """The search tools build SQL from operator input, so binding rather than
    interpolating is what keeps a quote in a search term from being a syntax
    error — or worse."""
    index = build(tmp_path, cycle(inputs=MarketInputs(headlines=["O'Reilly beats"])))

    hit = run_query(
        index._path, "SELECT text FROM news WHERE text LIKE ?", params=["%O'Reilly%"]
    )
    assert hit.ok, hit.error
    assert hit.rows[0]["text"] == "O'Reilly beats"

    # A term that would be a write if it were interpolated is just a miss.
    miss = run_query(
        index._path,
        "SELECT text FROM news WHERE text LIKE ?",
        params=["%'; DELETE FROM cycles; --%"],
    )
    assert miss.ok
    assert miss.rows == []
    assert len(rows(index, "SELECT ts FROM cycles")) == 1


# ------------------------------------------------------------ watch grading


def _watch(symbol: str, level: float, *, prose: str = "waiting", trigger=True):
    from bot.models import AssessmentTrigger, TriggerField, TriggerOp

    return SymbolAssessment(
        symbol=symbol,
        stance=Stance.WATCH,
        reasoning="Stretched below the 20-day; volume has not confirmed yet.",
        waiting_for=prose,
        trigger=(
            AssessmentTrigger(
                field=TriggerField.CLOSE, op=TriggerOp.BELOW, value=level
            )
            if trigger
            else None
        ),
    )


def _readings(symbol: str, close: float) -> MarketInputs:
    from bot.models import IndicatorSnapshot

    return MarketInputs(readings={symbol: IndicatorSnapshot(close=close)})


def test_a_trigger_survives_the_round_trip_into_the_index(tmp_path):
    index = build(tmp_path, cycle(assessments=[_watch("SPY", 641.20)]))

    row = rows(
        index,
        "SELECT symbol, trigger_field, trigger_op, trigger_value FROM assessments",
    )[0]
    assert row == {
        "symbol": "SPY",
        "trigger_field": "close",
        "trigger_op": "below",
        "trigger_value": 641.20,
    }


def test_numeric_readings_are_indexed_as_numbers(tmp_path):
    """Distinct from `readings.summary`, which is prose and stays prose."""
    index = build(tmp_path, cycle(inputs=_readings("SPY", 580.12)))

    row = rows(index, "SELECT symbol, close, sma_200 FROM numeric_readings")[0]
    assert row["close"] == 580.12
    # Unavailable survives as NULL rather than becoming a plausible zero.
    assert row["sma_200"] is None


def test_a_watch_whose_trigger_fires_and_is_ignored_is_reported(tmp_path):
    """End to end: written on one cycle, graded against the ones after it."""
    index = build(
        tmp_path,
        cycle(minutes_ago=200, assessments=[_watch("SPY", 641.20)]),
        cycle(minutes_ago=100, inputs=_readings("SPY", 645.00)),
        cycle(minutes_ago=50, inputs=_readings("SPY", 639.00)),
        cycle(minutes_ago=1, inputs=_readings("SPY", 638.00)),
    )

    report = index.watch_report(days=30, horizon_days=1.0)

    assert report.graded == 1
    assert len(report.fired) == 1
    assert len(report.missed) == 1

    missed = report.missed[0]
    assert missed.symbol == "SPY"
    assert missed.fired_value == 639.00
    assert missed.acted is False


def test_a_watch_acted_on_is_not_reported_as_missed(tmp_path):
    index = build(
        tmp_path,
        cycle(minutes_ago=200, assessments=[_watch("SPY", 641.20)]),
        cycle(
            minutes_ago=50,
            inputs=_readings("SPY", 639.00),
            proposals=[proposal("SPY")],
            verdicts=[RiskVerdict.approve()],
        ),
    )

    report = index.watch_report(days=30, horizon_days=1.0)

    assert len(report.fired) == 1
    assert report.missed == []


def test_a_watch_with_no_trigger_is_counted_apart_from_one_naming_nothing(tmp_path):
    index = build(
        tmp_path,
        cycle(
            assessments=[
                _watch("SPY", 0, prose="waiting for more confirmation", trigger=False),
                _watch("QQQ", 0, prose="", trigger=False),
            ]
        ),
    )

    report = index.watch_report(days=30)

    assert report.watches_with_prose_only == 1
    assert report.watches_naming_nothing == 1
    assert report.graded == 0


def test_an_empty_watch_report_does_not_read_as_a_clean_record(tmp_path):
    index = build(tmp_path, cycle(notes="held"))
    report = index.watch_report(days=30)

    assert report.can_grade_anything is False
    assert report.graded == 0


# --------------------------------------------------------- full-text search


def _fts_rows(index: InsightIndex, expression: str) -> int:
    """Count index entries directly, which is the only way to see a double."""
    with index._connect() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) c FROM search WHERE search MATCH ?", (expression,)
            ).fetchone()["c"]
        )


def _integrity(index: InsightIndex) -> None:
    """FTS5's own check that the index matches the content table.

    Stronger than counting rows: it verifies every term position, so an entry
    left behind by a mishandled update fails here even when the totals happen
    to agree.
    """
    with index._connect() as conn:
        conn.execute("INSERT INTO search(search) VALUES('integrity-check')")


def _searched(index: InsightIndex, query: str, **kwargs: Any):
    return search_history(index._path, query, **kwargs)


def test_the_prose_of_a_cycle_is_searchable(tmp_path):
    """One phrase from each kind of thing the loop writes down."""
    index = build(
        tmp_path,
        cycle(
            proposals=[proposal()],
            verdicts=[RiskVerdict.reject("risk 1,131.00 exceeds the per-trade cap")],
            assessments=[
                SymbolAssessment(
                    symbol="SPY",
                    stance=Stance.WATCH,
                    reasoning="Stretched below the 20-day on a tungsten squeeze.",
                    waiting_for="SPY closing above 574.30",
                )
            ],
            position_plans=[
                PositionPlan(
                    symbol="SPY",
                    action=PositionAction.HOLD,
                    reasoning="The thesis is intact while freight rates hold.",
                    invalidation="A daily close below 561.20.",
                )
            ],
            notes="Nothing else met the conditions.",
            inputs=MarketInputs(headlines=["Cicada brood damages sesame producers"]),
        ),
    )

    for phrase, kind in (
        ("invalidated", "rationale"),
        ("per-trade cap", "rejection"),
        ("tungsten", "assessment"),
        ("freight", "position_plan"),
        ("conditions", "note"),
        ("sesame", "headline"),
    ):
        result = _searched(index, phrase)
        assert result.ok, result.error
        assert [h.kind for h in result.hits] == [kind], phrase


def test_an_event_is_searchable_by_its_name_as_well_as_its_payload(tmp_path):
    """An event is often all name and no payload, and a corpus that could not
    find one by name would be missing the only word it has."""
    log = AuditLog(tmp_path / "audit")
    log.record(cycle())
    log.record_event("model_call_failed", {"error": "rationale ran 34 over the cap"})
    log.record_event("loop_end", {})

    index = InsightIndex(tmp_path / "insight.db", audit_dir=tmp_path / "audit")
    index.refresh()

    assert _searched(index, "loop_end").hits[0].kind == "event"
    assert "34 over" in _searched(index, "rationale ran").hits[0].body


def test_a_re_read_does_not_double_a_document(tmp_path):
    """The load-bearing one for FTS.

    Every other table here survives a re-read because it has a natural key and
    `INSERT OR REPLACE` lands on it. An FTS5 index has no natural key: index
    the same document twice and it is in there twice, ranked twice, counted
    twice. `documents` carries the key and the index is derived from it by
    trigger, which is what makes the offset an optimisation here too.
    """
    index = build(
        tmp_path,
        cycle(
            30,
            assessments=[
                SymbolAssessment(
                    symbol="SPY",
                    stance=Stance.WATCH,
                    reasoning="A distinctive phrase about tungsten.",
                )
            ],
        ),
    )
    assert _fts_rows(index, "tungsten") == 1

    # Forget the offsets, keep the rows: every line is read a second time.
    with index._connect() as conn:
        conn.execute("DELETE FROM source_files")
    index.refresh()

    assert _fts_rows(index, "tungsten") == 1
    assert len(rows(index, "SELECT id FROM documents")) == 1
    _integrity(index)


def test_a_changed_body_replaces_its_index_entry_rather_than_joining_it(tmp_path):
    """The trap the upsert exists for.

    `INSERT OR REPLACE` resolves a conflict by DELETING the old row, and the
    delete trigger does not fire for it unless recursive triggers are on — so
    the old index entry would survive beside the new one, and the document
    would answer to text it no longer contains.
    """
    index = build(
        tmp_path,
        cycle(assessments=[
            SymbolAssessment(symbol="SPY", stance=Stance.PASS, reasoning="about tungsten"),
        ]),
    )
    with index._connect() as conn:
        conn.execute("UPDATE documents SET body = 'about lithium' WHERE body LIKE '%tungsten%'")

    assert _fts_rows(index, "tungsten") == 0
    assert _fts_rows(index, "lithium") == 1
    _integrity(index)


def test_a_headline_seen_every_cycle_is_one_document_stamped_when_it_arrived(tmp_path):
    """The 30-minute cache re-serves a story, so a per-cycle document would put
    it in the index ninety-six times over — ranked ninety-six times, crowding
    out everything said once."""
    headline = "[SPY] Tungsten shortage widens"
    index = build(
        tmp_path,
        cycle(45, inputs=MarketInputs(headlines=[headline])),
        cycle(30, inputs=MarketInputs(headlines=[headline])),
        cycle(15, inputs=MarketInputs(headlines=[headline])),
    )

    hits = _searched(index, "tungsten").hits
    assert len(hits) == 1
    assert hits[0].ts == (NOW - timedelta(minutes=45)).isoformat()
    # The sighting count is untouched by any of this.
    assert rows(index, "SELECT cycles FROM news")[0]["cycles"] == 3


def test_a_rendered_reading_line_is_deliberately_not_in_the_corpus(tmp_path):
    """It is searchable text and always was — but it is a TEMPLATE with figures
    substituted, so every row carries the same words and none of them tells one
    row from another, while being the largest thing in the index by volume. The
    question it answers is answered EXACTLY by `numeric_readings`."""
    index = build(
        tmp_path,
        cycle(
            inputs=MarketInputs(
                indicators={"SPY": "close 580.12, sma20 574.30, atr 6.41"},
                readings={"SPY": _readings("SPY", 580.12).readings["SPY"]},
            )
        ),
    )

    assert _searched(index, "sma20").hits == []
    # Still there, and still queryable the exact way.
    assert rows(index, "SELECT summary FROM readings")[0]["summary"].startswith("close")
    assert rows(index, "SELECT close FROM numeric_readings")[0]["close"] == 580.12


def test_a_search_term_is_a_word_and_never_syntax(tmp_path):
    """`run_query`'s statement guard refuses the token `delete` anywhere in a
    query, which is right for open SQL and would be absurd here. This builds
    its own SQL with bound parameters, so the guard never sees a search term."""
    index = build(
        tmp_path,
        cycle(notes="The operator asked us to delete the resting order."),
    )

    result = _searched(index, "delete")
    assert result.ok, result.error
    assert len(result.hits) == 1

    # And a term full of syntax is a miss rather than an error or a write.
    hostile = _searched(index, "'; DROP TABLE cycles; --")
    assert hostile.ok, hostile.error
    assert hostile.hits == []
    assert len(rows(index, "SELECT ts FROM cycles")) == 1


def test_matching_is_stemmed_and_prefixes_are_honoured(tmp_path):
    index = build(
        tmp_path,
        cycle(assessments=[
            SymbolAssessment(symbol="SPY", stance=Stance.PASS,
                             reasoning="Price reclaimed the level it lost."),
        ]),
    )
    assert len(_searched(index, "reclaim").hits) == 1     # porter stemming
    assert len(_searched(index, "reclaim*").hits) == 1    # explicit prefix


def test_a_query_with_no_words_in_it_is_refused(tmp_path):
    """An all-punctuation term tokenises to nothing, and FTS5 answers an empty
    phrase with a syntax error that tells a reader nothing about what they
    typed."""
    index = build(tmp_path, cycle(notes="anything"))
    result = _searched(index, "!!! ???")
    assert not result.ok
    assert "no searchable words" in result.error


def test_a_malformed_raw_expression_is_reported_rather_than_raised(tmp_path):
    """FTS5 has its own syntax, and a chat surface must not render a stack
    trace at somebody who typed a stray quote."""
    index = build(tmp_path, cycle(notes="anything"))

    for expression in ('nope:', 'unbalanced "', "NEAR("):
        result = _searched(index, expression, raw=True)
        assert not result.ok, expression
        assert result.error

    # And the operators raw mode exists for do work.
    index2 = build(
        tmp_path / "two",
        cycle(notes="tungsten"),
        cycle(15, notes="sesame"),
    )
    both = _searched(index2, "tungsten OR sesame", raw=True)
    assert both.ok, both.error
    assert len(both.hits) == 2


def test_an_unknown_kind_is_refused_rather_than_matching_nothing(tmp_path):
    """A typo must not be indistinguishable from silence. Same rule as
    `has_cycles`: an empty result is a finding, not an error message."""
    index = build(tmp_path, cycle(notes="tungsten"))

    result = _searched(index, "tungsten", kinds=["dreams"])
    assert not result.ok
    assert "dreams" in result.error
    assert "rationale" in result.error  # it names what IS known

    narrowed = _searched(index, "tungsten", kinds=["note"])
    assert narrowed.ok
    assert narrowed.kinds_searched == ["note"]


def test_an_empty_result_says_how_big_the_corpus_was(tmp_path):
    """No hits in a corpus of thousands and no hits in a corpus of nothing are
    opposite findings, and a caller handed only an empty list reaches for the
    wrong one every time."""
    empty = InsightIndex(tmp_path / "insight.db", audit_dir=tmp_path / "nothing")
    result = search_history(empty._path, "tungsten")
    assert result.ok
    assert result.hits == []
    assert result.documents_searched == 0

    index = build(tmp_path / "two", cycle(notes="something else entirely"))
    miss = _searched(index, "tungsten")
    assert miss.ok
    assert miss.hits == []
    assert miss.documents_searched == 1


def test_a_symbol_filter_is_exact_and_excludes_the_unattributed(tmp_path):
    index = build(
        tmp_path,
        cycle(
            proposals=[proposal("AAPL")],
            verdicts=[RiskVerdict.approve()],
            notes="A tungsten note filed against no symbol.",
            inputs=MarketInputs(headlines=["Tungsten shortage widens"]),
            assessments=[
                SymbolAssessment(symbol="MSFT", stance=Stance.PASS,
                                 reasoning="Tungsten exposure is small here."),
            ],
        ),
    )

    assert len(_searched(index, "tungsten").hits) == 3
    only = _searched(index, "tungsten", symbol="msft")  # case is forgiven
    assert [h.symbol for h in only.hits] == ["MSFT"]


def test_search_results_are_bounded_and_say_when_they_were_cut(tmp_path):
    index = build(
        tmp_path,
        *[cycle(m, notes=f"tungsten cycle {m}") for m in range(1, 11)],
    )
    result = _searched(index, "tungsten", limit=3)
    assert len(result.hits) == 3
    assert result.truncated is True


def test_search_reports_a_missing_index_rather_than_raising(tmp_path):
    result = search_history(tmp_path / "absent.db", "tungsten")
    assert not result.ok
    assert "reindex" in result.error


def test_a_schema_bump_drops_the_search_index_rather_than_leaving_it_stale(tmp_path):
    """The FTS table owns four shadow tables of its own, so dropping the corpus
    out from under it is not enough — and the failure is subtle rather than
    loud.

    `documents.id` restarts at 1 after a rebuild, so a surviving index entry
    ends up filed against a REUSED rowid. The old term then answers with the
    new document's text: a phrase attributed to a record that never contained
    it, which is the confident-wrong-answer failure this repository exists to
    refuse. Measured: FTS5's own `integrity-check` does NOT catch it, so this
    test is the only thing standing there.
    """
    log = AuditLog(tmp_path / "audit")
    log.record(cycle(30, notes="A tungsten cycle."))
    index = InsightIndex(tmp_path / "insight.db", audit_dir=tmp_path / "audit")
    index.refresh()
    assert len(_searched(index, "tungsten").hits) == 1

    # The log is replaced by different text and the schema version moves, which
    # is exactly what a version bump is for.
    for path in (tmp_path / "audit").glob("*.jsonl"):
        path.unlink()
    AuditLog(tmp_path / "audit").record(cycle(15, notes="A lithium cycle."))
    with index._connect() as conn:
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")

    reopened = InsightIndex(tmp_path / "insight.db", audit_dir=tmp_path / "audit")
    assert _searched(reopened, "lithium").hits == []  # dropped with the rest
    reopened.refresh()

    assert len(_searched(reopened, "lithium").hits) == 1
    assert _searched(reopened, "tungsten").hits == []
    _integrity(reopened)


def test_a_rebuild_produces_the_same_index_as_an_incremental_pass(tmp_path):
    index = build(tmp_path, cycle(30, notes="tungsten"), cycle(15, notes="sesame"))
    before = {(h.doc_id, h.body) for h in _searched(index, "tungsten OR sesame", raw=True).hits}

    index.rebuild()

    after = {(h.doc_id, h.body) for h in _searched(index, "tungsten OR sesame", raw=True).hits}
    assert after == before
    _integrity(index)


def test_the_corpus_is_described_so_a_caller_can_narrow_a_search(tmp_path):
    index = build(tmp_path, cycle(notes="tungsten"))
    described = index.describe()

    assert "documents" in described["tables"]
    assert "search" in described["tables"]
    assert described["row_counts"]["documents"] == 1
    # Counting an external-content FTS table means reconstructing every row out
    # of `documents`, which already answers it.
    assert "search" not in described["row_counts"]
    assert "assessment" in described["searchable_kinds"]
