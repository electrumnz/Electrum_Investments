"""The dreamer.

The first test in this file is the one that matters. Everything else is
bookkeeping about a store; that one is the reason the module is allowed to
exist next to a live order path.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from bot.dreaming import (
    DREAMER,
    FUSION,
    OPERATOR,
    TEXT_MAX_CHARS,
    TRADER,
    ConditionState,
    ConferenceDecision,
    ConferenceVerdict,
    Dream,
    DreamCondition,
    DreamStage,
    DreamStore,
    DreamSummary,
    DreamVerdict,
    Hop,
    MoveRefusal,
    NoDecision,
    SettleRefusal,
    Vault,
    VaultCaps,
    VaultTTLs,
    Verification,
    carry_forward_grading,
    fusion_candidates,
    grade_conditions,
    pending_observations,
    plan_fusion,
    promotion_for,
    weaker_of,
)
from bot.models import IndicatorSnapshot, OrderProposal, TriggerField, TriggerOp
from bot.triggers import CycleReadings

# ------------------------------------------------- the reason this is allowed


def test_a_dream_cannot_describe_an_order():
    """The safety guarantee is structural, not a matter of discipline.

    An order needs a quantity, a limit and a stop. `Dream` carries none of
    them, so there is no function that turns one into an `OrderProposal`
    without somebody first adding fields and validation by hand — which is a
    reviewable diff rather than an accident.

    If this test ever fails because a field was added for a good reason, the
    right response is to ask why the dreamer needs to express a size, and the
    answer is that it does not. It produces hypotheses. The decision loop
    proposes and `RiskGate.evaluate` vets what it proposes.
    """
    dream_fields = {f.name for f in fields(Dream)}
    order_fields = set(OrderProposal.model_fields)

    forbidden = {"qty", "limit_price", "stop_loss_price", "take_profit_price", "direction"}
    assert dream_fields & forbidden == set()
    # `symbol` is the other half of an instruction. A dream names its subject in
    # `instruments`, deliberately free text, so it cannot be read as a ticker
    # the bot trades.
    assert "symbol" not in dream_fields
    assert dream_fields & order_fields == set()


def test_claimed_symbols_are_a_permission_and_not_an_order():
    """`symbols` can become a trading permission, and that is still not an order.

    A granted symbol widens what the trading agent may CONSIDER. Nothing that
    decides whether a considered trade happens is touched by it: the risk gate
    still runs, the stop is still required, and the size still follows from the
    stop. So a dream naming SPY is a dream saying "this is about something you
    can trade", never "buy 21 of them".

    `instruments` and `symbols` both exist on purpose and must not be collapsed.
    The first is prose naming the subject — "Indonesian sesame" — and is free
    text precisely so it can never be read as a ticker. The second is the
    structured claim. Merging them would either start parsing the subject line
    for tickers or turn the permission into prose.
    """
    dream_fields = {f.name for f in fields(Dream)}

    assert {"symbols", "instruments"} <= dream_fields
    assert dream_fields & {"qty", "direction", "limit_price", "stop_loss_price"} == set()


# ------------------------------------------------------------- verification


def test_verification_is_counted_from_the_chain_not_claimed():
    """A model asked to rate its own sourcing rates it generously.

    So the badge is arithmetic over the `checked` flags rather than a field the
    agent fills in.
    """
    unchecked = Dream(title="t", seed="s", chain=[Hop("a"), Hop("b")])
    assert unchecked.verification is Verification.UNVERIFIED

    mixed = Dream(title="t", seed="s", chain=[Hop("a", True, "ref"), Hop("b")])
    assert mixed.verification is Verification.PARTIAL

    sourced = Dream(
        title="t", seed="s", chain=[Hop("a", True, "ref"), Hop("b", True, "ref")]
    )
    assert sourced.verification is Verification.SOURCED


def test_an_empty_chain_is_unverified_rather_than_sourced():
    """Nothing checked must never read as everything checked.

    Same rule as the tailnet status reporting `unknown` instead of healthy: the
    good outcome must not be what an absence of evidence looks like.
    """
    assert Dream(title="t", seed="s").verification is Verification.UNVERIFIED


def test_the_unchecked_hops_are_reportable():
    dream = Dream(
        title="t", seed="s", chain=[Hop("checked", True, "ref"), Hop("assumed")]
    )
    assert [h.claim for h in dream.unverified_hops] == ["assumed"]


# ------------------------------------------------------------------ stages


def test_a_dream_is_open_until_it_reaches_a_verdict():
    dream = Dream(title="t", seed="s")
    assert dream.is_open

    dream.add_thought(DreamStage.EXPLORE, "pulling on it")
    assert dream.is_open
    assert dream.stage is DreamStage.EXPLORE

    dream.add_thought(DreamStage.VERDICT, "it broke on hop three")
    assert not dream.is_open


def test_thoughts_accumulate_rather_than_overwrite():
    """The step where it changed its mind is usually the interesting one."""
    dream = Dream(title="t", seed="s")
    dream.add_thought(DreamStage.EXPLORE, "first")
    dream.add_thought(DreamStage.ITERATE, "that hop is weak")
    dream.add_thought(DreamStage.EXPLORE, "repaired it")

    assert [t.text for t in dream.thoughts] == ["first", "that hop is weak", "repaired it"]


def test_long_prose_is_trimmed_rather_than_rejected():
    """Same split as the decision loop: prose truncates, numbers reject.

    A rationale overrunning a cap once killed a live cycle and systemd
    restarted it straight into the same failure. Nothing downstream parses a
    thought, so losing the tail of one costs a reader some context.
    """
    dream = Dream(title="t", seed="s")
    dream.add_thought(DreamStage.EXPLORE, "x" * 5000)

    assert len(dream.thoughts[0].text) < 1000
    assert dream.thoughts[0].text.endswith("…")


# ------------------------------------------------------------------- store


@pytest.fixture
def store(tmp_path):
    return DreamStore(tmp_path / "dreams.db")


def test_a_dream_survives_a_round_trip(store):
    dream = Dream(
        title="Cicada broods and sesame",
        seed="Two of three producers inside overlapping brood ranges.",
        origin="a headline about crop insurance",
        chain=[Hop("broods are on fixed cycles", True, "entomological records"),
               Hop("the overlap happens this year")],
        weakest_hop="whether the overlap and the concentration coincide",
        trigger="the brood map published for next season",
        instruments=["sesame", "Indonesian agriculture"],
    )
    dream.add_thought(DreamStage.EXPLORE, "who is downstream of this")
    dream_id = store.save(dream)

    loaded = store.get(dream_id)

    assert loaded is not None
    assert loaded.title == dream.title
    assert loaded.weakest_hop == dream.weakest_hop
    assert loaded.trigger == dream.trigger
    assert loaded.instruments == ["sesame", "Indonesian agriculture"]
    assert [h.claim for h in loaded.chain] == [h.claim for h in dream.chain]
    assert loaded.chain[0].checked is True
    assert loaded.chain[1].checked is False
    assert [t.text for t in loaded.thoughts] == ["who is downstream of this"]
    assert loaded.verification is Verification.PARTIAL


def test_saving_twice_updates_rather_than_duplicating(store):
    dream = Dream(title="t", seed="s")
    first = store.save(dream)
    dream.add_thought(DreamStage.ITERATE, "second pass")
    second = store.save(dream)

    assert first == second
    assert len(store.recent()) == 1
    stored = store.get(first)
    assert stored is not None
    assert stored.stage is DreamStage.ITERATE


def test_recent_is_newest_first(store):
    old = Dream(title="old", seed="s", updated_at=datetime(2026, 1, 1, tzinfo=UTC))
    new = Dream(title="new", seed="s", updated_at=datetime(2026, 6, 1, tzinfo=UTC))
    store.save(old)
    store.save(new)

    assert [d.title for d in store.recent()] == ["new", "old"]


def test_a_damaged_row_is_skipped_rather_than_raising(store):
    """Same tolerance as the audit reader.

    One unparseable row must not take down a page an operator opened to look at
    everything else.
    """
    store.save(Dream(title="good", seed="s"))
    store.save(Dream(title="broken", seed="s"))
    with store._connect() as conn:
        conn.execute("UPDATE dreams SET chain='{not json' WHERE title='broken'")

    surviving = store.recent()

    assert [d.title for d in surviving] == ["good"]


def test_an_unknown_stage_does_not_lose_the_row(store):
    """A store that rejected an unfamiliar value would discard history."""
    store.save(Dream(title="t", seed="s"))
    with store._connect() as conn:
        conn.execute("UPDATE dreams SET stage='from-a-future-version'")

    loaded = store.recent()

    assert len(loaded) == 1
    assert loaded[0].stage is DreamStage.SEED


# ----------------------------------------------------------------- summary


def test_the_summary_counts_what_is_there():
    dreams = [
        Dream(title="a", seed="s", stage=DreamStage.EXPLORE),
        Dream(title="b", seed="s", stage=DreamStage.VERDICT, verdict=DreamVerdict.KEEP,
              chain=[Hop("checked", True, "ref")]),
        Dream(title="c", seed="s", stage=DreamStage.VERDICT, verdict=DreamVerdict.DROP),
        Dream(title="d", seed="s", stage=DreamStage.VERDICT, verdict=DreamVerdict.PARK),
    ]

    summary = DreamSummary.of(dreams)

    assert summary.total == 4
    assert summary.open_dreams == 1
    assert summary.kept == 1
    assert summary.dropped == 1
    assert summary.parked == 1
    # a, c and d have no checked hops at all.
    assert summary.unverified == 3


# ------------------------------------------------------------------ ledger


def test_the_ledger_counts_reasoning_and_never_returns():
    """The one form of learning this repository allows.

    Anthropic's Dreaming consolidates an agent's memory from its own past
    sessions. Applied to a trading account that means learning from profit and
    loss, which is forbidden here: forty trades is noise and a model shown three
    losses will confidently change approach. So the consolidation pass counts
    properties of the REASONING, which are true regardless of how any trade
    went, and there is no outcome sample to overfit to.
    """
    from bot.dreaming import DreamLedger

    ledger_fields = set(DreamLedger.__dataclass_fields__)
    money = {"pnl", "pnl_usd", "realised", "return_pct", "win_rate", "expectancy",
             "profit_factor", "r_multiple"}

    assert ledger_fields & money == set()


def test_the_ledger_reports_rates_as_unavailable_rather_than_zero():
    """An empty store has not scored nought for sourcing; it has no evidence.

    Same rule as the tailnet status and as `metrics.py`: the absence of a figure
    must not render as the worst possible value of it.
    """
    from bot.dreaming import DreamLedger

    empty = DreamLedger.of([])

    assert empty.sourcing_rate is None
    assert empty.drop_rate is None
    assert empty.median_hops is None


def test_the_ledger_counts_sourcing_and_drops():
    from bot.dreaming import DreamLedger

    ledger = DreamLedger.of([
        Dream(title="a", seed="s", stage=DreamStage.VERDICT, verdict=DreamVerdict.DROP,
              chain=[Hop("x", True, "ref"), Hop("y")]),
        Dream(title="b", seed="s", stage=DreamStage.VERDICT, verdict=DreamVerdict.KEEP,
              chain=[Hop("x", True, "ref"), Hop("y", True, "ref")]),
        Dream(title="c", seed="s", chain=[Hop("x")]),
    ])

    assert ledger.dreams == 3
    assert ledger.resolved == 2
    assert ledger.sourcing_rate == pytest.approx(60.0)   # 3 checked of 5 hops
    assert ledger.drop_rate == pytest.approx(50.0)       # 1 dropped of 2 resolved
    assert ledger.median_hops == 2


def test_the_ledger_flags_a_kept_idea_with_no_trigger():
    """A watch with no named trigger is a note. Same rule the Decisions page
    applies to the decision loop's own watches."""
    from bot.dreaming import DreamLedger

    ledger = DreamLedger.of([
        Dream(title="a", seed="s", stage=DreamStage.VERDICT, verdict=DreamVerdict.KEEP),
        Dream(title="b", seed="s", stage=DreamStage.VERDICT, verdict=DreamVerdict.KEEP,
              trigger="the brood map"),
    ])

    assert ledger.untriggered_keeps == 1


def test_the_ledger_flags_a_chain_nobody_attacked():
    from bot.dreaming import DreamLedger

    ledger = DreamLedger.of([
        Dream(title="a", seed="s", chain=[Hop("x")]),
        Dream(title="b", seed="s", chain=[Hop("x")], weakest_hop="x is assumed"),
    ])

    assert ledger.unattacked == 1


def test_a_thin_ledger_says_so():
    """A rate without its sample count gets believed anyway."""
    from bot.dreaming import THIN_LEDGER_THRESHOLD, DreamLedger

    few = [
        Dream(title=str(i), seed="s", stage=DreamStage.VERDICT,
              verdict=DreamVerdict.DROP)
        for i in range(3)
    ]
    many = [
        Dream(title=str(i), seed="s", stage=DreamStage.VERDICT,
              verdict=DreamVerdict.DROP)
        for i in range(THIN_LEDGER_THRESHOLD)
    ]

    assert DreamLedger.of(few).sample_is_thin
    assert not DreamLedger.of(many).sample_is_thin


# --------------------------------------------------------------- the migration

# The `dreams` table exactly as it was before the vaults existed. Written out by
# hand rather than imported, because importing today's SCHEMA would test nothing
# at all: the whole point is to start from the shape that is on the droplet.
OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS dreams (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  title        TEXT NOT NULL,
  seed         TEXT NOT NULL,
  stage        TEXT NOT NULL,
  verdict      TEXT,
  weakest_hop  TEXT NOT NULL DEFAULT '',
  trigger_note TEXT NOT NULL DEFAULT '',
  origin       TEXT NOT NULL DEFAULT '',
  chain        TEXT NOT NULL DEFAULT '[]',
  thoughts     TEXT NOT NULL DEFAULT '[]',
  instruments  TEXT NOT NULL DEFAULT '[]',
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
"""


def _old_store_with_a_row(
    path: Path, *, updated_at: str = "2026-03-01T09:00:00+00:00"
) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO dreams (title, seed, stage, verdict, weakest_hop, trigger_note,"
        " origin, chain, thoughts, instruments, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "Cicada broods and sesame",
            "Two of three producers inside overlapping brood ranges.",
            "explore",
            None,
            "whether the overlap and the concentration coincide",
            "the brood map published for next season",
            "a headline about crop insurance",
            json.dumps([{"claim": "broods are on fixed cycles", "checked": True,
                         "source": "entomological records"}]),
            json.dumps([{"stage": "explore", "text": "who is downstream",
                         "at": "2026-03-01T09:00:00+00:00", "by": ""}]),
            json.dumps(["sesame"]),
            "2026-02-01T09:00:00+00:00",
            updated_at,
        ),
    )
    conn.commit()
    conn.close()


def test_a_database_that_predates_the_vaults_is_migrated_in_place(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` is a no-op on a table that already exists.

    So editing `SCHEMA` changes what a FRESH store gets and nothing whatever
    about `data/dreams.db` on the droplet. The suite is structurally blind to
    that — every other test in this file builds its store from scratch in a
    `tmp_path` and therefore always gets the new shape — which is why this one
    builds the OLD schema by hand. That is the only way to exercise a migration
    at all.

    The same mistake in `journal.py` cost a live incident: 866 tests were green
    over a database that could not store the row the models had just been
    changed to allow, and the first real order reached Alpaca and then failed to
    journal.
    """
    path = tmp_path / "dreams.db"
    _old_store_with_a_row(path)

    store = DreamStore(path)

    # The old row survived, whole. A migration that dropped history to add a
    # column would be a far worse trade than the missing column.
    loaded = store.recent()
    assert len(loaded) == 1
    dream = loaded[0]
    assert dream.title == "Cicada broods and sesame"
    assert dream.weakest_hop == "whether the overlap and the concentration coincide"
    assert [h.claim for h in dream.chain] == ["broods are on fixed cycles"]
    assert dream.instruments == ["sesame"]

    # And the new columns are there and carry sane values rather than nulls.
    assert dream.vault is Vault.WORKBENCH
    assert dream.conditions == []
    assert dream.symbols == []
    assert dream.asset_class_key == ""
    assert dream.wisp == ""

    # Usable, not merely present: the migrated row round-trips through a write.
    dream.symbols = ["SPY"]
    dream.asset_class_key = "us_equity"
    dream.conditions = [DreamCondition(text="the brood map is published")]
    dream_id = store.save(dream)

    again = store.get(dream_id)
    assert again is not None
    assert again.symbols == ["SPY"]
    assert again.asset_class_key == "us_equity"
    assert [c.text for c in again.conditions] == ["the brood map is published"]


def test_the_migrated_vault_clock_is_backfilled_rather_than_left_empty(tmp_path):
    """An empty stamp would parse as `now` on every open.

    Which means the expiry clock restarts on every process start and nothing
    ever ages out — a bug whose only symptom is a workbench that grows forever.
    `updated_at` is the honest reading available: the row was last touched then.
    """
    path = tmp_path / "dreams.db"
    _old_store_with_a_row(path, updated_at="2026-03-01T09:00:00+00:00")

    dream = DreamStore(path).recent()[0]

    assert dream.vault_entered_at == datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


def test_a_migrated_database_ends_up_identical_to_a_fresh_one(tmp_path):
    """The columns live in two places, and this is what keeps them in step.

    `SCHEMA` has them so a FRESH store is built with them, and
    `_ADDED_DREAM_COLUMNS` has them so an EXISTING table gains them. Both halves
    are needed. Leaving them out of `SCHEMA` looks like it works — a new store
    gets migrated on its first open — but it means the migration warning fires
    on every first run and `SCHEMA` stops being the answer to "what shape is
    this table", which is where the next person will look. Found by comparing
    the two, not by reading either.
    """
    old = tmp_path / "old.db"
    _old_store_with_a_row(old)
    DreamStore(old)
    DreamStore(tmp_path / "fresh.db")

    def columns(path: Path, table: str) -> list[str]:
        conn = sqlite3.connect(path)
        try:
            return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]
        finally:
            conn.close()

    for table in ("dreams", "dream_messages", "adoptions", "conference_decisions"):
        assert columns(old, table) == columns(tmp_path / "fresh.db", table), table


def test_a_fresh_database_is_not_migrated_at_all(tmp_path):
    """`PRAGMA table_info` is a lookup; a correct database pays one query.

    Nothing is altered and nothing is backfilled, so opening a new store neither
    logs a migration nor rewrites a clock that `save` has just set.
    """
    path = tmp_path / "dreams.db"
    store = DreamStore(path)
    entered = datetime(2026, 6, 1, tzinfo=UTC)
    dream_id = store.save(
        Dream(title="t", seed="s", vault_entered_at=entered, updated_at=entered)
    )

    reopened = DreamStore(path).get(dream_id)

    assert reopened is not None
    assert reopened.vault_entered_at == entered


def test_the_migration_is_idempotent_and_leaves_a_current_database_alone(tmp_path):
    """It runs on every open, so re-running it must change nothing.

    `PRAGMA table_info` is a lookup: a correct database pays one query and
    stops. Opening the same file three times must not duplicate a column, lose a
    row, or reset a clock that a move has already set.
    """
    path = tmp_path / "dreams.db"
    _old_store_with_a_row(path)

    first = DreamStore(path)
    dream = first.recent()[0]
    assert dream.id is not None
    moved_at = datetime(2026, 6, 1, tzinfo=UTC)
    first.move(dream.id, Vault.PROPHECY, by=DREAMER, at=moved_at)

    DreamStore(path)
    third = DreamStore(path)

    reopened = third.recent()
    assert len(reopened) == 1
    assert reopened[0].vault is Vault.PROPHECY
    assert reopened[0].vault_entered_at == moved_at


# ------------------------------------------------------------------ conditions


def test_a_dream_with_no_conditions_is_not_all_conditions_met():
    """`all([])` is True, and that is exactly the trap.

    An empty condition list must not report a prophecy as fulfilled the moment
    it is written, because the vault it unlocks is the one the trading agent can
    see. Same rule as `can_grade_anything` in triggers.py and `has_cycles` in
    news_history: absence of evidence is its own state, never the good outcome.
    """
    bare = Dream(title="t", seed="s")

    assert bare.has_conditions is False
    assert bare.all_conditions_met is False
    assert bare.conditions_met == 0


def test_conditions_are_counted_and_only_all_of_them_counts_as_met():
    partly = Dream(
        title="t",
        seed="s",
        conditions=[
            DreamCondition(text="the brood map is published", fulfilled=True),
            DreamCondition(text="the harvest report lands"),
        ],
    )
    assert partly.has_conditions is True
    assert partly.conditions_met == 1
    assert partly.all_conditions_met is False
    assert [c.text for c in partly.unmet_conditions] == ["the harvest report lands"]

    done = Dream(
        title="t",
        seed="s",
        conditions=[DreamCondition(text="published", fulfilled=True)],
    )
    assert done.all_conditions_met is True


def test_a_condition_carries_prose_and_a_checkable_triple():
    """Same split as `SymbolAssessment` and `AssessmentTrigger`.

    The sentence is what a person reads; the triple is what code can settle. A
    condition with only prose is legal and reports itself as not checkable
    rather than being rejected — refusing it would push a dreamer towards
    inventing a number to satisfy a validator.
    """
    prose_only = DreamCondition(text="the spread normalises")
    assert prose_only.is_checkable is False
    assert prose_only.as_trigger() is None

    checkable = DreamCondition(
        text="SPY closes below 641.20, roughly 1 ATR under the 20-day",
        field=TriggerField.CLOSE,
        op=TriggerOp.BELOW,
        value=641.20,
    )
    assert checkable.is_checkable is True
    trigger = checkable.as_trigger()
    assert trigger is not None
    # Graded by the same comparison `triggers.py` already uses, rather than a
    # second implementation that can drift from it.
    assert trigger.holds(640.0) is True
    assert trigger.holds(645.0) is False
    assert trigger.holds(None) is None


def test_conditions_survive_a_round_trip_and_a_damaged_one_keeps_its_sentence(store):
    dream = Dream(
        title="t",
        seed="s",
        conditions=[
            DreamCondition(
                text="SPY closes below 641.20",
                field=TriggerField.CLOSE,
                op=TriggerOp.BELOW,
                value=641.20,
                fulfilled=True,
                fulfilled_at=datetime(2026, 5, 4, tzinfo=UTC),
                note="closed 638.90",
            )
        ],
    )
    dream_id = store.save(dream)

    loaded = store.get(dream_id)
    assert loaded is not None
    condition = loaded.conditions[0]
    assert condition.field is TriggerField.CLOSE
    assert condition.value == pytest.approx(641.20)
    assert condition.fulfilled is True
    assert condition.fulfilled_at == datetime(2026, 5, 4, tzinfo=UTC)
    assert condition.note == "closed 638.90"

    # A field name from a newer version comes back as prose rather than taking
    # the row down. Losing the ability to grade it and keeping the sentence is
    # the right way round: the operator can still read what was claimed.
    with store._connect() as conn:
        conn.execute(
            "UPDATE dreams SET conditions=?",
            (json.dumps([{"text": "something", "field": "from_the_future",
                          "op": "below", "value": 1.0}]),),
        )
    recovered = store.get(dream_id)
    assert recovered is not None
    assert recovered.conditions[0].text == "something"
    assert recovered.conditions[0].is_checkable is False


# ------------------------------------------------------------- moving vaults


def _vaulted(
    store: DreamStore,
    *,
    title: str = "ready",
    symbols: list[str] | None = None,
    asset_class_key: str = "us_equity",
) -> int:
    """A dream sitting in VAULT, ready to be adopted."""
    dream = Dream(
        title=title,
        seed="s",
        symbols=["SPY"] if symbols is None else symbols,
        asset_class_key=asset_class_key,
    )
    dream_id = store.save(dream)
    assert store.move(dream_id, Vault.VAULT, by=DREAMER)
    return dream_id


def test_a_dream_starts_on_the_workbench(store):
    dream_id = store.save(Dream(title="t", seed="s"))

    loaded = store.get(dream_id)
    assert loaded is not None
    assert loaded.vault is Vault.WORKBENCH
    assert loaded.is_offerable is False


def test_the_dreamer_moves_between_its_own_shelves(store):
    dream_id = store.save(Dream(title="t", seed="s"))

    for destination in (Vault.PROPHECY, Vault.VAULT, Vault.ARCHIVE, Vault.WORKBENCH):
        result = store.move(dream_id, destination, by=DREAMER)
        assert result.ok, result.detail
        loaded = store.get(dream_id)
        assert loaded is not None
        assert loaded.vault is destination


def test_the_dreamer_cannot_adopt_on_the_traders_behalf(store):
    """Adoption is the trading agent taking something, not the dreamer offering.

    A dreamer that could move a dream into ADOPTED would be granting itself a
    symbol permission, which is the one thing the split exists to prevent.
    """
    dream_id = _vaulted(store)

    result = store.move(dream_id, Vault.ADOPTED, by=DREAMER)

    assert result.refused
    assert MoveRefusal.FORBIDDEN_ACTOR in result.refusals
    loaded = store.get(dream_id)
    assert loaded is not None
    assert loaded.vault is Vault.VAULT


def test_the_trader_cannot_move_a_dream_with_move(store):
    """The trading agent has exactly two verbs, and both are their own method."""
    dream_id = _vaulted(store)

    result = store.move(dream_id, Vault.ARCHIVE, by=TRADER)

    assert result.refused
    assert MoveRefusal.FORBIDDEN_ACTOR in result.refusals
    assert "adopt()" in result.detail


def test_an_unrecognised_actor_is_refused_rather_than_waved_through(store):
    """Failing open on an authorisation question is how the rule stops applying."""
    dream_id = store.save(Dream(title="t", seed="s"))

    result = store.move(dream_id, Vault.VAULT, by="some-new-agent")

    assert result.refused
    assert MoveRefusal.FORBIDDEN_ACTOR in result.refusals
    loaded = store.get(dream_id)
    assert loaded is not None
    assert loaded.vault is Vault.WORKBENCH


def test_moving_a_missing_dream_answers_rather_than_raising(store):
    result = store.move(9999, Vault.VAULT, by=DREAMER)

    assert result.refused
    assert result.refusals == (MoveRefusal.NOT_FOUND,)


def test_a_full_vault_refuses_and_does_not_raise(store):
    """A full vault must not take down the page an operator opened to see it."""
    caps = VaultCaps(vault=2)
    for i in range(2):
        store.move(store.save(Dream(title=str(i), seed="s")), Vault.VAULT,
                   by=DREAMER, caps=caps)
    third = store.save(Dream(title="third", seed="s"))

    result = store.move(third, Vault.VAULT, by=DREAMER, caps=caps)

    assert result.refused
    assert MoveRefusal.FULL in result.refusals
    assert "2 of 2" in result.detail
    assert store.counts_by_vault()[Vault.VAULT] == 2


def test_every_refusal_is_collected_rather_than_the_first_one(store):
    """Same property as `RiskGate`, and for the same reason.

    An agent told one reason at a time fixes it, asks again, and is told the
    next one.
    """
    caps = VaultCaps(vault=1)
    store.move(store.save(Dream(title="filler", seed="s")), Vault.VAULT,
               by=DREAMER, caps=caps)
    blocked = store.save(Dream(title="blocked", seed="s"))

    result = store.move(blocked, Vault.VAULT, by=TRADER, caps=caps)

    assert set(result.refusals) == {MoveRefusal.FORBIDDEN_ACTOR, MoveRefusal.FULL}


def test_the_archive_is_never_full(store):
    """The only alternative to archiving a full archive is deleting from it."""
    caps = VaultCaps(archive=None)
    ids = [store.save(Dream(title=str(i), seed="s")) for i in range(6)]

    for dream_id in ids:
        assert store.move(dream_id, Vault.ARCHIVE, by=DREAMER, caps=caps).ok

    assert store.counts_by_vault()[Vault.ARCHIVE] == 6


# ------------------------------------------------------------- the vault clock


def test_entering_a_vault_restarts_that_vaults_clock(store):
    """Expiry is measured from `vault_entered_at`, never from `created_at`.

    A dream pulled back out of the vault for more work would otherwise inherit a
    nearly-dead clock and expire mid-rework, which punishes exactly the
    behaviour the system wants.
    """
    born = datetime(2026, 1, 1, tzinfo=UTC)
    dream_id = store.save(Dream(title="t", seed="s", created_at=born, updated_at=born))

    reworked_at = datetime(2026, 6, 1, tzinfo=UTC)
    store.move(dream_id, Vault.PROPHECY, by=DREAMER, at=reworked_at)

    loaded = store.get(dream_id)
    assert loaded is not None
    assert loaded.created_at == born
    assert loaded.vault_entered_at == reworked_at

    # Five months old, one day in its current vault: not expired on a 90-day TTL.
    a_day_later = reworked_at + timedelta(days=1)
    assert store.expired(a_day_later, VaultTTLs(prophecy=90)) == []


def test_a_move_to_the_vault_it_is_already_in_is_refused(store):
    """Otherwise a caller with a refresh loop keeps everything alive forever."""
    entered = datetime(2026, 6, 1, tzinfo=UTC)
    dream_id = store.save(Dream(title="t", seed="s"))
    store.move(dream_id, Vault.VAULT, by=DREAMER, at=entered)

    result = store.move(dream_id, Vault.VAULT, by=DREAMER,
                        at=datetime(2026, 9, 1, tzinfo=UTC))

    assert result.refused
    assert MoveRefusal.ALREADY_THERE in result.refusals
    loaded = store.get(dream_id)
    assert loaded is not None
    assert loaded.vault_entered_at == entered


def test_expiry_marks_and_never_deletes(store):
    """Same shape as `stop_watch`: report loudly, act never.

    An automatic action nobody watched is a different proposition from a loud
    report, and archiving a chain somebody is halfway through is the worse of
    the two.
    """
    entered = datetime(2026, 1, 1, tzinfo=UTC)
    stale = store.save(Dream(title="stale", seed="s"))
    store.move(stale, Vault.PROPHECY, by=DREAMER, at=entered)
    fresh = store.save(Dream(title="fresh", seed="s"))
    store.move(fresh, Vault.PROPHECY, by=DREAMER,
               at=datetime(2026, 6, 1, tzinfo=UTC))

    now = datetime(2026, 6, 10, tzinfo=UTC)
    expired = store.expired(now, VaultTTLs(prophecy=90))

    assert [d.title for d in expired] == ["stale"]
    # Nothing was removed and nothing was moved.
    assert len(store.recent()) == 2
    assert store.counts_by_vault()[Vault.PROPHECY] == 2


def test_the_archive_never_expires(store):
    """The archive IS the retirement state, so a TTL on it is an expiry on one."""
    dream_id = store.save(Dream(title="t", seed="s"))
    store.move(dream_id, Vault.ARCHIVE, by=DREAMER,
               at=datetime(2020, 1, 1, tzinfo=UTC))

    assert store.expired(datetime(2026, 8, 10, tzinfo=UTC)) == []


# ---------------------------------------------------------------- the handover


def test_the_trader_adopts_from_the_vault_and_the_dreamer_keeps_a_wisp(store):
    at = datetime(2026, 6, 1, tzinfo=UTC)
    dream_id = _vaulted(store, symbols=["SPY", "spy", " QQQ "])

    result = store.adopt(dream_id, at=at, ttl_days=90)

    assert result.ok, result.detail
    assert result.moved_from is Vault.VAULT
    assert result.moved_to is Vault.ADOPTED

    loaded = store.get(dream_id)
    assert loaded is not None
    assert loaded.vault is Vault.ADOPTED
    assert loaded.wisp
    # Nothing was destroyed to leave a trace. The chain, the thoughts and the
    # conditions all stay on the row; the wisp is what the DREAMER is handed
    # back, not what survives.
    assert loaded.symbols == ["SPY", "QQQ"]

    adoption = store.adoptions(dream_id)[0]
    assert adoption.symbols_granted == ["SPY", "QQQ"]
    assert adoption.asset_class == "us_equity"
    assert adoption.expires_at == at + timedelta(days=90)
    assert adoption.is_live(at + timedelta(days=1)) is True


def test_adoption_copies_the_grant_rather_than_pointing_at_the_dream(store):
    """A permission has to be a fixed record of what was agreed.

    The dream can be edited after it is taken, and a grant re-read off a mutable
    field at query time would silently change scope. Same reasoning as the
    journal recording the proposal rather than re-deriving it later.
    """
    at = datetime(2026, 6, 1, tzinfo=UTC)
    dream_id = _vaulted(store, symbols=["SPY"])
    store.adopt(dream_id, at=at)

    dream = store.get(dream_id)
    assert dream is not None
    dream.symbols = ["TSLA", "NVDA", "GME"]
    store.save(dream)

    assert store.granted_symbols(at + timedelta(days=1)) == {"SPY": "us_equity"}


def test_adoption_is_refused_from_anywhere_but_the_vault(store):
    dream_id = store.save(
        Dream(title="t", seed="s", symbols=["SPY"], asset_class_key="us_equity")
    )
    store.move(dream_id, Vault.PROPHECY, by=DREAMER)

    result = store.adopt(dream_id)

    assert result.refused
    assert MoveRefusal.WRONG_VAULT in result.refusals


def test_adoption_needs_symbols_and_a_resolved_class(store):
    """A grant of nothing reads on the page as a grant."""
    bare = store.save(Dream(title="t", seed="s"))
    store.move(bare, Vault.VAULT, by=DREAMER)

    result = store.adopt(bare)

    assert result.refused
    assert set(result.refusals) == {
        MoveRefusal.NEEDS_SYMBOLS,
        MoveRefusal.NEEDS_ASSET_CLASS,
    }
    loaded = store.get(bare)
    assert loaded is not None
    assert loaded.vault is Vault.VAULT


def test_the_adopted_cap_matches_the_position_cap(store):
    """An adopted dream the account has no slot to trade is a promise it cannot
    keep."""
    caps = VaultCaps(adopted=3)
    for i in range(3):
        store.adopt(_vaulted(store, title=f"held-{i}"), caps=caps)
    fourth = _vaulted(store, title="fourth")

    result = store.adopt(fourth, caps=caps)

    assert result.refused
    assert MoveRefusal.FULL in result.refusals
    assert store.counts_by_vault()[Vault.ADOPTED] == 3


def test_the_trader_hands_a_dream_back_with_a_reason(store):
    at = datetime(2026, 6, 1, tzinfo=UTC)
    dream_id = _vaulted(store)
    store.adopt(dream_id, at=at)

    returned_at = at + timedelta(days=10)
    result = store.return_to_vault(
        dream_id, reason="the setup never formed and the class limit moved", at=returned_at
    )

    assert result.ok, result.detail
    loaded = store.get(dream_id)
    assert loaded is not None
    assert loaded.vault is Vault.VAULT
    # The dream is the dreamer's again, so a trace of something it now holds
    # whole would be misleading.
    assert loaded.wisp == ""

    adoption = store.adoptions(dream_id)[0]
    assert adoption.returned_at == returned_at
    assert adoption.return_reason.startswith("the setup never formed")
    assert adoption.is_live(returned_at) is False
    assert store.granted_symbols(returned_at) == {}


def test_handing_a_dream_back_without_a_reason_is_refused(store):
    """An adoption is an argument the dreamer won.

    Giving it back without a word is that argument reversed with no record, and
    the record is most of what the Dreaming page is for.
    """
    dream_id = _vaulted(store)
    store.adopt(dream_id)

    result = store.return_to_vault(dream_id, reason="   ")

    assert result.refused
    assert MoveRefusal.NEEDS_REASON in result.refusals
    loaded = store.get(dream_id)
    assert loaded is not None
    assert loaded.vault is Vault.ADOPTED


def test_returning_something_that_was_never_adopted_is_refused(store):
    dream_id = _vaulted(store)

    result = store.return_to_vault(dream_id, reason="changed my mind")

    assert result.refused
    assert MoveRefusal.WRONG_VAULT in result.refusals


def test_the_handover_narrates_itself_in_the_transcript(store):
    """Written by the store so the transcript is complete even when neither
    agent thought to narrate the handover."""
    dream_id = _vaulted(store)
    store.adopt(dream_id)
    store.return_to_vault(dream_id, reason="the thesis broke on the second hop")

    kinds = [m.kind for m in store.messages(dream_id)]

    assert kinds == ["accept", "return"]


# ---------------------------------------------------------------- the deleting


def test_the_trader_can_never_delete(store):
    """Deleting is how a record of a disagreement disappears, and the agent with
    a route to the broker is the one that must not be able to do that."""
    dream_id = _vaulted(store)

    result = store.delete(dream_id, by=TRADER)

    assert result.refused
    assert MoveRefusal.FORBIDDEN_ACTOR in result.refusals
    assert store.get(dream_id) is not None


def test_an_adopted_dream_cannot_be_deleted_by_anyone(store):
    """A live grant whose dream has been deleted is a permission nobody can
    explain."""
    dream_id = _vaulted(store)
    store.adopt(dream_id)

    for actor in (DREAMER, OPERATOR):
        result = store.delete(dream_id, by=actor)
        assert result.refused
        assert MoveRefusal.FORBIDDEN_ACTOR in result.refusals

    assert store.get(dream_id) is not None


def test_the_dreamer_deletes_from_its_own_shelves_and_takes_the_trail_with_it(store):
    dream_id = store.save(Dream(title="t", seed="s"))
    store.add_message(dream_id, speaker=DREAMER, text="a note")

    assert store.delete(dream_id, by=DREAMER).ok

    assert store.get(dream_id) is None
    assert store.messages(dream_id) == []


# ---------------------------------------------------------- granted permissions


def test_granted_symbols_is_deterministic_given_now(store):
    at = datetime(2026, 6, 1, tzinfo=UTC)
    store.adopt(_vaulted(store, symbols=["SPY"]), at=at, ttl_days=30)

    during = at + timedelta(days=1)
    assert store.granted_symbols(during) == {"SPY": "us_equity"}
    assert store.granted_symbols(during) == store.granted_symbols(during)


def test_an_expired_grant_permits_nothing(store):
    """A permission with no end is a permission nobody revisits."""
    at = datetime(2026, 6, 1, tzinfo=UTC)
    store.adopt(_vaulted(store, symbols=["SPY"]), at=at, ttl_days=30)

    assert store.granted_symbols(at + timedelta(days=29)) == {"SPY": "us_equity"}
    assert store.granted_symbols(at + timedelta(days=31)) == {}


def test_a_grant_with_no_resolved_class_permits_nothing(store):
    """A symbol whose class is unknown is a symbol whose limits are unknown.

    `adopt` refuses one, and this is the second lock, for a row that arrived any
    other way. Same arrangement as `mode=ro` plus the statement guard in
    insight.py: the first check is the useful message, the second is the
    guarantee.
    """
    at = datetime(2026, 6, 1, tzinfo=UTC)
    dream_id = store.adopt(_vaulted(store, symbols=["SPY"]), at=at).dream_id
    with store._connect() as conn:
        conn.execute("UPDATE adoptions SET asset_class='' WHERE dream_id=?", (dream_id,))

    assert store.granted_symbols(at + timedelta(days=1)) == {}


def test_a_grant_whose_dream_is_gone_permits_nothing(store):
    """`delete` already refuses an adopted dream; this is the lock that does not
    depend on that having been got right."""
    at = datetime(2026, 6, 1, tzinfo=UTC)
    dream_id = store.adopt(_vaulted(store, symbols=["SPY"]), at=at).dream_id
    with store._connect() as conn:
        conn.execute("DELETE FROM dreams WHERE id=?", (dream_id,))

    assert store.granted_symbols(at + timedelta(days=1)) == {}


def test_a_symbol_claimed_by_two_classes_is_dropped_rather_than_resolved(store):
    """There is no correct answer to which cap applies.

    Picking one would produce a plausible wrong figure, which is the failure
    this repository exists to prevent.
    """
    at = datetime(2026, 6, 1, tzinfo=UTC)
    store.adopt(_vaulted(store, title="a", symbols=["SPY", "QQQ"]), at=at)
    second = _vaulted(store, title="b", symbols=["SPY"], asset_class_key="crypto")
    store.adopt(second, at=at)

    granted = store.granted_symbols(at + timedelta(days=1))

    assert "SPY" not in granted
    assert granted == {"QQQ": "us_equity"}


def test_an_unreadable_grant_permits_nothing_rather_than_raising(store):
    at = datetime(2026, 6, 1, tzinfo=UTC)
    dream_id = store.adopt(_vaulted(store, symbols=["SPY"]), at=at).dream_id
    with store._connect() as conn:
        conn.execute(
            "UPDATE adoptions SET symbols_granted='{not json' WHERE dream_id=?",
            (dream_id,),
        )

    assert store.granted_symbols(at + timedelta(days=1)) == {}
    # And the reader survives it too, for the reason every read here is
    # forgiving: one bad row must not take a page down.
    assert store.adoptions(dream_id) == []


# ------------------------------------------------------------------- messages


def test_messages_accumulate_and_read_oldest_first(store):
    """A negotiation read newest-first is a negotiation read backwards."""
    dream_id = _vaulted(store)
    base = datetime(2026, 6, 1, tzinfo=UTC)
    store.add_message(dream_id, speaker=DREAMER, kind="offer", text="this one is ready",
                      at=base)
    store.add_message(dream_id, speaker=TRADER, kind="question",
                      text="what would kill it?", at=base + timedelta(minutes=1))
    store.add_message(dream_id, speaker=DREAMER, kind="answer",
                      text="the brood map slipping a season",
                      at=base + timedelta(minutes=2))

    conversation = store.messages(dream_id)

    assert [m.speaker for m in conversation] == [DREAMER, TRADER, DREAMER]
    assert [m.kind for m in conversation] == ["offer", "question", "answer"]
    assert conversation[0].text == "this one is ready"


def test_a_message_is_trimmed_rather_than_rejected(store):
    """A message that would not store is a turn of the conversation lost to a
    character count."""
    dream_id = store.save(Dream(title="t", seed="s"))

    stored = store.add_message(dream_id, speaker=TRADER, text="x" * 5000)

    assert len(stored.text) < 1000
    assert stored.text.endswith("…")


def test_an_unfamiliar_message_kind_is_kept(store):
    """A closed enum would refuse the transcript of a second dreamer."""
    dream_id = store.save(Dream(title="t", seed="s"))
    store.add_message(dream_id, speaker="dreamer-2", kind="rebuttal", text="no")

    assert [m.kind for m in store.messages(dream_id)] == ["rebuttal"]


# -------------------------------------------------------------- vault counting


def test_counts_by_vault_names_every_shelf_including_the_empty_ones(store):
    """A missing key reads as 'no data'; a zero reads as a stated fact."""
    store.move(store.save(Dream(title="a", seed="s")), Vault.PROPHECY, by=DREAMER)

    counts = store.counts_by_vault()

    assert set(counts) == set(Vault)
    assert counts[Vault.PROPHECY] == 1
    assert counts[Vault.ADOPTED] == 0


def test_the_summary_counts_the_vaults_too():
    dreams = [
        Dream(title="a", seed="s"),
        Dream(title="b", seed="s", vault=Vault.PROPHECY),
        Dream(title="c", seed="s", vault=Vault.VAULT),
        Dream(title="d", seed="s", vault=Vault.ADOPTED),
    ]

    summary = DreamSummary.of(dreams)

    assert summary.total == 4
    assert set(summary.by_vault) == set(Vault)
    assert summary.workbench == 1
    assert summary.prophecies == 1
    assert summary.vaulted == 1
    assert summary.adopted == 1
    assert summary.archived == 0


def test_the_ledger_flags_a_prophecy_that_promises_nothing():
    """The `untriggered_keeps` failure in a new costume.

    A prophecy is a claim that the world will do something. One with no
    condition attached is a claim nobody can ever settle.
    """
    from bot.dreaming import DreamLedger

    ledger = DreamLedger.of([
        Dream(title="a", seed="s", vault=Vault.PROPHECY),
        Dream(title="b", seed="s", vault=Vault.PROPHECY,
              conditions=[DreamCondition(text="the brood map is published")]),
        Dream(title="c", seed="s", vault=Vault.PROPHECY,
              conditions=[DreamCondition(text="published", fulfilled=True)]),
    ])

    assert ledger.conditionless_prophecies == 1
    assert ledger.fulfilled_not_offered == 1


# ------------------------------------- an adoption may only narrow the offer


def test_adoption_cannot_grant_a_symbol_the_dream_never_claimed(store):
    """Any dream in the vault was a blank cheque, and the tool hands the fields
    to a model.

    `adopt` took `symbols` and `asset_class` as given, so a dream about sesame
    supply chains could be adopted as a permission to trade anything — the id
    was the only thing being checked. `mcp_server.adopt_dream` exposes both
    arguments, so this was one tool call from a self-granted symbol.
    """
    dream_id = _vaulted(store, symbols=["SPY"])

    result = store.adopt(dream_id, symbols=["NVDA"])

    assert result.refused
    assert MoveRefusal.SYMBOLS_NOT_OFFERED in result.refusals
    assert store.granted_symbols(datetime(2026, 6, 1, tzinfo=UTC)) == {}
    loaded = store.get(dream_id)
    assert loaded is not None
    assert loaded.vault is Vault.VAULT


def test_a_dream_claiming_nothing_cannot_be_adopted_into_a_permission(store):
    """The blank cheque in its purest form: no symbols, no class, both invented
    at the moment of adoption."""
    bare = store.save(Dream(title="a perfectly ordinary dream", seed="s"))
    store.move(bare, Vault.VAULT, by=DREAMER)

    result = store.adopt(bare, symbols=["BTC/USD"], asset_class="us_equity")

    assert result.refused
    assert MoveRefusal.SYMBOLS_NOT_OFFERED in result.refusals
    assert MoveRefusal.CLASS_NOT_OFFERED in result.refusals


def test_adoption_cannot_choose_a_different_class_from_the_dreams(store):
    """The class decides which limits the symbols face, so picking one at
    adoption is picking the caps rather than accepting them."""
    dream_id = _vaulted(store, symbols=["SPY"], asset_class_key="us_equity")

    result = store.adopt(dream_id, asset_class="crypto")

    assert result.refused
    assert MoveRefusal.CLASS_NOT_OFFERED in result.refusals


def test_adoption_may_take_less_than_the_dream_offers(store):
    """Narrowing is the trading agent being careful and must stay allowed."""
    at = datetime(2026, 6, 1, tzinfo=UTC)
    dream_id = _vaulted(store, symbols=["SPY", "QQQ", "AAPL"])

    result = store.adopt(dream_id, symbols=["qqq"], at=at)

    assert result.ok, result.detail
    assert store.adoptions(dream_id)[0].symbols_granted == ["QQQ"]


def test_restating_the_dreams_own_class_is_not_an_override(store):
    at = datetime(2026, 6, 1, tzinfo=UTC)
    dream_id = _vaulted(store, symbols=["SPY"], asset_class_key="us_equity")

    assert store.adopt(dream_id, asset_class="us_equity", at=at).ok
    assert store.granted_symbols(at + timedelta(days=1)) == {"SPY": "us_equity"}


# ------------------------------------------------- one event, one transaction


def test_an_interrupted_adoption_leaves_no_grant_behind(store, monkeypatch):
    """`adopt` claimed to be one event and was three connections.

    A failure between them left a live symbol permission on a dream still
    sitting in the VAULT — a grant no page describes, which `return_to_vault`
    then refuses to close with `WRONG_VAULT`, leaving deletion as the only exit.
    """
    dream_id = _vaulted(store, symbols=["SPY"])
    at = datetime(2026, 6, 1, tzinfo=UTC)

    def boom(*args, **kwargs):
        raise OSError("the box died between two writes")

    monkeypatch.setattr(DreamStore, "_apply_move", boom)
    with pytest.raises(OSError):
        store.adopt(dream_id, at=at)

    assert store.adoptions(dream_id) == []
    assert store.granted_symbols(at) == {}
    loaded = store.get(dream_id)
    assert loaded is not None
    assert loaded.vault is Vault.VAULT


def test_a_grant_needs_the_dream_to_still_be_on_the_adopted_shelf(store):
    """`save()` deliberately bypasses `move`, so `dream.vault` can be set by
    hand — and the grant join only checked that the dream EXISTED."""
    at = datetime(2026, 6, 1, tzinfo=UTC)
    dream_id = _vaulted(store, symbols=["SPY"])
    assert store.adopt(dream_id, at=at).ok
    assert store.granted_symbols(at) == {"SPY": "us_equity"}

    dream = store.get(dream_id)
    assert dream is not None
    dream.vault = Vault.ARCHIVE
    store.save(dream)

    assert store.granted_symbols(at) == {}


# ------------------------------------------------- one definition of "live"


def test_a_grant_written_in_new_zealand_time_expires_when_it_expires(store):
    """The permission path compared ISO timestamps as TEXT.

    `adopt` wrote whatever offset the caller's `at` carried, and the dream timer
    runs on `Pacific/Auckland` by design — so a `+13:00` stamp sorted as though
    it were thirteen hours later than it was, and `granted_symbols` reported a
    lapsed grant as live while `Adoption.is_live` correctly said expired. Two
    answers, and the permission one was the lenient one.
    """
    nz = timezone(timedelta(hours=13))
    at = datetime(2026, 6, 1, 16, 0, tzinfo=UTC)
    dream_id = _vaulted(store, symbols=["SPY"])

    store.adopt(dream_id, at=at.astimezone(nz), ttl_days=1)

    six_hours_past_expiry = at + timedelta(days=1, hours=6)
    adoption = store.adoptions(dream_id)[0]
    assert adoption.is_live(six_hours_past_expiry) is False
    assert store.granted_symbols(six_hours_past_expiry) == {}
    # And still live before it lapses, so the test is about the offset rather
    # than about expiry being broken in general.
    assert store.granted_symbols(at + timedelta(hours=1)) == {"SPY": "us_equity"}


def test_every_stamp_this_store_writes_is_utc(store):
    """Text ordering is only instant ordering when every row shares an offset."""
    nz = timezone(timedelta(hours=13))
    at = datetime(2026, 6, 1, 16, 0, tzinfo=UTC)
    dream_id = _vaulted(store, symbols=["SPY"])
    store.adopt(dream_id, at=at.astimezone(nz), ttl_days=1)

    with sqlite3.connect(store.path) as conn:
        row = conn.execute(
            "SELECT adopted_at, expires_at FROM adoptions"
        ).fetchone()
    assert row[0].endswith("+00:00")
    assert row[1].endswith("+00:00")


def test_an_adoption_with_no_expiry_grants_nothing(store):
    """`adopt` always writes one, so a NULL is hand-edited or migrated — and a
    permission nobody set an end on is exactly the one that should not outlive
    everybody who remembers it."""
    at = datetime(2026, 6, 1, tzinfo=UTC)
    dream_id = _vaulted(store, symbols=["SPY"])
    store.adopt(dream_id, at=at)
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE adoptions SET expires_at=NULL")

    assert store.adoptions(dream_id)[0].is_live(at) is False
    assert store.granted_symbols(at) == {}


def test_a_naive_now_is_read_as_utc_rather_than_raising(store):
    """`is_live` sits in the decision cycle's path. A `TypeError` out of it is
    the shape `claude.propose` was wrapped for."""
    at = datetime(2026, 6, 1, tzinfo=UTC)
    dream_id = _vaulted(store, symbols=["SPY"])
    store.adopt(dream_id, at=at, ttl_days=10)

    adoption = store.adoptions(dream_id)[0]
    assert adoption.is_live((at + timedelta(days=1)).replace(tzinfo=None)) is True
    assert adoption.is_live((at + timedelta(days=30)).replace(tzinfo=None)) is False


# ------------------------------------------ the adopted shelf counts LIVE grants


def test_an_expired_adoption_stops_occupying_a_slot(store):
    """Three expiries used to brick the shelf permanently.

    The cap exists because `caps.adopted` matches `max_concurrent_positions`: an
    adoption is a promise the trading agent may act on it. A dream whose grant
    lapsed is no longer such a promise, and counting it left nothing adoptable
    while `delete` refused an ADOPTED dream and `move` refused every actor.
    """
    caps = VaultCaps(adopted=3)
    at = datetime(2026, 6, 1, tzinfo=UTC)
    for i in range(3):
        assert store.adopt(
            _vaulted(store, title=f"held-{i}"), caps=caps, at=at, ttl_days=30
        ).ok
    fourth = _vaulted(store, title="fourth")

    assert store.adopt(fourth, caps=caps, at=at, ttl_days=30).refused

    long_after = at + timedelta(days=200)
    assert store.granted_symbols(long_after) == {}
    assert store.has_room(Vault.ADOPTED, now=long_after, caps=caps) is True
    assert store.adopt(fourth, caps=caps, at=long_after).ok


def test_a_live_adoption_still_fills_its_slot(store):
    """The other half, so the test is about liveness rather than about the cap
    having been removed."""
    caps = VaultCaps(adopted=3)
    at = datetime(2026, 6, 1, tzinfo=UTC)
    for i in range(3):
        store.adopt(_vaulted(store, title=f"held-{i}"), caps=caps, at=at, ttl_days=90)
    fourth = _vaulted(store, title="fourth")

    result = store.adopt(fourth, caps=caps, at=at + timedelta(days=1))

    assert result.refused
    assert MoveRefusal.FULL in result.refusals
    assert store.has_room(Vault.ADOPTED, now=at + timedelta(days=1), caps=caps) is False


def test_the_other_shelves_still_count_rows_rather_than_grants(store):
    """Only ADOPTED has grants behind it. A vault cap counts what is on it."""
    caps = VaultCaps(vault=2)
    for i in range(2):
        _vaulted(store, title=f"offer-{i}")
    third = store.save(Dream(title="third", seed="s"))

    result = store.move(third, Vault.VAULT, by=DREAMER, caps=caps)

    assert result.refused
    assert MoveRefusal.FULL in result.refusals


# ------------------------------------------------- the shelf answers by WAITING


def test_a_shelf_is_ordered_by_when_a_dream_ARRIVED_not_when_it_changed(store):
    """`confer` reverses this list to answer the longest-waiting offer first.

    Ordered by `updated_at` that reversal answered the least recently EDITED
    first, which is a different dream: one shelved 150 days ago and touched this
    morning sorted as the newest thing here and went to the back of the queue —
    the starvation the reversal exists to prevent, with the code that prevents
    it still in place.
    """
    old = store.save(Dream(title="waiting since March", seed="s"))
    store.move(old, Vault.VAULT, by=DREAMER, at=datetime(2026, 3, 1, tzinfo=UTC))
    new = store.save(Dream(title="offered yesterday", seed="s"))
    store.move(new, Vault.VAULT, by=DREAMER, at=datetime(2026, 5, 31, tzinfo=UTC))

    # The old one is edited today, which moves `updated_at` and must not move
    # its place in the queue.
    touched = store.get(old)
    assert touched is not None
    touched.updated_at = datetime(2026, 6, 1, tzinfo=UTC)
    store.save(touched)

    shelf = store.in_vault(Vault.VAULT)

    assert [d.title for d in shelf] == ["offered yesterday", "waiting since March"]
    # Which is what `confer` reverses to get longest-waiting first.
    assert next(d.title for d in reversed(shelf)) == "waiting since March"


def test_a_dream_stamp_is_normalised_to_UTC_on_the_way_into_the_row(store):
    """**`in_vault` orders on TEXT, so every offset in the column must match.**

    `_iso_utc` exists so that "text ordering is instant ordering", and
    `adoptions`, `dream_messages` and `_apply_move` all went through it while
    `save` called `.isoformat()` on whatever the object carried. That is the
    string-comparison bug this repository has already paid for twice, sitting
    in the one table nobody had checked.

    Pacific/Auckland is not a hypothetical offset here: it is what the dream
    timer's schedule is written in, by design.
    """
    nz = timezone(timedelta(hours=13))
    store.save(
        Dream(
            title="stamped in Auckland",
            seed="s",
            vault=Vault.VAULT,
            created_at=datetime(2026, 8, 12, 9, 0, tzinfo=nz),
            updated_at=datetime(2026, 8, 12, 9, 0, tzinfo=nz),
            vault_entered_at=datetime(2026, 8, 12, 9, 0, tzinfo=nz),
        )
    )

    with sqlite3.connect(store.path) as conn:
        row = conn.execute(
            "SELECT created_at, updated_at, vault_entered_at FROM dreams"
        ).fetchone()

    assert all(str(stamp).endswith("+00:00") for stamp in row)


def test_a_shelf_ordered_on_text_still_answers_the_longest_wait_first(store):
    """The consequence, measured rather than reasoned about.

    A dream shelved at 2026-08-11 20:00Z but stamped `+13:00` sorted AFTER one
    shelved ten hours later, so `confer.run`'s reversal answered it LAST — the
    exact starvation the reversal exists to prevent.
    """
    nz = timezone(timedelta(hours=13))
    store.save(
        Dream(title="waiting longest", seed="s", vault=Vault.VAULT,
              vault_entered_at=datetime(2026, 8, 12, 9, 0, tzinfo=nz))
    )
    store.save(
        Dream(title="shelved later", seed="s", vault=Vault.VAULT,
              vault_entered_at=datetime(2026, 8, 12, 6, 0, tzinfo=UTC))
    )

    shelf = store.in_vault(Vault.VAULT)

    assert next(d.title for d in reversed(shelf)) == "waiting longest"


# ------------------------------------------------------ promotion off the bench
#
# The gap that made the whole feature inert. `is_offerable` was defined and
# never called, nothing moved a dream off the workbench, and `confer` reads only
# `Vault.VAULT` — so the vault was permanently empty and the conference
# permanently a no-op. Every test below is about the RULE, because the rule is
# the decision and the plumbing is not.


def _keeper(**kw) -> Dream:
    """A dream that has reached a keep verdict, with whatever conditions given."""
    base = {"title": "Smelters and power", "seed": "s", "verdict": DreamVerdict.KEEP}
    base.update(kw)
    return Dream(**base)  # type: ignore[arg-type]


def _checkable(value: float = 100.0, *, fulfilled: bool = False) -> DreamCondition:
    return DreamCondition(
        text="Alcoa closes above 100",
        symbol="AA",
        field=TriggerField.CLOSE,
        op=TriggerOp.ABOVE,
        value=value,
        fulfilled=fulfilled,
    )


def test_a_keep_with_a_checkable_condition_becomes_a_prophecy():
    """The first half of the promotion rule.

    A conclusion plus a pre-registered number is what the prophecy shelf is
    for — a claim that can later be graded rather than an opinion.
    """
    promotion = promotion_for(_keeper(conditions=[_checkable()]))

    assert promotion.to is Vault.PROPHECY
    assert promotion.moves


def test_a_keep_with_every_condition_met_goes_straight_to_the_vault():
    """The second half: met conditions put a dream in front of the trading agent."""
    promotion = promotion_for(_keeper(conditions=[_checkable(fulfilled=True)]))

    assert promotion.to is Vault.VAULT


def test_a_keep_with_no_conditions_at_all_stays_on_the_workbench():
    """**The one that must never regress.**

    `all([])` is True, so an empty condition list read as "all conditions met"
    would put every dream that reached a conclusion straight into the vault
    claiming to have been proven. `has_conditions` and `all_conditions_met` are
    separate questions precisely so an ABSENCE of evidence cannot look like
    satisfied evidence — the same rule as an empty chain reading UNVERIFIED.
    """
    bare = _keeper()

    assert bare.has_conditions is False
    assert bare.all_conditions_met is False
    assert promotion_for(bare).to is None
    # The refusal names BOTH routes out, because a dreamer told only "no
    # threshold" reaches for a number, which is the failure that made this
    # shelf unreachable in the first place.
    reason = promotion_for(bare).reason
    assert "nothing pre-registered" in reason
    assert "threshold" in reason and "observation" in reason


def test_a_keep_whose_only_condition_is_prose_stays_on_the_workbench():
    """A conclusion nobody can grade is an opinion.

    The condition is legal and is kept — refusing it would push the dreamer
    towards inventing a threshold — but it does not buy a place on the prophecy
    shelf, which exists to hold claims that can be checked.
    """
    prose = _keeper(conditions=[DreamCondition(text="the spread normalises")])

    assert prose.has_conditions is True
    assert promotion_for(prose).to is None


@pytest.mark.parametrize(
    "verdict", [None, DreamVerdict.PARK, DreamVerdict.DROP]
)
def test_only_a_keep_is_ever_promoted(verdict):
    """A dream still running, parked or broken reaches nobody."""
    dream = Dream(
        title="t", seed="s", verdict=verdict, conditions=[_checkable(fulfilled=True)]
    )

    assert promotion_for(dream).to is None


@pytest.mark.parametrize("shelf", [Vault.VAULT, Vault.ADOPTED, Vault.ARCHIVE])
def test_promotion_never_moves_a_dream_off_the_other_shelves(shelf):
    """The vault is where promotion ends, adoption is the trading agent's, and
    the archive is deliberate. An automatic rule that pulled a dream back out of
    the archive would resurrect ideas somebody put down on purpose."""
    dream = _keeper(vault=shelf, conditions=[_checkable(fulfilled=True)])

    assert promotion_for(dream).to is None


def test_a_prophecy_with_unmet_conditions_is_not_moved_again():
    """It is already on the right shelf.

    Returning PROPHECY here would be refused as ALREADY_THERE, and a caller that
    retried would reset `vault_entered_at` on every pass — so nothing would ever
    age out of the shelf whose TTL is a year.
    """
    prophecy = _keeper(vault=Vault.PROPHECY, conditions=[_checkable()])

    assert promotion_for(prophecy).to is None
    assert "Already a prophecy" in promotion_for(prophecy).reason


def test_a_prophecy_whose_conditions_have_fired_is_promoted_to_the_vault():
    prophecy = _keeper(vault=Vault.PROPHECY, conditions=[_checkable(fulfilled=True)])

    assert promotion_for(prophecy).to is Vault.VAULT


def test_one_unmet_condition_holds_the_whole_dream_back():
    """Every condition, not a majority."""
    dream = _keeper(
        conditions=[
            _checkable(fulfilled=True),
            DreamCondition(text="the harvest report lands", symbol="AA",
                           field=TriggerField.CLOSE, op=TriggerOp.BELOW, value=50.0),
        ]
    )

    assert promotion_for(dream).to is Vault.PROPHECY


# ------------------------------------------- the weakest hop has to be covered
#
# The operator's correction: *"the weakest hop you speak of here is what that
# prophecy vault was for, dreams that were parked awaiting the prophecy to be
# fulfilled."* Before this, a keep plus any checkable condition promoted — so a
# dream could reach the prophecy shelf on a threshold about a link nobody
# doubted, which grades cleanly, promotes cleanly and settles nothing. That
# makes the shelf a filing decision rather than a claim awaiting settlement.
#
# Every test below is about the RULE. The first one is the one that must never
# regress, because it is the only one that proves the gate REJECTS.


def _chain() -> list[Hop]:
    """Two sourced links and one assumption. The assumption is hop 3."""
    return [
        Hop("Smelters buy power on long interconnect contracts.", True, "a filing"),
        Hop("Data centres bid for the same interconnect.", True, "a grid notice"),
        Hop("Alcoa is the listed instrument that exposure reaches."),
    ]


def _pinned(*hops: int, **kw) -> DreamCondition:
    """A checkable condition claiming to settle the given one-based hops."""
    return replace(_checkable(**kw), settles_hops=tuple(hops))


def test_a_keep_whose_conditions_miss_the_weakest_hop_stays_on_the_workbench():
    """**The rejection. Everything else here is about the edges of it.**

    A perfectly good pre-registered number, on a link that was already sourced.
    Under the old rule this promoted, and the prophecy shelf then held a dream
    parked awaiting nothing in particular.
    """
    dream = _keeper(
        chain=_chain(),
        weakest_hop_index=3,
        weakest_hop="that the exposure actually reaches a listed instrument",
        conditions=[_pinned(1)],
    )

    promotion = promotion_for(dream)

    assert promotion.to is None
    assert promotion.moves is False


def test_the_refusal_names_the_HOP_and_what_would_fix_it():
    """A rejection message carries its consequence, like every other one here.

    "not promotable" would be a code. The dreamer reads this sentence on its
    next run and has to be able to tell an unpinned weakest hop apart from a
    missing verdict, a missing condition and a full shelf — and to know that
    the fix is a hop number rather than a better sentence.
    """
    dream = _keeper(
        chain=_chain(), weakest_hop_index=3, weakest_hop="the bridge to a ticker",
        conditions=[_pinned(1)],
    )

    reason = promotion_for(dream).reason

    assert "weakest link has nothing pinned to it" in reason
    assert "Hop 3" in reason
    assert "Alcoa is the listed instrument" in reason
    assert "settles_hops" in reason


def test_a_condition_pinned_to_the_weakest_hop_is_what_buys_the_shelf():
    """The other side of the same rule, or it would only ever refuse."""
    dream = _keeper(
        chain=_chain(), weakest_hop_index=3, weakest_hop="the bridge to a ticker",
        conditions=[_pinned(3)],
    )

    assert promotion_for(dream).to is Vault.PROPHECY


def test_one_condition_may_settle_several_hops():
    """A single public fact often settles more than one link.

    Forcing a choice would push the dreamer into writing the same threshold
    twice, and two rows for one claim is two things that can disagree.
    """
    dream = _keeper(
        chain=_chain(), weakest_hop_index=3, weakest_hop="the bridge",
        conditions=[_pinned(1, 3)],
    )

    assert promotion_for(dream).to is Vault.PROPHECY


def test_a_prose_only_condition_on_the_weakest_hop_is_not_enough():
    """A hop number attached to an opinion is still an opinion.

    `weakest_hop_is_pinned` asks for `is_checkable`, matching the clause above
    it: the shelf holds claims that can be GRADED, and "the spread normalises,
    hop 3" cannot be.
    """
    dream = _keeper(
        chain=_chain(), weakest_hop_index=3, weakest_hop="the bridge",
        conditions=[
            _pinned(1),
            DreamCondition(text="the spread normalises", settles_hops=(3,)),
        ],
    )

    assert promotion_for(dream).to is None


def test_a_weakest_hop_that_matches_no_hop_refuses_rather_than_guessing():
    """Prose is a paraphrase in practice, so an unnumbered sentence is unusable.

    Fail closed: if which link is weakest cannot be established, nothing can be
    shown to settle it. The refusal names the fix, which is a number.
    """
    dream = _keeper(
        chain=_chain(),
        weakest_hop="that the smelter cannot re-contract power elsewhere",
        conditions=[_pinned(3)],
    )

    promotion = promotion_for(dream)

    assert promotion.to is None
    assert "matches no hop" in promotion.reason
    assert "weakest_hop_index" in promotion.reason


def test_a_chain_whose_weakest_hop_was_never_named_refuses_and_says_so():
    """Distinct from the message above, because it is fixed differently.

    Nothing to match and no number given: the answer is to name the link, not
    to renumber a sentence that does not exist.
    """
    dream = _keeper(chain=_chain(), conditions=[_pinned(3)])

    promotion = promotion_for(dream)

    assert promotion.to is None
    assert "nobody has said which link it is" in promotion.reason


def test_an_index_outside_the_chain_names_no_hop_and_is_not_clamped():
    """A model answering 9 on a three-hop chain has named no hop.

    Clamping to the last one would be this file choosing which link is weakest,
    which is exactly the judgement it refuses to make anywhere else.
    """
    dream = _keeper(chain=_chain(), weakest_hop_index=9, conditions=[_pinned(3)])

    assert dream.resolved_weakest_hop is None
    assert promotion_for(dream).to is None


def test_the_prose_resolves_the_hop_when_it_quotes_the_claim_exactly():
    """The fallback, and it is deliberately exact rather than fuzzy.

    `_claim_key` normalises whitespace and case and nothing else, which is the
    same rule fusion uses to decide two hops are the same claim. A similarity
    score would decide two differently-worded claims are one, which is precisely
    the judgement a reader has to be able to check.
    """
    dream = _keeper(
        chain=_chain(),
        weakest_hop="  alcoa IS the listed instrument that exposure reaches. ",
        conditions=[_pinned(3)],
    )

    assert dream.resolved_weakest_hop == 3
    assert promotion_for(dream).to is Vault.PROPHECY


def test_a_chain_with_every_hop_checked_has_no_weakest_link_to_pin():
    """The first honest edge case: nothing is awaiting settlement.

    A prophecy is a dream parked awaiting the link that could kill it. When
    every link is sourced there is no such link, and demanding a threshold on
    one would be asking for a number against a hop already nailed down. The
    badge reads SOURCED on every surface, which is where that claim is checked.
    """
    sourced = _keeper(
        chain=[Hop("a", True, "s"), Hop("b", True, "s")],
        conditions=[_checkable()],
    )

    assert sourced.awaits_settlement is False
    assert sourced.verification is Verification.SOURCED
    assert promotion_for(sourced).to is Vault.PROPHECY


def test_a_dream_with_no_chain_is_not_held_back_by_a_rule_about_chains():
    """The second edge case, and it reaches the same answer for another reason.

    This clause pins a chain's weakest link; it does not require a chain to
    exist. Refusing a chainless keep is a separate rule about whether a dream
    needs an argument at all, and smuggling it in here would be adding a limit
    nobody agreed to.
    """
    bare = _keeper(conditions=[_checkable()])

    assert bare.chain == []
    assert bare.resolved_weakest_hop is None
    assert promotion_for(bare).to is Vault.PROPHECY


def test_a_fusion_is_refused_for_its_VERDICT_and_never_for_its_weakest_hop():
    """The third edge case, and it needs no exemption — only the right order.

    A fresh fusion has no verdict and no `weakest_hop` by design: nobody has
    attacked it yet. The verdict clause runs first, so the fusion is held back
    for the honest reason. Reverse the two and every fusion would be reported
    as having an unpinned weak link, which reads like a defect in the fusion
    rather than a chain nobody has examined.
    """
    fresh = Dream(
        title="fused", seed="s", chain=_chain(), conditions=[_pinned(1)]
    )

    reason = promotion_for(fresh).reason

    assert promotion_for(fresh).to is None
    assert "no verdict yet" in reason
    assert "weakest" not in reason


def test_a_fusion_that_has_been_attacked_faces_the_ordinary_rule():
    """And it is not exempt once it has one. A keep means somebody looked."""
    attacked = Dream(
        title="fused", seed="s", parents=[1, 2], verdict=DreamVerdict.KEEP,
        chain=_chain(), weakest_hop_index=3, conditions=[_pinned(1)],
    )

    assert attacked.is_fusion is True
    assert promotion_for(attacked).to is None
    assert "Hop 3" in promotion_for(attacked).reason


def test_the_direct_route_to_the_vault_cannot_slip_past_the_rule():
    """The clause sits AHEAD of the all-conditions-met branch on purpose.

    Otherwise a workbench dream whose conditions all fired would go straight to
    the shelf the trading agent can see, having settled nothing the chain rests
    on — the same defect, arriving one shelf further along.
    """
    dream = _keeper(
        chain=_chain(), weakest_hop_index=3,
        conditions=[_pinned(1, fulfilled=True)],
    )

    assert dream.all_conditions_met is True
    assert promotion_for(dream).to is None


def test_a_prophecy_already_on_the_shelf_is_not_stranded_by_the_new_rule():
    """The rule gates leaving the WORKBENCH, not arriving at the vault.

    A dream already on the prophecy shelf is by definition parked awaiting its
    conditions. Re-testing the pin when they fire would strand every prophecy
    promoted before this clause existed — turning a tightening into a silent
    deletion of the shelf's contents.
    """
    legacy = _keeper(
        vault=Vault.PROPHECY, chain=_chain(),
        weakest_hop="an unnumbered sentence from before the pin existed",
        conditions=[_checkable(fulfilled=True)],
    )

    assert legacy.resolved_weakest_hop is None
    assert promotion_for(legacy).to is Vault.VAULT


def test_a_pin_at_hop_zero_is_dropped_rather_than_read_as_a_hop():
    """Hop 0 is not a hop, and a pin pointing nowhere would report as covered."""
    condition = DreamCondition.from_row({"text": "t", "settles_hops": [0, -2, "x", 2, 2]})

    assert condition.settles_hops == (2,)
    assert condition.settles(None) is False


def test_repinning_a_condition_keeps_the_grade_it_already_earned():
    """`settles_hops` is out of `DreamCondition.key`, and this is why.

    The number is the claim; which hop it bears on is bookkeeping about the
    chain. Including the pin in the key would reset a fulfilled condition every
    time a step renumbered the chain, which is the failure `carry_forward_grading`
    exists to prevent. The NEW pin still travels.
    """
    was = [_pinned(1, fulfilled=True)]

    carried = carry_forward_grading(was, [_pinned(3)])

    assert carried[0].fulfilled is True
    assert carried[0].settles_hops == (3,)


# ------------------------------------------------------------- the store method


def test_promote_walks_a_dream_from_the_bench_to_the_vault(store):
    """The whole ladder, through the store rather than through the pure rule."""
    dream_id = store.save(_keeper(conditions=[_checkable()]))

    first = store.promote(dream_id)
    assert first.ok and first.moved_to is Vault.PROPHECY

    # Nothing moves while the condition is unmet, and the refusal is ordinary.
    again = store.promote(dream_id)
    assert not again.ok
    assert MoveRefusal.NOT_PROMOTABLE in again.refusals

    fired = store.get(dream_id)
    assert fired is not None
    fired.conditions = [_checkable(fulfilled=True)]
    store.save(fired)

    second = store.promote(dream_id)
    assert second.ok and second.moved_to is Vault.VAULT
    assert [d.id for d in store.in_vault(Vault.VAULT)] == [dream_id]


def test_promote_is_idempotent_and_does_not_reset_the_expiry_clock(store):
    """Safe to call on every dream on every run, which is how it is called."""
    dream_id = store.save(_keeper(conditions=[_checkable()]))
    store.promote(dream_id, at=datetime(2026, 5, 1, tzinfo=UTC))
    entered = store.get(dream_id).vault_entered_at

    store.promote(dream_id, at=datetime(2026, 6, 1, tzinfo=UTC))

    assert store.get(dream_id).vault_entered_at == entered


def test_promote_respects_the_shelf_cap(store):
    """A full shelf refuses and says so; it does not raise and does not overflow."""
    caps = VaultCaps(prophecy=1)
    first = store.save(_keeper(conditions=[_checkable()]))
    second = store.save(_keeper(conditions=[_checkable()]))

    assert store.promote(first, caps=caps).ok
    refused = store.promote(second, caps=caps)

    assert not refused.ok
    assert MoveRefusal.FULL in refused.refusals


def test_promote_refuses_an_unpinned_weakest_hop_and_carries_the_sentence(store):
    """The rule through the store, which is what a caller actually sees.

    `MoveResult.detail` is where `promotion_for`'s sentence lands, so the
    dreamer, `promote_dreams` and the Dreaming page all get the consequence
    rather than the code `NOT_PROMOTABLE`.
    """
    dream_id = store.save(
        _keeper(chain=_chain(), weakest_hop_index=3, conditions=[_pinned(1)])
    )

    result = store.promote(dream_id)

    assert not result.ok
    assert MoveRefusal.NOT_PROMOTABLE in result.refusals
    assert "weakest link has nothing pinned to it" in result.detail
    assert store.get(dream_id).vault is Vault.WORKBENCH

    # And it is not a dead end: pinning the condition to the weakest hop is all
    # it takes, on the next pass, with nothing else changed.
    fixed = store.get(dream_id)
    fixed.conditions = [_pinned(3)]
    store.save(fixed)

    assert store.promote(dream_id).moved_to is Vault.PROPHECY


def test_promote_reports_a_missing_dream_rather_than_raising(store):
    result = store.promote(9999)

    assert not result.ok
    assert MoveRefusal.NOT_FOUND in result.refusals


# ------------------------------------------------------- grading the conditions


def _cycle(at: datetime, close: float, symbol: str = "AA") -> CycleReadings:
    return CycleReadings(at=at, readings={symbol: IndicatorSnapshot(close=close)})


def test_a_condition_fires_on_the_first_reading_that_meets_it():
    """The first, not the latest.

    Stamped with the most recent moment it held rather than the moment it became
    true, a prophecy would report as having fired days after it did.
    """
    dream = _keeper(conditions=[_checkable(value=100.0)])
    later = [
        _cycle(datetime(2026, 6, 1, tzinfo=UTC), 95.0),
        _cycle(datetime(2026, 6, 2, tzinfo=UTC), 101.0),
        _cycle(datetime(2026, 6, 3, tzinfo=UTC), 110.0),
    ]

    grading = grade_conditions(dream, later)

    assert grading.changed
    assert grading.conditions[0].fulfilled
    assert grading.conditions[0].fulfilled_at == datetime(2026, 6, 2, tzinfo=UTC)
    assert "101" in grading.conditions[0].note


def test_a_condition_no_reading_reaches_stays_unmet():
    dream = _keeper(conditions=[_checkable(value=100.0)])

    grading = grade_conditions(dream, [_cycle(datetime(2026, 6, 1, tzinfo=UTC), 95.0)])

    assert not grading.changed
    assert grading.conditions[0].fulfilled is False


def test_a_missing_figure_is_not_a_failed_condition():
    """`holds` answers None for an unavailable reading, and None is not False.

    A symbol whose bars could not supply the field has not failed a test against
    it — the `IndicatorSnapshot` keeps `None` rather than a zero for exactly this
    reason, and a zero close would fire every `below` condition ever written.
    """
    dream = _keeper(conditions=[_checkable(value=100.0)])
    blank = CycleReadings(
        at=datetime(2026, 6, 2, tzinfo=UTC), readings={"AA": IndicatorSnapshot()}
    )

    grading = grade_conditions(dream, [blank])

    assert not grading.changed
    assert grading.cycles_checked == 1


def test_a_condition_with_no_symbol_is_counted_as_ungradeable_not_as_unmet():
    """A triple with no subject is a comparison with nothing to look up.

    Counted rather than dropped, the same as `watches_with_prose_only`: a
    prophecy nobody can grade is the interesting failure, and an invisible one
    looks exactly like patience.
    """
    dream = _keeper(
        conditions=[
            DreamCondition(
                text="close above 100",
                field=TriggerField.CLOSE,
                op=TriggerOp.ABOVE,
                value=100.0,
            )
        ]
    )

    grading = grade_conditions(dream, [_cycle(datetime(2026, 6, 2, tzinfo=UTC), 500.0)])

    assert grading.ungradeable == 1
    assert not grading.changed


def test_grading_never_unfires_a_condition_that_already_held():
    """The claim was that the figure would REACH a level, not stay there."""
    dream = _keeper(conditions=[_checkable(value=100.0, fulfilled=True)])

    grading = grade_conditions(dream, [_cycle(datetime(2026, 6, 2, tzinfo=UTC), 10.0)])

    assert grading.conditions[0].fulfilled is True
    assert not grading.changed


def test_no_recorded_cycles_is_not_evidence_that_nothing_fired():
    """`can_grade_anything`, the same distinction as `has_cycles`."""
    grading = grade_conditions(_keeper(conditions=[_checkable()]), [])

    assert grading.can_grade_anything is False
    assert grade_conditions(
        _keeper(conditions=[_checkable()]),
        [_cycle(datetime(2026, 6, 2, tzinfo=UTC), 1.0)],
    ).can_grade_anything is True


def test_the_store_writes_back_what_fired_without_touching_the_shelf_clock(store):
    """A condition firing is not the dream entering its shelf again."""
    dream_id = store.save(_keeper(vault=Vault.PROPHECY, conditions=[_checkable()]))
    entered = store.get(dream_id).vault_entered_at

    grading = store.grade(
        dream_id, [_cycle(datetime(2026, 6, 2, tzinfo=UTC), 150.0)]
    )

    assert grading.changed
    reloaded = store.get(dream_id)
    assert reloaded is not None
    assert reloaded.conditions[0].fulfilled is True
    assert reloaded.all_conditions_met is True
    assert reloaded.vault_entered_at == entered


# --------------------------------------------- keeping a grade across a restate


def test_a_restated_condition_keeps_the_grade_it_already_earned():
    """Wiping it would make the vault unreachable.

    A later dream step may return the whole condition list again. Taken as
    written, every `fulfilled` flag would reset, `all_conditions_met` could never
    be true two steps running, and the dream would be re-checked forever against
    readings that had already fired it.
    """
    was = [_checkable(value=100.0, fulfilled=True)]
    restated = [
        DreamCondition(
            text="Alcoa finally clears the hundred handle",  # reworded
            symbol="AA",
            field=TriggerField.CLOSE,
            op=TriggerOp.ABOVE,
            value=100.0,  # same claim
        )
    ]

    carried = carry_forward_grading(was, restated)

    assert carried[0].fulfilled is True
    assert carried[0].text == "Alcoa finally clears the hundred handle"


def test_moving_the_threshold_starts_a_new_claim_ungraded():
    """Inheriting the old verdict would be back-dating a prediction.

    A number changed after the fact is exactly what pre-registering one exists
    to prevent, so it is a NEW condition and starts unfulfilled.
    """
    was = [_checkable(value=100.0, fulfilled=True)]
    moved = [_checkable(value=80.0)]

    assert carry_forward_grading(was, moved)[0].fulfilled is False


def test_a_condition_symbol_survives_the_round_trip(store):
    dream_id = store.save(_keeper(conditions=[_checkable()]))

    reloaded = store.get(dream_id)

    assert reloaded is not None
    assert reloaded.conditions[0].symbol == "AA"
    assert reloaded.conditions[0].is_gradeable is True


def test_a_condition_written_before_symbols_existed_reads_as_ungradeable():
    """The store is JSON in a TEXT column, so this needed no migration — but a
    row from before the field shipped genuinely does not say whose figure it is,
    and inventing one would be the confident wrong value this repo refuses."""
    old = DreamCondition.from_row(
        {"text": "close above 100", "field": "close", "op": "above", "value": 100.0}
    )

    assert old.symbol == ""
    assert old.is_checkable is True  # still promotes to the prophecy shelf
    assert old.is_gradeable is False  # but nothing can settle it


# ============================================ the condition a PERSON settles
#
# An honest dreamer could never leave the workbench. `promotion_for` wanted a
# checkable condition pinned to the weakest hop, every `TriggerField` is a price
# or a technical figure, and the weakest hop of a second-order supply-chain
# chain is never a price fact — so the only route to the shelf was to invent a
# price threshold for a non-price claim, which the prompt forbids. Honesty won
# every time and the vault stayed empty.
#
# These tests are organised around what must stay true now that a second shape
# of pre-registration exists. Every one of the promotion tests below proves a
# REFUSAL, because the risk in widening a rule is that it stops refusing
# anything.

DUE = datetime(2027, 3, 1, 23, 59, tzinfo=UTC)
BEFORE = datetime(2027, 1, 5, tzinfo=UTC)
AFTER = datetime(2027, 6, 1, tzinfo=UTC)


def _observation(**kw) -> DreamCondition:
    """A claim the world can settle and this box cannot read. No number in it."""
    base = {
        "text": (
            "Century Aluminum brings the idled Mt. Holly potline back, which is "
            "the link the whole chain rests on and the one nothing here can read."
        ),
        "subject": "Century Aluminum's quarterly production release for Mt. Holly",
        "observable": (
            "reports the idled potline restarted, or under restart, rather than "
            "still curtailed"
        ),
        "observe_by": DUE,
        "settles_hops": (3,),
    }
    base.update(kw)
    return DreamCondition(**base)  # type: ignore[arg-type]


def _smelter() -> Dream:
    """A real second-order supply-chain dream, of the shape 0b was about.

    Four hops, two sourced and two assumptions, and the weakest is hop 3 — a
    claim about an industrial decision that no `TriggerField` measures. The
    listed instrument arrives through a bridge hop, as the prompt requires.
    """
    return Dream(
        title="The idled potline and the domestic premium",
        seed=(
            "Half a smelter has been switched off for a decade, and everyone "
            "prices the half that is running."
        ),
        verdict=DreamVerdict.KEEP,
        chain=[
            Hop(
                "Mt. Holly has run at part capacity since 2015 because the site's "
                "power contract does not support the idled potline.",
                True,
                "Century Aluminum's own filings",
            ),
            Hop(
                "Restarting it needs an industrial power tariff on different "
                "terms, and South Carolina files those publicly.",
                True,
                "the state utility commission docket",
            ),
            Hop(
                "On such terms the restart is economic, so the idled potline "
                "comes back.",
            ),
            Hop(
                "Alcoa is the largest US-listed primary producer, so a domestic "
                "supply change of that size reaches its realised price.",
            ),
        ],
        weakest_hop="that the restart actually happens once the tariff exists",
        weakest_hop_index=3,
        symbols=["AA"],
        asset_class_key="us_equity",
        conditions=[_observation()],
    )


def test_a_supply_chain_dream_reaches_the_prophecy_shelf_with_no_invented_figure():
    """**The one this section exists for.**

    Six real dream steps produced zero checkable conditions, twice, on two
    different transports, and the model said why itself: no field available to
    it measures the claim, and the listed company's share price prices the
    market's guess about it rather than testing it. So the shelf was reachable
    only by inventing a threshold, and an honest dreamer stayed on the bench.

    This dream reaches the shelf carrying NO number at all — not a fabricated
    one and not a real one. What it carries instead is a claim about the world,
    the thing to look at, and a date the answer should exist by.
    """
    dream = _smelter()

    # Nothing numeric was pre-registered anywhere on it.
    assert all(c.value is None for c in dream.conditions)
    assert not any(c.is_checkable for c in dream.conditions)

    # And it promotes anyway, on the strength of an observation pinned to the
    # hop that could kill the chain.
    assert dream.resolved_weakest_hop == 3
    assert dream.weakest_hop_is_pinned is True
    assert promotion_for(dream).to is Vault.PROPHECY


def test_the_shelf_is_still_refused_to_a_conclusion_with_nothing_registered():
    """Widening the rule must not empty it. A keep alone still goes nowhere."""
    promotion = promotion_for(_keeper(chain=_smelter().chain))

    assert promotion.to is None
    assert "nothing pre-registered" in promotion.reason


def test_an_observation_with_no_date_is_refused():
    """"Someday" is not a pre-registration.

    A question that can never come due never reaches anybody's list, so nobody
    is ever asked and the dream waits for ever while every surface reports
    patience. The date is what makes the claim answerable, exactly as the number
    is what makes a threshold checkable.
    """
    dream = _smelter()
    dream.conditions = [_observation(observe_by=None)]

    assert dream.conditions[0].is_observable is False
    assert dream.conditions[0].is_pre_registered is False
    assert promotion_for(dream).to is None


def test_an_observation_with_no_subject_is_refused():
    """A claim with nothing to look at is prose with extra fields.

    "The market will confirm it" names no findable thing, so no two people
    would answer it the same way — which is the whole difference between an
    observation and an opinion.
    """
    dream = _smelter()
    dream.conditions = [_observation(subject="   ")]

    assert dream.conditions[0].is_observable is False
    assert promotion_for(dream).to is None


def test_an_observation_with_nothing_it_must_show_is_refused():
    """A thing to look at with no question attached settles nothing either."""
    dream = _smelter()
    dream.conditions = [_observation(observable="")]

    assert dream.conditions[0].is_observable is False
    assert promotion_for(dream).to is None


def test_an_observation_on_a_link_nobody_doubted_does_not_buy_the_shelf():
    """The weakest-hop clause survives the widening.

    Pinning the new shape to an easy hop would turn the shelf back into a filing
    decision, which is exactly what the pin was added to stop. The refusal names
    the hop and names both ways to cover it.
    """
    dream = _smelter()
    dream.conditions = [_observation(settles_hops=(1,))]

    promotion = promotion_for(dream)

    assert dream.weakest_hop_is_pinned is False
    assert promotion.to is None
    assert "Hop 3" in promotion.reason
    assert "observation" in promotion.reason


def test_a_bare_sentence_pinned_to_the_weakest_hop_is_still_not_enough():
    """Prose does not become a pre-registration by being pinned.

    An observation is three specific things; a sentence naming none of them is
    the `waiting_for`-with-no-trigger failure, and it must not have been made
    promotable by accident.
    """
    dream = _smelter()
    dream.conditions = [DreamCondition(text="the restart happens", settles_hops=(3,))]

    assert dream.weakest_hop_is_pinned is False
    assert promotion_for(dream).to is None


# ------------------------------------- the four states, and none of them merge


def test_an_unanswered_observation_is_awaiting_and_never_fulfilled():
    """The good outcome must not be what an absence of evidence looks like."""
    condition = _observation()

    assert condition.state(BEFORE) is ConditionState.AWAITING
    assert condition.fulfilled is False
    assert condition.is_answered is False


def test_an_observation_past_its_date_is_overdue_and_not_a_failure():
    """OVERDUE is its own state, and it is a fact about the LOOKING.

    What is late is nobody having gone to look — the world has not answered
    "no". Reporting it as ruled out would turn an unopened dashboard into a
    refuted prophecy, which is the missing-versus-zero rule in a new place.
    """
    condition = _observation()

    assert condition.state(AFTER) is ConditionState.OVERDUE
    assert condition.state(AFTER) is not ConditionState.RULED_OUT
    assert condition.fulfilled is False
    assert condition.ruled_out is False


def test_a_condition_with_neither_shape_is_unsettleable_not_awaiting():
    """Nobody is coming. Distinct from a question waiting on somebody."""
    assert (
        DreamCondition(text="the spread normalises").state(BEFORE)
        is ConditionState.UNSETTLEABLE
    )


def test_grading_never_settles_an_observation_in_either_direction():
    """**Code must not answer a question code cannot answer.**

    Marking it met would manufacture a trading permission out of silence — the
    vault is what an adoption is taken from, and an adoption is a live symbol
    grant. Marking it ruled out would refute a prophecy nobody had looked at.
    So `grade_conditions` leaves it alone however many cycles arrive, and counts
    it as awaiting a person rather than as ungradeable.
    """
    dream = _smelter()
    dream.id = 4
    readings = [
        CycleReadings(
            at=AFTER,
            readings={"AA": IndicatorSnapshot(close=9999.0), "": IndicatorSnapshot()},
        )
    ]

    grading = grade_conditions(dream, readings)

    assert grading.newly_fulfilled == ()
    assert grading.conditions[0].fulfilled is False
    assert grading.conditions[0].ruled_out is False
    assert grading.awaiting_operator == 1
    # And NOT counted as ungradeable: "no reading can settle this" and
    # "somebody has to look at this" are opposite findings.
    assert grading.ungradeable == 0


def test_a_prose_only_condition_is_still_counted_as_ungradeable():
    """The old counter keeps its old meaning. Nothing can settle this one."""
    dream = _keeper(conditions=[DreamCondition(text="the spread normalises")])
    dream.id = 5

    grading = grade_conditions(dream, [CycleReadings(at=AFTER, readings={})])

    assert grading.ungradeable == 1
    assert grading.awaiting_operator == 0


def test_an_unanswered_observation_never_reaches_the_vault_by_waiting():
    """Time alone must not promote anything. The vault is where a dream becomes
    adoptable, and an adoption is a live permission to trade a symbol that is in
    no allowlist — so the unattended route to one still runs entirely on figures
    the loop recorded, and this route runs through a person."""
    dream = _smelter()
    dream.vault = Vault.PROPHECY

    assert dream.all_conditions_met is False
    assert promotion_for(dream).to is None


# --------------------------------------------------- the operator's own answer


def _shelved(store, dream: Dream | None = None) -> int:
    """One dream on the prophecy shelf, through the real promotion rule."""
    dream_id = store.save(dream or _smelter())
    assert store.promote(dream_id).moved_to is Vault.PROPHECY
    return int(dream_id)


def test_the_operator_settles_an_observation_and_the_dream_reaches_the_vault(store):
    """The whole ladder, on a dream carrying no number anywhere.

    Workbench → prophecy on a pre-registered observation, then the operator
    looks, answers, and the dream is offered to the trading agent. That is the
    pipeline 0b said could not start.
    """
    dream_id = _shelved(store)
    shelved = store.get(dream_id)
    assert shelved is not None

    result = store.settle_condition(
        dream_id,
        shelved.conditions[0].key,
        by=OPERATOR,
        met=True,
        note="Q4 release: potline 2 restarting, first metal expected next quarter.",
        at=AFTER,
    )

    assert result.ok
    assert result.condition is not None
    assert result.condition.fulfilled is True
    assert result.condition.observed_by == OPERATOR
    assert result.condition.state(AFTER) is ConditionState.MET

    moved = store.promote(dream_id, at=AFTER)
    assert moved.ok and moved.moved_to is Vault.VAULT


def test_the_dreamer_may_not_settle_its_own_observation(store):
    """**The refusal that keeps this from being a model writing itself a grant.**

    A fulfilled condition can carry a dream to the vault, a vaulted dream can be
    adopted, and an adoption is a live permission to trade a symbol in no
    allowlist. An agent answering its own question would be marking its own
    homework on that route.
    """
    dream_id = _shelved(store)
    shelved = store.get(dream_id)
    assert shelved is not None

    for actor in (DREAMER, TRADER, "", "operator "):
        refused = store.settle_condition(
            dream_id, shelved.conditions[0].key, by=actor, met=True, note="it happened"
        )
        assert refused.refused
        assert SettleRefusal.FORBIDDEN_ACTOR in refused.refusals

    after = store.get(dream_id)
    assert after is not None
    assert after.conditions[0].fulfilled is False
    assert store.promote(dream_id).refused


def test_an_answer_lands_on_the_claim_that_was_shown_or_on_nothing(store):
    """Identified by key, never by index.

    A dream run between the operator reading the list and answering can restate
    and reorder the conditions. An index would land the yes on a different
    claim — and on this route a yes is a step towards a trading permission.
    """
    dream_id = _shelved(store)
    stale = _observation(observable="reports anything at all").key

    refused = store.settle_condition(
        dream_id, stale, by=OPERATOR, met=True, note="looked it up"
    )

    assert refused.refused
    assert SettleRefusal.NO_SUCH_CONDITION in refused.refusals
    assert refused.condition is None


def test_a_threshold_may_not_be_settled_by_hand(store):
    """Code owns the graded route. A person overriding a number would be a
    reading nobody can check, which is the failure the number exists to
    prevent arriving from the other direction."""
    dream_id = store.save(_keeper(conditions=[_checkable()]))
    dream = store.get(dream_id)
    assert dream is not None

    refused = store.settle_condition(
        dream_id, dream.conditions[0].key, by=OPERATOR, met=True, note="close enough"
    )

    assert refused.refused
    assert SettleRefusal.NOT_AN_OBSERVATION in refused.refusals


def test_an_answer_needs_a_reason(store):
    """The note is the only record of what was seen. Without it a settlement
    cannot afterwards be told from a mis-click that granted a symbol."""
    dream_id = _shelved(store)
    dream = store.get(dream_id)
    assert dream is not None

    refused = store.settle_condition(
        dream_id, dream.conditions[0].key, by=OPERATOR, met=True, note="   "
    )

    assert refused.refused
    assert SettleRefusal.NEEDS_REASON in refused.refusals


def test_neither_answer_can_be_overwritten(store):
    """A fulfilled condition is never un-fulfilled, and a ruled-out one is not
    quietly reopened. Same rule, both directions, and the refusal says who
    answered."""
    for met in (True, False):
        dream_id = _shelved(store)
        dream = store.get(dream_id)
        assert dream is not None
        key = dream.conditions[0].key

        assert store.settle_condition(
            dream_id, key, by=OPERATOR, met=met, note="looked", at=AFTER
        ).ok

        again = store.settle_condition(
            dream_id, key, by=OPERATOR, met=not met, note="looked again", at=AFTER
        )
        assert again.refused
        assert SettleRefusal.ALREADY_ANSWERED in again.refusals


def test_a_ruled_out_observation_is_answered_and_blocks_the_vault(store):
    """A mechanism that can record a yes and not a no manufactures confirmations.

    The refutation is a real answer: it is not "still waiting", it is settled,
    and the dream can never be offered to the trading agent on that condition.
    Nothing archives it — the dreamer may take the chain apart again, and a
    weakest link that was disproved is one of the more useful things it can read.
    """
    dream_id = _shelved(store)
    dream = store.get(dream_id)
    assert dream is not None

    assert store.settle_condition(
        dream_id,
        dream.conditions[0].key,
        by=OPERATOR,
        met=False,
        note="Q4 release: curtailment extended, no restart planned.",
        at=AFTER,
    ).ok

    settled = store.get(dream_id)
    assert settled is not None
    condition = settled.conditions[0]
    assert condition.ruled_out is True
    assert condition.fulfilled is False
    assert condition.is_answered is True
    assert condition.state(AFTER) is ConditionState.RULED_OUT
    assert settled.ruled_out_conditions == [condition]
    assert settled.all_conditions_met is False
    assert store.promote(dream_id, at=AFTER).refused
    assert settled.vault is Vault.PROPHECY


# ------------------------------------------------- carrying an answer forward


def test_a_restated_observation_keeps_the_answer_a_person_gave():
    """Rewording is not re-asking.

    A dreamer restating its list would otherwise erase the operator's answer and
    put the same question back on the worklist, spending a person's attention
    twice on something already settled — and `all_conditions_met` promotes the
    dream, so an answer that reset every step could never carry one anywhere.
    """
    was = [_observation(fulfilled=True, observed_by=OPERATOR, note="saw it")]
    restated = [_observation(text="reworded entirely, same question")]

    carried = carry_forward_grading(was, restated)

    assert carried[0].fulfilled is True
    assert carried[0].observed_by == OPERATOR
    assert carried[0].text == "reworded entirely, same question"


def test_a_restated_observation_keeps_a_RULED_OUT_answer_too():
    """The half that would have been missed: `fulfilled` alone is not the answer.

    Carrying only fulfilments would silently reopen every refuted claim on the
    next dream step, which is the same question asked again as though nobody
    had ever looked.
    """
    was = [_observation(ruled_out=True, observed_by=OPERATOR, note="no restart")]

    carried = carry_forward_grading(was, [_observation()])

    assert carried[0].ruled_out is True
    assert carried[0].is_answered is True


def test_changing_what_the_observation_must_show_starts_a_new_claim():
    """The observation equivalent of moving a threshold.

    Inheriting the answer across a changed claim would be back-dating a
    prediction — settling one question and quietly applying it to another.
    """
    was = [_observation(fulfilled=True, observed_by=OPERATOR)]
    moved = [_observation(observable="reports any capacity change at all")]

    assert carry_forward_grading(was, moved)[0].fulfilled is False


def test_moving_only_the_review_date_keeps_the_answer():
    """The date says when to look, not what is claimed — the `settles_hops`
    rule, in a second place. Re-scheduling a question does not re-ask it."""
    was = [_observation(fulfilled=True, observed_by=OPERATOR)]
    later = [_observation(observe_by=datetime(2028, 1, 1, tzinfo=UTC))]

    carried = carry_forward_grading(was, later)

    assert carried[0].fulfilled is True
    assert carried[0].observe_by == datetime(2028, 1, 1, tzinfo=UTC)


def test_a_restated_list_that_OMITS_a_refused_claim_does_not_erase_it():
    """**Omission was the hole that restatement was already closed against.**

    Carrying the verdict across a reworded claim is only half the rule. The
    other half is that the output used to be exactly `incoming`, so a step
    that simply did not mention a claim DELETED it — and dropping a ruled-out
    condition is strictly stronger than restating one, because
    `all_conditions_met` is what promotes a dream to the vault and a
    ruled-out condition is the thing stopping it. Measured end to end: the
    dream went workbench → vault → adopted and granted a symbol in no
    allowlist, with `ruled_out_conditions` empty so even the absence was
    invisible.

    A model must not be able to change a settled fact by not mentioning it.
    """
    refused = _observation(ruled_out=True, observed_by=OPERATOR, note="no restart")
    was = [_checkable(fulfilled=True), refused]

    carried = carry_forward_grading(was, [_checkable()])

    assert [c.key for c in carried] == [_checkable().key, refused.key]
    assert carried[-1].ruled_out is True
    assert carried[-1].observed_by == OPERATOR


def test_a_restated_list_that_omits_a_MET_claim_keeps_that_too():
    """One rule reads as one rule.

    `is_answered` is the test on the way in and it is the test here, so both
    halves of an answer survive an omission. Retaining a fulfilment costs
    nothing — it is met, so it blocks nothing — and a rule that preserved only
    refusals would be asymmetric bookkeeping somebody talks themselves out of.
    """
    met = _observation(fulfilled=True, observed_by=OPERATOR, note="saw it")

    carried = carry_forward_grading([met], [_checkable()])

    assert [c.key for c in carried] == [_checkable().key, met.key]
    assert carried[-1].fulfilled is True


def test_an_unanswered_condition_a_step_drops_is_gone():
    """The retention is about ANSWERS, not about every condition ever written.

    A dreamer reworking its own unsettled claims is the ordinary case and must
    stay possible; what it may not do is withdraw a question somebody has
    already answered. Without this the list would only ever grow.
    """
    stale = _observation(subject="a subject nobody ever answered")

    carried = carry_forward_grading([stale], [_checkable()])

    assert [c.key for c in carried] == [_checkable().key]


def test_a_threshold_and_an_observation_never_collide_on_one_key():
    """The key names the SHAPE first, so two different kinds of claim cannot be
    read as the same claim by filling the same blanks."""
    keys = {_checkable().key, _observation().key, DreamCondition(text="t").key}

    assert len(keys) == 3
    assert {k[0] for k in keys} == {"threshold", "observation", "prose"}


# ---------------------------------------------- the old shape still reads back


def test_a_condition_written_before_observations_existed_reads_back_unchanged(store):
    """The store keeps conditions as JSON in a TEXT column, so there is no
    column to add and no migration to run — but a row written before these
    fields existed genuinely says nothing about them, and it must come back as
    "not an observation" rather than as a half-formed one.

    Built from the OLD shape deliberately, which is the only way to exercise
    this at all: every other test constructs a current `DreamCondition` and so
    always gets the new keys.
    """
    old_row = {
        "text": "close above 100",
        "field": "close",
        "op": "above",
        "value": 100.0,
        "symbol": "AA",
        "settles_hops": [2],
        "fulfilled": False,
        "fulfilled_at": None,
        "note": "",
    }
    dream_id = store.save(_keeper())
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE dreams SET conditions=? WHERE id=?",
            (json.dumps([old_row]), dream_id),
        )

    reloaded = store.get(dream_id)

    assert reloaded is not None
    condition = reloaded.conditions[0]
    assert condition.subject == "" and condition.observable == ""
    assert condition.observe_by is None
    assert condition.observed_by == "" and condition.ruled_out is False
    assert condition.is_observable is False
    assert condition.is_checkable is True  # unchanged: it still promotes
    assert condition.state(AFTER) is ConditionState.AWAITING


def test_an_unreadable_review_date_reads_as_no_date_not_as_overdue_now(store):
    """`_dt` answers "now" for anything it cannot parse, which is right for a
    stamp recording when something happened and very wrong here: a corrupt
    value would land at the top of the operator's worklist as already overdue.
    Unknown is unknown, and an unknown date is not an observation."""
    condition = DreamCondition.from_row(
        {"text": "t", "subject": "s", "observable": "o", "observe_by": "not a date"}
    )

    assert condition.observe_by is None
    assert condition.is_observable is False


def test_an_observation_survives_the_round_trip(store):
    dream_id = store.save(_smelter())

    reloaded = store.get(dream_id)

    assert reloaded is not None
    condition = reloaded.conditions[0]
    assert condition.subject.startswith("Century Aluminum")
    assert condition.observe_by == DUE
    assert condition.is_observable is True
    assert condition.settles_hops == (3,)


# --------------------------------------------- what a person is asked to answer


def test_the_worklist_names_what_is_due_and_keeps_awaiting_apart_from_overdue():
    """The half of this that fails SILENTLY.

    A threshold is checked by code on every run whether or not anybody is
    watching. An observation is checked by a person, who never checks if never
    asked — so without something that can be asked "what is waiting on me" the
    prophecy shelf becomes a place dreams go to wait while every surface reports
    patience.

    Both states are returned, because a caller that dropped the merely-awaiting
    would answer "nothing is waiting on you" to an operator with a question due
    next week.
    """
    soon = _smelter()
    soon.id = 1
    later = _smelter()
    later.id = 2
    later.conditions = [
        _observation(
            subject="Santee Cooper's industrial tariff docket",
            observe_by=datetime(2028, 9, 1, tzinfo=UTC),
        )
    ]
    answered = _smelter()
    answered.id = 3
    answered.conditions = [_observation(fulfilled=True, observed_by=OPERATOR)]

    due = pending_observations([later, answered, soon], now=AFTER)

    # Answered questions are not asked again, and the oldest date sorts first.
    assert [item.dream_id for item in due] == [1, 2]
    assert due[0].state is ConditionState.OVERDUE and due[0].is_overdue
    assert due[1].state is ConditionState.AWAITING and not due[1].is_overdue
    assert due[0].title == soon.title
    assert soon.overdue_observations(AFTER) == soon.conditions
    assert soon.overdue_observations(BEFORE) == []


# =========================================================== symbiosis: fusing
#
# Two chains that meet at the same hop. The tests are organised around the six
# properties in the module docstring, because those are the decisions; the store
# mechanics underneath them are not.


def _hydro() -> Dream:
    """Drought cuts hydro output, so power in that region gets scarce and dear."""
    return Dream(
        title="Drought and hydro",
        seed="the reservoir is at a decade low",
        chain=[
            Hop("the reservoir is at a decade low", True, "the operator's own gauge"),
            Hop("that region's power gets scarce and dear", True, "spot auction prints"),
        ],
        instruments=["hydro", "the South Island"],
        symbols=["SPY"],
        asset_class_key="us_equity",
        conditions=[_checkable(value=100.0)],
    )


def _smelters() -> Dream:
    """Smelters chase cheap power, and they meet the hydro chain at the same hop."""
    return Dream(
        title="Smelters chase cheap power",
        seed="the marginal smelter moves for a cent",
        chain=[
            # The SAME claim, and this parent never sourced it.
            Hop("that region's power gets scarce and dear"),
            Hop("the marginal smelter curtails first"),
        ],
        instruments=["aluminium"],
        symbols=["QQQ"],
        asset_class_key="us_equity",
        conditions=[
            DreamCondition(
                text="aluminium holds above 2,400",
                symbol="QQQ",
                field=TriggerField.CLOSE,
                op=TriggerOp.ABOVE,
                value=2400.0,
            )
        ],
    )


def _fused(store: DreamStore, **kw) -> tuple[int, int, int]:
    """Two parents sharing a hop, fused. Returns (child, parent a, parent b)."""
    a = store.save(_hydro())
    b = store.save(_smelters())
    result = store.fuse([a, b], by=DREAMER, **kw)
    assert result.ok, result.detail
    assert result.dream_id is not None
    return result.dream_id, a, b


def test_a_fusion_is_a_new_dream_and_both_parents_survive(store):
    """History is the point. A fused dream that ate its sources would destroy
    the ability to attack either of them, which is the whole activity."""
    child_id, a, b = _fused(store)

    child = store.get(child_id)
    assert child is not None
    assert child.parents == [a, b]
    assert child.is_fusion

    for parent_id, expected in ((a, _hydro()), (b, _smelters())):
        parent = store.get(parent_id)
        assert parent is not None
        assert [h.claim for h in parent.chain] == [h.claim for h in expected.chain]
        assert parent.vault is Vault.WORKBENCH
        assert parent.symbols == expected.symbols


def test_the_back_reference_is_derived_rather_than_stored(store):
    """A second copy of one fact is a second thing that can disagree with it —
    the reasoning that keeps `Adoption.is_live` computed rather than flagged."""
    child_id, a, b = _fused(store)

    assert store.children_of(a) == [child_id]
    assert store.children_of(b) == [child_id]
    assert store.children_of(child_id) == []


def test_the_chain_is_the_union_and_the_shared_hop_is_named(store):
    """The overlap is the REASON the fusion exists, so it is a field rather than
    something a reader has to spot by holding two chains side by side."""
    child_id, _, _ = _fused(store)

    child = store.get(child_id)
    assert child is not None
    assert [h.claim for h in child.chain] == [
        "the reservoir is at a decade low",
        "that region's power gets scarce and dear",
        "the marginal smelter curtails first",
    ]
    assert child.shared_hops == ["that region's power gets scarce and dear"]


def test_verification_never_improves_by_fusing(store):
    """**Two unverified chains do not make a sourced one.**

    The union of a SOURCED parent and an UNVERIFIED one contains checked hops,
    which counts as PARTIAL — strictly better than the worse parent. The
    ceiling is what stops the badge reading better than the argument it came
    from, and it is the assertion this feature turns on.
    """
    sourced = Dream(
        title="sourced",
        seed="s",
        chain=[Hop("a shared claim", True, "a real source"), Hop("b", True, "another")],
    )
    unverified = Dream(
        title="unverified", seed="s", chain=[Hop("a shared claim"), Hop("c")]
    )
    assert sourced.verification is Verification.SOURCED
    assert unverified.verification is Verification.UNVERIFIED

    a, b = store.save(sourced), store.save(unverified)
    result = store.fuse([a, b], by=DREAMER)
    child = store.get(int(result.dream_id or 0))

    assert child is not None
    assert child.verification is Verification.UNVERIFIED
    assert child.verification_ceiling is Verification.UNVERIFIED
    # And it survives the round trip, because the cap is what the badge means.
    reopened = store.get(int(child.id or 0))
    assert reopened is not None and reopened.verification is Verification.UNVERIFIED


def test_a_hop_one_parent_sourced_and_the_other_did_not_arrives_unchecked(store):
    """A link whose sourcing is in dispute must not come across as the more
    flattering of the two readings. The source is not lost: it is on the parent,
    which survives."""
    child_id, a, _ = _fused(store)

    child = store.get(child_id)
    assert child is not None
    shared = next(h for h in child.chain if h.claim in child.shared_hops)
    assert shared.checked is False
    assert shared.source == ""

    # The parent that did source it still has the source.
    parent = store.get(a)
    assert parent is not None
    assert parent.chain[1].checked is True
    assert parent.chain[1].source == "spot auction prints"


def test_the_conditions_are_the_union_so_a_fusion_is_harder_to_promote(store):
    """A fusion is a strictly stronger claim, so it must be harder to promote
    than either parent and never easier."""
    child_id, _, _ = _fused(store)

    child = store.get(child_id)
    assert child is not None
    assert len(child.conditions) == 2
    assert child.all_conditions_met is False

    # One of the two firing is not enough, which is the point.
    child.conditions = [replace(child.conditions[0], fulfilled=True), child.conditions[1]]
    assert child.all_conditions_met is False


def test_a_condition_already_fulfilled_on_a_parent_arrives_fulfilled(store):
    """`carry_forward_grading` is the one grading rule in the repository, and
    the merge reuses it rather than growing a second copy."""
    hydro = _hydro()
    hydro.conditions = [_checkable(value=100.0, fulfilled=True)]
    a, b = store.save(hydro), store.save(_smelters())

    result = store.fuse([a, b], by=DREAMER)
    child = store.get(int(result.dream_id or 0))

    assert child is not None
    assert child.conditions_met == 1
    assert child.all_conditions_met is False


def test_a_hop_pin_is_RENUMBERED_onto_the_fused_chain(store):
    """The child's chain is a deduped union in parent order, so hop 2 of a
    parent is rarely hop 2 of the child.

    An index carried across untouched would still report as covered while
    naming a different link, which is the one failure worse than no pin at all.
    Here the smelters chain is [shared hop, "the marginal smelter curtails
    first"] and the shared hop is already the child's hop 2, so the second
    parent's hop 2 becomes the child's hop 3.
    """
    smelters = _smelters()
    smelters.conditions = [replace(smelters.conditions[0], settles_hops=(2,))]
    a, b = store.save(_hydro()), store.save(smelters)

    result = store.fuse([a, b], by=DREAMER)
    child = store.get(int(result.dream_id or 0))

    assert child is not None
    assert [h.claim for h in child.chain][2] == "the marginal smelter curtails first"
    assert child.conditions[1].settles_hops == (3,)


def test_a_pin_that_cannot_be_mapped_is_DROPPED_rather_than_left_pointing(store):
    """A stale index is worse than an absent one: it reads as covered.

    Nothing in this repository invents a plausible figure to fill a field, and
    a hop number is a figure. The condition arrives honestly unpinned, and the
    dream it lands on stays on the workbench until a dreamer pins it again.
    """
    smelters = _smelters()
    smelters.conditions = [replace(smelters.conditions[0], settles_hops=(7,))]
    a, b = store.save(_hydro()), store.save(smelters)

    result = store.fuse([a, b], by=DREAMER)
    child = store.get(int(result.dream_id or 0))

    assert child is not None
    assert child.conditions[1].settles_hops == ()


def test_the_symbols_are_the_union_and_never_wider(store):
    """A fusion must not become a route around the class hard-block, so it can
    only name what its parents already claimed."""
    child_id, _, _ = _fused(store)

    child = store.get(child_id)
    assert child is not None
    assert child.symbols == ["SPY", "QQQ"]
    assert child.asset_class_key == "us_equity"


def test_an_override_may_narrow_the_union_but_not_widen_it(store):
    """The `adopt` lock in a second place. A symbol arriving from nowhere would
    be a permission with no argument behind it."""
    a = store.save(_hydro())
    b = store.save(_smelters())

    narrowed = store.fuse([a, b], by=DREAMER, symbols=["SPY"])
    assert narrowed.ok
    child = store.get(int(narrowed.dream_id or 0))
    assert child is not None and child.symbols == ["SPY"]

    wider = store.fuse([a, b], by=DREAMER, symbols=["SPY", "TSLA"])
    assert wider.refused
    assert MoveRefusal.SYMBOLS_NOT_OFFERED in wider.refusals
    assert "TSLA" in wider.detail


def test_parents_that_disagree_about_the_class_leave_it_unresolved(store):
    """Unresolved grants nothing — `adopt` refuses it and `granted_symbols`
    drops it — which is the direction to fail in. A symbol whose class is
    unknown is a symbol whose limits are unknown."""
    equity = _hydro()
    crypto = _smelters()
    crypto.symbols = ["BTC/USD"]
    crypto.asset_class_key = "crypto"
    a, b = store.save(equity), store.save(crypto)

    result = store.fuse([a, b], by=DREAMER)
    child = store.get(int(result.dream_id or 0))

    assert child is not None
    assert child.symbols == ["SPY", "BTC/USD"]
    assert child.asset_class_key == ""


def test_a_fusion_of_one_dream_is_refused(store):
    """One dream is not a fusion; it is the dream you already have."""
    a = store.save(_hydro())

    result = store.fuse([a], by=DREAMER)

    assert result.refused
    assert MoveRefusal.NEEDS_PARENTS in result.refusals
    assert result.dream_id is None


def test_a_fusion_of_four_dreams_is_refused(store):
    """At four the shared-hop argument stops meaning anything: a link that many
    chains reach is a truism rather than a mechanism."""
    ids = [store.save(Dream(title=f"d{i}", seed="s", chain=[Hop("shared")])) for i in range(4)]

    result = store.fuse(ids, by=DREAMER)

    assert result.refused
    assert MoveRefusal.TOO_MANY_PARENTS in result.refusals
    assert result.dream_id is None


def test_an_adopted_dream_cannot_be_fused(store):
    """A live grant points at that row and the trading agent is holding it —
    the same reason the dreamer may neither move nor delete one."""
    adopted = _vaulted(store, title="taken")
    assert store.adopt(adopted).ok
    other = store.save(_hydro())

    result = store.fuse([adopted, other], by=DREAMER)

    assert result.refused
    assert MoveRefusal.PARENT_ADOPTED in result.refusals
    assert "return_to_vault" in result.detail
    # Nothing was written, so the shelf and the grant are untouched.
    assert store.get(adopted).vault is Vault.ADOPTED
    assert store.children_of(adopted) == []


def test_only_the_dreamer_may_fuse(store):
    """Same actor table as `move`: the trading agent has two verbs and this is
    not one of them, and an unrecognised name is refused rather than waved
    through."""
    a, b = store.save(_hydro()), store.save(_smelters())

    trader = store.fuse([a, b], by=TRADER)
    stranger = store.fuse([a, b], by="somebody")

    assert MoveRefusal.FORBIDDEN_ACTOR in trader.refusals
    assert "adopt" in trader.detail
    assert MoveRefusal.FORBIDDEN_ACTOR in stranger.refusals
    assert store.children_of(a) == []


def test_a_missing_parent_is_refused_by_id_rather_than_raising(store):
    a = store.save(_hydro())

    result = store.fuse([a, 9999], by=DREAMER)

    assert result.refused
    assert MoveRefusal.NOT_FOUND in result.refusals
    assert "9999" in result.detail


def test_a_fusion_collects_every_refusal_rather_than_the_first_one(store):
    """`RiskGate`'s property, in this store's third place. A caller told one
    reason at a time fixes one thing at a time and asks again."""
    result = DreamStore(store.path).fuse([1], by=TRADER)

    assert {MoveRefusal.FORBIDDEN_ACTOR, MoveRefusal.NEEDS_PARENTS} <= set(result.refusals)


def test_a_fusion_counts_against_the_workbench_cap(store):
    """It is a dream on the workbench like any other, and the cap is about what
    a person can hold in their head."""
    a, b = store.save(_hydro()), store.save(_smelters())

    result = store.fuse([a, b], by=DREAMER, caps=VaultCaps(workbench=2))

    assert result.refused
    assert MoveRefusal.FULL in result.refusals
    assert store.children_of(a) == []


def test_the_fusion_is_narrated_into_both_parents_transcripts(store):
    """The transcript is what a human reads beside a dream, and a fusion is a
    significant event in its life. The speaker is neither agent, so
    `confer.last_agent_turn_at` cannot read it as a turn."""
    child_id, a, b = _fused(store)

    for parent_id in (a, b):
        notes = store.messages(parent_id)
        assert len(notes) == 1
        assert notes[0].speaker == FUSION
        assert notes[0].kind == "fusion"
        assert str(child_id) in notes[0].text
        assert "unchanged and still yours" in notes[0].text


def test_a_fusion_starts_with_no_verdict_and_no_weakest_hop(store):
    """Nobody has attacked the combined chain yet. Inheriting a parent's
    weakest hop would claim it had been examined, and a verdict would let it
    promote off the workbench without anyone working it."""
    child_id, _, _ = _fused(store)

    child = store.get(child_id)
    assert child is not None
    assert child.verdict is None
    assert child.weakest_hop == ""
    assert child.stage is DreamStage.EXPLORE
    assert promotion_for(child).moves is False


def test_a_fusion_names_the_shared_hop_in_its_own_seed(store):
    child_id, _, _ = _fused(store)

    child = store.get(child_id)
    assert child is not None
    assert "that region's power gets scarce and dear" in child.seed
    assert child.title == "Drought and hydro + Smelters chase cheap power"


def test_a_fusion_with_no_overlap_says_so_rather_than_implying_one(store):
    """The good outcome must not be what an absence of evidence looks like, and
    a seed claiming a shared mechanism where there is none would be the
    confident wrong sentence written by the store itself."""
    a = store.save(Dream(title="one", seed="s", chain=[Hop("nothing in common")]))
    b = store.save(Dream(title="two", seed="s", chain=[Hop("nor here")]))

    result = store.fuse([a, b], by=DREAMER)
    child = store.get(int(result.dream_id or 0))

    assert child is not None
    assert child.shared_hops == []
    assert "share no hop" in child.seed


def test_a_fusion_survives_the_round_trip(store):
    child_id, a, b = _fused(store)

    reopened = DreamStore(store.path).get(child_id)

    assert reopened is not None
    assert reopened.parents == [a, b]
    assert reopened.shared_hops == ["that region's power gets scarce and dear"]
    assert reopened.verification_ceiling is Verification.UNVERIFIED


def test_the_summary_counts_fusions(store):
    child_id, a, b = _fused(store)

    summary = DreamSummary.of(store.recent())

    assert summary.total == 3
    assert summary.fusions == 1
    assert child_id and a and b


# --------------------------------------------------- finding what could fuse


def test_the_candidate_finder_pairs_dreams_that_share_a_hop():
    """Arithmetic proposes, the dreamer confirms. Nothing fuses unattended:
    a machine that combined hypotheses on its own would be generating confident
    new claims out of arithmetic over old ones."""
    a, b = _hydro(), _smelters()
    a.id, b.id = 1, 2
    unrelated = Dream(title="elsewhere", seed="s", chain=[Hop("nothing to do with it")])
    unrelated.id = 3

    found = fusion_candidates([a, b, unrelated])

    assert len(found) == 1
    assert found[0].dream_ids == (1, 2)
    assert found[0].shared_hops == ("that region's power gets scarce and dear",)
    assert found[0].overlap == 1
    assert found[0].titles == ("Drought and hydro", "Smelters chase cheap power")


def test_a_hop_four_dreams_share_is_not_a_candidate_at_all():
    """A link that many chains reach is a truism, and truncating to the first
    three would hide that by picking an arbitrary three of them."""
    dreams = []
    for index in range(4):
        dream = Dream(title=f"d{index}", seed="s", chain=[Hop("energy prices matter")])
        dream.id = index + 1
        dreams.append(dream)

    assert fusion_candidates(dreams) == []


def test_a_dream_that_is_already_a_fusion_is_not_offered_again():
    """It shares every hop with its own parents by construction, so a finder
    that could see one would propose re-fusing it with them forever."""
    a, b = _hydro(), _smelters()
    a.id, b.id = 1, 2
    child = Dream(
        title="fused",
        seed="s",
        chain=[Hop("that region's power gets scarce and dear")],
        parents=[1, 2],
    )
    child.id = 3

    found = fusion_candidates([a, b, child])

    assert [c.dream_ids for c in found] == [(1, 2)]


def test_an_adopted_dream_is_not_offered_as_a_candidate():
    """`fuse` refuses one anyway, so offering it would be offering a move that
    cannot be made."""
    a, b = _hydro(), _smelters()
    a.id, b.id = 1, 2
    b.vault = Vault.ADOPTED

    assert fusion_candidates([a, b]) == []


def test_candidates_are_ordered_by_overlap_and_then_stably():
    """A list that reshuffled between runs would make 'the model picked the
    second one' unreproducible."""
    strong_a = Dream(title="a", seed="s", chain=[Hop("one"), Hop("two")])
    strong_b = Dream(title="b", seed="s", chain=[Hop("one"), Hop("two")])
    weak_a = Dream(title="c", seed="s", chain=[Hop("three")])
    weak_b = Dream(title="d", seed="s", chain=[Hop("three")])
    for index, dream in enumerate((strong_a, strong_b, weak_a, weak_b), start=1):
        dream.id = index

    found = fusion_candidates([strong_a, strong_b, weak_a, weak_b])

    assert [c.dream_ids for c in found] == [(1, 2), (3, 4)]
    assert found[0].overlap == 2


def test_plan_fusion_is_pure_and_needs_no_store():
    """The rule can be read and tested without a database, exactly as
    `promotion_for` can. `DreamStore.fuse` is only the half that needs a
    connection, the caps and a clock."""
    a, b = _hydro(), _smelters()
    a.id, b.id = 7, 9

    plan = plan_fusion([a, b])

    assert plan.parents == (7, 9)
    assert plan.has_overlap
    assert plan.verification_ceiling is Verification.UNVERIFIED
    assert len(plan.chain) == 3


def test_a_claim_two_parents_answered_OPPOSITE_ways_fuses_as_RULED_OUT():
    """**The parent ORDER used to decide which operator answer survived.**

    The answers were collected into a flat list and read into a dict, so the
    last parent won. Measured: `[met, refuted]` produced a refuted child and
    `[refuted, met]` produced a met one, over the same pair — and the dreamer
    chooses that order, so a model was choosing between two of the operator's
    answers on the route that ends in a symbol permission.

    In dispute the refusal stands. It is the AND on `checked` in a second
    place, and it makes the fusion harder to promote, which is the direction a
    fusion is supposed to move in.
    """
    met = DreamCondition(text="t", subject="s", observable="o", observe_by=DUE,
                         fulfilled=True, observed_by=OPERATOR, note="saw it")
    refuted = DreamCondition(text="t", subject="s", observable="o", observe_by=DUE,
                             ruled_out=True, observed_by=OPERATOR, note="it did not")
    a = Dream(id=1, title="a", seed="s", chain=[Hop("m")], conditions=[met])
    b = Dream(id=2, title="b", seed="s", chain=[Hop("m")], conditions=[refuted])

    for parents in ([a, b], [b, a]):
        settled = plan_fusion(parents).conditions[0]
        assert settled.ruled_out is True
        assert settled.fulfilled is False


def test_expiry_reads_a_naive_clock_as_UTC_rather_than_raising(store):
    """Two functions on one store answering the same argument must not fail
    differently. `Adoption.is_live` reads a naive `now` as UTC; this raised
    `TypeError: can't subtract offset-naive and offset-aware datetimes`, in a
    scheduled command where an exception is a job that silently stopped."""
    store.save(
        Dream(title="ancient", seed="s",
              vault_entered_at=datetime(2026, 1, 1, tzinfo=UTC))
    )

    assert [d.title for d in store.expired(datetime(2027, 1, 1))] == ["ancient"]


def test_the_weaker_badge_wins_and_it_is_not_alphabetical():
    """`Verification` is a StrEnum, so `min()` over one would compare the WORDS
    — partial < sourced < unverified — putting the weakest badge last. A silent
    alphabetical answer to an evidence question is the plausible wrong figure
    this repository refuses."""
    assert weaker_of(Verification.SOURCED, Verification.UNVERIFIED) is Verification.UNVERIFIED
    assert weaker_of(Verification.PARTIAL, Verification.SOURCED) is Verification.PARTIAL
    assert weaker_of(Verification.PARTIAL, Verification.PARTIAL) is Verification.PARTIAL


# ------------------------------------------------- the migration, second time

# The `dreams` table exactly as it was after the vaults and BEFORE symbiosis.
# Written out by hand for the reason `OLD_SCHEMA` above is: importing today's
# SCHEMA would test nothing, because the whole point is to start from the shape
# that is actually on the droplet.
PRE_FUSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS dreams (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  title        TEXT NOT NULL,
  seed         TEXT NOT NULL,
  stage        TEXT NOT NULL,
  verdict      TEXT,
  weakest_hop  TEXT NOT NULL DEFAULT '',
  trigger_note TEXT NOT NULL DEFAULT '',
  origin       TEXT NOT NULL DEFAULT '',
  chain        TEXT NOT NULL DEFAULT '[]',
  thoughts     TEXT NOT NULL DEFAULT '[]',
  instruments  TEXT NOT NULL DEFAULT '[]',
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  vault           TEXT NOT NULL DEFAULT 'workbench',
  vault_entered_at TEXT NOT NULL DEFAULT '',
  conditions      TEXT NOT NULL DEFAULT '[]',
  symbols         TEXT NOT NULL DEFAULT '[]',
  asset_class_key TEXT NOT NULL DEFAULT '',
  wisp            TEXT NOT NULL DEFAULT ''
);
"""


def _pre_fusion_store_with_a_row(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(PRE_FUSION_SCHEMA)
    conn.execute(
        "INSERT INTO dreams (title, seed, stage, verdict, weakest_hop, trigger_note,"
        " origin, chain, thoughts, instruments, created_at, updated_at, vault,"
        " vault_entered_at, conditions, symbols, asset_class_key, wisp)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "Drought and hydro",
            "the reservoir is at a decade low",
            "explore",
            None,
            "whether the smelter can actually curtail",
            "the spot auction print",
            "a headline about rainfall",
            json.dumps([{"claim": "the reservoir is at a decade low", "checked": True,
                         "source": "the operator's own gauge"}]),
            json.dumps([{"stage": "explore", "text": "who buys that power",
                         "at": "2026-07-01T09:00:00+00:00", "by": ""}]),
            json.dumps(["hydro"]),
            "2026-06-01T09:00:00+00:00",
            "2026-07-01T09:00:00+00:00",
            "vault",
            "2026-07-01T09:00:00+00:00",
            json.dumps([{"text": "the auction clears above 200", "fulfilled": False}]),
            json.dumps(["SPY"]),
            "us_equity",
            "",
        ),
    )
    conn.commit()
    conn.close()


def test_a_database_that_predates_symbiosis_is_migrated_in_place(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` is a no-op on a table that already exists,
    and every other test here builds its store from scratch — so the suite is
    structurally blind to a missing column and cannot tell you that you have
    forgotten one. This is the second generation of that migration and it starts
    from the shape the droplet is actually running."""
    path = tmp_path / "dreams.db"
    _pre_fusion_store_with_a_row(path)

    store = DreamStore(path)
    loaded = store.recent()

    assert len(loaded) == 1
    dream = loaded[0]
    # The row survived whole. A migration that dropped history to add a column
    # would be a far worse trade than the missing column.
    assert dream.title == "Drought and hydro"
    assert dream.vault is Vault.VAULT
    assert dream.symbols == ["SPY"]
    assert [c.text for c in dream.conditions] == ["the auction clears above 200"]
    # And the new columns are there, carrying the shape an ordinary dream has.
    assert dream.parents == []
    assert dream.shared_hops == []
    assert dream.verification_ceiling is None
    assert dream.is_fusion is False


def test_a_migrated_database_can_still_be_fused_into(tmp_path):
    """Present is not the same as usable. The migrated row round-trips through
    a real fusion, which is the only way to know the columns actually work."""
    path = tmp_path / "dreams.db"
    _pre_fusion_store_with_a_row(path)
    store = DreamStore(path)
    old = store.recent()[0]
    assert old.id is not None
    assert store.move(old.id, Vault.WORKBENCH, by=DREAMER)
    other = store.save(_smelters())

    result = store.fuse([old.id, other], by=DREAMER)

    assert result.ok, result.detail
    child = store.get(int(result.dream_id or 0))
    assert child is not None
    assert child.parents == [old.id, other]
    assert store.children_of(old.id) == [child.id]


def test_a_symbiosis_migration_ends_up_identical_to_a_fresh_database(tmp_path):
    """The columns live in two places — `SCHEMA` for a fresh store and
    `_ADDED_DREAM_COLUMNS` for an existing one — and this is what keeps them in
    step. Found by comparing the two, not by reading either."""
    old = tmp_path / "old.db"
    _pre_fusion_store_with_a_row(old)
    DreamStore(old)
    DreamStore(tmp_path / "fresh.db")

    def columns(path: Path) -> list[str]:
        conn = sqlite3.connect(path)
        try:
            return [str(row[1]) for row in conn.execute("PRAGMA table_info(dreams)")]
        finally:
            conn.close()

    assert columns(old) == columns(tmp_path / "fresh.db")


def test_the_symbiosis_migration_is_idempotent(tmp_path):
    """It runs on every open, so re-running it must not duplicate a column, lose
    a row, or disturb a fusion already written."""
    path = tmp_path / "dreams.db"
    _pre_fusion_store_with_a_row(path)
    store = DreamStore(path)
    old = store.recent()[0]
    assert old.id is not None
    store.move(old.id, Vault.WORKBENCH, by=DREAMER)
    other = store.save(_smelters())
    child_id = int((store.fuse([old.id, other], by=DREAMER)).dream_id or 0)

    DreamStore(path)
    third = DreamStore(path)

    assert len(third.recent()) == 3
    reopened = third.get(child_id)
    assert reopened is not None
    assert reopened.parents == [old.id, other]


# ------------------------------------------------- the migration, third time
#
# `weakest_hop_index` is the third column added to `dreams` after the table
# already existed on the droplet. The CONDITIONS' own hop pins needed no column
# at all — `conditions` is one JSON blob in a TEXT column, so a new key inside
# it reaches an existing database with no ALTER whatever. Both halves are
# tested, because "it needed no migration" is a claim and this is what makes it
# checkable rather than remembered.

PRE_PIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS dreams (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  title        TEXT NOT NULL,
  seed         TEXT NOT NULL,
  stage        TEXT NOT NULL,
  verdict      TEXT,
  weakest_hop  TEXT NOT NULL DEFAULT '',
  trigger_note TEXT NOT NULL DEFAULT '',
  origin       TEXT NOT NULL DEFAULT '',
  chain        TEXT NOT NULL DEFAULT '[]',
  thoughts     TEXT NOT NULL DEFAULT '[]',
  instruments  TEXT NOT NULL DEFAULT '[]',
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  vault           TEXT NOT NULL DEFAULT 'workbench',
  vault_entered_at TEXT NOT NULL DEFAULT '',
  conditions      TEXT NOT NULL DEFAULT '[]',
  symbols         TEXT NOT NULL DEFAULT '[]',
  asset_class_key TEXT NOT NULL DEFAULT '',
  wisp            TEXT NOT NULL DEFAULT '',
  parents              TEXT NOT NULL DEFAULT '[]',
  shared_hops          TEXT NOT NULL DEFAULT '[]',
  verification_ceiling TEXT NOT NULL DEFAULT ''
);
"""


def _pre_pin_store_with_a_row(path: Path) -> None:
    """A prophecy written before either half of the pin existed.

    Its condition carries no `settles_hops` key at all, which is the shape the
    droplet's JSON actually has — not an empty list somebody helpfully wrote.
    """
    conn = sqlite3.connect(path)
    conn.executescript(PRE_PIN_SCHEMA)
    conn.execute(
        "INSERT INTO dreams (title, seed, stage, verdict, weakest_hop, trigger_note,"
        " origin, chain, thoughts, instruments, created_at, updated_at, vault,"
        " vault_entered_at, conditions, symbols, asset_class_key, wisp, parents,"
        " shared_hops, verification_ceiling)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "Drought and hydro",
            "the reservoir is at a decade low",
            "verdict",
            "keep",
            "whether the smelter can actually curtail",
            "the spot auction print",
            "a headline about rainfall",
            json.dumps(
                [
                    {"claim": "the reservoir is at a decade low", "checked": True,
                     "source": "the operator's own gauge"},
                    {"claim": "the smelter curtails first", "checked": False,
                     "source": ""},
                ]
            ),
            json.dumps([{"stage": "verdict", "text": "it holds",
                         "at": "2026-07-01T09:00:00+00:00", "by": ""}]),
            json.dumps(["hydro"]),
            "2026-06-01T09:00:00+00:00",
            "2026-07-01T09:00:00+00:00",
            "workbench",
            "2026-07-01T09:00:00+00:00",
            json.dumps(
                [{"text": "SPY clears 600", "symbol": "SPY", "field": "close",
                  "op": "above", "value": 600.0, "fulfilled": False}]
            ),
            json.dumps(["SPY"]),
            "us_equity",
            "",
            "[]",
            "[]",
            "",
        ),
    )
    conn.commit()
    conn.close()


def test_a_database_that_predates_the_hop_pin_is_migrated_in_place(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` is a no-op on a table that already exists.

    Every other test in this file builds its store from scratch in a `tmp_path`
    and therefore always gets the new shape, so the suite is structurally blind
    to a missing column. This is the third generation of that migration and it
    starts from the shape the droplet is actually running.
    """
    path = tmp_path / "dreams.db"
    _pre_pin_store_with_a_row(path)

    store = DreamStore(path)
    loaded = store.recent()

    assert len(loaded) == 1
    dream = loaded[0]
    # The row survived whole, including the condition inside the JSON column.
    assert dream.title == "Drought and hydro"
    assert dream.verdict is DreamVerdict.KEEP
    assert [c.text for c in dream.conditions] == ["SPY clears 600"]
    assert dream.conditions[0].is_checkable

    # Both halves of the pin read as "not said" rather than as an error or a
    # zero. A row from before this shipped genuinely does not record which hop
    # its sentence meant, and inventing one would be the confident wrong figure.
    assert dream.weakest_hop_index is None
    assert dream.conditions[0].settles_hops == ()


def test_a_pre_pin_prophecy_is_held_back_rather_than_promoted_blind(tmp_path):
    """What the migration means for the RULE, which is the half worth stating.

    This dream is a finished keep with a checkable condition and would have
    promoted before. It cannot now: its weakest hop is a sentence matching no
    hop, so nothing can be shown to settle the link the chain rests on. Held on
    the workbench until a dreamer numbers it — not promoted on a guess, and not
    deleted.
    """
    path = tmp_path / "dreams.db"
    _pre_pin_store_with_a_row(path)
    store = DreamStore(path)
    dream = store.recent()[0]

    assert promotion_for(dream).to is None
    assert "matches no hop" in promotion_for(dream).reason


def test_the_pin_migration_ends_up_identical_to_a_fresh_database(tmp_path):
    """The columns live in `SCHEMA` and in `_ADDED_DREAM_COLUMNS`, and this is
    what keeps the two in step. Found by comparing them, not by reading either."""
    old = tmp_path / "old.db"
    _pre_pin_store_with_a_row(old)
    DreamStore(old)
    DreamStore(tmp_path / "fresh.db")

    def columns(path: Path) -> list[str]:
        conn = sqlite3.connect(path)
        try:
            return [str(row[1]) for row in conn.execute("PRAGMA table_info(dreams)")]
        finally:
            conn.close()

    assert columns(old) == columns(tmp_path / "fresh.db")
    assert "weakest_hop_index" in columns(old)


def test_a_migrated_row_can_be_pinned_and_promoted(tmp_path):
    """Present is not the same as usable. The migrated row round-trips through
    a real write of both halves and then through the rule that reads them."""
    path = tmp_path / "dreams.db"
    _pre_pin_store_with_a_row(path)
    store = DreamStore(path)
    dream = store.recent()[0]

    dream.weakest_hop_index = 2
    dream.conditions = [replace(dream.conditions[0], settles_hops=(2,))]
    store.save(dream)

    reopened = DreamStore(path).get(int(dream.id or 0))
    assert reopened is not None
    assert reopened.weakest_hop_index == 2
    assert reopened.conditions[0].settles_hops == (2,)
    assert promotion_for(reopened).to is Vault.PROPHECY


def test_the_hop_pin_needed_no_column_of_its_own(tmp_path):
    """The claim in `DreamCondition`, made checkable.

    `settles_hops` lives inside the `conditions` JSON blob, so it reaches an
    existing database with no ALTER at all — which is why the migration above
    covers only `weakest_hop_index`. If somebody ever gives conditions their own
    columns, this test is the one that should start failing.
    """
    path = tmp_path / "dreams.db"
    _pre_pin_store_with_a_row(path)
    DreamStore(path)

    conn = sqlite3.connect(path)
    try:
        columns = [str(row[1]) for row in conn.execute("PRAGMA table_info(dreams)")]
    finally:
        conn.close()

    assert "conditions" in columns
    assert not [c for c in columns if "settles" in c]


# ============================================ the conference decision record
#
# `dream_messages` is what was SAID. `conference_decisions` is what was DECIDED,
# and it exists because the second used to have to be reconstructed out of the
# first plus a scattering of side effects. `tests/test_confer.py` covers which
# verdict each ending produces; this section is about the store keeping it.


# `confer.CONFERENCE`, written out rather than imported. `decided_by` is a plain
# string here for the reason `DreamMessage.speaker` is one, and this file is
# about the store keeping a row rather than about the module that fills it.
CONFERENCE = "conference"


def _decision(**overrides) -> ConferenceDecision:
    base = {
        "dream_id": 1,
        "verdict": ConferenceVerdict.DECLINE,
        "decided_by": TRADER,
        "at": datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        "reason": "hop 2 is unchecked and it is the whole chain",
    }
    return ConferenceDecision(**{**base, **overrides})


def test_a_verdict_round_trips_whole(tmp_path):
    store = DreamStore(tmp_path / "dreams.db")

    written = store.record_conference_decision(_decision(turns=4))

    assert written.id is not None
    read = store.conference_decisions(1)
    assert len(read) == 1
    assert read[0] == written


def test_a_deferrals_wake_condition_survives_the_round_trip(tmp_path):
    """The checkable half is the point of it. A deferral whose threshold did not
    come back would be a deferral with nothing to wake it, one storage layer
    later."""
    store = DreamStore(tmp_path / "dreams.db")
    wake = DreamCondition(
        text="SPY closing above 620",
        symbol="SPY",
        field=TriggerField.CLOSE,
        op=TriggerOp.ABOVE,
        value=620.0,
    )

    store.record_conference_decision(
        _decision(verdict=ConferenceVerdict.DEFER, wake=wake)
    )

    read = store.conference_decisions(1)[0]
    assert read.wake == wake
    assert read.wake is not None and read.wake.is_gradeable


def test_a_deferral_with_nothing_to_wake_it_is_REFUSED(tmp_path):
    """**A rule that rejects.**

    A stored verdict is precisely the thing a renderer stops questioning, so a
    half-written one is worse than the reconstruction it replaced. A deferral
    that names nothing is not a deferral, and prose with no threshold is the
    same gap wearing a sentence.
    """
    store = DreamStore(tmp_path / "dreams.db")

    with pytest.raises(ValueError, match="checkable wake condition"):
        store.record_conference_decision(_decision(verdict=ConferenceVerdict.DEFER))

    with pytest.raises(ValueError, match="checkable wake condition"):
        store.record_conference_decision(
            _decision(
                verdict=ConferenceVerdict.DEFER,
                wake=DreamCondition(text="when the spread normalises"),
            )
        )

    # A threshold with no symbol is a comparison with no subject: `is_gradeable`
    # is stricter than `is_checkable` and this is the case it exists for.
    with pytest.raises(ValueError, match="checkable wake condition"):
        store.record_conference_decision(
            _decision(
                verdict=ConferenceVerdict.DEFER,
                wake=DreamCondition(
                    text="a close above 620",
                    field=TriggerField.CLOSE,
                    op=TriggerOp.ABOVE,
                    value=620.0,
                ),
            )
        )

    assert store.conference_decisions(1) == []


def test_a_no_decision_with_no_stated_cause_is_REFUSED(tmp_path):
    """The absence of a decision is its own state, and it has to say which way
    it happened. A turn cap, a failed call and an unconditioned deferral are
    three different facts about the two agents."""
    store = DreamStore(tmp_path / "dreams.db")

    with pytest.raises(ValueError, match="stated cause"):
        store.record_conference_decision(
            _decision(verdict=ConferenceVerdict.NO_DECISION, decided_by=CONFERENCE)
        )

    assert store.conference_decisions(1) == []


def test_no_decision_is_never_a_decision(tmp_path):
    """`decided` is what a renderer reads to know whether anybody settled
    anything, and the four real verdicts are the ones that answer yes."""
    settled = [
        ConferenceVerdict.PROMOTE,
        ConferenceVerdict.DECLINE,
        ConferenceVerdict.ARCHIVE,
    ]
    for verdict in settled:
        assert _decision(verdict=verdict).decided

    undecided = _decision(
        verdict=ConferenceVerdict.NO_DECISION,
        decided_by=CONFERENCE,
        undecided=NoDecision.TURNS_EXHAUSTED,
    )
    assert not undecided.decided


def test_the_verdicts_are_the_operators_three_plus_decline_plus_no_decision():
    """Five, and none may be folded into another.

    A decline is a judgement on the merits with no wake condition, so it is not
    a deferral; it is emphatically not an archive either. And nobody deciding is
    its own state rather than the mildest real decision.
    """
    assert {str(v) for v in ConferenceVerdict} == {
        "promote",
        "defer",
        "decline",
        "archive",
        "no_decision",
    }


def test_the_latest_verdict_is_the_latest(tmp_path):
    store = DreamStore(tmp_path / "dreams.db")
    first = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)

    store.record_conference_decision(_decision(at=first, reason="first"))
    store.record_conference_decision(
        _decision(at=first + timedelta(days=1), reason="second")
    )

    latest = store.latest_conference_decision(1)
    assert latest is not None and latest.reason == "second"
    # And the feed is newest-first, unlike the transcript. A transcript read
    # backwards is a negotiation read backwards; a feed of outcomes is asked
    # what happened most recently.
    assert [d.reason for d in store.conference_decisions(1)] == ["second", "first"]


def test_a_dream_never_conferred_is_a_third_state_and_not_the_mildest_verdict(
    tmp_path,
):
    """`None` means the two of them have never reached this dream. A caller that
    rendered it as a no-decision would report a conversation that never happened
    as one that happened and settled nothing — the same rule as a first visit in
    `seen.py`."""
    store = DreamStore(tmp_path / "dreams.db")

    assert store.latest_conference_decision(1) is None
    assert store.conference_decisions(1) == []


def test_verdicts_are_kept_per_dream(tmp_path):
    store = DreamStore(tmp_path / "dreams.db")

    store.record_conference_decision(_decision(dream_id=1, reason="about one"))
    store.record_conference_decision(_decision(dream_id=2, reason="about two"))

    assert [d.reason for d in store.conference_decisions(1)] == ["about one"]
    assert len(store.conference_decisions()) == 2


def test_a_verdict_word_this_version_cannot_read_is_skipped_and_never_coerced(
    tmp_path,
):
    """Forgiving on read like every other row here — and note what is NOT done.

    Nothing defaults to `NO_DECISION` and no unknown word is mapped to the
    nearest known one. A verdict nobody can read rendered as "they settled
    nothing" is a plausible wrong statement about what two agents did, which is
    the coercion trap `str()` on an SDK enum already sprang once on the order
    status.
    """
    path = tmp_path / "dreams.db"
    store = DreamStore(path)
    store.record_conference_decision(_decision(reason="readable"))

    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO conference_decisions (dream_id, at, verdict, decided_by,"
        " reason, effected, effect_detail, undecided, turns)"
        " VALUES (1, '2026-06-02T09:00:00+00:00', 'escalate', 'trader', 'x',"
        " NULL, '', '', 2)"
    )
    conn.commit()
    conn.close()

    read = store.conference_decisions(1)

    assert [d.reason for d in read] == ["readable"]


def test_a_stored_deferral_whose_wake_condition_is_gone_is_skipped_on_read(tmp_path):
    """The same guard on the way out as on the way in.

    A row written by hand, or by a version that allowed it, would otherwise
    render as a deferral with nothing to wake it — which is exactly the shape
    this record exists to make impossible.
    """
    path = tmp_path / "dreams.db"
    store = DreamStore(path)

    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO conference_decisions (dream_id, at, verdict, decided_by,"
        " reason, wake, effected, effect_detail, undecided, turns)"
        " VALUES (1, '2026-06-02T09:00:00+00:00', 'defer', 'trader', 'x', NULL,"
        " NULL, '', '', 2)"
    )
    conn.commit()
    conn.close()

    assert store.conference_decisions(1) == []


def test_effected_keeps_its_three_answers_through_the_store(tmp_path):
    """`None` is "this verdict asks nothing of any shelf" and is not `False`.

    Stored as a nullable INTEGER for that reason. A column defaulting to 0 would
    turn every decline into a decision the store had refused.
    """
    store = DreamStore(tmp_path / "dreams.db")
    at = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)

    store.record_conference_decision(
        _decision(dream_id=1, at=at, verdict=ConferenceVerdict.PROMOTE, effected=True)
    )
    store.record_conference_decision(
        _decision(
            dream_id=2,
            at=at,
            verdict=ConferenceVerdict.PROMOTE,
            effected=False,
            effect_detail="the adopted shelf is full",
        )
    )
    store.record_conference_decision(_decision(dream_id=3, at=at))

    assert store.conference_decisions(1)[0].effected is True
    assert store.conference_decisions(2)[0].effected is False
    assert store.conference_decisions(3)[0].effected is None


def test_a_verdicts_prose_is_trimmed_rather_than_rejected(tmp_path):
    """The `TEXT_MAX_CHARS` rule, in a new place. Nothing downstream parses a
    reason, so losing the tail of a sentence costs a reader some context where a
    rejected write would cost the whole verdict."""
    store = DreamStore(tmp_path / "dreams.db")

    store.record_conference_decision(_decision(reason="x" * (TEXT_MAX_CHARS + 400)))

    stored = store.conference_decisions(1)[0]
    assert len(stored.reason) <= TEXT_MAX_CHARS + 1


# ------------------------------------------------- the migration, fourth time
#
# `conference_decisions` is a whole TABLE rather than a column, and that is the
# one case `CREATE TABLE IF NOT EXISTS` genuinely handles: the statement is a
# no-op on a table that exists and creates one that does not, on the droplet as
# much as in a `tmp_path`. "It needed no ALTER" is still a claim, so it is
# checked here rather than remembered — starting, as every migration test in
# this file does, from a database built without it.

PRE_DECISION_SCHEMA = """
CREATE TABLE IF NOT EXISTS dreams (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  title        TEXT NOT NULL,
  seed         TEXT NOT NULL,
  stage        TEXT NOT NULL,
  verdict      TEXT,
  weakest_hop  TEXT NOT NULL DEFAULT '',
  trigger_note TEXT NOT NULL DEFAULT '',
  origin       TEXT NOT NULL DEFAULT '',
  chain        TEXT NOT NULL DEFAULT '[]',
  thoughts     TEXT NOT NULL DEFAULT '[]',
  instruments  TEXT NOT NULL DEFAULT '[]',
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  vault           TEXT NOT NULL DEFAULT 'workbench',
  vault_entered_at TEXT NOT NULL DEFAULT '',
  conditions      TEXT NOT NULL DEFAULT '[]',
  symbols         TEXT NOT NULL DEFAULT '[]',
  asset_class_key TEXT NOT NULL DEFAULT '',
  wisp            TEXT NOT NULL DEFAULT '',
  parents              TEXT NOT NULL DEFAULT '[]',
  shared_hops          TEXT NOT NULL DEFAULT '[]',
  verification_ceiling TEXT NOT NULL DEFAULT '',
  weakest_hop_index    INTEGER
);
CREATE TABLE IF NOT EXISTS dream_messages (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  dream_id INTEGER NOT NULL,
  at       TEXT NOT NULL,
  speaker  TEXT NOT NULL,
  kind     TEXT NOT NULL DEFAULT 'note',
  text     TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS adoptions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  dream_id        INTEGER NOT NULL,
  adopted_at      TEXT NOT NULL,
  symbols_granted TEXT NOT NULL DEFAULT '[]',
  asset_class     TEXT NOT NULL DEFAULT '',
  returned_at     TEXT,
  return_reason   TEXT NOT NULL DEFAULT '',
  expires_at      TEXT
);
"""


def _pre_decision_store_with_a_row(path: Path) -> None:
    """A dream and a conversation, written before any verdict was recorded."""
    conn = sqlite3.connect(path)
    conn.executescript(PRE_DECISION_SCHEMA)
    conn.execute(
        "INSERT INTO dreams (title, seed, stage, verdict, weakest_hop,"
        " trigger_note, origin, chain, thoughts, instruments, created_at,"
        " updated_at, vault, vault_entered_at, conditions, symbols,"
        " asset_class_key, wisp, parents, shared_hops, verification_ceiling,"
        " weakest_hop_index)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "Drought and hydro",
            "the reservoir is at a decade low",
            "verdict",
            "keep",
            "whether the smelter can actually curtail",
            "the spot auction print",
            "a headline about rainfall",
            json.dumps([{"claim": "the reservoir is low", "checked": True,
                         "source": "the operator's own gauge"}]),
            json.dumps([{"stage": "verdict", "text": "it holds",
                         "at": "2026-07-01T09:00:00+00:00", "by": ""}]),
            json.dumps(["hydro"]),
            "2026-06-01T09:00:00+00:00",
            "2026-07-01T09:00:00+00:00",
            "vault",
            "2026-07-01T09:00:00+00:00",
            json.dumps([{"text": "SPY clears 600", "symbol": "SPY",
                         "field": "close", "op": "above", "value": 600.0,
                         "fulfilled": True,
                         "fulfilled_at": "2026-07-02T09:00:00+00:00"}]),
            json.dumps(["SPY"]),
            "us_equity",
            "",
            "[]",
            "[]",
            "",
            1,
        ),
    )
    conn.execute(
        "INSERT INTO dream_messages (dream_id, at, speaker, kind, text)"
        " VALUES (1, '2026-07-03T09:00:00+00:00', 'dreamer', 'offer', 'here it is')"
    )
    conn.commit()
    conn.close()


def test_a_database_that_predates_the_decision_record_gains_the_table_in_place(
    tmp_path,
):
    """Every existing row survives, and the new table is usable rather than
    merely present.

    The suite is structurally blind to a missing schema change — every other
    test in this file builds its store from scratch and therefore always gets
    the new shape — which is why this one starts from the shape the droplet is
    actually running.
    """
    path = tmp_path / "dreams.db"
    _pre_decision_store_with_a_row(path)

    store = DreamStore(path)

    dreams = store.recent()
    assert len(dreams) == 1
    assert dreams[0].title == "Drought and hydro"
    assert dreams[0].vault is Vault.VAULT
    assert [c.text for c in dreams[0].conditions] == ["SPY clears 600"]
    assert [m.text for m in store.messages(1)] == ["here it is"]

    # Nothing has been decided about it, and that is the honest answer for a
    # database written before verdicts were recorded — never a manufactured one.
    assert store.latest_conference_decision(1) is None

    written = store.record_conference_decision(
        _decision(reason="not while hop 2 is an assumption")
    )
    assert store.conference_decisions(1) == [written]


def test_the_decision_table_migration_ends_up_identical_to_a_fresh_database(tmp_path):
    old = tmp_path / "old.db"
    _pre_decision_store_with_a_row(old)
    DreamStore(old)
    DreamStore(tmp_path / "fresh.db")

    def columns(path: Path, table: str) -> list[str]:
        conn = sqlite3.connect(path)
        try:
            return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]
        finally:
            conn.close()

    assert columns(old, "conference_decisions") == columns(
        tmp_path / "fresh.db", "conference_decisions"
    )
    assert columns(old, "conference_decisions")


def test_opening_a_migrated_store_twice_keeps_the_verdicts(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` runs on every open, so it must be a no-op on
    the second one. A statement that dropped and recreated would lose the
    record."""
    path = tmp_path / "dreams.db"
    _pre_decision_store_with_a_row(path)

    DreamStore(path).record_conference_decision(_decision(reason="kept"))
    reopened = DreamStore(path)

    assert [d.reason for d in reopened.conference_decisions(1)] == ["kept"]
