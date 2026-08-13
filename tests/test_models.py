"""The domain models, and the one place they are deliberately lenient.

Everything here rejects bad input except free-form prose, which truncates. That
split is the whole content of this file: it was written after a live cycle died
because one rationale came back 34 characters over a 500-character cap, and the
`ValidationError` took the trading loop down with it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from bot.config import Rules
from bot.models import (
    RATIONALE_MAX_CHARS,
    Direction,
    OrderProposal,
    PositionAction,
    PositionPlan,
    Stance,
    SymbolAssessment,
    trailing_stop_level,
)
from bot.risk import RiskGate

from .conftest import INSIDE_SESSION, PAPER_EQUITY


def _proposal(rationale: str) -> OrderProposal:
    return OrderProposal(
        symbol="SPY",
        direction=Direction.BUY,
        qty=10,
        limit_price=580.00,
        stop_loss_price=575.00,
        take_profit_price=590.00,
        rationale=rationale,
    )


# ------------------------------------------------------------- free-form prose


def test_an_over_long_rationale_is_trimmed_rather_than_rejected():
    """The live failure. Nothing downstream parses this field.

    A rejected response costs the entire cycle and, before the loop caught it,
    the whole process. Losing the last clause of a sentence costs a reader a
    little context on the Decisions page. Those are not close.
    """
    proposal = _proposal("A" * (RATIONALE_MAX_CHARS + 500))

    assert len(proposal.rationale) == RATIONALE_MAX_CHARS
    assert proposal.rationale.endswith("…")


def test_a_rationale_within_the_cap_is_left_exactly_alone():
    text = "AAPL is 1.13 ATR below its 20-day average with volume confirming."

    assert _proposal(text).rationale == text


def test_the_cap_is_generous_enough_for_the_answer_the_prompt_asks_for():
    """500 was chosen before the prompt asked for a stance on every symbol.

    The observed failure was a rationale of roughly 530 characters, which is an
    ordinary paragraph, not a rambling one.
    """
    assert RATIONALE_MAX_CHARS >= 2000


def test_an_empty_rationale_is_still_refused():
    """Lenient about length is not lenient about having said nothing."""
    with pytest.raises(ValidationError):
        _proposal("short")


@pytest.mark.parametrize("field", ["reasoning", "waiting_for"])
def test_assessment_prose_truncates_too(field):
    """Same reasoning, and there is one assessment per symbol every cycle."""
    values: dict[str, Any] = {"reasoning": "Watching for a break.", "waiting_for": ""}
    values[field] = "B" * (RATIONALE_MAX_CHARS + 100)

    assessment = SymbolAssessment(symbol="QQQ", stance=Stance.WATCH, **values)

    assert len(getattr(assessment, field)) == RATIONALE_MAX_CHARS


def test_position_plan_prose_truncates_too():
    plan = PositionPlan(
        symbol="SPY",
        action=PositionAction.HOLD,
        reasoning="C" * (RATIONALE_MAX_CHARS + 100),
    )

    assert len(plan.reasoning) == RATIONALE_MAX_CHARS


# ------------------------------------------------------------------- numbers


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("qty", 0),
        ("qty", -5),
        ("limit_price", 0),
        ("stop_loss_price", 0),
        ("take_profit_price", -1),
    ],
)
def test_numbers_still_reject_and_are_never_coerced(field, value):
    """The leniency above must not spread.

    A truncated number is a different number, and a plausible wrong figure that
    passes validation is precisely the failure this repository exists to
    prevent. Prose is safe to trim because nothing reads it; a price is not.
    """
    values: dict[str, object] = {
        "symbol": "SPY",
        "direction": Direction.BUY,
        "qty": 10,
        "limit_price": 580.00,
        "stop_loss_price": 575.00,
        "take_profit_price": 590.00,
        "rationale": "Perfectly reasonable rationale here.",
    }
    values[field] = value

    with pytest.raises(ValidationError):
        OrderProposal(**values)  # type: ignore[arg-type]


def test_a_position_still_refuses_a_negative_quantity():
    from bot.models import Position

    with pytest.raises(ValidationError):
        Position(
            symbol="SPY",
            direction=Direction.BUY,
            qty=-1,
            entry_price=580.0,
            opened_at=datetime.now(UTC),
        )


# ---------------------------------------------------------------- the trail
#
# `take_profit_price` was already optional, so the agent could choose a target
# or none. What it could not express was a TRAIL, and the exit is the agent's
# decision — so the field it needed was a trail, not a second fixed price.
#
# Everything below is about the one property that makes a trail safe to have
# beside a risk gate that never sees it: a trail can only ever TIGHTEN.


def _trailing(**overrides: Any) -> OrderProposal:
    values: dict[str, Any] = {
        "symbol": "SPY",
        "direction": Direction.BUY,
        "qty": 3,
        "limit_price": 580.00,
        "stop_loss_price": 575.00,
        "take_profit_price": None,
        "trail_percent": 1.5,
        "rationale": "Trend trade; let it run behind a 1.5% trail.",
    }
    values.update(overrides)
    return OrderProposal(**values)


def test_no_trail_is_the_default_and_is_a_fixed_stop():
    """A field that has to be omitted to get today's behaviour would be a
    change to every existing caller. `None` is the fixed stop."""
    assert _proposal("Perfectly ordinary fixed-stop trade.").trail_percent is None
    assert _proposal("Perfectly ordinary fixed-stop trade.").exit_is_trailing is False


def test_a_trail_is_expressed_by_one_field_and_nothing_else():
    """One fact, not a style enum plus a number that can disagree with it.

    `exit_is_trailing` is the single reading of it, so no caller writes
    `> 0` where another wrote `is not None`.
    """
    proposal = _trailing()

    assert proposal.exit_is_trailing is True
    assert proposal.trail_percent == 1.5


@pytest.mark.parametrize("value", [0, -1.0, 100.0, 250.0])
def test_a_nonsense_trail_rejects_rather_than_being_coerced(value):
    """Numbers reject. A trail of 100% puts a long's stop at zero and a short's
    at twice the price, which is not a stop — that is arithmetic, not an
    opinion about where a stop belongs."""
    with pytest.raises(ValidationError):
        _trailing(trail_percent=value)


def test_a_trail_does_not_change_what_the_trade_risks():
    """The reason the risk gate needed no edit at all.

    Risk is `|entry - stop| x qty` and the initial stop is unchanged, so a
    trailing exit is sized exactly as the same trade with a fixed stop. A trail
    that moved this figure would be a bigger loss at the same 1%.
    """
    fixed = _proposal("Perfectly ordinary fixed-stop trade.")
    trailing = _trailing(qty=fixed.qty, take_profit_price=590.00)

    assert trailing.risk_usd == fixed.risk_usd == pytest.approx(50.0)


def test_a_trail_only_tightens_a_long():
    """The whole guarantee, on the side where it is easy to believe.

    580 entry, 575 stop, 1% trail. The trail implies 574.20 while price sits at
    the entry, which is LOOSER than the stop already in force — so it does
    nothing. Only once price has run does it take over.
    """
    at_entry = trailing_stop_level(
        direction=Direction.BUY,
        high_water_mark=580.0,
        trail_percent=1.0,
        stop_in_force=575.0,
    )
    assert at_entry == 575.0

    after_a_run = trailing_stop_level(
        direction=Direction.BUY,
        high_water_mark=600.0,
        trail_percent=1.0,
        stop_in_force=575.0,
    )
    assert after_a_run == pytest.approx(594.0)


def test_a_trail_only_tightens_a_short():
    """Same property, mirrored, and this is the side that would have been wrong.

    On a short the stop sits ABOVE entry and tightening means coming DOWN, so
    the floor is a cap. Short from 773.32 with the stop at 820: a 2% trail off a
    low of 740 implies 754.80, which is tighter, and off the entry itself
    implies 788.79 — still tighter than 820, so it takes over immediately.
    Nothing here may ever return a number above the stop in force.
    """
    tightened = trailing_stop_level(
        direction=Direction.SELL,
        high_water_mark=740.0,
        trail_percent=2.0,
        stop_in_force=820.0,
    )
    assert tightened == pytest.approx(754.8)

    # A high-water mark that has gone the wrong way cannot give room back.
    assert (
        trailing_stop_level(
            direction=Direction.SELL,
            high_water_mark=900.0,
            trail_percent=2.0,
            stop_in_force=820.0,
        )
        == 820.0
    )


def test_a_trail_wider_than_the_initial_stop_simply_does_nothing_yet():
    """It must not be read as widening. A 10% trail on a stop 0.9% away is a
    stop that stays where it is until price has run far enough to earn the
    move — never a stop that drops to 522."""
    assert (
        trailing_stop_level(
            direction=Direction.BUY,
            high_water_mark=580.0,
            trail_percent=10.0,
            stop_in_force=575.0,
        )
        == 575.0
    )


def test_a_high_water_mark_that_is_not_a_price_is_refused():
    """The two sides must not fail differently.

    Zero is absorbed harmlessly by the floor on a long and would compute a stop
    of ZERO on a short — capped down to it, so the position is stopped out
    instantly. A function that is safe on one side and catastrophic on the other
    is worse than one that refuses on both.
    """
    with pytest.raises(ValueError, match="real price"):
        trailing_stop_level(
            direction=Direction.SELL,
            high_water_mark=0.0,
            trail_percent=2.0,
            stop_in_force=820.0,
        )


# ------------------------------------------- the gate still refuses what it did
#
# A new field on `OrderProposal` is a new order path, and the rule in this
# repository is that one needs a test proving the gate still REJECTS rather than
# merely that the field exists. `risk.py` was not touched; these are what say so.


def _gate(rules: Rules) -> RiskGate:
    return RiskGate(
        rules, equity_at_session_start=PAPER_EQUITY, now=INSIDE_SESSION
    )


def test_a_trailing_proposal_is_still_sized_from_the_initial_stop(
    rules, account, spy_tick
):
    """Asking for a trail buys no extra room.

    250 shares with the stop $5 below entry is $1,250 of risk against a $1,000
    per-trade cap on $100k of equity. The 1.5% trail on the same proposal is
    worth $8.70 a share, so a gate that had started sizing from the trail would
    have found $3,262 and refused it for a different reason — or, had it taken
    the trail as the risk on a TIGHTER trail, approved a trade that does not
    fit. Neither happens, because the gate reads `stop_loss_price` and nothing
    about the exit.
    """
    oversized = _trailing(qty=250, stop_loss_price=575.00)

    verdict = _gate(rules).evaluate(oversized, account=account, tick=spy_tick)

    assert not verdict.approved
    assert any("per-trade cap" in reason for reason in verdict.reasons), verdict.reasons


def test_a_trail_does_not_excuse_a_stop_on_the_wrong_side(rules, account, spy_tick):
    """`_stops_on_correct_side` checks the SIDE each level sits on, and a trail
    has no side of its own — it starts at the initial stop. A buy whose stop is
    above entry is still not a stop, trail or no trail."""
    backwards = _trailing(stop_loss_price=585.00)

    verdict = _gate(rules).evaluate(backwards, account=account, tick=spy_tick)

    assert not verdict.approved
    assert any("stop-loss" in reason for reason in verdict.reasons), verdict.reasons


def test_the_same_trade_is_approved_with_and_without_a_trail(
    rules, account, spy_tick, buy_proposal
):
    """The other half of the same claim: a trail is not a new gate to pass.

    If adding one changed a verdict, the exit would have become a risk input,
    and the gate would be measuring intent rather than arithmetic.
    """
    with_trail = buy_proposal.model_copy(update={"trail_percent": 1.5})

    plain = _gate(rules).evaluate(buy_proposal, account=account, tick=spy_tick)
    trailed = _gate(rules).evaluate(with_trail, account=account, tick=spy_tick)

    assert plain.approved, plain.reasons
    assert trailed.approved, trailed.reasons
