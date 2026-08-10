"""The agent-to-agent conference: the caps, and the things the caps protect.

No test here touches the network. `Conference` takes both of its clients, so
every model call is a canned turn, which is what makes the five caps testable at
all — the interesting question about a cap is what happens when it is reached,
and that is exactly the case a live call would be expensive to produce.

The file is organised by cap, because the caps are the feature. `has_something_changed`
gets the longest section for the reason its own docstring gives: it is the one
that stops two agents having the same conversation every day forever, and it is
the one most likely to look redundant to somebody tidying up.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bot.claude_client import CallUsage
from bot.confer import (
    CONFERENCE,
    MAX_DREAMS_PER_RUN,
    MAX_EXCHANGES_PER_DREAM,
    MAX_TURNS_PER_EXCHANGE,
    Change,
    Conference,
    ConferOutcome,
    DreamerTurn,
    TraderPowers,
    TraderTurn,
    TraderVerdict,
    exchanges_so_far,
    has_something_changed,
    last_agent_turn_at,
    render_classes,
    render_dream,
    run_conference,
)
from bot.config import Env, Rules
from bot.dreaming import (
    DREAMER,
    OPERATOR,
    TEXT_MAX_CHARS,
    TRADER,
    Dream,
    DreamCondition,
    DreamMessage,
    DreamStore,
    Hop,
    Vault,
    VaultCaps,
)

CONFER_SOURCE = Path(__file__).resolve().parent.parent / "src" / "bot" / "confer.py"

BASE = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
LATER = BASE + timedelta(days=1)
USAGE = CallUsage(
    input_tokens=10,
    output_tokens=10,
    cache_read_tokens=0,
    cache_write_tokens=0,
    estimated_cost_usd=0.002,
)


def _env() -> Env:
    env = Env(_env_file=None)  # type: ignore[call-arg]
    env.anthropic_api_key = "test"
    return env


class _Speaker:
    """One side of a conference, answering from a script.

    Runs out of script rather than raising: a dreamer with nothing left to say
    keeps saying something, and a trader with nothing left to say keeps asking,
    which is precisely the state the turn cap exists for. `verdict` is the
    standing answer for a trader that always reaches the same conclusion, which
    is what a multi-dream run needs — a single scripted turn would be consumed
    by the first dream and leave the second in an exhausted exchange.
    """

    def __init__(
        self,
        *,
        turns: list[object] | None = None,
        raises: Exception | None = None,
        verdict: TraderVerdict | None = None,
        text: str = "still thinking about it",
    ):
        self.turns = list(turns or [])
        self.raises = raises
        self.verdict = verdict
        self.text = text
        self.prompts: list[str] = []

    def confer(self, prompt: str, output_format: type) -> tuple[object, CallUsage]:
        self.prompts.append(prompt)
        if self.raises is not None:
            raise self.raises
        if self.turns:
            return self.turns.pop(0), USAGE
        if output_format is TraderTurn and self.verdict is not None:
            return TraderTurn(text=self.text, verdict=self.verdict), USAGE
        return output_format(text=self.text), USAGE


@pytest.fixture
def store(tmp_path) -> DreamStore:
    return DreamStore(tmp_path / "dreams.db")


@pytest.fixture
def rules() -> Rules:
    return Rules.load(Path("config/rules.yaml"))


def _vaulted(store: DreamStore, *, at: datetime = BASE, title: str = "Brood overlap") -> int:
    """A dream sitting in the one vault the trading agent can see."""
    dream_id = store.save(
        Dream(
            title=title,
            seed="two of three producers inside overlapping ranges",
            chain=[Hop("the brood map is published", True, "USDA"), Hop("assumed")],
            weakest_hop="the overlap",
            symbols=["SPY"],
            asset_class_key="us_equity",
        )
    )
    store.move(dream_id, Vault.VAULT, by=DREAMER, at=at)
    return dream_id


def _conference(
    rules: Rules,
    store: DreamStore,
    *,
    dreamer: _Speaker | None = None,
    trader: _Speaker | None = None,
) -> Conference:
    return Conference(
        _env(),
        rules,
        store,
        dreamer_client=dreamer or _Speaker(),
        trader_client=trader or _Speaker(),
    )


def _declines() -> _Speaker:
    return _Speaker(
        verdict=TraderVerdict.DECLINE, text="not for me: the second hop is unchecked"
    )


# ============================================================ cap 1: six turns


def test_an_exchange_stops_dead_at_six_turns(rules, store):
    """Three each. A conversation that has not converged in six turns is not one
    turn away from converging; it is two agents that disagree."""
    dream_id = _vaulted(store)
    dreamer, trader = _Speaker(), _Speaker()

    result = _conference(rules, store, dreamer=dreamer, trader=trader).confer_once(
        store.get(dream_id), now=LATER
    )

    assert result.outcome is ConferOutcome.TURNS_EXHAUSTED
    assert result.turns == MAX_TURNS_PER_EXCHANGE
    assert len(dreamer.prompts) == 3
    assert len(trader.prompts) == 3

    turns = [m for m in store.messages(dream_id) if m.speaker in (DREAMER, TRADER)]
    assert [m.speaker for m in turns] == [DREAMER, TRADER] * 3
    # The kind is decided from the turn's position, never by the model, so a
    # speaker cannot mislabel its own turn.
    assert [m.kind for m in turns] == [
        "offer", "question", "answer", "question", "answer", "question",
    ]


def test_the_transcript_of_an_exchange_that_decided_nothing_is_still_stored(rules, store):
    """A dream the trader kept declining is a fact about the dreamer worth
    having, and one that ran out of turns is a fact about both of them. Storing
    only the exchanges that concluded would leave a record that flatters
    everyone in it."""
    dream_id = _vaulted(store)

    _conference(rules, store).confer_once(store.get(dream_id), now=LATER)

    notes = [m for m in store.messages(dream_id) if m.speaker == CONFERENCE]
    assert len(notes) == 1
    assert "no verdict" in notes[0].text


def test_the_trading_agent_is_told_when_it_is_out_of_turns(rules, store):
    """A model that does not know it is on its last turn writes another
    question, and the exchange closes with a decision nobody made."""
    dream_id = _vaulted(store)
    trader = _Speaker()

    _conference(rules, store, trader=trader).confer_once(store.get(dream_id), now=LATER)

    assert "LAST turn" not in trader.prompts[0]
    assert "LAST turn" in trader.prompts[-1]


def test_an_early_verdict_ends_the_exchange_before_the_cap(rules, store):
    dream_id = _vaulted(store)
    dreamer = _Speaker()

    result = _conference(rules, store, dreamer=dreamer, trader=_declines()).confer_once(
        store.get(dream_id), now=LATER
    )

    assert result.outcome is ConferOutcome.DECLINED
    assert result.turns == 2
    assert len(dreamer.prompts) == 1


# ====================================================== cap 2: two dreams a run


def test_only_two_dreams_are_conferred_in_a_run(rules, store):
    """So a daily run costs at most twelve model calls and the bill on an
    unattended timer is predictable."""
    for index in range(4):
        _vaulted(store, at=BASE + timedelta(minutes=index), title=f"dream {index}")

    report = _conference(rules, store, trader=_Speaker()).run(now=LATER)

    assert report.considered == 4
    assert report.conferred == MAX_DREAMS_PER_RUN
    assert len(report.exchanges) == MAX_DREAMS_PER_RUN


def test_the_longest_waiting_offer_is_answered_first(rules, store):
    """An offer nobody answered is the one worth answering. Newest-first would
    let a productive dreamer starve its own older offers indefinitely.

    **The two clocks are set apart on purpose, and this test could not tell them
    apart before.** `_vaulted` moved and saved at one moment, so
    `vault_entered_at` and `updated_at` agreed for every dream and any ORDER BY
    over either gave the same answer — the query could be rewritten to the wrong
    clock and this stayed green. It was on the wrong clock: `in_vault` sorted by
    `updated_at`, so the oldest offer, edited this morning, was answered LAST.
    Here the longest-waiting dream is also the most recently EDITED one, which
    is the case that separates them.
    """
    oldest = _vaulted(store, at=BASE, title="oldest")
    middle = _vaulted(store, at=BASE + timedelta(hours=1), title="middle")
    newest = _vaulted(store, at=BASE + timedelta(hours=2), title="newest")

    # The dreamer reworks its oldest offer, newest-edit-first order would put it
    # at the front of the shelf and therefore at the BACK of the queue.
    touched = store.get(oldest)
    assert touched is not None
    touched.updated_at = BASE + timedelta(hours=6)
    store.save(touched)

    report = _conference(rules, store, trader=_declines()).run(now=LATER)

    assert [e.dream_id for e in report.exchanges] == [oldest, middle]
    assert newest not in [e.dream_id for e in report.exchanges]


def test_a_skip_costs_no_model_call_and_does_not_use_up_the_run(rules, store):
    """Counting a free skip against the budget would make a quiet day eat the
    allowance of a busy one."""
    stale = _vaulted(store, at=BASE, title="stale")
    _conference(rules, store, trader=_declines()).run(now=LATER)

    fresh_a = _vaulted(store, at=BASE + timedelta(hours=1), title="a")
    fresh_b = _vaulted(store, at=BASE + timedelta(hours=2), title="b")

    report = _conference(rules, store, trader=_declines()).run(now=LATER + timedelta(days=1))

    outcomes = {e.dream_id: e.outcome for e in report.exchanges}
    assert outcomes[stale] is ConferOutcome.NOTHING_NEW
    assert outcomes[fresh_a] is ConferOutcome.DECLINED
    assert outcomes[fresh_b] is ConferOutcome.DECLINED
    assert report.skipped == 1
    assert report.conferred == 2


# ================================================ cap 3: three exchanges, ever


def test_a_dream_is_conferred_at_most_three_times(rules, store):
    """A fourth conference would be the same argument again, and a fourth in a
    different order is still the same argument."""
    dream_id = _vaulted(store)

    for day in range(MAX_EXCHANGES_PER_DREAM + 2):
        # Something changes every day, so only the exchange cap can stop them.
        dream = store.get(dream_id)
        assert dream is not None
        dream.updated_at = LATER + timedelta(days=day)
        store.save(dream)
        _conference(rules, store, trader=_declines()).run(
            now=LATER + timedelta(days=day, hours=1)
        )

    assert exchanges_so_far(store.messages(dream_id)) == MAX_EXCHANGES_PER_DREAM


def test_the_fourth_attempt_is_skipped_without_calling_a_model(rules, store):
    dream_id = _vaulted(store)
    for index in range(MAX_EXCHANGES_PER_DREAM):
        store.add_message(
            dream_id,
            speaker=DREAMER,
            kind="offer",
            text=f"offer {index}",
            at=BASE + timedelta(hours=index),
        )
    dreamer, trader = _Speaker(), _Speaker()

    report = _conference(rules, store, dreamer=dreamer, trader=trader).run(now=LATER)

    assert report.exchanges[0].outcome is ConferOutcome.EXCHANGES_EXHAUSTED
    assert dreamer.prompts == [] and trader.prompts == []
    assert report.calls == 0


def test_the_dream_is_told_once_that_it_has_parked(rules, store):
    """At the boundary, not on every later skip. A warning that repeats teaches
    an operator to ignore it — the same reason the tailnet banner names the
    command that clears it."""
    dream_id = _vaulted(store)

    for day in range(MAX_EXCHANGES_PER_DREAM + 1):
        dream = store.get(dream_id)
        assert dream is not None
        dream.updated_at = LATER + timedelta(days=day)
        store.save(dream)
        _conference(rules, store, trader=_declines()).run(
            now=LATER + timedelta(days=day, hours=1)
        )

    parked = [m for m in store.messages(dream_id) if "parks" in m.text]
    assert len(parked) == 1
    assert parked[0].speaker == CONFERENCE


def test_an_offer_written_by_hand_counts_as_an_exchange(rules, store):
    """The conservative direction: it can only make this module talk less, and
    an offer is an offer whoever wrote it."""
    dream_id = _vaulted(store)
    store.add_message(dream_id, speaker=DREAMER, kind="offer", text="by hand", at=BASE)

    assert exchanges_so_far(store.messages(dream_id)) == 1


# ============================================== cap 4: one prose cap, not two


def test_a_long_turn_is_trimmed_by_the_existing_cap_rather_than_a_new_one(rules, store):
    """`TEXT_MAX_CHARS` is imported from `dreaming.py`, not redefined here. Two
    numbers for one rule means the wrong one is the one nobody is reading."""
    import bot.confer as confer_mod
    import bot.dreaming as dreaming_mod

    assert confer_mod.TEXT_MAX_CHARS is dreaming_mod.TEXT_MAX_CHARS

    dream_id = _vaulted(store)
    dreamer = _Speaker(turns=[DreamerTurn(text="x" * 5000)])

    _conference(rules, store, dreamer=dreamer, trader=_declines()).confer_once(
        store.get(dream_id), now=LATER
    )

    offer = store.messages(dream_id)[0]
    assert len(offer.text) <= TEXT_MAX_CHARS
    assert offer.text.endswith("…")


# ============================ cap 5: the one that actually stops them talking


def test_a_dream_that_has_never_been_conferred_always_qualifies():
    change = has_something_changed(Dream(title="t", seed="s"), None)

    assert change
    assert "no exchange" in change.summary


def test_an_unchanged_dream_is_refused_a_second_conversation():
    """Without this, two agents re-litigate an unchanged dream every day
    forever, politely, at cost, with every other cap satisfied on every pass."""
    dream = Dream(
        title="t", seed="s", created_at=BASE, updated_at=BASE, vault_entered_at=BASE
    )

    change = has_something_changed(dream, BASE + timedelta(hours=1))

    assert not change
    assert change.summary == "nothing new to discuss"


def test_a_fulfilled_condition_reopens_the_conversation():
    dream = Dream(
        title="t",
        seed="s",
        updated_at=BASE,
        vault_entered_at=BASE,
        conditions=[
            DreamCondition(
                text="the brood map lands",
                fulfilled=True,
                fulfilled_at=BASE + timedelta(days=2),
            )
        ],
    )

    change = has_something_changed(dream, BASE + timedelta(days=1))

    assert change
    assert "condition" in change.summary


def test_a_condition_fulfilled_without_a_stamp_cannot_establish_a_change():
    """It may have been fulfilled before the last exchange. An undated fact
    cannot say it happened since, and guessing that it did would be a plausible
    wrong answer — which is the failure this repository exists to prevent."""
    dream = Dream(
        title="t",
        seed="s",
        updated_at=BASE,
        vault_entered_at=BASE,
        conditions=[DreamCondition(text="something", fulfilled=True)],
    )

    assert not has_something_changed(dream, BASE + timedelta(days=1))


def test_an_edited_dream_reopens_the_conversation():
    """A hop added, a `checked` flag flipped, the chain reworked — every write
    path stamps `updated_at`, so one stamp answers for all of them."""
    dream = Dream(
        title="t", seed="s", updated_at=BASE + timedelta(days=2), vault_entered_at=BASE
    )

    change = has_something_changed(dream, BASE + timedelta(days=1))

    assert change
    assert "edited" in change.summary


def test_a_vault_move_reopens_the_conversation():
    dream = Dream(
        title="t", seed="s", updated_at=BASE, vault_entered_at=BASE + timedelta(days=2)
    )

    assert has_something_changed(dream, BASE + timedelta(days=1))


def test_an_operator_note_reopens_the_conversation():
    dream = Dream(title="t", seed="s", updated_at=BASE, vault_entered_at=BASE)
    note = DreamMessage(
        dream_id=1, at=BASE + timedelta(days=2), speaker=OPERATOR, text="have another look"
    )

    change = has_something_changed(dream, BASE + timedelta(days=1), messages=[note])

    assert change
    assert OPERATOR in change.summary


def test_this_modules_own_notes_never_count_as_a_change():
    """A machine note wearing the operator's name would trip the change gate on
    its own writing, and the two agents would talk forever about nothing."""
    dream = Dream(title="t", seed="s", updated_at=BASE, vault_entered_at=BASE)
    note = DreamMessage(
        dream_id=1, at=BASE + timedelta(days=2), speaker=CONFERENCE, text="the call failed"
    )

    assert not has_something_changed(dream, BASE + timedelta(days=1), messages=[note])


def test_the_marker_is_the_last_agent_turn_and_not_the_last_message():
    """An operator note posted after an exchange is one of the things that makes
    a dream worth reopening. A marker taken from the last message of any kind
    would move past the very event it exists to notice, and the operator would
    be the only participant unable to restart the conversation. Same shape as
    `seen.py`."""
    messages = [
        DreamMessage(dream_id=1, at=BASE, speaker=DREAMER, text="offer", kind="offer"),
        DreamMessage(dream_id=1, at=BASE + timedelta(minutes=1), speaker=TRADER, text="no"),
        DreamMessage(dream_id=1, at=BASE + timedelta(days=1), speaker=OPERATOR, text="look again"),
    ]

    assert last_agent_turn_at(messages) == BASE + timedelta(minutes=1)

    dream = Dream(title="t", seed="s", updated_at=BASE, vault_entered_at=BASE)
    assert has_something_changed(dream, last_agent_turn_at(messages), messages=messages)


def test_the_end_of_an_exchange_does_not_itself_count_as_a_change(rules, store):
    """`return_to_vault` stamps the message, `updated_at` and `vault_entered_at`
    with the same moment. A non-strict comparison would hand the two agents a
    fresh round created by the ending of the last one."""
    dream_id = _vaulted(store)
    store.adopt(dream_id, at=BASE + timedelta(days=1))
    store.return_to_vault(dream_id, reason="no slot for it", at=BASE + timedelta(days=2))

    dream = store.get(dream_id)
    messages = store.messages(dream_id)
    assert dream is not None

    assert not has_something_changed(dream, last_agent_turn_at(messages), messages=messages)


def test_a_skip_is_recorded_rather_than_silent(rules, store):
    """Recorded in the report and the log, deliberately NOT in the transcript: a
    note every day saying 'nothing has changed' would fill the record a human
    reads with the exact noise this cap exists to prevent, in the one place
    where the noise is permanent."""
    dream_id = _vaulted(store)
    _conference(rules, store, trader=_declines()).run(now=LATER)
    before = len(store.messages(dream_id))

    report = _conference(rules, store, trader=_declines()).run(now=LATER + timedelta(days=1))

    assert report.exchanges[0].outcome is ConferOutcome.NOTHING_NEW
    assert report.exchanges[0].detail == "nothing new to discuss"
    assert len(store.messages(dream_id)) == before


# =========================================== what the trading agent may do


def test_the_trading_agent_has_exactly_two_powers():
    """Adopt from the vault, or hand back with a stated reason. The list being
    short is the point rather than an accident of what has been needed."""
    public = {
        name
        for name, _ in inspect.getmembers(TraderPowers, inspect.isfunction)
        if not name.startswith("_")
    }

    assert public == {"adopt", "hand_back"}


def test_the_conference_has_no_route_to_an_order():
    """The whole safety argument, asserted structurally rather than trusted.

    A speculative-idea generator wired to an execution path is the Alpha Arena
    failure with extra steps. `Dream` carrying no order fields is the first
    lock; this is the second, on the module that lets two models talk about one.
    """
    source = CONFER_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)

    modules: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)

    forbidden = {"broker", "risk", "models", "journal", "mcp_server", "reconcile"}
    assert not (forbidden & modules), f"confer.py reached for {forbidden & modules}"
    # Names rather than raw text, so the module docstring may go on explaining
    # WHY there is no `OrderProposal` here without failing its own test.
    assert "OrderProposal" not in names
    assert "place_order" not in names
    # It may not delete a dream either, whoever it is speaking as.
    assert "delete" not in names


def test_the_verdicts_are_three_and_none_of_them_is_a_trade():
    assert {str(v) for v in TraderVerdict} == {"accept", "decline", "park"}


def test_a_decline_leaves_the_dream_where_it_was(rules, store):
    dream_id = _vaulted(store)

    result = _conference(rules, store, trader=_declines()).confer_once(
        store.get(dream_id), now=LATER
    )

    assert result.outcome is ConferOutcome.DECLINED
    dream = store.get(dream_id)
    assert dream is not None and dream.vault is Vault.VAULT
    assert store.adoptions(dream_id) == []


def test_a_park_moves_nothing_either(rules, store):
    """The trading agent may not move a dream between the dreamer's shelves, so
    a park is a state of the conversation and never a state of the vault."""
    dream_id = _vaulted(store)
    trader = _Speaker(turns=[TraderTurn(text="not today", verdict=TraderVerdict.PARK)])

    result = _conference(rules, store, trader=trader).confer_once(
        store.get(dream_id), now=LATER
    )

    assert result.outcome is ConferOutcome.PARKED
    dream = store.get(dream_id)
    assert dream is not None and dream.vault is Vault.VAULT


def test_an_accept_adopts_through_the_store_and_grants_the_symbols(rules, store):
    dream_id = _vaulted(store)
    trader = _Speaker(turns=[TraderTurn(text="taking it", verdict=TraderVerdict.ACCEPT)])

    result = _conference(rules, store, trader=trader).confer_once(
        store.get(dream_id), now=LATER
    )

    assert result.outcome is ConferOutcome.ADOPTED
    dream = store.get(dream_id)
    assert dream is not None and dream.vault is Vault.ADOPTED
    assert store.granted_symbols(LATER + timedelta(days=1)) == {"SPY": "us_equity"}
    # The store narrates the handover itself, so the transcript is complete.
    assert [m.kind for m in store.messages(dream_id)][-1] == "accept"


def test_an_adoption_the_store_refuses_is_not_reported_as_a_failed_conversation(
    rules, store
):
    """A full adopted vault is a normal Tuesday. The two of them agreed; the
    store had no room, and the difference matters to whoever reads the report."""
    dream_id = _vaulted(store)
    trader = _Speaker(turns=[TraderTurn(text="taking it", verdict=TraderVerdict.ACCEPT)])

    result = _conference(rules, store, trader=trader).confer_once(
        store.get(dream_id),
        now=LATER,
        caps=VaultCaps(adopted=0),
    )

    assert result.outcome is ConferOutcome.ADOPTION_REFUSED
    dream = store.get(dream_id)
    assert dream is not None and dream.vault is Vault.VAULT
    assert any("refused the adoption" in m.text for m in store.messages(dream_id))


# ==================================================================== failure


@pytest.mark.parametrize(
    "boom",
    [RuntimeError("no parsable turn"), ValueError("bad schema"), KeyError("surprise")],
)
def test_a_failed_call_ends_the_exchange_without_raising(rules, store, boom):
    """Same rule as the decision loop's model call. A ValidationError out of the
    SDK killed a live cycle once and systemd restarted straight into it."""
    dream_id = _vaulted(store)

    result = _conference(
        rules, store, dreamer=_Speaker(raises=boom)
    ).confer_once(store.get(dream_id), now=LATER)

    assert result.outcome is ConferOutcome.CALL_FAILED
    assert result.turns == 0


def test_a_failure_before_the_offer_is_not_recorded_as_an_exchange(rules, store):
    """A cycle that could not get a decision must not be recorded as one that
    decided to do nothing. So the dream keeps all three of its exchanges and is
    eligible again tomorrow."""
    dream_id = _vaulted(store)

    _conference(rules, store, dreamer=_Speaker(raises=RuntimeError("boom"))).run(now=LATER)

    messages = store.messages(dream_id)
    assert exchanges_so_far(messages) == 0
    assert last_agent_turn_at(messages) is None
    assert [m.speaker for m in messages] == [CONFERENCE]
    assert "failed" in messages[0].text


def test_a_failure_reaches_the_transcript_rather_than_only_the_log(rules, store):
    dream_id = _vaulted(store)

    _conference(rules, store, trader=_Speaker(raises=RuntimeError("upstream 529"))).run(
        now=LATER
    )

    notes = [m for m in store.messages(dream_id) if m.speaker == CONFERENCE]
    assert len(notes) == 1
    assert "529" in notes[0].text


def test_a_failure_part_way_through_still_counts_the_exchange(rules, store):
    """The offer was made and the exchange happened. Only a failure BEFORE the
    opening offer costs the dream nothing."""
    dream_id = _vaulted(store)

    _conference(rules, store, trader=_Speaker(raises=RuntimeError("boom"))).run(now=LATER)

    assert exchanges_so_far(store.messages(dream_id)) == 1


def test_one_dreams_failure_does_not_stop_the_run(rules, store):
    """A run is unattended. A single bad exchange must not take the others with
    it, for the same reason a failed feed degrades a cycle rather than ending
    the loop."""
    _vaulted(store, at=BASE, title="first")
    _vaulted(store, at=BASE + timedelta(hours=1), title="second")

    report = _conference(rules, store, trader=_Speaker(raises=RuntimeError("boom"))).run(
        now=LATER
    )

    assert len(report.exchanges) == 2
    assert {e.outcome for e in report.exchanges} == {ConferOutcome.CALL_FAILED}


# =================================================================== the run


def test_an_empty_vault_is_a_different_answer_from_everything_being_skipped(rules, store):
    """`has_cycles` and `can_grade_anything` again: opposite findings produce
    identical empty lists, and a caller handed only a list reads it wrongly."""
    empty = _conference(rules, store).run(now=LATER)

    assert empty.considered == 0
    assert empty.exchanges == ()

    _vaulted(store)
    _conference(rules, store, trader=_declines()).run(now=LATER)
    skipped = _conference(rules, store, trader=_declines()).run(now=LATER + timedelta(days=1))

    assert skipped.considered == 1
    assert skipped.conferred == 0
    assert skipped.skipped == 1


def test_only_dreams_in_the_vault_are_conferred(rules, store):
    """The vault is the only shelf the trading agent can see."""
    store.save(Dream(title="on the workbench", seed="s"))
    store.move(
        store.save(Dream(title="a prophecy", seed="s")), Vault.PROPHECY, by=DREAMER
    )

    report = _conference(rules, store).run(now=LATER)

    assert report.considered == 0


def test_the_report_totals_the_cost_of_a_run(rules, store):
    _vaulted(store)

    report = _conference(rules, store, trader=_declines()).run(now=LATER)

    assert report.calls == 2
    assert report.cost_usd == pytest.approx(2 * USAGE.estimated_cost_usd)


def test_the_entry_point_a_command_calls_runs_a_whole_conference(rules, store):
    """`run_conference` is the single seam a CLI command uses, deliberately not
    reachable from `cmd_loop`."""
    _vaulted(store)

    report = run_conference(
        _env(),
        rules,
        store,
        now=LATER,
        dreamer_client=_Speaker(),
        trader_client=_declines(),
    )

    assert report.adopted == 0
    assert report.conferred == 1


# ================================================================ the prompts


def test_the_chain_is_never_rendered_without_its_badge_and_its_weakest_hop(rules, store):
    """An unqualified chain in a prompt reads as established fact, and the whole
    reason `Hop.checked` exists is that some of those sentences were invented."""
    dream = Dream(
        title="t",
        seed="s",
        chain=[Hop("sourced", True, "a source"), Hop("assumed")],
        weakest_hop="the overlap",
    )

    rendered = render_dream(dream)

    assert rendered.index("verification") < rendered.index("hop 1")
    assert rendered.index("weakest hop") < rendered.index("hop 1")
    assert "partial" in rendered
    assert "UNCHECKED" in rendered


def test_a_dream_with_no_conditions_does_not_read_as_all_conditions_met():
    """`all([])` is True, which is exactly the trap. An absence of evidence must
    not look like the good outcome."""
    rendered = render_dream(Dream(title="t", seed="s"))

    assert "none were ever pre-registered" in rendered


def test_a_dream_that_never_named_a_weakest_hop_says_so():
    rendered = render_dream(Dream(title="t", seed="s", chain=[Hop("a claim")]))

    assert "NOT NAMED" in rendered


def test_the_trading_agent_is_shown_the_blocked_classes_by_name(rules):
    """It is deciding whether to grant a symbol permission, so it has to be able
    to SEE the block rather than infer it from an absence."""
    rendered = render_classes(rules)

    assert "us_equity — ENABLED" in rendered
    assert "crypto — BLOCKED" in rendered


def test_the_trading_agent_is_told_an_adoption_is_not_a_position(rules, store):
    """A dream is a reason a symbol is available, never a reason to be in it."""
    from bot.confer import TRADER_SYSTEM_PROMPT

    assert "does not open a position" in TRADER_SYSTEM_PROMPT
    assert "cannot place an order" in TRADER_SYSTEM_PROMPT


def test_neither_side_of_the_conference_is_shown_profit_and_loss(rules, store):
    """The dreamer's rule, arriving at the conference. Nothing in this module
    reads the journal at all, which is the structural half of it."""
    dream_id = _vaulted(store)
    dreamer, trader = _Speaker(), _Speaker()

    _conference(rules, store, dreamer=dreamer, trader=trader).confer_once(
        store.get(dream_id), now=LATER
    )

    for prompt in dreamer.prompts + trader.prompts:
        for leak in ("realised", "P&L", "win rate", "profit factor", "expectancy"):
            assert leak not in prompt


def test_the_whole_history_is_shown_rather_than_only_this_exchange(rules, store):
    """An agent shown only today's turns asks a question it was answered last
    month. The caps are what bound the document, not a slice here."""
    dream_id = _vaulted(store)
    store.add_message(
        dream_id, speaker=TRADER, kind="question", text="what killed it last time", at=BASE
    )
    dreamer = _Speaker()

    _conference(rules, store, dreamer=dreamer, trader=_declines()).confer_once(
        store.get(dream_id), now=LATER
    )

    assert "what killed it last time" in dreamer.prompts[0]


def test_a_change_with_no_reasons_reads_as_nothing_new():
    assert Change(False).summary == "nothing new to discuss"
    assert bool(Change(True, ("something",))) is True


# ------------------------- a refusal by the STORE is not the dream's own fault


def test_a_dream_the_shelf_refused_is_reconsidered_once_a_slot_frees(rules, store):
    """The change gate measures the DREAM; this blocker was the STORE.

    An exchange ending `ADOPTION_REFUSED` leaves the dream in the vault with
    nothing about it altered, so `has_something_changed` said no — and said no
    again every day after, including after an adoption expired and the shelf had
    room. The two agents had agreed and the answer was never revisited.
    """
    dream_id = _vaulted(store)
    accepts = _Speaker(verdict=TraderVerdict.ACCEPT, text="taking it")

    full = _conference(rules, store, trader=accepts).run(
        now=LATER, caps=VaultCaps(adopted=0)
    )
    assert [e.outcome for e in full.exchanges] == [ConferOutcome.ADOPTION_REFUSED]

    # Nothing about the dream has changed; only the shelf has.
    again = _conference(rules, store, trader=accepts).run(
        now=LATER + timedelta(days=1), caps=VaultCaps(adopted=3)
    )

    assert [e.outcome for e in again.exchanges] == [ConferOutcome.ADOPTED]
    dream = store.get(dream_id)
    assert dream is not None and dream.vault is Vault.ADOPTED


def test_a_refused_dream_is_still_skipped_while_the_shelf_stays_full(rules, store):
    """Deliberately narrow. Re-testing on a blocker that has NOT cleared would
    buy a model call a day to be told the same thing, which is the fifth cap
    being routed around rather than honoured."""
    _vaulted(store)
    accepts = _Speaker(verdict=TraderVerdict.ACCEPT, text="taking it")
    caps = VaultCaps(adopted=0)

    _conference(rules, store, trader=accepts).run(now=LATER, caps=caps)
    again = _conference(rules, store, trader=accepts).run(
        now=LATER + timedelta(days=1), caps=caps
    )

    assert [e.outcome for e in again.exchanges] == [ConferOutcome.NOTHING_NEW]


def test_a_dream_declined_on_its_merits_is_not_reconsidered_for_free(rules, store):
    """The re-test is about a full shelf, never about a verdict. A trader that
    said no is not owed another conversation tomorrow."""
    _vaulted(store)

    _conference(rules, store, trader=_declines()).run(now=LATER)
    again = _conference(rules, store, trader=_declines()).run(
        now=LATER + timedelta(days=1)
    )

    assert [e.outcome for e in again.exchanges] == [ConferOutcome.NOTHING_NEW]


# ------------------------------------- the numbers come from the file, not code


def test_the_conference_takes_its_caps_from_the_rules_file(rules, store):
    """`vault_caps()` had no production caller, so `caps.vault: 1` admitted
    three. A limit an operator can read has to be the limit that applies."""
    tightened = rules.model_copy(deep=True)
    tightened.dreaming.caps.adopted = 0
    _vaulted(store)
    accepts = _Speaker(verdict=TraderVerdict.ACCEPT, text="taking it")

    report = _conference(tightened, store, trader=accepts).run(now=LATER)

    assert [e.outcome for e in report.exchanges] == [ConferOutcome.ADOPTION_REFUSED]


def test_the_grant_expires_on_the_files_clock_rather_than_the_dataclass_default(
    rules, store
):
    """`TraderPowers.adopt` had no `ttl_days` at all, so every conference-made
    grant took the dataclass's 90 days and `ttl_days.adopted` in the file
    applied to nothing."""
    shortened = rules.model_copy(deep=True)
    shortened.dreaming.ttl_days.adopted = 7
    dream_id = _vaulted(store)
    accepts = _Speaker(verdict=TraderVerdict.ACCEPT, text="taking it")

    _conference(shortened, store, trader=accepts).confer_once(
        store.get(dream_id), now=LATER
    )

    adoption = store.adoptions(dream_id)[0]
    assert adoption.expires_at == LATER + timedelta(days=7)
