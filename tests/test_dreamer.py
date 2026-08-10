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
from bot.dreamer import (
    CARRY_FORWARD,
    SCOPE,
    Dreamer,
    DreamHop,
    DreamStep,
    build_prompt,
    class_key_for_symbol,
    scope_symbols,
)
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
            return ["Crop insurers raise premiums across the Midwest"]

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

    assert seen["headlines"] == ["Crop insurers raise premiums across the Midwest"]
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
    from bot.claude_client import ClaudeClient

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

    client = ClaudeClient(_env(), "system text", cache_system=False)
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
    """
    from bot.claude_client import DREAM_MAX_TOKENS, DREAM_TIMEOUT_SECONDS, ClaudeClient
    from bot.config import ClaudeTier

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

    client = ClaudeClient(_env(), "system", tier=ClaudeTier.SONNET, cache_system=False)
    monkeypatch.setattr(client, "_client", _Inner())

    with pytest.raises(RuntimeError):
        client.dream("a prompt")

    assert captured["max_tokens"] == DREAM_MAX_TOKENS
    assert captured["output_config"] == {"effort": "high"}
    assert captured["thinking"] == {"type": "adaptive"}
    assert captured["options"] == {"timeout": DREAM_TIMEOUT_SECONDS}


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


def test_the_cost_estimate_reflects_the_tier():
    """Haiku has no thinking pass, so it is far cheaper and far shallower."""
    from bot.config import ClaudeTier
    from bot.dreamer import estimated_cost_usd

    haiku_run, haiku_year = estimated_cost_usd(ClaudeTier.HAIKU)
    sonnet_run, sonnet_year = estimated_cost_usd(ClaudeTier.SONNET)
    opus_run, _ = estimated_cost_usd(ClaudeTier.OPUS)

    assert haiku_run < sonnet_run < opus_run
    assert haiku_year == pytest.approx(haiku_run * 365)
    # A year of daily dreaming stays in double digits on the shipped tier.
    assert sonnet_year < 100
