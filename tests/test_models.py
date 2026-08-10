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

from bot.models import (
    RATIONALE_MAX_CHARS,
    Direction,
    OrderProposal,
    PositionAction,
    PositionPlan,
    Stance,
    SymbolAssessment,
)


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
