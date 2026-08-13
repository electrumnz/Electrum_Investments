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

from bot.config import load_rules
from bot.model_client import build_system_prompt
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


def test_no_unescaped_brace_reaches_the_prompt_template():
    """A literal `{` in SYSTEM_PROMPT_TEMPLATE is read by `str.format` first.

    The template is rendered with `.format(rules_summary=...)`, so an example
    written as `{field: "close"}` raises `KeyError: 'field'` and takes down
    every cycle — the model call, the smoketest and the loop alike. It is
    invisible until something builds the prompt, which is exactly the shape of
    the `render.STYLES` backslash trap.

    Braces meant literally must be doubled. This asserts the template still
    renders rather than trying to police the source text.
    """
    from bot.model_client import SYSTEM_PROMPT_TEMPLATE, build_system_prompt

    rendered = build_system_prompt(load_rules())

    # The escaped example survives as a single brace in the output.
    assert '{field: "close", op: "below", value: 641.20}' in rendered
    # And nothing else in the template is an accidental placeholder.
    assert SYSTEM_PROMPT_TEMPLATE.format(rules_summary="x")


def test_the_prompt_states_that_an_out_of_hours_entry_rests():
    """The operator opened the gate's window to pre-market and after hours, so
    the model may now propose there. What it cannot discover for itself is that
    the order does not TRADE there.

    Every entry is a bracket or an OTO because the stop has to reach the broker
    with it, and Alpaca refuses `extended_hours` on both. So the order rests and
    fills at the next open, at a price that appears nowhere in its context. A
    model that does not know this reads a thin one-sided pre-market quote as its
    fill price, which is the confident-wrong-figure failure this repository is
    built around.

    The session block in the market context says this too, but only while a
    session is shut. This belongs in the cached system prompt because it is a
    permanent property of the order path.
    """
    # Collapsed, because the template is hard-wrapped and a phrase that happens
    # to straddle a line break is still a phrase the model reads. Pinning the
    # wording should not also pin where the newlines fall.
    prompt = " ".join(build_system_prompt(load_rules()).split())

    assert "does not fill out of hours" in prompt
    assert "It rests" in prompt
    assert "extended-hours venues accept limit orders only" in prompt
    assert "not the quote you were shown" in prompt


def test_the_prompt_does_not_offer_a_looser_stop_as_the_answer():
    """The tempting wrong response to an uncertain fill. Size is computed from
    the stop distance, so widening the stop to feel safer buys a bigger loss at
    the same 1% — and the gate approves it, because it checks the arithmetic and
    not the intent."""
    prompt = " ".join(build_system_prompt(load_rules()).split())

    assert "Widen nothing to compensate" in prompt
    assert "smaller size or no trade" in prompt


def test_the_prompt_exempts_crypto_from_the_session_mechanics():
    """A 24/7 market has no out of hours, and Alpaca accepts no bracket on it,
    so both halves of the warning are wrong there. Stated rather than left to
    inference, because the model is told everything else in equity terms."""
    prompt = " ".join(build_system_prompt(load_rules()).split())

    assert "Crypto has no sessions" in prompt


# ------------------------------------------------- moving a stop, in the prompt


def test_the_prompt_asks_for_the_level_a_tighten_needs():
    """`PositionPlan.new_stop_price` was in the schema and in nothing the model
    reads, so it came back empty on every cycle and `execute_position_plan`
    refused every tighten for want of a level.

    A field the prompt never mentions is a field the model does not fill. This
    is the plumbing half of the position-actions work rather than a wording
    preference.
    """
    prompt = " ".join(build_system_prompt(load_rules()).split())

    assert "new_stop_price" in prompt
    assert "needs `reasoning` and `new_stop_price` together" in prompt


def test_the_prompt_refuses_a_widened_stop_in_the_same_words_the_code_does():
    """Tighter is toward ENTRY, which inverts between a long and a short, and
    half the trades this repository can hold are shorts.

    The refusal itself lives in `position_actions.classify_stop_move` and is
    deterministic — this is the model being told what will happen rather than
    the thing that makes it happen. Both are needed: prose is the wrong place
    for the only guard, and a silent refusal every cycle is the wrong way to
    teach.
    """
    prompt = " ".join(build_system_prompt(load_rules()).split())

    assert "may only be TIGHTENED, never widened" in prompt
    assert "on a long that is a HIGHER stop, on a short a LOWER one" in prompt
    assert "bigger loss at the same size" in prompt


def test_the_prompt_does_not_let_a_plan_read_as_an_action():
    """`position_actions.enabled` ships false, so a plan is recorded and not
    executed. The model must not write as though the move has happened, and
    must not restate it as done next cycle — it has no memory, so the only
    thing it can rely on is the level it is shown in the position line."""
    prompt = " ".join(build_system_prompt(load_rules()).split())

    assert "ships false" in prompt
    assert "recorded and none is executed" in prompt
    assert "do not restate a tighten as done" in prompt
