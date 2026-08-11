"""Tests for the exit review — the PLAN, and never the profit.

Two things are being held in place here and they are not the same thing.

The first is the classification itself: `record_exit` takes a price, a time and
a realised figure, so stop-hit, target-hit, closed-by-hand and expiry used to be
indistinguishable afterwards. Every category below has a test that produces it
and, where it matters, a test that proves it does NOT produce it from evidence
that would not support it.

The second is the boundary. This module must never learn about profit and loss,
because "review the trade so it can learn" is the reasonable-sounding request
this repository exists to refuse: forty trades is noise, and a model shown three
losses will confidently change approach. That is asserted structurally, over the
module's own AST, rather than trusted to a paragraph in a docstring — the
`test_a_dream_cannot_describe_an_order` shape, and for the same reason. A
guarantee written in prose is a guarantee that stops being checked.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bot.exit_review import classify_exit, render, review_closed_trades
from bot.models import (
    AssetClass,
    Direction,
    ExitReason,
    FillState,
    PositionAction,
    PositionActionRecord,
    Trade,
)

ENTRY = datetime(2026, 5, 4, 15, 0, tzinfo=UTC)
EXIT = datetime(2026, 5, 6, 18, 30, tzinfo=UTC)


def _trade(
    *,
    symbol: str = "SPY",
    direction: Direction = Direction.BUY,
    entry_price: float = 580.0,
    stop: float = 575.0,
    target: float | None = 590.0,
    current_stop: float | None = None,
    exit_price: float | None = None,
    exit_time: datetime | None = EXIT,
    exit_price_estimated: bool | None = False,
    fill_state: FillState = FillState.COMPLETE,
    asset_class: AssetClass = AssetClass.EQUITY,
    trade_id: int | None = 1,
) -> Trade:
    return Trade(
        id=trade_id,
        symbol=symbol,
        asset_class=asset_class,
        direction=direction,
        qty=10,
        entry_time=ENTRY,
        entry_price=entry_price,
        planned_stop=stop,
        planned_target=target,
        current_stop=current_stop,
        exit_time=exit_time,
        exit_price=exit_price,
        exit_price_estimated=exit_price_estimated,
        fill_state=fill_state,
        rationale="A trade with a plan.",
    )


def _close(
    *,
    symbol: str = "SPY",
    trade_id: int | None = 1,
    at: datetime | None = None,
    reason: str = "Taking it off ahead of the print.",
) -> PositionActionRecord:
    return PositionActionRecord(
        trade_id=trade_id,
        symbol=symbol,
        action=PositionAction.CLOSE,
        actor="operator",
        at=at or (EXIT - timedelta(minutes=5)),
        reason=reason,
    )


# --------------------------------------------------------- the plan working


def test_a_long_that_ended_at_its_stop_is_a_stop_hit():
    review = classify_exit(_trade(exit_price=574.20))
    assert review.reason is ExitReason.STOP_HIT
    assert review.ended_as_designed is True
    assert review.plan_abandoned is False
    assert "575" in review.detail        # the level it was judged against


def test_a_short_that_ended_at_its_stop_is_a_stop_hit():
    """The comparison flips with the side. A stop below entry on a short is not
    a stop, it is a target, so the sides cannot share one inequality."""
    review = classify_exit(
        _trade(
            direction=Direction.SELL,
            entry_price=773.32,
            stop=820.0,
            target=740.0,
            exit_price=821.40,
        )
    )
    assert review.reason is ExitReason.STOP_HIT


def test_a_long_that_ended_at_its_target_is_a_target_hit():
    review = classify_exit(_trade(exit_price=590.60))
    assert review.reason is ExitReason.TARGET_HIT
    assert review.ended_as_designed is True


def test_a_tightened_stop_is_the_level_a_hit_is_judged_against():
    """`effective_stop`, not `planned_stop`. Judging against the level the agent
    moved away from would make its own action invisible in the review of it."""
    review = classify_exit(_trade(current_stop=578.0, exit_price=577.50))
    assert review.reason is ExitReason.STOP_HIT
    # And against the original stop it would not have been one.
    assert classify_exit(_trade(exit_price=577.50)).reason is ExitReason.UNKNOWN


def test_a_trade_with_no_target_can_still_hit_its_stop():
    """`take_profit_price` is optional — the stop is not — so a target-shaped
    comparison must not be required to reach a verdict."""
    review = classify_exit(_trade(target=None, exit_price=574.0))
    assert review.reason is ExitReason.STOP_HIT


# ------------------------------------------- the plan abandoned: the one that matters


def test_a_hand_close_before_either_level_is_the_plan_abandoned():
    """The bucket the whole feature exists for. Discipline rather than luck: the
    market did neither of the two things the trade was built around, and
    somebody ended it anyway."""
    review = classify_exit(_trade(exit_price=581.20), actions=[_close()])

    assert review.reason is ExitReason.CLOSED_EARLY
    assert review.plan_abandoned is True
    assert review.closed_by_hand is True
    assert review.ended_as_designed is False
    # The words recorded at the moment of the move — the one thing no later
    # arithmetic could reconstruct.
    assert review.close_reason == "Taking it off ahead of the print."


def test_a_hand_close_at_a_level_is_the_plan_followed_not_abandoned():
    """Somebody pressed the button where the resting leg would have fired. That
    is the plan working, and counting it as abandonment would make discipline
    look like its opposite."""
    review = classify_exit(_trade(exit_price=574.0), actions=[_close()])
    assert review.reason is ExitReason.CLOSED_AT_LEVEL
    assert review.plan_abandoned is False
    assert review.closed_by_hand is True


def test_a_hand_close_with_no_target_set_still_reads_as_abandoned():
    review = classify_exit(_trade(target=None, exit_price=581.0), actions=[_close()])
    assert review.reason is ExitReason.CLOSED_EARLY
    assert "none was set" in review.detail


def test_a_close_recorded_against_a_different_trade_is_not_read_as_this_one():
    """Matched on `trade_id`, which is exact. Reading another row's close as
    this one's would invent an operator decision that was never made."""
    review = classify_exit(
        _trade(trade_id=7, exit_price=581.20), actions=[_close(trade_id=99)]
    )
    assert review.closed_by_hand is False
    assert review.reason is ExitReason.UNKNOWN


def test_a_close_from_an_earlier_trade_in_the_same_symbol_is_not_read_as_this_one():
    """The fallback path, for a move recorded when the journal had no row for
    the position. Bounded at both ends, or the previous SPY trade's close would
    be attributed to this one forever."""
    review = classify_exit(
        _trade(trade_id=None, exit_price=581.20),
        actions=[_close(trade_id=None, at=ENTRY - timedelta(days=3))],
    )
    assert review.closed_by_hand is False


def test_a_stop_move_is_not_a_close():
    """Only `CLOSE` ends a position. A tighten in the same symbol is a different
    move and must not be read as somebody ending the trade."""
    tighten = PositionActionRecord(
        trade_id=1,
        symbol="SPY",
        action=PositionAction.TIGHTEN_STOP,
        actor="trader",
        at=EXIT - timedelta(hours=1),
        reason="Pulling the stop in after the break held.",
        before_stop=575.0,
        after_stop=578.0,
    )
    review = classify_exit(_trade(exit_price=581.20), actions=[tighten])
    assert review.closed_by_hand is False


# ------------------------------------------------------------------- expiry


def test_an_option_gone_at_its_expiry_close_is_recorded_as_expiry():
    """Alpaca resolves these itself — auto-exercise, assignment, or liquidating
    a position the account cannot fund. None of that is a decision made here."""
    review = classify_exit(
        _trade(
            symbol="SPY260918C00580000",
            asset_class=AssetClass.OPTION,
            entry_price=5.0,
            stop=2.5,
            target=10.0,
            exit_price=3.0,
            exit_time=datetime(2026, 9, 18, 21, 0, tzinfo=UTC),
        )
    )
    assert review.reason is ExitReason.EXPIRY


def test_an_option_closed_before_expiry_is_classified_on_its_price():
    """Closing early is the only programmatic way to choose a different outcome
    at expiry, so it must stay distinguishable from letting it run out."""
    review = classify_exit(
        _trade(
            symbol="SPY260918C00580000",
            asset_class=AssetClass.OPTION,
            entry_price=5.0,
            stop=2.5,
            target=10.0,
            exit_price=2.10,
            exit_time=datetime(2026, 9, 10, 18, 0, tzinfo=UTC),
        )
    )
    assert review.reason is ExitReason.STOP_HIT


# --------------------------------------- unknown, and why it must stay unknown


def test_an_estimated_exit_price_is_never_read_as_a_level():
    """The fallback is the ENTRY price, which sits between the stop and the
    target by construction. A classifier that did not know would report a
    confident `closed_early` off no evidence at all — which is exactly the
    plausible wrong figure this repository exists to refuse."""
    review = classify_exit(_trade(exit_price=580.0, exit_price_estimated=True))
    assert review.reason is ExitReason.UNKNOWN
    assert "fallback" in review.detail


def test_a_hand_close_with_an_estimated_price_keeps_the_fact_it_was_by_hand():
    """The plan verdict is unknowable and WHO ended it is not. Losing the second
    to report the first would throw away the only fact on file."""
    review = classify_exit(
        _trade(exit_price=580.0, exit_price_estimated=True), actions=[_close()]
    )
    assert review.reason is ExitReason.UNKNOWN
    assert review.closed_by_hand is True
    assert review.close_reason
    assert review.plan_abandoned is False


def test_an_exit_price_of_unrecorded_provenance_is_not_treated_as_evidence():
    """A row closed before `exit_price_estimated` existed. Whether that price is
    a broker reading or the fallback was never written down, so it establishes
    nothing — and assuming it was real would turn every legacy row into
    evidence."""
    review = classify_exit(_trade(exit_price=574.0, exit_price_estimated=None))
    assert review.reason is ExitReason.UNKNOWN
    assert "predates" in review.detail


def test_a_close_at_neither_level_with_nothing_on_file_is_unknown():
    """Closed outside this bot: through the Alpaca web UI, or by a leg somebody
    cancelled. No reason exists, and one is not invented."""
    review = classify_exit(_trade(exit_price=581.20))
    assert review.reason is ExitReason.UNKNOWN
    assert review.ended_as_designed is None
    assert review.closed_by_hand is False


def test_an_entry_that_was_never_confirmed_is_flagged_on_the_review():
    """The row may describe an order that never filled at all. That does not
    change the classification, and a reader has to be told it."""
    review = classify_exit(
        _trade(exit_price=574.0, fill_state=FillState.RESTING)
    )
    assert review.entry_never_confirmed is True
    assert classify_exit(_trade(exit_price=574.0)).entry_never_confirmed is False


# ------------------------------------------------------------------ the report


def test_the_report_counts_every_bucket_including_the_zeros():
    """A zero beside `closed_early` is a stated fact each time this is read. The
    absence of a line is what an outage looks like."""
    report = review_closed_trades([_trade(exit_price=574.0)])
    assert set(report.counts) == set(ExitReason)
    assert report.counts[ExitReason.STOP_HIT] == 1
    assert report.counts[ExitReason.CLOSED_EARLY] == 0


def test_no_closed_trades_and_nothing_gradeable_are_different_findings():
    """Same rule as `has_cycles` in `news_history` and `can_grade_anything` in
    `triggers`. A caller handed only an empty tally reaches for the wrong
    reading every time."""
    nothing_closed = review_closed_trades([])
    assert nothing_closed.reviewed == 0
    assert nothing_closed.can_grade_anything is False

    closed_but_unknowable = review_closed_trades(
        [_trade(exit_price=580.0, exit_price_estimated=True)]
    )
    assert closed_but_unknowable.reviewed == 1
    assert closed_but_unknowable.established == 0
    assert closed_but_unknowable.can_grade_anything is False


def test_open_trades_are_skipped_rather_than_guessed_at():
    report = review_closed_trades([_trade(exit_time=None, exit_price=None)])
    assert report.reviewed == 0


def test_the_report_surfaces_the_abandoned_plans_and_the_hand_closes_apart():
    """Every hand close is counted; only the ones at neither level are the
    abandoned plans. Collapsing the two would make discipline and its opposite
    read the same."""
    report = review_closed_trades(
        [
            _trade(trade_id=1, exit_price=581.20),
            _trade(trade_id=2, exit_price=574.0),
        ],
        actions=[_close(trade_id=1), _close(trade_id=2)],
    )
    assert [r.trade_id for r in report.plan_abandoned] == [1]
    assert report.closed_by_hand == 2


def test_rows_without_price_provenance_are_counted_rather_than_believed():
    report = review_closed_trades(
        [_trade(exit_price=574.0, exit_price_estimated=None)]
    )
    assert report.exits_without_price_provenance == 1
    assert report.can_grade_anything is False


def test_render_says_nothing_closed_rather_than_printing_an_empty_tally():
    lines = render(review_closed_trades([]))
    assert any("No closed trades" in line for line in lines)


def test_render_names_the_abandoned_plans_loudly():
    lines = render(
        review_closed_trades([_trade(exit_price=581.20)], actions=[_close()])
    )
    assert any("PLAN ABANDONED" in line for line in lines)
    assert any("Taking it off ahead of the print." in line for line in lines)


# ----------------------------------- the boundary: no profit and loss, ever


#: Everything on `Trade` that is, or trivially yields, a result. Reading any one
#: of them here would make this a performance review of the trade rather than of
#: the plan — and that is the Alpha Arena failure arriving as a feature request.
FORBIDDEN_ATTRIBUTES = frozenset(
    {
        "realised_pnl_usd",
        "net_pnl_usd",
        "r_multiple",
        "mae_usd",
        "mfe_usd",
        "outcome",
        "planned_risk_usd",
        "current_risk_usd",
    }
)

MODULE = Path(__file__).resolve().parent.parent / "src" / "bot" / "exit_review.py"


def _module_ast() -> ast.Module:
    return ast.parse(MODULE.read_text())


def test_the_exit_review_reads_no_profit_or_loss():
    """Asserted over the AST rather than trusted to the docstring.

    A structural property this repository states in prose has already been found
    false once — the class hard-block on dream grants was written up as a
    guarantee, was never true, and 166 tests were green over the hole. Where a
    property is claimed, something has to fail when it is removed.
    """
    used = {
        node.attr
        for node in ast.walk(_module_ast())
        if isinstance(node, ast.Attribute)
    }
    assert not (used & FORBIDDEN_ATTRIBUTES), (
        "The exit review grades the plan, not the profit. It reached for "
        f"{sorted(used & FORBIDDEN_ATTRIBUTES)}."
    )


def test_the_exit_review_imports_no_metrics():
    """`metrics.py` is win rate, profit factor, expectancy and R. This belongs
    beside `triggers.py` and `DreamLedger`, which grade plan-following and
    reasoning quality — facts with no outcome sample to overfit to."""
    imported: set[str] = set()
    for node in ast.walk(_module_ast()):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert "metrics" not in imported
    assert not any("metrics" in name for name in imported)


def test_a_review_carries_no_figure_a_result_could_be_rebuilt_from():
    """The badge has to be safe to hand an operator without handing them a P&L
    by the back door."""
    review = classify_exit(_trade(exit_price=574.0))
    assert not hasattr(review, "realised_pnl_usd")
    assert not hasattr(review, "r_multiple")
    assert not hasattr(review, "pnl")


@pytest.mark.parametrize("reason", list(ExitReason))
def test_every_reason_is_reachable_from_some_evidence(reason):
    """A category nothing can produce is a category that lies about coverage."""
    produced = {
        classify_exit(_trade(exit_price=574.20)).reason,
        classify_exit(_trade(exit_price=590.60)).reason,
        classify_exit(_trade(exit_price=581.20), actions=[_close()]).reason,
        classify_exit(_trade(exit_price=574.0), actions=[_close()]).reason,
        classify_exit(
            _trade(
                symbol="SPY260918C00580000",
                asset_class=AssetClass.OPTION,
                entry_price=5.0,
                stop=2.5,
                target=10.0,
                exit_price=3.0,
                exit_time=datetime(2026, 9, 18, 21, 0, tzinfo=UTC),
            )
        ).reason,
        classify_exit(_trade(exit_price=580.0, exit_price_estimated=True)).reason,
    }
    assert reason in produced
