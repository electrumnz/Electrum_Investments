"""Tests for the base strategies.

These are placeholders with no demonstrated edge, so there is nothing here that
asserts they make money. What is worth pinning is the honesty machinery: that
every strategy states an invalidation, that any strategy depending on data the
bot cannot see says so in the prompt, and that an unknown name fails loudly
rather than quietly producing a blank instruction.

That last group matters most. A model told to apply a moving-average filter it
cannot see does not decline — it estimates one and phrases the estimate
confidently, and the risk gate approves it because the gate checks size and
stops rather than whether the reasoning was invented.
"""

from __future__ import annotations

import pytest

from bot.claude_client import build_system_prompt
from bot.config import load_rules
from bot.strategy import REGISTRY, Strategy, guidance_for


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_every_strategy_states_an_invalidation(name):
    """The invalidation becomes the stop, which the risk gate requires."""
    assert REGISTRY[name].invalidation.strip()


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_every_strategy_states_a_falsifiable_thesis(name):
    """"Trade well" is not a thesis. A sentence you could be shown to be wrong about is."""
    assert len(REGISTRY[name].thesis.split()) >= 5


@pytest.mark.parametrize(
    "name", [n for n, s in sorted(REGISTRY.items()) if s.requires]
)
def test_missing_data_is_declared_in_the_prompt(name):
    """The context now carries daily bars and the indicators computed from them.

    Anything a strategy needs beyond that must say so where the model will read
    it, or the model will invent the numbers rather than decline.
    """
    rendered = guidance_for(name)

    assert "DATA YOU DO NOT HAVE" in rendered
    assert "Do not estimate the values listed above" in rendered
    assert "propose nothing" in rendered.lower()


@pytest.mark.parametrize("name", ["mean_reversion", "momentum", "trend_break"])
def test_the_strategies_now_evaluable_carry_no_warning(name):
    """Everything these name is now measured and handed over.

    `get_daily_bars` plus `indicators.py` cover mean reversion and momentum.
    `get_intraday_bars` plus `intraday.py` cover trend break: the counts of
    bars that closed beyond a level, the ones that only wicked through, the
    break volume ratio and the reclaimed flag are all computed.

    The warning has to keep meaning something. A strategy whose data is now
    present must stop claiming otherwise, or the sentence becomes decoration and
    the model learns to skip it on the two that still need it.
    """
    strategy = REGISTRY[name]

    assert strategy.requires == []
    assert "DATA YOU DO NOT HAVE" not in strategy.render()


def test_news_reaction_keeps_the_gap_intraday_bars_did_not_close():
    """Bars do not carry a spread, so "normalised" still cannot be checked.

    This is the test that stops the warning from being trimmed out of
    enthusiasm. Intraday bars closed one of this strategy's two gaps and left
    the other exactly where it was, and the surviving `requires` must name the
    survivor rather than being deleted alongside the one that was fixed.
    """
    strategy = REGISTRY["news_reaction"]

    assert strategy.requires != []
    assert any("spread" in item for item in strategy.requires)
    # The half that IS now supplied must stop being claimed as missing.
    assert not any("intraday bars" in item for item in strategy.requires)
    assert "DATA YOU DO NOT HAVE" in strategy.render()


def test_trend_break_names_the_computed_intraday_figures():
    """Evaluable is not enough; it has to point at the figures it may use.

    Without this the model reads "a break" as something to judge from the quote,
    which is the estimating-it-anyway failure the requires list existed to stop.
    """
    rendered = guidance_for("trend_break")

    assert "CLOSED beyond" in rendered
    assert "RECLAIMED" in rendered
    assert "unavailable" in rendered


def test_an_unknown_strategy_name_fails_loudly():
    """A typo in rules.yaml must not silently become 'no guidance'."""
    rendered = guidance_for("nonexistent_strategy")

    assert "No strategy named" in rendered
    assert "propose nothing" in rendered.lower()


def test_the_configured_strategy_reaches_the_system_prompt():
    """rules.yaml says mean_reversion for us_equity; the model must actually see it."""
    prompt = build_system_prompt(load_rules())

    assert "Invalidation (this is your stop)" in prompt
    assert "200-day moving average" in prompt
    # mean_reversion is evaluable now, so it carries no missing-data warning.
    # What it must carry instead is the instruction to read the computed
    # figures rather than work them out, which is the same failure guarded
    # from the other side.
    assert "DATA YOU DO NOT HAVE" not in prompt
    assert "Do not recompute them" in prompt


def test_trend_break_names_the_failed_break_as_the_main_risk():
    """The operator's stated preference, so its worst case is worth pinning."""
    assert "reclaims the broken level" in REGISTRY["trend_break"].invalidation
    assert "futures" in REGISTRY["trend_break"].notes.lower()


def test_news_reaction_defers_to_the_blackout():
    """The two could look like they conflict. The doc must say which wins."""
    notes = REGISTRY["news_reaction"].notes

    assert "blackout wins" in notes


def test_rendering_is_stable_for_the_prompt_cache():
    """Same rules in, same bytes out, or the 1-hour cache never hits."""
    assert build_system_prompt(load_rules()) == build_system_prompt(load_rules())


def test_a_strategy_with_no_missing_data_omits_the_warning():
    """The warning must mean something, so it cannot appear unconditionally."""
    complete = Strategy(
        name="complete",
        thesis="Everything needed to evaluate this is already in the context",
        entry=["a condition that uses only the current quote"],
        invalidation="a level",
        exit="another level",
    )

    assert "DATA YOU DO NOT HAVE" not in complete.render()
