"""A stop's width against the symbol's own range, and the two ways it is unknown.

`TODO.md` item 25: size is the risk ceiling divided by the stop distance, so a
stop inside the spread buys an arbitrarily large position at the same stated
dollar risk. Every figure the sizing block prints stays correct while it
happens, because the exposure arrives through the denominator.

The tests here pin three things, and the second and third are the ones that
would rot quietly:

- the arithmetic, including that it is the same for a long and for a short;
- that an unmeasurable ATR reports NOT MEASURED and never reads as fine;
- that nothing here refuses anything — this reports, and the gate keeps holding
  no opinion on stop placement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from bot.audit import AuditView, DecisionEntry
from bot.indicators import Indicators
from bot.models import (
    AssetClass,
    Decision,
    Direction,
    IndicatorSnapshot,
    MarketInputs,
    OrderProposal,
)
from bot.stop_width import (
    TIGHT_ATR_FRACTION,
    StopWidth,
    measure_stop_widths,
    tight_stop_symbols,
    unmeasured_stop_symbols,
)
from bot.web import render


def _proposal(
    symbol: str = "KO",
    *,
    direction: Direction = Direction.BUY,
    limit: float = 70.00,
    stop: float = 69.95,
) -> OrderProposal:
    return OrderProposal(
        symbol=symbol,
        asset_class=AssetClass.EQUITY,
        direction=direction,
        qty=10,
        limit_price=limit,
        stop_loss_price=stop,
        rationale="a reason long enough to satisfy the model's own contract",
    )


def _indicators(atr: float | None) -> Indicators:
    return Indicators(
        symbol="KO", bar_count=200, last_close=70.0, last_volume=1_000_000, atr_14=atr
    )


def test_the_KO_proposal_from_item_25_is_reported_as_tight() -> None:
    """The measurement that produced the item: $0.05 against a $1.32 ATR.

    0.04 ATR — inside the spread — which at the same stated risk buys roughly
    twenty-six times the position a 1-ATR stop would. Nothing refused it and
    nothing should; what was missing was anybody saying so.
    """
    widths = measure_stop_widths(
        [_proposal(limit=70.00, stop=69.95)], {"KO": _indicators(1.32)}
    )
    (width,) = widths
    assert width.atr_multiple is not None
    assert width.atr_multiple == pytest.approx(0.0378, abs=1e-4)
    assert width.is_tight
    assert not width.is_unmeasured
    assert "TIGHT" in width.render()
    # The consequence, not only the ratio. A reader scanning for a problem sees
    # "26x" long before they work out what 0.04 ATR implies.
    assert "26x" in width.render()


def test_an_ordinary_stop_is_measured_and_is_not_tight() -> None:
    widths = measure_stop_widths(
        [_proposal(limit=70.00, stop=68.00)], {"KO": _indicators(1.32)}
    )
    (width,) = widths
    assert width.atr_multiple == pytest.approx(2.0 / 1.32)
    assert not width.is_tight
    assert not width.is_unmeasured
    assert "TIGHT" not in width.render()
    assert tight_stop_symbols(widths) == []


def test_a_short_measures_the_same_distance_as_the_long() -> None:
    """The classic arithmetic error here is a sign that is right one way only.

    On a short the stop sits ABOVE entry, so a signed subtraction would come
    back negative and compare as comfortably wide against any threshold. The
    distance is absolute for exactly that reason.
    """
    long_width = measure_stop_widths(
        [_proposal(direction=Direction.BUY, limit=70.00, stop=69.95)],
        {"KO": _indicators(1.32)},
    )[0]
    short_width = measure_stop_widths(
        [_proposal(direction=Direction.SELL, limit=70.00, stop=70.05)],
        {"KO": _indicators(1.32)},
    )[0]
    assert short_width.stop_distance == pytest.approx(long_width.stop_distance)
    assert short_width.is_tight and long_width.is_tight


def test_a_symbol_with_no_indicators_is_unmeasured_and_never_tight() -> None:
    """`stops_unchecked` one column across.

    `fetch_indicators` drops a symbol whose bars failed, so an absent entry
    means "could not ask". Counting it as not-tight would make the reassuring
    answer the one an outage produces.
    """
    widths = measure_stop_widths([_proposal("ZZZZ")], {})
    (width,) = widths
    assert width.atr_multiple is None
    assert width.is_unmeasured
    assert not width.is_tight
    assert unmeasured_stop_symbols(widths) == ["ZZZZ"]
    assert tight_stop_symbols(widths) == []
    assert "NOT measured" in width.render()


def test_an_indicator_present_but_carrying_no_ATR_is_also_unmeasured() -> None:
    """Present-with-None is a different route to the same honest answer.

    `Indicators` keeps `None` when the bars cannot support a 14-period ATR —
    a fresh listing, a symbol granted by an adopted dream. The symbol IS in the
    mapping, so a `.get()` that only checked membership would sail past it.
    """
    widths = measure_stop_widths([_proposal()], {"KO": _indicators(None)})
    assert widths[0].is_unmeasured
    assert "NOT measured" in widths[0].render()


def test_a_zero_ATR_is_unmeasurable_rather_than_infinitely_tight() -> None:
    """A flat instrument has no denominator, and the honest answer is unknown.

    The tempting arrangement — treat a zero range as maximally tight — states a
    comparison against a denominator that does not exist, and it divides by
    zero to do it. A very large number here would be a plausible wrong figure
    on the exact surface built to refuse them.
    """
    for atr in (0.0, -1.0):
        (width,) = measure_stop_widths([_proposal()], {"KO": _indicators(atr)})
        assert width.atr_multiple is None
        assert width.is_unmeasured
        assert not width.is_tight


def test_the_recorded_snapshot_measures_the_same_as_the_live_indicators() -> None:
    """One arithmetic, two inputs, so the page cannot drift from the heartbeat.

    The loop measures against `Indicators` and the Decisions page measures the
    same proposal months later against the `IndicatorSnapshot` the audit log
    recorded. Two implementations would be two answers, and the one on the page
    is the one nobody would re-check.
    """
    live = measure_stop_widths([_proposal()], {"KO": _indicators(1.32)})[0]
    recorded = measure_stop_widths(
        [_proposal()], {"KO": IndicatorSnapshot(close=70.0, atr_14=1.32)}
    )[0]
    assert recorded.atr_multiple == live.atr_multiple
    assert recorded.render() == live.render()


def test_the_boundary_is_below_the_fraction_and_not_at_it() -> None:
    """Exactly at the line is not tight. A threshold has to answer at its edge."""
    atr = 4.0
    exactly = TIGHT_ATR_FRACTION * atr
    at = StopWidth("KO", "buy", 70.0, 70.0 - exactly, atr)
    under = StopWidth("KO", "buy", 70.0, 70.0 - exactly * 0.99, atr)
    assert not at.is_tight
    assert under.is_tight


def test_the_order_of_the_measurements_follows_the_proposals() -> None:
    """The Decisions page pairs by index, the same way it pairs verdicts.

    A function that filtered or sorted would silently attach one proposal's
    measurement to another's row, which is the failure `verdict_for` refuses to
    make by returning None rather than guessing.
    """
    proposals = [_proposal("AAA"), _proposal("BBB"), _proposal("CCC")]
    widths = measure_stop_widths(proposals, {"BBB": _indicators(1.0)})
    assert [w.symbol for w in widths] == ["AAA", "BBB", "CCC"]


def test_measuring_a_stop_refuses_nothing() -> None:
    """The gate's silence on stop placement is deliberate and stays.

    `CLAUDE.md`: *"the honest answer to 'is this stop any good' is not the
    gate's to give"*. Item 25 says the same in different words — a minimum stop
    distance is a rule nobody agreed to, arriving through the back door. This
    module must therefore have no way to say no, and the wording it uses must
    say so out loud to whoever reads the page.
    """
    (width,) = measure_stop_widths(
        [_proposal(limit=70.00, stop=69.99)], {"KO": _indicators(2.0)}
    )
    assert width.is_tight
    assert "Not a rejection" in width.render()
    assert "holds no view" in width.render()


def test_the_protocol_accepts_any_carrier_of_an_ATR() -> None:
    """A structural type, so a third recorder cannot fork the arithmetic."""

    @dataclass(frozen=True)
    class OnlyAnATR:
        atr_14: float | None

    (width,) = measure_stop_widths([_proposal()], {"KO": OnlyAnATR(1.32)})
    assert width.atr_multiple == pytest.approx(0.05 / 1.32)


# ---------------------------------------------------------------------------
# The Decisions page, which is the only surface a REJECTED proposal exists on.
# ---------------------------------------------------------------------------


def _entry(
    proposals: list[OrderProposal], readings: dict[str, IndicatorSnapshot] | None
) -> DecisionEntry:
    decision = Decision(
        timestamp=datetime(2026, 5, 4, 15, 0, tzinfo=UTC),
        proposals=proposals,
        inputs=None if readings is None else MarketInputs(readings=readings),
    )
    return DecisionEntry(timestamp=decision.timestamp, decision=decision)


def test_the_decisions_page_states_a_tight_stop_under_the_proposal() -> None:
    body = render.decisions(
        AuditView(decisions=[_entry([_proposal()], {"KO": IndicatorSnapshot(close=70.0, atr_14=1.32)})])
    )
    assert "TIGHT" in body
    assert "rung stopw tight" in body


def test_the_decisions_page_says_NOT_measured_rather_than_leaving_it_blank() -> None:
    """A proposal with no line under it reads as one that was checked.

    Which is exactly the reading a failed indicator fetch must not produce —
    `stops_unchecked` again, on a page instead of on a log line.
    """
    body = render.decisions(AuditView(decisions=[_entry([_proposal()], {})]))
    assert "NOT measured" in body
    assert "rung stopw unmeasured" in body


def test_a_record_written_before_readings_existed_renders_unmeasured() -> None:
    """The audit log is append-only and never migrated, so this record is real.

    Nothing recovers a figure that was never written down. Rendering an
    ordinary-looking width for a cycle that recorded no numbers would be
    inventing one, which is the failure this whole surface exists to refuse.
    """
    body = render.decisions(AuditView(decisions=[_entry([_proposal()], None)]))
    assert "NOT measured" in body


def test_the_stop_line_renders_after_the_gate_verdict() -> None:
    """Order matters: nothing may read as an input to a verdict it had no part in.

    `RiskGate` holds no opinion on stop placement, deliberately and
    permanently. A line rendered ahead of the gate's own reasons would look
    like that opinion arriving through the UI.
    """
    body = render.decisions(
        AuditView(
            decisions=[
                _entry([_proposal()], {"KO": IndicatorSnapshot(close=70.0, atr_14=1.32)})
            ]
        )
    )
    assert body.index('class="lbl">Gate') < body.index('class="lbl">Stop')


def test_the_stop_modifier_classes_do_not_depend_on_winning_a_tie() -> None:
    """`.pill.seed`, `.rung.gate` and `.note.alert` all lost this tie already.

    Both modifiers are declared at two-class specificity under a descendant, so
    a later bare `.tight` or `.unmeasured` rule cannot silently restyle them.
    """
    assert ".chain .rung.stopw.tight{" in render.STYLES
    assert ".chain .rung.stopw.unmeasured{" in render.STYLES
