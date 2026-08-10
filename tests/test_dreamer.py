"""The thing that actually dreams.

No test here touches the network. `Dreamer` takes its client, so the model call
is a stub that returns whatever the test wants — including nonsense, which is
the interesting case.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bot.claude_client import CallUsage
from bot.config import Env, Rules
from bot.dreamer import CARRY_FORWARD, Dreamer, DreamHop, DreamStep, build_prompt
from bot.dreaming import Dream, DreamStage, DreamStore, DreamVerdict, Hop
from bot.journal import Journal
from bot.models import Direction, Trade

ENTRY = datetime(2026, 5, 4, 15, 0, tzinfo=UTC)
USAGE = CallUsage(
    input_tokens=10, output_tokens=10, cache_read_tokens=0,
    cache_write_tokens=0, estimated_cost_usd=0.001,
)


def _env() -> Env:
    env = Env(_env_file=None)  # type: ignore[call-arg]
    env.anthropic_api_key = "test"
    return env


class _StubClient:
    """Returns a canned step, or raises."""

    def __init__(self, step: DreamStep | None = None, raises: Exception | None = None):
        self.step = step
        self.raises = raises
        self.prompts: list[str] = []

    def dream(self, prompt: str):
        self.prompts.append(prompt)
        if self.raises:
            raise self.raises
        return self.step, USAGE


@pytest.fixture
def store(tmp_path):
    return DreamStore(tmp_path / "dreams.db")


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "journal.db")


@pytest.fixture
def rules():
    return Rules.load(Path("config/rules.yaml"))


def _step(**kw: object) -> DreamStep:
    base: dict[str, object] = {
        "title": "Cicada broods and sesame",
        "seed": "Two of three producers inside overlapping ranges.",
        "stage": DreamStage.EXPLORE,
        "thought": "Who is downstream of this?",
    }
    base.update(kw)
    return DreamStep(**base)  # type: ignore[arg-type]


def _closed(journal: Journal, symbol: str, pnl: float) -> None:
    tid = journal.record_entry(
        Trade(
            symbol=symbol,
            strategy="mean_reversion",
            direction=Direction.BUY,
            qty=10,
            entry_time=ENTRY,
            entry_price=580.0,
            planned_stop=570.0,
            planned_target=600.0,
            rationale="A trade.",
        )
    )
    journal.record_exit(
        tid, exit_time=ENTRY + timedelta(hours=1), exit_price=590.0,
        realised_pnl_usd=pnl,
    )


# ------------------------------------------- the property that must not break


def test_the_prompt_never_shows_profit_and_loss(rules, journal):
    """The Alpha Arena rule, enforced here rather than requested in the soul.

    `souls/grogu.md` tells the dreamer not to learn from the track record. This
    is what makes that true: the figures never enter the prompt, so there is
    nothing to overfit to. What closed is given as an EVENT, never as a result.
    """
    _closed(journal, "SPY", 1234.56)
    _closed(journal, "AAPL", -987.65)

    prompt = build_prompt(rules, journal, [])

    # The symbols and the fact they closed are fine and useful.
    assert "SPY" in prompt
    # The outcomes are not.
    for leak in ("1234.56", "1,234.56", "987.65", "-987", "profit", "P&L", "win rate"):
        assert leak not in prompt, f"the dreamer was shown {leak!r}"


def test_the_prompt_says_closures_happened_without_saying_how_they_went(rules, journal):
    _closed(journal, "SPY", 500.0)

    prompt = build_prompt(rules, journal, [])

    assert "recently closed" in prompt
    assert "not what it earned" in prompt


# ------------------------------------------------------------------- prompt


def test_headlines_and_posts_reach_the_prompt(rules, journal):
    prompt = build_prompt(
        rules, journal, [],
        headlines=["Crop insurers raise premiums across the Midwest"],
        posts=["[@someone 14:31] shipping rates spiking"],
    )

    assert "Crop insurers raise premiums" in prompt
    assert "shipping rates spiking" in prompt


def test_open_dreams_are_offered_back_for_advancing(rules, journal):
    """Without this it is a stream of unrelated notions rather than projects."""
    dream = Dream(
        id=7, title="Brood overlap", seed="a spark",
        chain=[Hop("checked claim", True, "a source"), Hop("assumed claim")],
        weakest_hop="the overlap",
    )

    prompt = build_prompt(rules, journal, [dream], now=ENTRY)

    assert "[id 7] Brood overlap" in prompt
    assert "hop 1 (checked)" in prompt
    assert "hop 2 (UNCHECKED)" in prompt
    assert "weakest: the overlap" in prompt
    assert "Prefer advancing" in prompt


def test_the_age_of_a_dream_is_stated_not_implied(rules, journal):
    """Same reasoning as the decision loop's recall block: a three-day-old
    thought must not read like a fifteen-minute-old one."""
    dream = Dream(id=1, title="t", seed="s", updated_at=ENTRY - timedelta(days=3))

    prompt = build_prompt(rules, journal, [dream], now=ENTRY)

    assert "3 day(s) ago" in prompt


# -------------------------------------------------------------------- steps


def test_a_new_dream_is_written(rules, journal, store):
    client = _StubClient(_step(chain=[DreamHop(claim="a claim")]))
    dreamer = Dreamer(_env(), rules, store, journal, client=client)

    result = dreamer.run_once()

    assert result is not None
    assert result.advanced is False
    stored = store.recent()
    assert len(stored) == 1
    assert stored[0].title == "Cicada broods and sesame"
    assert [t.text for t in stored[0].thoughts] == ["Who is downstream of this?"]


def test_an_existing_dream_is_advanced_rather_than_duplicated(rules, journal, store):
    first = Dream(title="Brood overlap", seed="a spark")
    dream_id = store.save(first)

    client = _StubClient(
        _step(advance_id=dream_id, stage=DreamStage.ITERATE, thought="hop three is weak")
    )
    result = Dreamer(_env(), rules, store, journal, client=client).run_once()

    assert result is not None
    assert result.advanced is True
    assert len(store.recent()) == 1
    stored = store.get(dream_id)
    assert stored is not None
    assert stored.stage is DreamStage.ITERATE
    assert [t.text for t in stored.thoughts] == ["hop three is weak"]


def test_an_unknown_advance_id_starts_a_new_dream_rather_than_overwriting(
    rules, journal, store
):
    """A model returning an id for a row it was never offered must not be able
    to write over an unrelated dream."""
    store.save(Dream(title="Existing", seed="s"))

    client = _StubClient(_step(advance_id=9999, title="New one"))
    result = Dreamer(_env(), rules, store, journal, client=client).run_once()

    assert result is not None
    assert result.advanced is False
    assert {d.title for d in store.recent()} == {"Existing", "New one"}


def test_a_source_is_dropped_when_the_hop_is_not_checked(rules, journal, store):
    """An unchecked hop citing a source is a contradiction, and the honest half
    of it is the unchecked flag."""
    client = _StubClient(
        _step(chain=[DreamHop(claim="assumed", checked=False, source="somewhere")])
    )
    result = Dreamer(_env(), rules, store, journal, client=client).run_once()

    assert result is not None
    assert result.dream.chain[0].source == ""
    assert result.dream.chain[0].checked is False


def test_a_verdict_is_only_honoured_on_a_verdict_step(rules, journal, store):
    """A stray verdict on an explore step must not silently close a dream that
    is still running."""
    client = _StubClient(
        _step(stage=DreamStage.EXPLORE, verdict=DreamVerdict.DROP)
    )
    result = Dreamer(_env(), rules, store, journal, client=client).run_once()

    assert result is not None
    assert result.dream.verdict is None
    assert result.dream.is_open


def test_a_verdict_step_closes_the_dream(rules, journal, store):
    client = _StubClient(
        _step(stage=DreamStage.VERDICT, verdict=DreamVerdict.DROP,
              thought="hop three broke")
    )
    result = Dreamer(_env(), rules, store, journal, client=client).run_once()

    assert result is not None
    assert result.dream.verdict is DreamVerdict.DROP
    assert not result.dream.is_open


def test_only_open_dreams_are_offered_and_the_list_is_capped(rules, journal, store):
    """A long list turns the choice into a survey, and a closed dream is done."""
    for i in range(CARRY_FORWARD + 4):
        store.save(Dream(title=f"open {i}", seed="s"))
    store.save(
        Dream(title="finished", seed="s", stage=DreamStage.VERDICT,
              verdict=DreamVerdict.DROP)
    )

    client = _StubClient(_step())
    Dreamer(_env(), rules, store, journal, client=client).run_once()

    prompt = client.prompts[0]
    assert "finished" not in prompt
    assert prompt.count("[id ") == CARRY_FORWARD


# ------------------------------------------------------------------ failure


@pytest.mark.parametrize(
    "boom",
    [RuntimeError("no parsable step"), ValueError("bad schema"), KeyError("surprise")],
)
def test_a_failed_call_writes_nothing_and_does_not_raise(rules, journal, store, boom):
    """Same shape as the decision loop's model call, learned the same way.

    A ValidationError escaping here would kill whatever timer drives this and
    restart straight into the same failure. And a dream that could not be had
    must not be recorded as one that decided nothing.
    """
    client = _StubClient(raises=boom)

    result = Dreamer(_env(), rules, store, journal, client=client).run_once()

    assert result is None
    assert store.recent() == []


def test_a_failure_leaves_an_existing_dream_untouched(rules, journal, store):
    store.save(Dream(title="Existing", seed="s", stage=DreamStage.EXPLORE))
    client = _StubClient(raises=RuntimeError("boom"))

    Dreamer(_env(), rules, store, journal, client=client).run_once()

    survivor = store.recent()[0]
    assert survivor.title == "Existing"
    assert survivor.stage is DreamStage.EXPLORE
    assert survivor.thoughts == []
