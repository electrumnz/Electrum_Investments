"""The character files the three agents speak in.

These are prompt text, so nothing here can assert that an agent *behaves*. What
it can assert is that the files are present, that they still carry the clauses
the rest of the system leans on, and that losing one degrades a page rather than
breaking it.
"""

from __future__ import annotations

from bot.souls import (
    ARMORER,
    DEFAULT_SOULS_DIR,
    GROGU,
    KNOWN_SOULS,
    YODA,
    Soul,
    load_soul,
)

ALL = (YODA, GROGU, ARMORER)


# ------------------------------------------------------------------ present


def test_every_soul_ships_and_loads():
    for name in ALL:
        soul = load_soul(name)
        assert soul.found, f"{name}.md did not load from {DEFAULT_SOULS_DIR}"
        assert soul.text.strip()


def test_each_soul_carries_the_sections_the_convention_expects():
    """The Hermes SOUL.md shape, so anyone who has written one recognises it."""
    for name in ALL:
        text = load_soul(name).text
        for heading in ("## Personality", "## Style", "## What to avoid"):
            assert heading in text, f"{name}.md is missing {heading}"


def test_each_soul_says_how_long_an_answer_should_be():
    """A voice with no length budget writes an essay, every time.

    The operator reads these replies on a phone between other things, and the
    failure they reported was not that the agents were wrong — it was that a
    figure arrived wrapped in three paragraphs of reasoning nobody asked for.
    Character is in the word choice; length is not character. So each file
    carries a concrete target rather than an instruction to "be concise",
    which every model already believes it is following.
    """
    for name in ALL:
        assert "## How long to be" in load_soul(name).text, f"{name}.md sets no length"


# The ceiling is generous — every file sits comfortably under it — and it is
# here to catch REGROWTH rather than to police a rewrite. A soul is a voice; the
# mechanics of dreaming, conferring and settings live in the system prompts in
# `dreamer.py`, `confer.py` and `settings_agent.py`, which is where a new rule
# belongs. A file that has grown past this has started restating one of those,
# and the fix is to move it back rather than to raise the number.
SOUL_MAX_WORDS = 1600


def test_no_soul_has_grown_back_into_a_manual():
    for name in ALL:
        words = len(load_soul(name).text.split())
        assert words <= SOUL_MAX_WORDS, f"{name}.md is {words} words; it is a manual again"


# ------------------------------- the clauses the rest of the system leans on


def test_no_agent_is_permitted_to_propose_a_trade():
    """All three say it, and all three have to.

    Yoda is not the model that proposes, the dreamer has no order path at all,
    and the Armorer is the wrong agent for the question entirely — a limit
    discussed in terms of a specific trade is a limit being widened to admit
    that trade. None of it is enforced by prose: `RiskGate.evaluate`, the
    absence of order fields on `Dream`, and the root ownership of `config/` do
    that. But a character file that invited a trade recommendation would be
    working against all three.
    """
    for name in ALL:
        assert "Never propose a trade" in load_soul(name).text


def test_neither_agent_may_invent_a_figure():
    """The single most dangerous output either could produce.

    A plausible fabricated statistic is the part a reader does not think to
    check, which is the whole failure this repository is built around.
    """
    yoda = load_soul(YODA).text
    grogu = load_soul(GROGU).text

    assert "Never state a figure you did not read" in yoda
    assert "Never state a number you did not read somewhere" in grogu
    assert "Never state a figure you did not read" in load_soul(ARMORER).text


def test_neither_agent_learns_from_the_track_record():
    """Forty trades is noise, and a model shown three losses changes approach.

    `metrics.py` reaches the operator through the Analytics page and stops
    there, on purpose. A soul that encouraged either agent to draw lessons from
    profit and loss would route around that decision in prose.
    """
    assert "Never present a track record as a lesson" in load_soul(YODA).text
    assert "Never learn from the account's profit and loss" in load_soul(GROGU).text


def test_the_gate_cannot_be_argued_with_in_character():
    assert "Never argue with the risk gate" in load_soul(YODA).text


def test_the_dreamer_states_its_weakest_hop():
    """Confidence in a chain is the minimum across its links, not the average."""
    assert "Never present a chain as stronger than its weakest hop" in load_soul(GROGU).text


# ------------------------------------------------------------------ the forge


def test_the_armorer_pushes_back_and_does_not_deny():
    """The whole design, and the one thing this character must not get wrong.

    A hard refusal is the same intent as an argument implemented as a wall, at
    the moment it helps least. There was a validator in `config.py` that refused
    a looser per-class limit at config load; it was removed, and this agent is
    what replaced it. A soul that told it to refuse would reinstate the wall in
    prose.
    """
    text = load_soul(ARMORER).text

    assert "You push back. You do not deny." in text
    assert "Tightening is not an argument." in text
    assert "Loosening is an argument" in text


def test_the_armorer_never_claims_to_have_changed_anything():
    """It cannot write `config/rules.yaml` and must never say it did.

    The structural guarantee is that `config/` is root-owned so the service
    account cannot edit its own limits. This clause is what stops the agent
    *describing* an outcome the arrangement did not produce, which is the
    confident-partial-answer failure arriving through the voice.
    """
    text = load_soul(ARMORER).text

    assert "Never claim to have changed anything" in text
    assert 'Say "recorded"' in text


def test_the_armorer_will_not_widen_a_limit_for_a_specific_trade():
    """The failure the whole feature exists to prevent.

    `CLAUDE.md`: never widen a limit in `config/rules.yaml` to make a specific
    trade fit. An agent whose job is arguing about limits is exactly the agent
    somebody would try that on.
    """
    text = load_soul(ARMORER).text

    assert "Never widen a limit to make a specific trade fit" in text
    assert "Never argue from the track record" in text


def test_the_armorer_keeps_its_objection_after_losing_the_argument():
    """An argument the operator won is a fact worth keeping, and so is one they
    lost. The store has a column for it; the character must not soften it away
    once the operator has decided."""
    assert (
        "Never let being agreeable cost the operator the objection"
        in load_soul(ARMORER).text
    )


def test_the_armorer_knows_a_class_limit_is_not_capped_by_the_portfolio_one():
    """`account:` is the default, not a ceiling — but a per-trade limit above
    `max_total_risk_pct` can never fill, because the portfolio gate refuses the
    trade first. That is a fact the operator deserves before the change, not
    after."""
    assert (
        "Never treat a per-class limit as capped by the portfolio one"
        in load_soul(ARMORER).text
    )


def test_every_shipped_soul_is_selectable_and_nothing_else_is():
    """`KNOWN_SOULS` is what a request body is checked against before the name
    reaches a path join.

    Two failures it prevents, and they pull in opposite directions: a soul
    added to `souls/` and forgotten there is a character nobody can select, and
    a name that is NOT in the set reaching `load_soul` is a path traversal.
    """
    assert set(ALL) == KNOWN_SOULS
    for name in ALL:
        assert load_soul(name).found


# ------------------------------------------------------------- the wrapping


def test_the_prefix_repeats_the_rule_the_files_carry():
    """Said twice on purpose.

    Souls are read from disk at call time so they can be edited without a
    deploy. That means the text reaching the model could be a file somebody
    changed on the box, so the framing this module wraps around it restates the
    one rule that must survive any edit.
    """
    prefix = load_soul(YODA).prompt_prefix()

    assert "never what is true" in prefix
    assert "exactly as the tools" in prefix
    assert "begin character: yoda" in prefix
    assert "end character: yoda" in prefix


# ------------------------------------------------------------------- absent


def test_a_missing_soul_degrades_instead_of_raising(tmp_path):
    """A personality must not be able to take a page down.

    Same failure direction as `HermesBridge.available`: the agent still answers,
    still reaches the same tools, still bound by the same limits. It simply
    sounds like nothing in particular.
    """
    soul = load_soul("nobody-home", souls_dir=tmp_path)

    assert not soul.found
    assert soul.prompt_prefix() == ""


def test_an_empty_soul_file_counts_as_absent(tmp_path):
    """A file truncated to nothing must not produce a prefix framing no character."""
    (tmp_path / "hollow.md").write_text("   \n\n")

    assert not load_soul("hollow", souls_dir=tmp_path).found


def test_an_unreadable_directory_is_absent_not_an_exception(tmp_path):
    assert not load_soul("anything", souls_dir=tmp_path / "does-not-exist").found


def test_an_absent_soul_contributes_nothing_to_a_prompt():
    assert Soul.absent("x").prompt_prefix() == ""
