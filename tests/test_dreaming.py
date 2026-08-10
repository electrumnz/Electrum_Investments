"""The dreamer.

The first test in this file is the one that matters. Everything else is
bookkeeping about a store; that one is the reason the module is allowed to
exist next to a live order path.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime

import pytest

from bot.dreaming import (
    Dream,
    DreamStage,
    DreamStore,
    DreamSummary,
    DreamVerdict,
    Hop,
    Verification,
)
from bot.models import OrderProposal

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
