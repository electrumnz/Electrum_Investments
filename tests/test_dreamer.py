"""The thing that actually dreams.

No test here touches the network. `Dreamer` takes its client, so the model call
is a stub that returns whatever the test wants — including nonsense, which is
the interesting case.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from bot import news_history
from bot.audit import AuditLog
from bot.config import Env, Rules
from bot.data.news import Sourced
from bot.dreamer import (
    CARRY_FORWARD,
    SCOPE,
    SYSTEM_PROMPT,
    Dreamer,
    DreamHop,
    DreamStep,
    StepCondition,
    build_prompt,
    class_key_for_symbol,
    promote_dreams,
    render_class_fence,
    scope_symbols,
)
from bot.dreaming import (
    DREAMER,
    OPERATOR,
    Dream,
    DreamCondition,
    DreamStage,
    DreamStore,
    DreamVerdict,
    FusionResult,
    Hop,
    MoveRefusal,
    Vault,
    Verification,
    fusion_candidates,
    promotion_for,
)
from bot.journal import Journal
from bot.model_client import CallUsage
from bot.models import (
    Direction,
    IndicatorSnapshot,
    Trade,
    TriggerField,
    TriggerOp,
)
from bot.news_history import (
    Consideration,
    ConsiderationRecall,
    describe_age,
    recall_considerations,
)
from bot.triggers import CycleReadings

ENTRY = datetime(2026, 5, 4, 15, 0, tzinfo=UTC)
# A second fixed moment, for the consideration tests at the bottom. Its own
# constant rather than ENTRY because those turn on ages measured in hours across
# a UTC midnight, and reusing the entry stamp would tie the two sets together
# for no reason.
NOW = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)
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


def test_the_prompt_tells_the_truth_about_what_a_verdict_DOES() -> None:
    """It used to say something false, and the shelf stalled for a fortnight.

    The old text read *"Only a `verdict` step carries a dream off the
    workbench"* and offered park as *"come back to it later"*. Both are wrong:
    `promotion_for` refuses anything but a KEEP at its first clause, and nothing
    in this repository revisits a parked chain — no sweep, no schedule, no
    later step unless the dreamer picks that dream again itself.

    Measured on the live shelf 27 Aug 2026: three chains parked at `verdict`
    for between 9.5 and 13.5 days, prophecy 0/12, vault 0/12, and `confer`
    reporting `considered: 0` on every day it had ever run. The machinery was
    correct throughout; the model had been told a falsehood about it.
    """
    assert "Only a `keep` moves a dream anywhere" in SYSTEM_PROMPT

    # The two false claims, pinned so neither can come back.
    assert "Only a `verdict` step carries a dream off the workbench" not in SYSTEM_PROMPT
    assert "park it if you genuinely want to come back to it" not in SYSTEM_PROMPT


def test_the_prompt_does_not_ask_the_dreamer_to_keep_MORE() -> None:
    """The fix is a correction, never a thumb on the scale.

    A prompt that pushed towards `keep` would manufacture promotions, which is
    the failure this whole repository is built to refuse — a keep is what goes
    in front of the agent that trades. So the truth about park is stated AND
    the pressure is explicitly disclaimed.
    """
    assert "NOT being asked to keep more" in SYSTEM_PROMPT

    # And it states the MECHANISM rather than a dated snapshot of the shelf. A
    # draft of this said "three chains have sat parked for nine to fourteen
    # days" — true when written, and the system prompt is built once at loop
    # start and cached for an hour, so it would have gone on asserting a stale
    # count long after the shelf cleared. Interpolated state does not belong in
    # the cached half; that is the grant block's rule arriving somewhere new.
    assert "nine and fourteen days" not in SYSTEM_PROMPT
    assert "three chains" not in SYSTEM_PROMPT
    assert "A `keep` that does not hold is worse" in SYSTEM_PROMPT
    # `drop` must stay a first-class outcome rather than becoming the loser's
    # option beside a newly-encouraged keep.
    assert "`drop` is a perfectly good one" in SYSTEM_PROMPT


def test_the_prompt_tells_the_truth_about_where_a_KEEP_GOES() -> None:
    """The second false claim about the verdict, and the one that deadlocked it.

    The prompt described `keep` as *"the chain holds, and it goes in front of
    the trading agent"*. `promotion_for` does not do that. A keep whose
    conditions are unmet goes to the PROPHECY shelf, which the trading agent
    cannot see; only `all_conditions_met` reaches `Vault.VAULT`.

    So a model holding unanswered observations on its own weakest hop was being
    asked to certify something it correctly would not certify, and parked
    instead — every time. Measured on the droplet 25-29 Aug 2026: dream 3 and
    dream 4 both parked at `verdict`, `promoted: []` on every run since the
    shelf existed, and `confer` reporting `considered: 0` every day.

    The correction is a statement about the MECHANISM, not encouragement:
    `test_the_prompt_does_not_ask_the_dreamer_to_keep_MORE` still has to pass.
    """
    assert "does NOT go straight to the trading agent" in SYSTEM_PROMPT
    assert "PROPHECY shelf" in SYSTEM_PROMPT
    # A keep is a claim about worth-settling, never about having proved it.
    assert "not a claim that the chain has been proved" in SYSTEM_PROMPT
    # And the normal state of a fresh keep is named, so an unsettled weakest
    # hop stops reading as a reason to park.
    assert "not a reason to park" in SYSTEM_PROMPT

    # The false claim itself, pinned so it cannot come back in either place.
    assert "it goes in front of the trading agent" not in SYSTEM_PROMPT
    assert "a keep goes in front of the agent that trades" not in SYSTEM_PROMPT


def test_the_prompt_says_answering_an_observation_is_what_frees_a_KEPT_dream() -> None:
    """`self_settled: 0` on every run while `awaiting_a_look` sat at 3.

    The other half of the same deadlock. `settle_own_observations` reads the
    `answer` field off a restated condition, and across five consecutive live
    runs no `dream_self_settled` line was ever logged -- not even a refusal,
    which is how we know the field was never filled rather than mis-matched.

    A kept dream with an open observation waits on the dreamer and on nothing
    else, so the prompt has to say that plainly.
    """
    assert "no person is coming" in SYSTEM_PROMPT
    assert "AWAITING" in SYSTEM_PROMPT and "OVERDUE" in SYSTEM_PROMPT


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
    assert "weakest (WHICH HOP NOT ESTABLISHED): the overlap" in prompt
    assert "Prefer advancing" in prompt


def test_the_weakest_hop_is_rendered_with_its_NUMBER_when_one_is_known(rules, journal):
    """The sentence is what a person reads; the number is what the rule uses.

    Rendered together because the dreamer has to be able to see which hop the
    promotion rule will look at — a sentence on its own is exactly what it
    already writes, and exactly what code cannot act on.
    """
    dream = Dream(
        id=7, title="Brood overlap", seed="a spark",
        chain=[Hop("checked claim", True, "a source"), Hop("assumed claim")],
        weakest_hop="the overlap",
        weakest_hop_index=2,
    )

    prompt = build_prompt(rules, journal, [dream], now=ENTRY)

    assert "weakest (hop 2): the overlap" in prompt


def test_a_condition_says_which_hop_it_settles_and_says_when_it_settles_none(
    rules, journal
):
    """The absence is stated rather than left blank.

    An unpinned condition is the reason a finished keep sits on the workbench,
    so a renderer that showed nothing there would hide the one field that has
    to change — the same reason `symbols_without_history` is named on the
    loop's cycle line rather than dropped.
    """
    dream = Dream(
        id=7, title="Brood overlap", seed="a spark",
        chain=[Hop("checked claim", True, "a source"), Hop("assumed claim")],
        weakest_hop="the overlap",
        weakest_hop_index=2,
        conditions=[
            DreamCondition(text="pinned to the weak link", settles_hops=(2,)),
            DreamCondition(text="pinned to nothing"),
        ],
    )

    prompt = build_prompt(rules, journal, [dream], now=ENTRY)

    assert "pinned to the weak link [nothing pre-registered] [hop 2]" in prompt
    assert "pinned to nothing [nothing pre-registered] [settles no hop yet]" in prompt


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


def test_an_unknown_advance_id_writes_nothing_at_all(rules, journal, store):
    """**The duplicate factory, closed.** This test used to assert the opposite.

    The old rule was "a model returning an id for a row it was never offered
    must not write over an unrelated dream", and the implementation was
    `dream = target or Dream(...)` — refuse the overwrite, create a new dream
    instead. The refusal half is right and is kept. The fallback half was
    quietly wrong, and it took two live runs to see why.

    A step that names an `advance_id` INTENDED to continue an existing chain,
    so its title and seed describe that chain. Turning it into a new dream
    therefore usually writes a DUPLICATE of the very row it was reaching for.
    Observed on the droplet: it asked for id 1 (a dropped chain, so not
    offered) and the run created dream 3 carrying the identical title to dream
    2 — the same dream twice on one shelf, with the observation worklist
    doubling behind it. That is the shelf-stacking the operator reported.

    So nothing is written. An unusable step is recoverable on the next pass; a
    polluted shelf is cleaned up by hand.

    **What is still not done is picking a dream on the model's behalf**, even
    when exactly one was offered and the intent looks obvious. That is code
    choosing what to think about, and a step attributed to a chain nobody
    selected is worse than a step discarded.
    """
    store.save(Dream(title="Existing", seed="s"))

    client = _StubClient(_step(advance_id=9999, title="New one"))
    result = Dreamer(_env(), rules, store, journal, client=client).run_once()

    # A step that could not be applied is not recorded as one that decided
    # nothing — same shape as a failed model call.
    assert result is None
    # And above all: no second row, and the existing one untouched.
    assert {d.title for d in store.recent()} == {"Existing"}


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


def test_a_checked_hop_that_names_no_source_is_stored_UNCHECKED(
    rules, journal, store
):
    """**The mirror of the test above, and the one that was missing.**

    The same contradiction points the other way and used to be taken at its
    most flattering reading. Measured through a real step: a three-hop chain
    where every hop claimed `checked=True` with an empty source came back
    `verification: sourced` — the badge rendered into the trading model's
    prompt beside the chain, on an argument citing nothing.

    The badge is arithmetic over `checked` flags precisely because a model
    asked to rate its own sourcing rates it generously. A flag with nothing
    behind it IS that rating, arriving through the field next to it.
    """
    client = _StubClient(
        _step(chain=[DreamHop(claim="a matter of public record", checked=True,
                              source="   ")])
    )
    result = Dreamer(_env(), rules, store, journal, client=client).run_once()

    assert result is not None
    assert result.dream.chain[0].checked is False
    assert result.dream.chain[0].source == ""
    assert result.dream.verification is not Verification.SOURCED


def test_a_sourceless_chain_cannot_skip_the_weakest_hop_clause(
    rules, journal, store
):
    """The consequence, which is worth more than the badge.

    `promotion_for` only demands a pinned weakest hop while the chain
    `awaits_settlement` — correctly, since a chain with every link nailed down
    has no link awaiting anything. A model that marks every hop checked
    without sourcing one therefore turned that clause off for itself and
    reached the prophecy shelf naming no weakest hop and pinning no condition
    to one, which is exactly what the operator added the clause to prevent.
    """
    client = _StubClient(
        _step(
            stage=DreamStage.VERDICT,
            verdict=DreamVerdict.KEEP,
            chain=[
                DreamHop(claim="one", checked=True, source=""),
                DreamHop(claim="two", checked=True, source=""),
            ],
            weakest_hop="",
            weakest_hop_index=None,
            conditions=[
                StepCondition(
                    text="the export share rises",
                    subject="the USDA oilseed circular",
                    observable="shows the third producer above a fifth",
                    observe_by="2027-03-01",
                )
            ],
        )
    )
    result = Dreamer(_env(), rules, store, journal, client=client).run_once()

    assert result is not None
    assert result.dream.awaits_settlement is True
    assert promotion_for(result.dream).to is None
    assert "weakest" in promotion_for(result.dream).reason


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


# --------------------------------------------- the fence round the instruments
#
# The operator's rule: the dreamer may look OUTSIDE `allowed_symbols` to other
# Alpaca instruments, and may not go around the hard block on a GROUP of them.
# Two locks, and only one of them is here — `grants.resolve_granted_symbols`
# enforces the same block deterministically at permission time. This one is the
# earlier, friendlier half.


def test_a_symbol_outside_the_watch_list_is_kept_when_its_class_is_enabled(rules):
    """The whole point of the feature. `allowed_symbols` is six names; the
    dreamer is allowed to be interested in a seventh."""
    scope = scope_symbols(["NVDA", "TSLA"], rules)

    assert scope.kept == ("NVDA", "TSLA")
    assert scope.dropped == ()
    assert scope.asset_class_key == "us_equity"


def test_a_symbol_in_a_disabled_class_is_dropped(rules):
    """Crypto is off in the shipped config, so `BTC/USD` is not the dreamer's to
    name — however good the chain that reaches it."""
    scope = scope_symbols(["SPY", "BTC/USD"], rules)

    assert scope.kept == ("SPY",)
    assert [symbol for symbol, _ in scope.dropped] == ["BTC/USD"]
    assert "crypto" in scope.dropped[0][1]


def test_enabling_a_class_admits_it_and_disabling_it_shuts_it_again(rules):
    """'If crypto is enabled the dreamer should see this, and vice versa.' The
    fence is derived from `enabled_instruments` rather than from a list here, so
    it follows the config in both directions with no second edit."""
    assert scope_symbols(["ETH/USD"], rules).kept == ()

    opened = rules.model_copy(deep=True)
    opened.instruments["crypto"].enabled = True

    assert scope_symbols(["ETH/USD"], opened).kept == ("ETH/USD",)


def test_a_dropped_symbol_is_recorded_rather_than_silently_discarded(rules):
    """A filter that left no trace is indistinguishable from a model that had
    simply stopped naming symbols."""
    scope = scope_symbols(["BTC/USD"], rules)

    assert "Dropped 1 symbol(s)" in scope.summary
    assert "BTC/USD" in scope.summary
    assert scope_symbols(["SPY"], rules).summary == ""


def test_a_dream_spanning_two_classes_resolves_to_no_class_at_all(rules):
    """Unresolved grants nothing. Picking one of two would be choosing which
    risk cap applies by accident — the same reason `granted_symbols` drops a
    symbol claimed by two live grants rather than resolving it."""
    opened = rules.model_copy(deep=True)
    opened.instruments["crypto"].enabled = True

    scope = scope_symbols(["SPY", "BTC/USD"], opened)

    assert scope.kept == ("SPY", "BTC/USD")
    assert scope.asset_class_key == ""


def test_symbols_are_normalised_without_being_truncated(rules):
    """Structural, so it is cleaned and never trimmed: a truncated symbol is a
    different symbol, exactly as a truncated price is a different price."""
    scope = scope_symbols([" spy ", "SPY", "", "qqq"], rules)

    assert scope.kept == ("SPY", "QQQ")


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("BTC/USD", "crypto"),
        ("SPY", "us_equity"),
        ("NVDA", "us_equity"),
        ("SPY260918C00500000", "us_option"),
        ("", ""),
    ],
)
def test_a_symbols_class_is_read_from_its_shape(symbol, expected):
    """A shape test rather than a lookup, because the point of the feature is
    that the dreamer may name symbols nobody has listed anywhere. A table of
    known symbols would refuse exactly the case this exists to permit."""
    assert class_key_for_symbol(symbol) == expected


def test_an_option_contract_is_dropped_because_the_bot_trades_no_options(rules):
    """`us_option` is not an `instruments:` key in the shipped config, so the
    fence answers this one without anybody having written a rule for it."""
    scope = scope_symbols(["SPY260918C00500000"], rules)

    assert scope.kept == ()
    assert "us_option" in scope.dropped[0][1]


def test_the_prompt_names_the_enabled_and_the_blocked_classes(rules, journal):
    """The opposite of what `build_system_prompt` does for the decision loop,
    and deliberate in both places. There, naming a class the bot cannot trade
    only invites proposals for it. Here the permission is 'look outside the
    watch list, inside the fence', so the dreamer has to be able to see it."""
    prompt = build_prompt(rules, journal, [])

    assert "us_equity — ENABLED" in prompt
    assert "crypto — BLOCKED" in prompt
    assert "not on the watch list" in prompt


def test_a_blocked_symbol_never_reaches_the_stored_dream(rules, journal, store):
    """The refusal happens at the point of STORAGE, not at the point of use. A
    dream that reached the vault naming a crypto pair would be an offer the
    trading agent could accept."""
    client = _StubClient(_step(symbols=["SPY", "BTC/USD"]))

    result = Dreamer(_env(), rules, store, journal, client=client).run_once()

    assert result is not None
    assert result.dream.symbols == ["SPY"]
    assert result.dream.asset_class_key == "us_equity"
    assert [s for s, _ in result.scope.dropped] == ["BTC/USD"]

    stored = store.recent()[0]
    assert stored.symbols == ["SPY"]


def test_the_drop_is_written_into_the_dreams_own_transcript(rules, journal, store):
    """Where the question 'why does this dream name nothing' actually gets
    asked. The speaker is neither agent, so `confer.last_agent_turn_at` cannot
    read it as a turn of anybody's conversation."""
    client = _StubClient(_step(symbols=["BTC/USD"]))

    result = Dreamer(_env(), rules, store, journal, client=client).run_once()

    assert result is not None
    notes = store.messages(int(result.dream.id or 0))
    assert [m.speaker for m in notes] == [SCOPE]
    assert "BTC/USD" in notes[0].text


def test_a_dream_that_names_nothing_writes_no_note(rules, journal, store):
    client = _StubClient(_step())

    result = Dreamer(_env(), rules, store, journal, client=client).run_once()

    assert result is not None
    assert result.dream.symbols == []
    assert store.messages(int(result.dream.id or 0)) == []


def test_widening_a_dream_across_classes_clears_the_class_rather_than_keeping_it(
    rules, journal, store
):
    """A permission described by a claim that is no longer true is worse than no
    permission. Unresolved is refused by `DreamStore.adopt` and dropped by
    `granted_symbols`, which is the direction to fail in."""
    opened = rules.model_copy(deep=True)
    opened.instruments["crypto"].enabled = True

    dream_id = store.save(
        Dream(title="t", seed="s", symbols=["SPY"], asset_class_key="us_equity")
    )
    client = _StubClient(_step(advance_id=dream_id, symbols=["SPY", "BTC/USD"]))

    Dreamer(_env(), opened, store, journal, client=client).run_once()

    stored = store.get(dream_id)
    assert stored is not None
    assert stored.asset_class_key == ""


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


# ------------------------------------------------------------ the CLI wiring


def test_the_dream_command_feeds_it_headlines_and_posts(tmp_path, monkeypatch):
    """The dreamer accepts headlines and posts; the CLI has to actually pass
    them. It did not at first, so the module was wired to a feed that never
    ran and "learns from news" was untrue in the only place it mattered.
    """
    import bot.main as main_mod

    seen: dict[str, object] = {}

    class _Feed:
        is_degraded = False

        def recent_headlines(self, symbols):
            return [h.line for h in self.recent_attributed(symbols)]  # type: ignore[no-untyped-call]

        def recent_attributed(self, symbols):
            # A double that answers only the loop's method would let a change
            # to the DREAMER's feed path pass unnoticed, which is the trap
            # `Broker.orders_degraded` already recorded: a stub that fails
            # differently from the thing it doubles pins a path production
            # never takes. `cmd_dream` reads this one.
            return [
                Sourced(
                    line="Crop insurers raise premiums across the Midwest",
                    url="https://example.com/crop-insurers",
                    publisher="Example Wire",
                )
            ]

        def recent_posts(self):
            return []

    class _Dreamer:
        def __init__(self, *a, **kw):
            pass

        def run_once(self, *, headlines=None, posts=None, now=None):
            seen["headlines"] = headlines
            seen["posts"] = posts
            return None  # a failed call: writes nothing, exits non-zero

    monkeypatch.setattr(main_mod, "build_news_feed", lambda env: _Feed())
    monkeypatch.setattr(main_mod, "build_social_feed", lambda env, rules: None)
    monkeypatch.setattr(main_mod, "Journal", lambda *a, **kw: object())
    monkeypatch.setattr("bot.dreamer.Dreamer", _Dreamer)
    # Patched where `main` binds it, not where it is defined. `cmd_dream` used
    # to import the store inside the function; it now shares the module-level
    # name the decision loop resolves grants through, so patching
    # `bot.dreaming.DreamStore` would no longer intercept it — and the symptom
    # is a real `data/dreams.db` written beside the production journal.
    monkeypatch.setattr(main_mod, "DreamStore", lambda *a, **kw: object())

    env = _env()
    rc = main_mod.cmd_dream(env, Rules.load(Path("config/rules.yaml")))

    # WITH the attribution, which is the whole difference between this feed
    # path and the decision loop's. A dreamer marks a hop `checked` only when it
    # can name a source; a headline stripped of its publisher and URL is a story
    # it can reason from and cannot cite, and that is how a live chain came to
    # open with an unchecked hop about a story we had fetched ourselves.
    assert seen["headlines"] == [
        "Crop insurers raise premiums across the Midwest "
        "— Example Wire https://example.com/crop-insurers"
    ]
    assert seen["posts"] == []
    # A failed call exits non-zero so a timer unit surfaces it in
    # `systemctl --failed` rather than logging into the void.
    assert rc == 1


def test_the_dream_command_refuses_without_an_api_key(tmp_path):
    """Fail closed and say why, rather than a stack trace from the SDK."""
    import bot.main as main_mod

    env = Env(_env_file=None)  # type: ignore[call-arg]
    env.anthropic_api_key = ""

    assert main_mod.cmd_dream(env, Rules.load(Path("config/rules.yaml"))) == 1


# --------------------------------------------------------------- the tuning


def test_the_dreamer_does_not_cache_its_system_prompt(monkeypatch):
    """The 1h cache is a PENALTY at a daily cadence, not an optimisation.

    A cache write bills at 2x base input and a read at 0.1x, so a caller that
    always misses pays double the system block every single call. The loop wakes
    every fifteen minutes and gets roughly four reads per write, so it caches.
    The dreamer runs once a day, misses every time, and must not.

    Measured on the real ~2,400-token system block: $0.0095 a run cached-and-
    missing against $0.0071 uncached.
    """
    from bot.model_client import ModelClient

    captured: dict[str, object] = {}

    class _Messages:
        def parse(self, **kw):
            captured.update(kw)
            raise RuntimeError("stop here; we only wanted the kwargs")

    class _Inner:
        messages = _Messages()

        def with_options(self, **kw):
            captured["options"] = kw
            return self

    client = ModelClient(_env(), "system text", cache_system=False)
    monkeypatch.setattr(client, "_client", _Inner())

    with pytest.raises(RuntimeError):
        client.dream("a prompt")

    system = captured["system"]
    assert isinstance(system, list)
    assert "cache_control" not in system[0], "the dreamer must not pay a cache write"


def test_the_dream_call_is_bought_deep_rather_than_fast(monkeypatch):
    """Nothing waits on this call, and depth is the entire product.

    High effort, a large budget that the thinking pass counts against, and a
    timeout that outlasts it. A proposal-sized budget would truncate the chain
    the thinking was spent producing.

    **Bought deep is not bought unbounded**, which is why the retry count is
    asserted beside the timeout. The pair is the real ceiling: 900 seconds
    against the SDK's default of two retries was forty-five minutes of a dream
    timer occupied by a call that had already failed, with nothing on any
    surface saying so.
    """
    from bot.config import CLAUDE_MODEL_SPECS, ClaudeTier
    from bot.model_client import (
        DREAM_MAX_RETRIES,
        DREAM_MAX_TOKENS,
        DREAM_TIMEOUT_SECONDS,
        ModelClient,
    )

    captured: dict[str, object] = {}

    class _Messages:
        def parse(self, **kw):
            captured.update(kw)
            raise RuntimeError("stop")

    class _Inner:
        messages = _Messages()

        def with_options(self, **kw):
            captured["options"] = kw
            return self

    client = ModelClient(
        _env(), "system", spec=CLAUDE_MODEL_SPECS[ClaudeTier.SONNET], cache_system=False
    )
    monkeypatch.setattr(client, "_client", _Inner())

    with pytest.raises(RuntimeError):
        client.dream("a prompt")

    assert captured["max_tokens"] == DREAM_MAX_TOKENS
    assert captured["output_config"] == {"effort": "high"}
    assert captured["thinking"] == {"type": "adaptive"}
    assert captured["options"] == {
        "timeout": DREAM_TIMEOUT_SECONDS,
        "max_retries": DREAM_MAX_RETRIES,
    }
    # The worst case an operator can be left waiting, stated as arithmetic
    # rather than trusted to a constant nobody multiplies out.
    worst_case_seconds = DREAM_TIMEOUT_SECONDS * (DREAM_MAX_RETRIES + 1)
    assert worst_case_seconds <= 600, (
        f"a dream step that cannot complete would occupy the timer for "
        f"{worst_case_seconds:.0f}s; it must fail fast and loudly"
    )


def test_the_dreamer_does_not_inherit_a_tier_that_cannot_think():
    """Haiku has no extended thinking, and thinking is how a dream gets past its
    first hop. So the dreamer's tier does NOT fall back to CLAUDE_TIER."""
    from bot.config import ClaudeTier

    env = Env(_env_file=None)  # type: ignore[call-arg]
    env.claude_tier = ClaudeTier.HAIKU

    assert env.dream_tier is ClaudeTier.SONNET


def test_an_explicit_dream_tier_is_honoured():
    from bot.config import ClaudeTier

    env = Env(_env_file=None)  # type: ignore[call-arg]
    env.dream_claude_tier = ClaudeTier.OPUS

    assert env.dream_tier is ClaudeTier.OPUS


def test_a_thought_can_say_who_had_it():
    """Empty today, because there is one dreamer. It exists now because several
    dreamers arguing a topic out is the intended direction, and a debate whose
    transcript cannot say who said what is not a transcript.

    The store is append-only and never migrated, so the field has to arrive with
    a default or every row written before it would stop loading.
    """
    from bot.dreaming import Dream, DreamStage, Thought

    dream = Dream(title="t", seed="s")
    dream.add_thought(DreamStage.EXPLORE, "who is downstream", by="grogu")

    assert dream.thoughts[0].by == "grogu"
    # A row written before the field existed still loads.
    old_row = {"stage": "explore", "text": "older thought", "at": ENTRY.isoformat()}
    assert Thought.from_row(old_row).by == ""


# ------------------------------------------------------------- the schedule


def test_the_schedule_is_read_from_the_unit_rather_than_hardcoded(tmp_path):
    """A Settings screen holding its own copy of a cadence keeps announcing the
    old one forever after somebody edits the timer on the box."""
    from bot.dreamer import read_schedule

    unit = tmp_path / "mudhorn-dream.timer"
    unit.write_text(
        "[Timer]\n# a comment\nOnCalendar=*-*-* 07:00:00 Pacific/Auckland\n"
        "Persistent=true\n"
    )

    schedule = read_schedule(installed=unit, repo=tmp_path / "nope.timer")

    assert schedule.calendar == "*-*-* 07:00:00 Pacific/Auckland"
    assert schedule.installed is True
    assert schedule.state == "installed"


def test_a_repo_only_unit_is_not_reported_as_installed(tmp_path):
    """A unit in the checkout is an intention, not a running schedule.

    Collapsing the two would put a confident "daily at 07:00" on the page for a
    box where the timer was never installed.
    """
    from bot.dreamer import read_schedule

    repo = tmp_path / "repo.timer"
    repo.write_text("[Timer]\nOnCalendar=*-*-* 07:00:00 Pacific/Auckland\n")

    schedule = read_schedule(installed=tmp_path / "absent.timer", repo=repo)

    assert schedule.found is True
    assert schedule.installed is False
    assert schedule.state == "in the repo, not installed"


def test_no_unit_anywhere_says_so_rather_than_guessing(tmp_path):
    from bot.dreamer import read_schedule

    schedule = read_schedule(installed=tmp_path / "a", repo=tmp_path / "b")

    assert schedule.found is False
    assert schedule.calendar == ""
    assert schedule.state == "no timer unit found"


def test_an_unreadable_unit_does_not_raise(tmp_path):
    """This is called while rendering Settings. A permissions problem on a
    deploy file must cost the card its contents, not the page."""
    from bot.dreamer import read_schedule

    directory = tmp_path / "a-directory.timer"
    directory.mkdir()

    schedule = read_schedule(installed=directory, repo=tmp_path / "absent")

    assert schedule.found is False


def test_the_shipped_timer_is_in_new_zealand_time():
    """Named zone, never a converted UTC hour.

    New Zealand observes daylight saving, so a hardcoded UTC time drifts by an
    hour twice a year and drifts silently. The suffix makes systemd do the
    arithmetic on every elapse.
    """
    from pathlib import Path

    from bot.dreamer import read_schedule

    schedule = read_schedule(
        installed=Path("does-not-exist"),
        repo=Path("deploy/systemd/mudhorn-dream.timer"),
    )

    assert "Pacific/Auckland" in schedule.calendar


def test_the_cost_estimate_reflects_the_model():
    """Haiku has no thinking pass, so it is far cheaper and far shallower."""
    from bot.config import CLAUDE_MODEL_SPECS, ClaudeTier
    from bot.dreamer import estimated_cost_usd

    haiku = estimated_cost_usd(CLAUDE_MODEL_SPECS[ClaudeTier.HAIKU])
    sonnet = estimated_cost_usd(CLAUDE_MODEL_SPECS[ClaudeTier.SONNET])
    opus = estimated_cost_usd(CLAUDE_MODEL_SPECS[ClaudeTier.OPUS])
    assert haiku is not None and sonnet is not None and opus is not None

    haiku_run, haiku_year = haiku
    sonnet_run, sonnet_year = sonnet
    opus_run, _ = opus

    assert haiku_run < sonnet_run < opus_run
    assert haiku_year == pytest.approx(haiku_run * 365)
    # A year of daily dreaming stays in double digits on the shipped tier.
    assert sonnet_year < 100


def test_a_model_with_no_prices_on_file_estimates_nothing_rather_than_zero():
    """`~$0.000 a run` would be the cheapest figure on the Settings page.

    The estimate used to key on the tier twice over — once for the price table
    and once for `0 if HAIKU else 4_000` — so it could describe one of three
    Claude models and had no way to express a fourth at all. A model nobody has
    priced now returns `None`, and the page says "unknown" rather than drawing a
    number nobody measured beside numbers that were.
    """
    from bot.config import ModelSpec
    from bot.dreamer import estimated_cost_usd

    assert estimated_cost_usd(ModelSpec(model_id="some-open-model")) is None


# ------------------------------------------------ asking for the two fields
#
# Three real dreams generated against the live model came back with
# `symbols: []` and `symbols_dropped: 0` — nothing was filtered, the field was
# simply never filled — and no conditions at all. Between them that left the
# vault permanently empty and the whole permission path inert, so the prompt has
# to ASK, and asking well is the whole fix.


def test_the_prompt_asks_for_symbols_and_says_empty_is_a_good_answer(rules, journal):
    """Both halves, because only one of them is the risk.

    A field that is merely demanded gets filled, and a dreamer that invents a
    ticker to satisfy a schema produces the confident-plausible-value failure
    this repository exists to refuse. The prompt has to make an empty list a
    respectable answer in the same breath as it asks for one.
    """
    prompt = build_prompt(rules, journal, [])

    assert "`symbols`" in prompt
    assert "leave it empty" in prompt
    assert "respectable answer" in prompt


def test_the_prompt_asks_for_the_bridge_as_a_HOP_not_as_a_bare_ticker(rules, journal):
    """The second-order move the dreamer exists for.

    The thing a good dream is about — a private supplier, a co-operative, a
    commodity — usually is not listed. The job is then to name the listed
    instrument whose fortunes that thing moves, and THAT step is a claim like any
    other: it is the hop most likely to be wrong and the one nobody checks,
    because it arrives looking like bookkeeping rather than an argument.
    """
    prompt = build_prompt(rules, journal, [])

    assert "bridge" in prompt
    assert "write that bridge as a hop" in prompt


def test_the_fence_says_a_symbol_must_be_something_the_broker_can_route(rules, journal):
    """Reasoning is unrestricted; naming is not.

    `scope_symbols` drops anything outside an enabled class silently from the
    model's point of view, so the fence has to state what will survive storage.
    """
    fence = "\n".join(render_class_fence(rules))

    assert "broker can route" in fence
    assert "private company" in fence
    assert "`instruments`" in fence


def test_the_prompt_asks_for_conditions_and_says_a_keep_without_one_goes_nowhere(
    rules, journal
):
    """The promotion rule, stated where the model can act on it.

    A keep with no checkable condition stays on the workbench for ever. Left
    unsaid, the model has no way to know its conclusions reach nobody.
    """
    prompt = build_prompt(rules, journal, [])

    assert "`conditions`" in prompt
    assert "never leaves the workbench" in prompt
    assert "never the name of another figure" in prompt


def test_the_prompt_asks_for_the_weakest_hop_to_be_PINNED(rules, journal):
    """The operator's correction, stated where the model can act on it.

    `promotion_for` refuses a keep whose conditions all settle links nobody
    doubted. Left unsaid, the dreamer has no way to know why a finished chain
    with a perfectly good number never leaves the workbench — it would rewrite
    the number rather than pin it.
    """
    prompt = build_prompt(rules, journal, [])

    assert "`weakest_hop_index`" in prompt
    assert "`settles_hops`" in prompt
    assert "parked awaiting the link that could kill it" in prompt


def test_the_prompt_still_says_the_threshold_is_a_number(rules, journal):
    """The pin does not replace the older rule; it makes it matter more.

    "Above the 20-day" re-checked next month tests a level nobody ever saw, and
    the condition carrying the weakest hop is the one that has to still mean
    something months later — a prophecy's TTL is 365 days.
    """
    from bot.dreamer import SYSTEM_PROMPT

    assert "never the name of another figure" in SYSTEM_PROMPT
    assert "settles nothing" in SYSTEM_PROMPT


def test_an_advancing_step_is_shown_the_conditions_already_on_the_dream(
    rules, journal, store
):
    """Or it restates them from scratch every time, blind to what has fired."""
    dream = Dream(
        title="t",
        seed="s",
        conditions=[
            DreamCondition(
                text="Alcoa clears 100",
                symbol="AA",
                field=TriggerField.CLOSE,
                op=TriggerOp.ABOVE,
                value=100.0,
                fulfilled=True,
            )
        ],
        symbols=["AA"],
    )
    dream.id = store.save(dream)

    prompt = build_prompt(rules, journal, [store.get(dream.id)])

    assert "condition (MET): Alcoa clears 100" in prompt
    assert "symbols claimed: AA" in prompt


# ------------------------------------------------- answering its own questions


def _observing_step(**kw: object) -> DreamStep:
    """A step carrying one observation, with whatever answer the test wants."""
    condition: dict[str, object] = {
        "text": "Century Aluminum restarts the idled potline",
        "subject": "Century Aluminum's quarterly production release",
        "observable": "Mt. Holly named as restarting",
        "observe_by": "2027-03-01",
        "settles_hops": [1],
    }
    condition.update(kw)
    return _step(
        chain=[DreamHop(claim="the potline comes back")],
        weakest_hop="the restart",
        weakest_hop_index=1,
        conditions=[StepCondition(**condition)],  # type: ignore[arg-type]
    )


def test_the_dreamer_answers_its_own_observation(rules, store, journal):
    """**Grogu settles its own, and nothing waits on a person.**

    The operator's instruction — *"grogu is his own soul, his own agent, the
    only standard communication we have is A2A"* — reversing a rule that left
    six questions on a dashboard card with no way to answer any of them.

    End to end through `run_once`, because the interesting part is the wiring:
    the step is saved with the condition unanswered, and `settle_own_observations`
    settles it afterwards through the store's own writer.
    """
    step = _observing_step(answer="met", answer_note="Q4 release names Mt. Holly.")
    dreamer = Dreamer(_env(), rules, store, journal, client=_StubClient(step))

    result = dreamer.run_once()

    assert result is not None
    condition = result.dream.conditions[0]
    assert condition.fulfilled is True
    # The ACTOR, never a constant. A claim Grogu answered for itself and one a
    # person looked at are different facts, and the trading agent is shown
    # which it is before it decides whether to adopt.
    assert condition.observed_by == DREAMER
    assert condition.note == "Q4 release names Mt. Holly."
    assert result.settled.met == ("Century Aluminum's quarterly production release",)

    # And the dream it is returned on is the SAVED one, not the stale copy the
    # run was holding before the settlement was written.
    stored = store.get(int(result.dream.id or 0))
    assert stored is not None
    assert stored.conditions[0].fulfilled is True


def test_the_dreamer_can_rule_out_its_own_observation(rules, store, journal):
    """`ruled_out` is a real answer and the more valuable one.

    A mechanism that can only record a yes is a mechanism that manufactures
    confirmations — and a dreamer refuting its own hop is the most useful thing
    it can do on a review step.
    """
    step = _observing_step(
        answer="ruled_out", answer_note="Q4 release: potline stays idle."
    )
    dreamer = Dreamer(_env(), rules, store, journal, client=_StubClient(step))

    result = dreamer.run_once()

    assert result is not None
    condition = result.dream.conditions[0]
    assert condition.ruled_out is True
    assert condition.fulfilled is False
    assert condition.is_answered is True
    assert result.settled.ruled_out == (
        "Century Aluminum's quarterly production release",
    )


def test_an_answer_with_no_note_is_refused_and_leaves_the_claim_open(
    rules, store, journal
):
    """The note is the rail that survived, and it binds the dreamer.

    It is the only record of WHAT was looked at, so an answer without one is
    indistinguishable afterwards from an invention. `settle_condition` refuses
    it — this is not enforced in the dreamer, which is the point: one writer,
    one rule, whoever is answering.
    """
    step = _observing_step(answer="met", answer_note="   ")
    dreamer = Dreamer(_env(), rules, store, journal, client=_StubClient(step))

    result = dreamer.run_once()

    assert result is not None
    assert result.dream.conditions[0].is_answered is False
    # Refused rather than swallowed. A dreamer whose answers are all being
    # rejected is a fact worth having.
    assert len(result.settled.refused) == 1
    assert result.settled.answered == 0


def test_an_unparseable_answer_leaves_the_condition_exactly_as_it_was(
    rules, store, journal
):
    """`_ANSWERS` is closed, and a value outside it is not guessed at.

    An unanswered observation costs a dream time; a mis-parsed one costs it a
    shelf. Same direction as `_observe_by` turning an unreadable date into
    `None` rather than into today.
    """
    step = _observing_step(answer="probably", answer_note="it seems likely")
    dreamer = Dreamer(_env(), rules, store, journal, client=_StubClient(step))

    result = dreamer.run_once()

    assert result is not None
    assert result.dream.conditions[0].is_answered is False
    # Not even recorded as a refusal: nothing was ever put to the store.
    assert not result.settled


def test_a_threshold_is_not_the_dreamers_to_answer(rules, store, journal):
    """Code settles those against recorded figures.

    A model answering one by hand would be a reading nobody can check, which is
    the `indicators.py` rule arriving through the condition list.
    """
    step = _step(
        chain=[DreamHop(claim="one")],
        conditions=[
            StepCondition(
                text="Alcoa clears 100",
                symbol="AA",
                field=TriggerField.CLOSE,
                op=TriggerOp.ABOVE,
                value=100.0,
                answer="met",
                answer_note="it went above",
            )
        ],
    )
    dreamer = Dreamer(_env(), rules, store, journal, client=_StubClient(step))

    result = dreamer.run_once()

    assert result is not None
    assert result.dream.conditions[0].is_answered is False
    assert len(result.settled.refused) == 1


def test_a_self_answered_observation_carries_the_dream_to_the_vault(
    rules, store, journal
):
    """The consequence, stated: this is what allowing it BUYS.

    An observation nobody answers holds its dream below the vault for ever,
    where the trading agent never sees it. That was the live state — the
    prophecy shelf filling while every surface reported patience.
    """
    step = _observing_step(answer="met", answer_note="Q4 release names Mt. Holly.")
    step.verdict = DreamVerdict.KEEP
    step.stage = DreamStage.VERDICT
    dreamer = Dreamer(_env(), rules, store, journal, client=_StubClient(step))

    result = dreamer.run_once()
    assert result is not None
    dream_id = int(result.dream.id or 0)

    promote_dreams(store)

    moved = store.get(dream_id)
    assert moved is not None
    assert moved.vault is Vault.VAULT


# ------------------------------------------------------ folding them in safely


def test_a_step_writes_its_conditions_onto_the_dream(rules, store, journal):
    step = _step(
        conditions=[
            StepCondition(
                text="Alcoa clears 100",
                symbol="aa",
                field=TriggerField.CLOSE,
                op=TriggerOp.ABOVE,
                value=100.0,
            )
        ]
    )
    dreamer = Dreamer(_env(), rules, store, journal, client=_StubClient(step))

    result = dreamer.run_once()

    assert result is not None
    condition = result.dream.conditions[0]
    assert condition.symbol == "AA"  # normalised on the way in
    assert condition.is_gradeable is True


def test_a_step_pins_its_conditions_to_the_hops_they_settle(rules, store, journal):
    """Both halves of the pin travel from the model onto the dream.

    Without this the promotion rule is unreachable: nothing else in the process
    can say which hop a threshold bears on, and a rule nothing can satisfy is a
    rule that silently empties a shelf.
    """
    step = _step(
        chain=[DreamHop(claim="one"), DreamHop(claim="two"), DreamHop(claim="three")],
        weakest_hop="the third link",
        weakest_hop_index=3,
        conditions=[
            StepCondition(
                text="Alcoa clears 100",
                symbol="AA",
                settles_hops=[3],
                field=TriggerField.CLOSE,
                op=TriggerOp.ABOVE,
                value=100.0,
            )
        ],
    )
    dreamer = Dreamer(_env(), rules, store, journal, client=_StubClient(step))

    result = dreamer.run_once()

    assert result is not None
    assert result.dream.weakest_hop_index == 3
    assert result.dream.resolved_weakest_hop == 3
    assert result.dream.conditions[0].settles_hops == (3,)
    assert result.dream.weakest_hop_is_pinned is True


def test_a_pin_outside_the_chain_is_dropped_rather_than_stored(rules, store, journal):
    """Structural, so it is cleaned and never clamped.

    A pin at hop 0 or past the end names no link, and one stored anyway would be
    reported as covering the weakest hop while naming nothing — the plausible
    wrong figure, arriving as a hop number.
    """
    step = _step(
        chain=[DreamHop(claim="one"), DreamHop(claim="two")],
        conditions=[
            StepCondition(text="prose", settles_hops=[0, 5, 2, 2, -1]),
        ],
    )
    dreamer = Dreamer(_env(), rules, store, journal, client=_StubClient(step))

    result = dreamer.run_once()

    assert result is not None
    assert result.dream.conditions[0].settles_hops == (2,)


def test_a_new_weakest_hop_sentence_clears_the_OLD_number(rules, store, journal):
    """They move together or the rule reads a stale answer.

    A fresh sentence with the previous step's index still attached would pin
    promotion to whichever hop the LAST step thought was weakest, while the
    dream on screen claims a different one. Clearing it costs a run and is
    recoverable; keeping it is wrong silently.
    """
    dream = Dream(
        title="t", seed="s",
        chain=[Hop("one"), Hop("two"), Hop("three")],
        weakest_hop="the third link", weakest_hop_index=3,
    )
    dream_id = store.save(dream)
    step = _step(advance_id=dream_id, weakest_hop="actually the second link")
    dreamer = Dreamer(_env(), rules, store, journal, client=_StubClient(step))

    result = dreamer.run_once()

    assert result is not None
    assert result.dream.weakest_hop == "actually the second link"
    assert result.dream.weakest_hop_index is None


def test_a_number_on_its_own_fixes_a_sentence_already_on_the_dream(
    rules, store, journal
):
    """The case where a step is repairing exactly what promotion refused it for."""
    dream = Dream(
        title="t", seed="s",
        chain=[Hop("one"), Hop("two")],
        weakest_hop="a paraphrase matching no hop",
    )
    dream_id = store.save(dream)
    step = _step(advance_id=dream_id, weakest_hop_index=2)
    dreamer = Dreamer(_env(), rules, store, journal, client=_StubClient(step))

    result = dreamer.run_once()

    assert result is not None
    assert result.dream.weakest_hop == "a paraphrase matching no hop"
    assert result.dream.resolved_weakest_hop == 2


def test_restating_a_condition_on_a_later_step_does_not_wipe_its_grade(
    rules, store, journal
):
    """**Verified to fail without `carry_forward_grading`.**

    The vault is reached by `all_conditions_met`. If a restated condition came
    back unfulfilled, a dream that restates its conditions on every step could
    never be promoted at all, and would be re-checked for ever against readings
    that had already fired it.
    """
    graded = DreamCondition(
        text="Alcoa clears 100",
        symbol="AA",
        field=TriggerField.CLOSE,
        op=TriggerOp.ABOVE,
        value=100.0,
        fulfilled=True,
    )
    dream = Dream(title="t", seed="s", conditions=[graded])
    dream_id = store.save(dream)

    step = _step(
        advance_id=dream_id,
        conditions=[
            StepCondition(
                text="Alcoa finally clears the hundred handle",
                symbol="AA",
                field=TriggerField.CLOSE,
                op=TriggerOp.ABOVE,
                value=100.0,
            )
        ],
    )
    dreamer = Dreamer(_env(), rules, store, journal, client=_StubClient(step))
    dreamer.run_once()

    reloaded = store.get(dream_id)
    assert reloaded is not None
    assert reloaded.conditions[0].fulfilled is True


# ------------------------------- the condition shape a supply-chain hop can use


def test_a_step_cannot_answer_its_own_condition():
    """**The structural half of the operator-settled condition.**

    A fulfilled condition can carry a dream to the vault, a vaulted dream can
    be adopted, and an adoption is a live permission to trade a symbol in no
    allowlist. So a model able to say "this one is met" would be a model able to
    write itself a permission — and the guarantee is that there is no field here
    it could say it with, not that it has been asked not to. Same shape as
    `test_a_dream_cannot_describe_an_order`.
    """
    answers = {"fulfilled", "fulfilled_at", "observed_by", "ruled_out", "note"}

    assert set(StepCondition.model_fields) & answers == set()


def test_a_step_writes_an_observation_a_person_will_have_to_settle(
    rules, store, journal
):
    """The route that closes the conflict, end to end through a real step.

    No figure the loop records measures a smelter restart, so the model writes
    what to look at, what it must show and by when — and the dream reaches the
    prophecy shelf carrying no number at all.
    """
    step = _step(
        stage=DreamStage.VERDICT,
        verdict=DreamVerdict.KEEP,
        chain=[DreamHop(claim="one"), DreamHop(claim="two"), DreamHop(claim="three")],
        weakest_hop="the third link",
        weakest_hop_index=3,
        conditions=[
            StepCondition(
                text="the idled potline comes back",
                settles_hops=[3],
                subject="  Century Aluminum's quarterly production release  ",
                observable="reports the idled potline restarted",
                observe_by="2027-03-01",
            )
        ],
    )
    dreamer = Dreamer(_env(), rules, store, journal, client=_StubClient(step))

    result = dreamer.run_once()

    assert result is not None
    condition = result.dream.conditions[0]
    assert condition.subject == "Century Aluminum's quarterly production release"
    assert condition.value is None and condition.is_checkable is False
    assert condition.is_observable is True
    assert condition.is_pre_registered is True
    # A bare date means the END of that day: "by the 1st" is not a condition
    # that spends the whole of the 1st reported as overdue.
    assert condition.observe_by is not None
    assert condition.observe_by.date() == date(2027, 3, 1)
    assert condition.observe_by.hour == 23
    assert result.dream.weakest_hop_is_pinned is True
    assert promotion_for(result.dream).to is Vault.PROPHECY


def test_an_unreadable_review_date_leaves_the_dream_where_it_was(
    rules, store, journal
):
    """Parsed, never trusted, and never defaulted.

    "Next spring" cannot be a date, and taking `now` for it would put the
    question at the top of the operator's list as already overdue — a plausible
    wrong figure wearing a timestamp. Unreadable is no date, which makes it not
    an observation, which holds the dream exactly where the old rule held it.
    """
    step = _step(
        stage=DreamStage.VERDICT,
        verdict=DreamVerdict.KEEP,
        chain=[DreamHop(claim="one")],
        weakest_hop_index=1,
        conditions=[
            StepCondition(
                text="the idled potline comes back",
                settles_hops=[1],
                subject="Century Aluminum's production release",
                observable="reports the restart",
                observe_by="next spring",
            )
        ],
    )
    dreamer = Dreamer(_env(), rules, store, journal, client=_StubClient(step))

    result = dreamer.run_once()

    assert result is not None
    assert result.dream.conditions[0].observe_by is None
    assert result.dream.conditions[0].is_observable is False
    assert promotion_for(result.dream).to is None


def test_a_step_cannot_erase_an_operator_refusal_by_leaving_it_out(
    rules, store, journal
):
    """**The whole chain, and it ended in a live grant.**

    `carry_forward_grading` already refused to let a RESTATED claim erase an
    answer. Omission was the entrance nobody had walked: the applied list was
    exactly what the step returned, so a claim the step did not mention was
    deleted — and a ruled-out condition is precisely what stops
    `all_conditions_met` carrying a dream into the vault the trading agent can
    see. Measured before the fix: operator says no, the next step returns the
    list minus that claim, and the dream reaches ADOPTED with
    `granted_symbols` handing out a symbol in no allowlist, with
    `ruled_out_conditions` empty so the absence was invisible too.

    A model settling its own condition is refused by name in
    `settle_condition`. Un-asking one by silence has to be refused as well, or
    the refusal is a formality.
    """
    keep = DreamCondition(
        text="the brood map is published",
        subject="the USGS periodical cicada brood map",
        observable="shows Brood XIV over two of the three producing counties",
        observe_by=datetime(2027, 3, 1, tzinfo=UTC),
        settles_hops=(1,),
    )
    refused = DreamCondition(
        text="the idled potline comes back",
        subject="Century Aluminum's quarterly production release",
        observable="reports the idled potline restarted",
        observe_by=datetime(2027, 3, 1, tzinfo=UTC),
    )
    dream_id = store.save(
        Dream(
            title="t",
            seed="s",
            stage=DreamStage.EXPLORE,
            chain=[Hop(claim="cicadas emerge over two of three", checked=False)],
            weakest_hop="cicadas emerge over two of three",
            weakest_hop_index=1,
            conditions=[keep, refused],
            symbols=["ZZZZ"],
            asset_class_key="us_equity",
        )
    )
    store.settle_condition(dream_id, keep.key, by=OPERATOR, met=True, note="published")
    store.settle_condition(
        dream_id, refused.key, by=OPERATOR, met=False, note="restart cancelled"
    )

    # The step restates the list and simply does not mention the refused claim.
    step = _step(
        advance_id=dream_id,
        conditions=[
            StepCondition(
                text="the brood map is published",
                settles_hops=[1],
                subject="the USGS periodical cicada brood map",
                observable="shows Brood XIV over two of the three producing counties",
                observe_by="2027-03-01",
            )
        ],
    )
    Dreamer(_env(), rules, store, journal, client=_StubClient(step)).run_once()

    after = store.get(dream_id)
    assert after is not None
    assert [c.text for c in after.ruled_out_conditions] == [refused.text]
    assert after.all_conditions_met is False
    # And the consequence, stated rather than inferred: nothing can be adopted
    # off it, so no symbol is granted.
    assert store.promote(dream_id).moved_to is not Vault.VAULT
    assert store.adopt(dream_id).refused
    assert store.granted_symbols(datetime.now(UTC)) == {}


def test_the_prompt_shows_an_observation_with_its_date_and_its_state(
    rules, journal
):
    """The dreamer has to see what it already asked for, and its state.

    "Nobody has looked yet" and "a person looked and the answer was no" are
    opposite facts. A model shown one word for both would keep re-proposing a
    chain whose weakest link has been refuted.
    """
    dream = Dream(
        id=9,
        title="The idled potline",
        seed="a spark",
        chain=[Hop("one"), Hop("two")],
        weakest_hop_index=2,
        conditions=[
            DreamCondition(
                text="the idled potline comes back",
                subject="Century Aluminum's production release",
                observable="reports the restart",
                observe_by=datetime(2026, 3, 1, tzinfo=UTC),
                settles_hops=(2,),
            ),
            DreamCondition(
                text="the tariff is filed",
                subject="the state utility docket",
                observable="carries a new industrial tariff",
                observe_by=datetime(2026, 3, 1, tzinfo=UTC),
                ruled_out=True,
                observed_by=OPERATOR,
            ),
        ],
    )

    prompt = build_prompt(rules, journal, [dream], now=ENTRY)

    assert "OVERDUE" in prompt
    assert "RULED_OUT" in prompt
    assert "observation: Century Aluminum's production release" in prompt
    assert "due 2026-03-01" in prompt


# ------------------------------------------------------------- promote_dreams


def _prophecy_ready(**kw) -> Dream:
    base = {
        "title": "Smelters",
        "seed": "s",
        "verdict": DreamVerdict.KEEP,
        "conditions": [
            DreamCondition(
                text="Alcoa clears 100",
                symbol="AA",
                field=TriggerField.CLOSE,
                op=TriggerOp.ABOVE,
                value=100.0,
            )
        ],
    }
    base.update(kw)
    return Dream(**base)  # type: ignore[arg-type]


def test_promote_dreams_moves_a_finished_keep_off_the_workbench(store):
    """The step that was missing. Without it the vault is permanently empty."""
    dream_id = store.save(_prophecy_ready())

    run = promote_dreams(store)

    assert run.considered == 1
    assert run.promoted == ((dream_id, str(Vault.PROPHECY)),)


def test_promote_dreams_counts_the_questions_waiting_on_a_person(store):
    """A still shelf has two explanations, and the log line has to say which.

    A prophecy waiting on an observation looks, from the log, exactly like one
    whose figures have not moved. The count is deliberately taken WITH NO
    READINGS too: an observation does not need any, so a counter that only ran
    inside the grading branch would report zero questions outstanding at exactly
    the moment nothing else could move either — which is the `cycles_available`
    lesson in the same function.
    """
    store.save(
        _prophecy_ready(
            vault=Vault.PROPHECY,
            conditions=[
                DreamCondition(
                    text="the idled potline comes back",
                    subject="Century Aluminum's production release",
                    observable="reports the restart",
                    observe_by=datetime(2027, 3, 1, tzinfo=UTC),
                )
            ],
        )
    )

    assert promote_dreams(store).awaiting_a_look == 1
    assert (
        promote_dreams(
            store,
            readings=[CycleReadings(at=datetime(2026, 6, 2, tzinfo=UTC), readings={})],
        ).awaiting_a_look
        == 1
    )


def test_promote_dreams_grades_before_it_promotes(store):
    """A condition that fires on this pass moves the dream on this pass.

    The other order holds every prophecy back a full day for no reason — the
    command runs daily.
    """
    dream_id = store.save(_prophecy_ready(vault=Vault.PROPHECY))
    fired = [
        CycleReadings(
            at=datetime(2026, 6, 2, tzinfo=UTC),
            readings={"AA": IndicatorSnapshot(close=140.0)},
        )
    ]

    run = promote_dreams(store, readings=fired)

    assert run.conditions_fulfilled == 1
    assert run.promoted == ((dream_id, str(Vault.VAULT)),)
    assert [d.id for d in store.in_vault(Vault.VAULT)] == [dream_id]


def test_promote_dreams_reports_what_it_looked_at_even_when_nothing_moved(store):
    """`considered` separates "nothing was promotable" from "nothing was looked
    at" — the `has_cycles` rule, and the fact the original bug hid behind."""
    store.save(Dream(title="still thinking", seed="s"))

    run = promote_dreams(store)

    assert run.considered == 1
    assert run.promoted == ()
    assert run.held and "workbench" in run.held[0][1]


def test_promote_dreams_says_when_it_had_no_readings_to_grade_against(store):
    """With no recorded cycles nothing can fire, and that is a fact about the
    audit log rather than about the prophecies. A zero explains an unchanging
    shelf."""
    store.save(_prophecy_ready(vault=Vault.PROPHECY))

    assert promote_dreams(store).cycles_available == 0


# ============================================ symbiosis: arithmetic proposes,
# the dreamer confirms, and nothing combines unattended.


def _pair(store: DreamStore) -> tuple[int, int]:
    """Two dreams that share a hop, on the workbench."""
    a = store.save(
        Dream(
            title="Drought and hydro",
            seed="the reservoir is at a decade low",
            chain=[
                Hop("the reservoir is at a decade low", True, "the gauge"),
                Hop("that region's power gets scarce and dear", True, "auction prints"),
            ],
            symbols=["SPY"],
            asset_class_key="us_equity",
        )
    )
    b = store.save(
        Dream(
            title="Smelters chase cheap power",
            seed="the marginal smelter moves for a cent",
            chain=[
                Hop("that region's power gets scarce and dear"),
                Hop("the marginal smelter curtails first"),
            ],
            symbols=["QQQ"],
            asset_class_key="us_equity",
        )
    )
    return a, b


def test_the_prompt_offers_the_dreams_that_share_a_hop(rules, journal, store):
    """The shared hop is rendered verbatim rather than summarised, because it is
    the thing being asked about — a paraphrase would ask the model to confirm a
    match it cannot see."""
    a, b = _pair(store)

    prompt = build_prompt(
        rules, journal, [], fusions=fusion_candidates(store.recent()), now=ENTRY
    )

    assert f"[id {a}] Drought and hydro" in prompt
    assert f"[id {b}] Smelters chase cheap power" in prompt
    assert "shared hop: that region's power gets scarce and dear" in prompt
    assert "cannot improve either one's verification" in prompt


def test_a_prompt_with_no_candidates_says_nothing_about_fusing(rules, journal):
    """An empty heading is noise, and noise in a prompt is paid for on every
    run."""
    prompt = build_prompt(rules, journal, [], now=ENTRY)

    assert "shared hop" not in prompt


def test_a_step_that_asks_to_fuse_writes_the_fusion_and_no_second_dream(
    rules, store, journal
):
    """The step's title, seed and thought describe the CHILD, so folding the
    same text into an ordinary dream as well would put one idea on the workbench
    twice."""
    a, b = _pair(store)
    step = _step(
        fuse_ids=[a, b],
        title="Drought, power and the marginal smelter",
        thought="Both routes arrive at the same scarce megawatt.",
    )
    dreamer = Dreamer(_env(), rules, store, journal, client=_StubClient(step))

    result = dreamer.run_once(now=ENTRY)

    assert result is not None
    assert result.fusion is not None and result.fusion.ok
    assert result.advanced is False
    assert result.dream.is_fusion
    assert result.dream.parents == [a, b]
    assert result.dream.title == "Drought, power and the marginal smelter"
    # Three dreams in the store, not four: the two parents and the one child.
    assert len(store.recent()) == 3
    assert [t.text for t in result.dream.thoughts] == [
        "Both routes arrive at the same scarce megawatt."
    ]


def test_fuse_ids_are_looked_up_in_what_was_offered_and_never_trusted(
    rules, store, journal
):
    """The `advance_id` rule, and here it is sharper: two unrelated chains
    combined on a hallucinated id would produce a dream carrying both their
    symbols, which is a permission assembled out of an invented number."""
    a, b = _pair(store)
    step = _step(fuse_ids=[a, 4242], title="not this")
    dreamer = Dreamer(_env(), rules, store, journal, client=_StubClient(step))

    result = dreamer.run_once(now=ENTRY)

    assert result is not None
    # Nothing was fused, and the run still produced its ordinary step.
    assert result.fusion is None
    assert result.dream.is_fusion is False
    assert store.children_of(a) == [] and store.children_of(b) == []


def test_a_refused_fusion_keeps_the_step_and_reports_the_refusal(
    rules, store, journal
):
    """A full workbench must not cost the thought the model just had — and a
    silent refusal would be indistinguishable from a model that stopped
    suggesting them.

    The cap comes from the rules FILE rather than the dataclass default, so the
    limit an operator can read is the one that applies. Same miss as
    `Conference._caps` was written for.
    """
    a, b = _pair(store)
    rules.dreaming.caps.workbench = 2
    step = _step(fuse_ids=[a, b])
    dreamer = Dreamer(_env(), rules, store, journal, client=_StubClient(step))

    result = dreamer.run_once(now=ENTRY)

    assert result is not None
    assert result.dream.is_fusion is False  # the ordinary step still ran
    assert result.fusion is not None and result.fusion.refused
    assert MoveRefusal.FULL in result.fusion.refusals
    assert store.children_of(a) == []


def test_a_fusion_cannot_carry_a_symbol_out_of_a_class_since_disabled(
    rules, store, journal
):
    """The class fence, applied to the union on the way in. A parent written
    while crypto was enabled must not smuggle its symbol into a child written
    after it was switched off — and `DreamStore.fuse` refuses anything wider
    than the union anyway, which is the lock that does not depend on this."""
    a, b = _pair(store)
    crypto = store.get(b)
    crypto.symbols = ["BTC/USD"]
    crypto.asset_class_key = "crypto"
    store.save(crypto)
    assert "crypto" not in rules.enabled_instruments

    step = _step(fuse_ids=[a, b])
    dreamer = Dreamer(_env(), rules, store, journal, client=_StubClient(step))
    result = dreamer.run_once(now=ENTRY)

    assert result is not None
    assert result.fusion is not None and result.fusion.ok
    assert result.dream.symbols == ["SPY"]
    assert [s for s, _ in result.scope.dropped] == ["BTC/USD"]
    # And the drop is on the record beside the dream, not only in the log.
    notes = [m for m in store.messages(int(result.dream.id or 0)) if m.speaker == SCOPE]
    assert notes and "BTC/USD" in notes[0].text


def test_most_runs_fuse_nothing(rules, store, journal):
    """Empty is the normal answer, and an ordinary step must not pay for the
    feature existing."""
    _pair(store)
    dreamer = Dreamer(_env(), rules, store, journal, client=_StubClient(_step()))

    result = dreamer.run_once(now=ENTRY)

    assert result is not None
    assert result.fusion is None
    assert result.dream.is_fusion is False


def test_the_system_prompt_says_fusing_does_not_improve_verification(rules):
    """The one thing a model would otherwise assume. Combining is an argument,
    not evidence, and a fused chain that read as better sourced than its parents
    would be the Alpha Arena failure arriving through the merge."""
    from bot.dreamer import SYSTEM_PROMPT

    assert "Two unverified chains do not" in SYSTEM_PROMPT
    assert "**The parents survive.**" in SYSTEM_PROMPT
    assert "It is harder to promote, not easier." in SYSTEM_PROMPT
    assert "Combining is an argument, not evidence." in SYSTEM_PROMPT


# ----------------------------------------------- raised in conversation
#
# `raise_consideration` writes ONE line to the audit log and never opens
# `data/dreams.db`. This is the other half: the dreamer reads those lines into
# its prompt as candidate sparks it may ignore.
#
# The reader itself lives in `news_history` — pure functions over an `AuditView`,
# beside the recall that answers the same shape of question about headlines — and
# is exercised from here because the property being defended is the dreamer's:
# a conversation must not be able to insert at the top of the chain that ends in
# a live trading permission.


def _raise_consideration(
    audit_dir: Path,
    *,
    minutes_ago: int,
    speaker: str = "grogu",
    spark: str = "Rare-earth refining sits in three plants.",
    why_now: str = "The operator asked about magnet supply.",
    prompted_by: str = "",
    prompt_echo: float | None = None,
    kind: str = "chat_consideration",
    payload: dict[str, object] | None = None,
) -> datetime:
    """Write one consideration into a dated log file, at a chosen age.

    Raw JSONL rather than `AuditLog.record_event`, because that stamps the line
    with the wall clock and the age is the thing under test here. The file name
    is the UTC date so `AuditLog.files()` orders it correctly.
    """
    at = NOW - timedelta(minutes=minutes_ago)
    body = payload if payload is not None else {
        "origin": f"chat:{speaker}",
        "speaker": speaker,
        "spark": spark,
        "why_now": why_now,
        "prompted_by": prompted_by,
        "prompt_echo": prompt_echo,
    }
    path = audit_dir / f"{at.date().isoformat()}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"kind": kind, "timestamp": at.isoformat(), "payload": body})
            + "\n"
        )
    return at


def _recall(audit_dir: Path, **kw) -> ConsiderationRecall:
    return recall_considerations(AuditLog(audit_dir).read(), now=NOW, **kw)


def test_the_reader_and_the_writer_agree_on_the_event_kind():
    """One string named in two modules is two places that can disagree.

    The literal is duplicated rather than imported so `news_history` stays pure
    functions over an `AuditView` instead of importing a whole MCP tool surface.
    That trade is only safe with this test under it — a renamed event on either
    side would otherwise leave the dreamer reading a kind nobody writes, and the
    symptom is a section that is quietly always empty.
    """
    from bot import mcp_server

    assert news_history.CONSIDERATION_EVENT == mcp_server.CONSIDERATION_EVENT


def test_a_consideration_is_read_back_out_of_the_audit_log(tmp_path):
    _raise_consideration(tmp_path, minutes_ago=30, prompted_by="what about magnets?")

    result = _recall(tmp_path)

    assert len(result.considerations) == 1
    item = result.considerations[0]
    assert item.speaker == "grogu"
    assert item.spark == "Rare-earth refining sits in three plants."
    assert item.prompted_by == "what about magnets?"
    assert item.age_minutes(NOW) == pytest.approx(30.0)


def test_the_reader_walks_dated_files_rather_than_todays_only(tmp_path):
    """`get_recent_decisions` shipped reading today's file alone, so its recall
    emptied at UTC midnight and after every restart. The window here is two days
    and crosses one midnight by construction."""
    _raise_consideration(tmp_path, minutes_ago=20 * 60, spark="Yesterday's note")
    _raise_consideration(tmp_path, minutes_ago=5, spark="This morning's note")

    result = _recall(tmp_path)

    assert [c.spark for c in result.considerations] == [
        "This morning's note",
        "Yesterday's note",
    ]


def test_a_consideration_outside_the_window_is_not_offered(tmp_path):
    """Anything older arriving under the heading "raised in conversation" is
    being presented as current when it is not."""
    _raise_consideration(tmp_path, minutes_ago=5 * 24 * 60, spark="Ancient")

    assert _recall(tmp_path).considerations == []


def test_a_consideration_already_shown_is_not_offered_again(tmp_path):
    """Once considered, a note must not be put up forever. A dreamer asked the
    same thing daily until it agreed is the operator steering it by accident."""
    at = _raise_consideration(tmp_path, minutes_ago=200, spark="Seen already")
    _raise_consideration(tmp_path, minutes_ago=10, spark="Not seen yet")
    _raise_consideration(
        tmp_path,
        minutes_ago=100,
        kind="dream_considerations_seen",
        payload={"shown": [at.isoformat()], "count": 1},
    )

    result = _recall(tmp_path)

    assert [c.spark for c in result.considerations] == ["Not seen yet"]
    # And the one already shown is COUNTED rather than forgotten: "nothing new"
    # and "nobody said anything" are different findings.
    assert result.seen_previously == 1


def test_the_marker_names_what_was_shown_and_nothing_else(tmp_path):
    """The seen record is the exact set that was rendered, never a high-water
    stamp. A stamp would mark as seen anything that arrived while the model was
    thinking, and anything a limit had trimmed off the bottom of the list —
    `seen.py`'s rule with a second door."""
    older = _raise_consideration(tmp_path, minutes_ago=300, spark="A")
    newer = _raise_consideration(tmp_path, minutes_ago=60, spark="B")
    # Only the newer one was ever displayed.
    _raise_consideration(
        tmp_path,
        minutes_ago=30,
        kind="dream_considerations_seen",
        payload={"shown": [newer.isoformat()], "count": 1},
    )

    result = _recall(tmp_path)

    assert [c.spark for c in result.considerations] == ["A"]
    assert result.considerations[0].at == older


def test_a_row_with_no_spark_is_counted_rather_than_dropped_in_silence(tmp_path):
    """Impossible through the tool, which refuses a blank spark deterministically.
    A hand-edited log is the case, and a row dropped in silence is exactly how
    "nothing was raised" becomes the wrong answer."""
    _raise_consideration(
        tmp_path, minutes_ago=10, payload={"speaker": "grogu", "spark": "   "}
    )

    result = _recall(tmp_path)

    assert result.considerations == []
    assert result.rows_without_a_spark == 1


def test_nothing_recorded_at_all_is_not_a_quiet_week(tmp_path):
    """The `has_cycles` rule. An empty log means the box was off or this is a
    fresh deployment; it says nothing whatever about whether anybody had an
    idea."""
    result = _recall(tmp_path)

    assert result.considerations == []
    assert result.has_record is False


def test_a_window_with_other_records_but_no_considerations_is_a_real_answer(tmp_path):
    """The other side of the same coin, and the reason `has_record` exists: the
    log was being written and nobody put anything up. That IS a quiet week."""
    AuditLog(tmp_path).record_event("loop_start", {})

    result = _recall(tmp_path)

    assert result.considerations == []
    assert result.has_record is True
    assert result.is_degraded is False


def test_an_unreadable_line_makes_the_reading_degraded(tmp_path):
    """A log that could not be fully read produces the same empty list as a quiet
    week, and only one of them is a fact about the world."""
    _raise_consideration(tmp_path, minutes_ago=10)
    (tmp_path / f"{NOW.date().isoformat()}.jsonl").open("a").write("{not json\n")

    result = _recall(tmp_path)

    assert result.is_degraded is True
    assert result.malformed_lines == 1


def test_what_the_limit_drops_is_named_and_stays_unseen(tmp_path):
    """A trimmed list must not be marked as considered. `shown_keys` is exactly
    what was rendered, so what the limit dropped comes back next run."""
    for minutes in (10, 20, 30):
        _raise_consideration(tmp_path, minutes_ago=minutes, spark=f"note {minutes}")

    result = _recall(tmp_path, limit=2)

    assert [c.spark for c in result.considerations] == ["note 10", "note 20"]
    assert result.omitted_for_limit == 1
    assert len(result.shown_keys) == 2


@pytest.mark.parametrize(
    "minutes,expected",
    [
        (0.4, "just now"),
        (25, "25 minutes ago"),
        (240, "4 hours ago"),
        (60 * 30, "30 hours ago"),
        (60 * 40, "2 days ago"),
    ],
)
def test_an_age_is_stated_in_a_unit_a_reader_can_act_on(minutes, expected):
    assert describe_age(minutes) == expected


# ------------------------------------------------------ the prompt section


def _one(**kw) -> ConsiderationRecall:
    base = dict(
        considerations=[
            Consideration(
                at=NOW - timedelta(hours=6),
                speaker="grogu",
                spark="Rare-earth refining sits in three plants.",
                why_now="A magnet maker just guided down.",
                prompted_by="what happens to magnets if China restricts exports?",
                prompt_echo=0.4,
            )
        ],
        entries_in_window=4,
    )
    base.update(kw)
    return ConsiderationRecall(**base)  # type: ignore[arg-type]


def test_a_considerations_age_travels_with_it_into_the_prompt(rules, journal):
    """A spark raised six hours ago rendered with no time on it is being
    presented as something said just now — the confident-partial-answer failure
    arriving through the prompt rather than through a feed."""
    prompt = build_prompt(rules, journal, [], considerations=_one(), now=NOW)

    assert "[6 hours ago] grogu: Rare-earth refining sits in three plants." in prompt


def test_the_operators_own_words_are_marked_as_the_operators(rules, journal):
    """So the model can tell what a person said from what an agent said about it.
    Verbatim and on its own line: folded into the spark it would read as one
    voice, which is the judgement the quote exists to allow."""
    prompt = build_prompt(rules, journal, [], considerations=_one(), now=NOW)

    assert (
        'the operator said, verbatim: "what happens to magnets if China '
        'restricts exports?"' in prompt
    )
    assert "why now (grogu's words): A magnet maker just guided down." in prompt


def test_the_echo_ratio_is_not_shown_to_the_model(rules, journal):
    """A deliberate omission, pinned so it stays deliberate.

    `prompt_echo` exists so a READER can judge whether a spark is the operator's
    sentence given back, and both texts are already one line apart here, so this
    reader can do that directly. Printing the number as well invites a model to
    treat 0.4 as a threshold — the figure enforced by inference, which is exactly
    what `raise_consideration` refused to enforce in code.
    """
    prompt = build_prompt(rules, journal, [], considerations=_one(), now=NOW)

    assert "0.4" not in prompt
    assert "echo" not in prompt


def test_an_unprompted_consideration_says_so_rather_than_leaving_a_gap(rules, journal):
    """Nobody prompting it is a real state. A blank line there would read as a
    quote that failed to render."""
    recall = _one(
        considerations=[
            Consideration(
                at=NOW - timedelta(minutes=20),
                speaker="yoda",
                spark="The stop on the SPY short is the only thing holding.",
                why_now="Nothing prompted this; it came up reading the account.",
            )
        ]
    )

    prompt = build_prompt(rules, journal, [], considerations=recall, now=NOW)

    assert "nobody prompted this one; it is the agent's own." in prompt
    assert "the operator said" not in prompt


def test_the_prompt_says_the_dreamer_may_ignore_them(rules, journal):
    """The property the whole feature turns on. A suggestion the model feels
    obliged to honour is the operator steering the dreamer by accident, and two
    brains was the point rather than agreement."""
    prompt = build_prompt(rules, journal, [], considerations=_one(), now=NOW)

    assert "**You may ignore every one of these.**" in prompt
    assert "not a task and not a seed" in prompt
    # And no shortcut: a suggested chain is still a chain that has to be checked.
    assert "every hop still has to be checkable" in prompt


def test_a_consideration_is_never_described_as_a_dream(rules, journal):
    """It is a note. The prompt must not let it read as something already on a
    shelf, because a dream is the first link of a chain that ends in a live
    trading permission."""
    prompt = build_prompt(rules, journal, [], considerations=_one(), now=NOW)

    assert "not dreams, not shelved, not instructions" in prompt
    assert "none of them permits a symbol" in prompt


def test_none_recorded_is_not_nobody_suggested_anything(rules, journal):
    """`has_cycles` and `can_grade_anything` in a third place. An empty list is
    the ordinary state and it is not evidence of anything."""
    prompt = build_prompt(
        rules,
        journal,
        [],
        considerations=ConsiderationRecall(entries_in_window=9),
        now=NOW,
    )

    assert "Nothing new has been put to you in conversation in the last 48h." in prompt
    assert "NOT evidence that nobody had anything" in prompt


def test_a_silent_log_is_reported_as_a_silence_rather_than_as_a_no(rules, journal):
    """No records at all in the window is the box saying nothing, not the
    operator. The two must not render identically."""
    prompt = build_prompt(
        rules, journal, [], considerations=ConsiderationRecall(), now=NOW
    )

    assert "The log holds no records at all for the last 48h" in prompt
    assert "silence from this box rather than a quiet week" in prompt


def test_a_degraded_read_is_said_out_loud_in_the_prompt(rules, journal):
    prompt = build_prompt(
        rules,
        journal,
        [],
        considerations=_one(malformed_lines=3, unreadable_files=["2026-08-09.jsonl: x"]),
        now=NOW,
    )

    assert "could not be fully read (3 unparseable line(s), 1 unreadable file(s))" in prompt


def test_notes_already_seen_are_counted_in_the_prompt_rather_than_repeated(
    rules, journal
):
    """Repeating them would be the same suggestion arriving daily until it was
    taken up, which is not a suggestion."""
    prompt = build_prompt(
        rules,
        journal,
        [],
        considerations=ConsiderationRecall(seen_previously=2, entries_in_window=5),
        now=NOW,
    )

    assert "2 other note(s) went up in this window" in prompt
    assert "already in front of you on an earlier run" in prompt


def test_never_having_looked_renders_nothing_at_all(rules, journal):
    """`None` and an empty recall are different claims. One says the dreamer has
    no view of the chat surface; the other says it looked and nothing was
    waiting. A prompt that stated the second when only the first was true would
    be inventing the one fact it cannot establish."""
    prompt = build_prompt(rules, journal, [], considerations=None, now=NOW)

    assert "conversation" not in prompt
    assert "put to you" not in prompt


def test_the_considerations_come_after_the_dreamers_own_chains(rules, journal):
    """The grant block's ordering rule, arriving here for the same reason: the
    one section carrying somebody else's suggestion has a pull that is not
    evidence, and a model that reads it before its own open chains anchors on
    it."""
    dream = Dream(id=3, title="Brood overlap", seed="a spark")

    prompt = build_prompt(rules, journal, [dream], considerations=_one(), now=NOW)

    assert prompt.index("[id 3] Brood overlap") < prompt.index("Raised in conversation")
    # And the closing instruction still lands last.
    assert prompt.index("Raised in conversation") < prompt.index("Produce one step.")


# --------------------------------------------------- the run, end to end


def test_a_run_shows_the_considerations_and_marks_exactly_those(rules, store, journal, tmp_path):
    audit = AuditLog(tmp_path / "audit")
    at = _raise_consideration(audit.base_dir, minutes_ago=45, spark="A live note")
    client = _StubClient(_step())

    result = Dreamer(
        _env(), rules, store, journal, client=client, audit=audit
    ).run_once(now=NOW)

    assert result is not None
    assert "A live note" in client.prompts[0]
    marks = [e for e in audit.read().events if e.kind == "dream_considerations_seen"]
    assert len(marks) == 1
    assert marks[0].payload["shown"] == [at.isoformat()]
    # And a second run is not offered it again.
    second = _StubClient(_step())
    Dreamer(_env(), rules, store, journal, client=second, audit=audit).run_once(now=NOW)
    assert "A live note" not in second.prompts[0]


def test_a_failed_call_marks_nothing_so_the_note_is_offered_again(
    rules, store, journal, tmp_path
):
    """A run whose model call failed never showed anybody anything. Same
    direction as writing no dream at all."""
    audit = AuditLog(tmp_path / "audit")
    _raise_consideration(audit.base_dir, minutes_ago=45, spark="A live note")

    failed = Dreamer(
        _env(), rules, store, journal,
        client=_StubClient(raises=RuntimeError("boom")), audit=audit,
    ).run_once(now=NOW)

    assert failed is None
    assert [e for e in audit.read().events if e.kind == "dream_considerations_seen"] == []
    after = _StubClient(_step())
    Dreamer(_env(), rules, store, journal, client=after, audit=audit).run_once(now=NOW)
    assert "A live note" in after.prompts[0]


def test_a_run_that_was_shown_nothing_writes_no_marker(rules, store, journal, tmp_path):
    """An event saying "considered nothing" would put an empty claim on the
    record every single day, and the log is the thing that has to stay
    readable."""
    audit = AuditLog(tmp_path / "audit")

    Dreamer(
        _env(), rules, store, journal, client=_StubClient(_step()), audit=audit
    ).run_once(now=NOW)

    assert [e for e in audit.read().events if e.kind == "dream_considerations_seen"] == []


def test_a_dreamer_with_no_audit_log_still_dreams(rules, store, journal):
    """The feature is both directions or neither, and its absence must not cost
    a run. A dreamer without a log reads no considerations, marks none, and
    reports `None` rather than an empty recall."""
    result = Dreamer(
        _env(), rules, store, journal, client=_StubClient(_step())
    ).run_once(now=NOW)

    assert result is not None
    assert result.considerations is None


def test_a_consideration_does_not_become_a_dream_by_any_route(
    rules, store, journal, tmp_path
):
    """**The containment, stated as a test.**

    A consideration reaches the prompt and nothing else. The only dream written
    is the one the model returned; nothing promotes a note, nothing writes a
    shelf row from one, and the store holds no trace of its text. If this ever
    fails, a conversation has become able to insert at the top of the chain that
    ends in a live trading permission.
    """
    audit = AuditLog(tmp_path / "audit")
    _raise_consideration(
        audit.base_dir, minutes_ago=15, spark="Cobalt smelters and the grid"
    )

    result = Dreamer(
        _env(), rules, store, journal, client=_StubClient(_step()), audit=audit
    ).run_once(now=NOW)

    assert result is not None
    dreams = store.recent()
    assert len(dreams) == 1
    assert dreams[0].title == "Cicada broods and sesame"  # the model's own step
    assert "Cobalt" not in dreams[0].seed
    assert dreams[0].symbols == []


def test_dreaming_still_cannot_read_the_audit_log():
    """The structural half of the guarantee above, and the reason it is
    structural rather than a matter of discipline.

    `raise_consideration` writes to the audit log and never to `data/dreams.db`;
    the containment is that **nothing in `dreaming.py` reads the audit log**, so
    no code path there can turn a consideration into a shelf row. This change
    added a reader, and the reader deliberately lives in `news_history` and is
    called by `dreamer.py`. A test rather than a paragraph, because a guarantee
    written in prose is one that stops being checked.
    """
    import ast

    source = Path("src/bot/dreaming.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.lstrip("."))
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert not imported & {"audit", "news_history", "mcp_server", "dreamer"}


# ------------------------------------------------------------ the log line


def test_the_dream_line_states_the_consideration_count_and_the_fusion(
    tmp_path, monkeypatch
):
    """A zero is a stated fact each run rather than the absence of a line — the
    reason `stops_breached` is on the cycle line — and `result.fusion` reached
    `DreamerResult` and stopped there until now."""
    import structlog

    import bot.main as main_mod
    from bot.dreamer import DreamerResult

    class _Feed:
        is_degraded = False

        def recent_headlines(self, symbols):
            return []

        def recent_attributed(self, symbols):
            return []

        def recent_posts(self):
            return []

    class _Dreamer:
        def __init__(self, *a, **kw):
            pass

        def run_once(self, *, headlines=None, posts=None, now=None):
            return DreamerResult(
                dream=Dream(id=1, title="t", seed="s"),
                usage=USAGE,
                advanced=False,
                considerations=ConsiderationRecall(
                    considerations=[], entries_in_window=3
                ),
                fusion=FusionResult(ok=True, dream_id=9, parents=(4, 5)),
            )

    monkeypatch.setattr(main_mod, "build_news_feed", lambda env: _Feed())
    monkeypatch.setattr(main_mod, "build_social_feed", lambda env, rules: None)
    monkeypatch.setattr(main_mod, "Journal", lambda *a, **kw: object())
    monkeypatch.setattr("bot.dreamer.Dreamer", _Dreamer)
    monkeypatch.setattr(main_mod, "DreamStore", lambda *a, **kw: object())
    monkeypatch.setattr(main_mod, "AuditLog", lambda *a, **kw: _NoAudit(tmp_path))
    monkeypatch.setattr("bot.dreamer.promote_dreams", lambda *a, **kw: _NoPromotion())

    with structlog.testing.capture_logs() as logs:
        assert main_mod.cmd_dream(_env(), Rules.load(Path("config/rules.yaml"))) == 0

    line = next(e for e in logs if e["event"] == "dream_complete")
    assert line["considerations_shown"] == 0
    assert line["considerations_record_incomplete"] is False
    assert line["fusion"] == "4+5 -> 9"


class _NoAudit:
    """An audit log pointed at a tmp path, so no test writes to `audit/`."""

    def __init__(self, path: Path) -> None:
        self._log = AuditLog(path / "audit")

    def read(self, **kw):
        return self._log.read(**kw)

    def record_event(self, kind, payload):
        self._log.record_event(kind, payload)

    @property
    def base_dir(self) -> Path:
        return self._log.base_dir


class _NoPromotion:
    considered = 0
    promoted: tuple[tuple[int, str], ...] = ()
    conditions_fulfilled = 0
    cycles_available = 0


def test_a_run_that_could_not_look_logs_none_rather_than_zero(tmp_path, monkeypatch):
    """Three states, not two. `None` says nobody looked; `0` says the log was
    read and nothing was waiting."""
    import structlog

    import bot.main as main_mod
    from bot.dreamer import DreamerResult

    class _Feed:
        is_degraded = False

        def recent_headlines(self, symbols):
            return []

        def recent_attributed(self, symbols):
            return []

        def recent_posts(self):
            return []

    class _Dreamer:
        def __init__(self, *a, **kw):
            pass

        def run_once(self, *, headlines=None, posts=None, now=None):
            return DreamerResult(
                dream=Dream(id=1, title="t", seed="s"), usage=None, advanced=False
            )

    monkeypatch.setattr(main_mod, "build_news_feed", lambda env: _Feed())
    monkeypatch.setattr(main_mod, "build_social_feed", lambda env, rules: None)
    monkeypatch.setattr(main_mod, "Journal", lambda *a, **kw: object())
    monkeypatch.setattr("bot.dreamer.Dreamer", _Dreamer)
    monkeypatch.setattr(main_mod, "DreamStore", lambda *a, **kw: object())
    monkeypatch.setattr(main_mod, "AuditLog", lambda *a, **kw: _NoAudit(tmp_path))
    monkeypatch.setattr("bot.dreamer.promote_dreams", lambda *a, **kw: _NoPromotion())

    with structlog.testing.capture_logs() as logs:
        main_mod.cmd_dream(_env(), Rules.load(Path("config/rules.yaml")))

    line = next(e for e in logs if e["event"] == "dream_complete")
    assert line["considerations_shown"] is None
    assert line["fusion"] is None


def test_a_refused_fusion_is_named_on_the_line_rather_than_reading_as_none():
    """A dreamer proposing fusions a full workbench keeps turning down looks,
    from outside, exactly like a dreamer that stopped proposing them."""
    from bot.main import _describe_fusion

    refused = FusionResult(ok=False, parents=(4, 5), refusals=(MoveRefusal.FULL,))

    assert _describe_fusion(None) is None
    assert _describe_fusion(refused) == "refused 4+5: full"


def test_a_hop_claimed_checked_with_no_source_is_counted_not_only_dropped(
    rules, store, journal, capsys
):
    """**The silent downgrade, made visible.**

    `_hop` resolves `checked=True` with an empty `source` DOWNWARDS, which is
    right and load-bearing: a flag with nothing behind it is exactly the
    generous self-rating the verification badge exists to refuse.

    What was missing is any trace that it happened. Measured on the droplet:
    five passes over one dream, ~31 citations fetched, `unchecked` stuck at 2
    every pass, and no way to tell whether the dreamer was failing to SOURCE
    its hops or failing to SAY where. Those need completely different fixes —
    a researcher problem versus a prompt problem — and the log could not
    distinguish them.

    Two counters, never one: the `stops_unchecked` / `stops_unmeasured` rule
    arriving at the chain.
    """
    step = _step(
        chain=[
            DreamHop(claim="sourced properly", checked=True, source="10-K, p.42"),
            DreamHop(claim="claimed, no source", checked=True, source=""),
            DreamHop(claim="claimed, blank source", checked=True, source="   "),
            DreamHop(claim="honestly unchecked", checked=False, source=""),
        ]
    )
    dreamer = Dreamer(_env(), rules, store, journal, client=_StubClient(step))

    result = dreamer.run_once()
    out = capsys.readouterr().out

    assert result is not None
    chain = result.dream.chain
    # The rail is unchanged: both unsourced claims are still downgraded.
    assert [h.checked for h in chain] == [True, False, False, False]
    # And a blank source is no source, so it is not kept on a checked hop.
    assert chain[0].source == "10-K, p.42"

    # The new half: it is COUNTED and said out loud. Two of the four hops
    # claimed a check they could not back, and the line says so.
    assert "dream_hops_claimed_without_source" in out
    assert "hops=2" in out


def test_a_clean_chain_states_its_nought_rather_than_staying_silent(
    rules, store, journal, capsys
):
    """A stated nought each run, because silence is also what an outage looks
    like. Same reason the stop-breach count is on `cycle_complete`."""
    step = _step(
        chain=[DreamHop(claim="sourced", checked=True, source="10-K, p.42")]
    )
    dreamer = Dreamer(_env(), rules, store, journal, client=_StubClient(step))

    result = dreamer.run_once()
    out = capsys.readouterr().out

    assert result is not None
    # No warning on a clean chain...
    assert "dream_hops_claimed_without_source" not in out
    # ...but the figure is on the summary line regardless of being zero, so a
    # clean run is a stated fact rather than the absence of a warning.
    assert "dream_step" in out
    assert "claimed_without_source=0" in out


def test_a_carried_dream_says_how_many_steps_it_has_already_had(rules, store, journal):
    """**A chain you never finish reaches nobody.**

    Measured on the droplet: dream 2 came back `stage: iterate, verdict: null`
    on eight consecutive passes — sourcing hops, growing the chain, and never
    concluding. `promotion_for` needs a `keep` verdict, so a dream that
    iterates for ever can never reach the prophecy shelf however well sourced
    it becomes. Nothing was broken; nothing could move either.

    The prompt described the stage machine and never said how long this dream
    had been in it. "Last touched N days ago" is about the CLOCK rather than
    about effort — five passes in one afternoon all read `0 day(s) ago`, so a
    model had no way to notice it was repeating itself.

    Counted from `thoughts`, the append-only record of steps actually taken,
    so the figure cannot drift from what happened.
    """
    dream = Dream(title="A long-running chain", seed="a spark")
    dream.add_thought(DreamStage.SEED, "the spark", at=ENTRY)
    dream.add_thought(DreamStage.EXPLORE, "the chain", at=ENTRY)
    dream.add_thought(DreamStage.ITERATE, "attack one", at=ENTRY)
    dream.add_thought(DreamStage.ITERATE, "attack two", at=ENTRY)
    dream.id = store.save(dream)

    prompt = build_prompt(rules, journal, [store.get(dream.id)])

    assert "4 step(s) so far of which 2 were iterate" in prompt

    # The instruction that makes the count actionable lives in the SYSTEM
    # prompt, beside the stage machine it qualifies — a per-run context is
    # rebuilt every call and the rule is a permanent property of the loop.
    # Asserted here anyway, because a count with no instruction is trivia and
    # an instruction with no count is unactionable: they only work as a pair,
    # and nothing else would fail if one of them went.
    from bot.dreamer import SYSTEM_PROMPT

    assert "A chain you never finish reaches nobody" in SYSTEM_PROMPT
    assert "the next step should be a `verdict`" in SYSTEM_PROMPT
    # `drop` must read as a real outcome rather than a failure, or the nudge
    # buys conclusions that are all `keep`.
    # Matched without the leading article: the line wraps in the prompt text.
    assert "chain you killed yourself is worth more" in SYSTEM_PROMPT
    # And it must not read as "conclude early", which would buy shallow chains.
    assert "NOT being asked to conclude before you have looked" in SYSTEM_PROMPT
