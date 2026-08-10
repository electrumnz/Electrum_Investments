"""The character files the two agents speak in.

These are prompt text, so nothing here can assert that an agent *behaves*. What
it can assert is that the files are present, that they still carry the clauses
the rest of the system leans on, and that losing one degrades a page rather than
breaking it.
"""

from __future__ import annotations

from bot.souls import DEFAULT_SOULS_DIR, GROGU, YODA, Soul, load_soul

BOTH = (YODA, GROGU)


# ------------------------------------------------------------------ present


def test_both_souls_ship_and_load():
    for name in BOTH:
        soul = load_soul(name)
        assert soul.found, f"{name}.md did not load from {DEFAULT_SOULS_DIR}"
        assert soul.text.strip()


def test_each_soul_carries_the_sections_the_convention_expects():
    """The Hermes SOUL.md shape, so anyone who has written one recognises it."""
    for name in BOTH:
        text = load_soul(name).text
        for heading in ("## Personality", "## Style", "## What to avoid"):
            assert heading in text, f"{name}.md is missing {heading}"


# ------------------------------- the clauses the rest of the system leans on


def test_neither_agent_is_permitted_to_propose_a_trade():
    """Both souls say it, and both have to.

    Yoda is not the model that proposes and the dreamer has no order path at
    all. Neither statement is enforced by prose — `RiskGate.evaluate` and the
    absence of order fields on `Dream` do that — but a character file that
    invited a trade recommendation would be working against both.
    """
    for name in BOTH:
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
