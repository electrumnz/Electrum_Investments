"""The dreamer.

The first test in this file is the one that matters. Everything else is
bookkeeping about a store; that one is the reason the module is allowed to
exist next to a live order path.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bot.dreaming import (
    DREAMER,
    OPERATOR,
    TRADER,
    Dream,
    DreamCondition,
    DreamStage,
    DreamStore,
    DreamSummary,
    DreamVerdict,
    Hop,
    MoveRefusal,
    Vault,
    VaultCaps,
    VaultTTLs,
    Verification,
)
from bot.models import OrderProposal, TriggerField, TriggerOp

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
