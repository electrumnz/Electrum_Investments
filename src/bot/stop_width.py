"""How far the stop sits from the entry, measured in the symbol's own ATR.

This exists because of `TODO.md` item 25, and the shape of that item is the
whole reason this module reports rather than refuses.

**Size is the risk ceiling divided by the stop distance**, so the denominator
is what actually decides how large a position gets. `qwen3-coder-flash`
proposed KO with a $0.05 stop against a $1.32 ATR — 0.04 ATR, inside the
spread — which at the same stated dollar risk buys a position roughly
twenty-six times larger than a 1-ATR stop would. Every figure the sizing block
prints is correct and the division is performed honestly; the exposure arrives
through the denominator, where nothing was looking.

**`RiskGate` deliberately holds no opinion on where the stop goes.**
`CLAUDE.md` is explicit: *"the honest answer to 'is this stop any good' is not
the gate's to give"*. `_stops_on_correct_side` checks only which SIDE of entry
each level sits on, because a stop below entry on a short is not a stop. A
minimum stop distance would be an opinion on placement dressed up as a limit —
a rule nobody agreed to, arriving through the back door — and item 25 says so
in as many words.

So this measures and states. Nothing here rejects anything, nothing here is
handed to `RiskGate.evaluate`, and the figure reaches exactly two places: the
`cycle_complete` heartbeat, so a stated zero each cycle is a fact rather than
the absence of a warning; and the Decisions page under the proposal it
describes, beside the gate's own reasons.

**A stop with no ATR to compare it against is UNMEASURED, never fine.** That
is the `stops_unchecked` rule arriving in a new place: `fetch_indicators` drops
a symbol whose bars failed, so an absent ATR means "could not ask", and
counting it as not-tight would make the reassuring answer the one an outage
produces. Two counters, never one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

from .models import OrderProposal


class CarriesATR(Protocol):
    """Anything that can state a 14-period ATR, or state that it has none.

    Two types in this repository do: `indicators.Indicators`, which the loop
    computes live and hands to `build_market_context`, and
    `models.IndicatorSnapshot`, which is what got RECORDED into the audit log
    for that cycle. The loop measures against the first and the Decisions page
    measures the same proposal, months later, against the second.

    A protocol rather than two functions, because two implementations of one
    piece of arithmetic are two answers that can disagree — and the one on the
    page would be the one nobody re-checked. The page derives the figure rather
    than reading a stored one for the same reason `Adoption.is_live` is
    computed: a third fact about the same thing is a third fact that can drift.
    """

    @property
    def atr_14(self) -> float | None: ...

# A stop closer than a quarter of the symbol's own daily range is the fact
# being reported. Not a limit — nothing refuses at this line — but a number has
# to be picked for the word "tight" to mean anything, and a quarter of an ATR
# is comfortably inside ordinary daily noise for every instrument this bot can
# reach. The KO proposal that produced item 25 was 0.04.
#
# Deliberately NOT in `config/rules.yaml`. Everything in that file is enforced
# in code and changing it changes what may be traded; this changes what a log
# line says. Putting it there would invite it to be read as a limit, and the
# first thing anyone would do with a limit is widen it.
TIGHT_ATR_FRACTION = 0.25


@dataclass(frozen=True)
class StopWidth:
    """One proposal's stop distance, in dollars and in ATRs where that is known.

    `atr_14` is `float | None` and the None is load-bearing. It means the bars
    could not support a 14-period ATR — a fresh listing, a failed fetch, a
    symbol granted by an adopted dream that has no history here yet — and every
    consumer has to say "not measured" rather than print a comparison it did
    not make.
    """

    symbol: str
    direction: str
    entry_price: float
    stop_price: float
    atr_14: float | None

    @property
    def stop_distance(self) -> float:
        """Absolute distance from entry to stop, in dollars per unit.

        Absolute on purpose. This is the same quantity the gate multiplies by
        `qty` to get the risk, and it is one number for a long and for a short
        — a signed version would be right for one direction and wrong for the
        other, which is the arithmetic mistake this repository keeps making.
        """
        return abs(self.entry_price - self.stop_price)

    @property
    def atr_multiple(self) -> float | None:
        """The stop distance as a fraction of one ATR, or None if unmeasurable.

        **A zero or negative ATR is unmeasurable, not infinitely tight.** An
        instrument whose recent range rounds to nothing produces a division by
        zero, and the honest answer to "how many ATRs is this" for a symbol
        with no measured range is that nobody knows. Returning a very large
        number instead would state a comparison against a denominator that does
        not exist.
        """
        if self.atr_14 is None or self.atr_14 <= 0:
            return None
        return self.stop_distance / self.atr_14

    @property
    def is_measured(self) -> bool:
        return self.atr_multiple is not None

    @property
    def is_tight(self) -> bool:
        """True only when the comparison was actually made and came back tight.

        An unmeasured stop is False here AND True in `is_unmeasured`, so the
        two counters never sum to a reassuring total on their own. A caller
        reporting only this one is reporting "no tight stops" on a cycle where
        nothing could be checked.
        """
        multiple = self.atr_multiple
        return multiple is not None and multiple < TIGHT_ATR_FRACTION

    @property
    def is_unmeasured(self) -> bool:
        return self.atr_multiple is None

    def render(self) -> str:
        """One line for a human, stating the measurement or its absence.

        Rendered on the Decisions page under the proposal, where the gate's
        reasons already go. It is deliberately phrased as an observation rather
        than as a verdict: the gate approved or refused this proposal on its
        own arithmetic and this sentence had no part in that, so it must not
        read like a third reason.
        """
        multiple = self.atr_multiple
        if multiple is None:
            # Named rather than omitted. A proposal with no line under it reads
            # as one that was checked and found ordinary, which is exactly the
            # reading a failed indicator fetch must not produce.
            return (
                f"Stop {self.stop_distance:,.2f} from entry. "
                f"No ATR(14) for {self.symbol}, so the stop's width against "
                f"this symbol's own range was NOT measured."
            )
        if multiple < TIGHT_ATR_FRACTION:
            return (
                f"Stop {self.stop_distance:,.2f} from entry — "
                f"{multiple:.2f} ATR(14). TIGHT: under "
                f"{TIGHT_ATR_FRACTION:g} ATR, so the size this buys is roughly "
                f"{1 / multiple:,.0f}x what a 1-ATR stop would at the same "
                f"dollar risk. Not a rejection; the gate holds no view on "
                f"stop placement."
            )
        return (
            f"Stop {self.stop_distance:,.2f} from entry — "
            f"{multiple:.2f} ATR(14)."
        )


def measure_stop_widths(
    proposals: Iterable[OrderProposal],
    indicators: Mapping[str, CarriesATR],
) -> list[StopWidth]:
    """Measure every proposal's stop against the ATR the loop actually read.

    `indicators` is the same mapping handed to `build_market_context`, so the
    comparison is against the figures the model was shown rather than against a
    second reading taken later. A symbol absent from it gets `atr_14=None` and
    is reported unmeasured — the mapping is authoritative about what was
    available this cycle, and looking the figure up somewhere else would
    reintroduce the drift this shares its input to avoid.

    Order follows `proposals`, so the Nth entry describes the Nth proposal.
    Callers on the Decisions page pair them by index the same way verdicts are
    paired, so a caller must not filter this list without filtering the other.
    """
    measured: list[StopWidth] = []
    for proposal in proposals:
        indicator = indicators.get(proposal.symbol)
        measured.append(
            StopWidth(
                symbol=proposal.symbol,
                direction=proposal.direction.value,
                entry_price=proposal.limit_price,
                stop_price=proposal.stop_loss_price,
                atr_14=None if indicator is None else indicator.atr_14,
            )
        )
    return measured


def tight_stop_symbols(widths: Iterable[StopWidth]) -> list[str]:
    """Symbols whose stop was measured and came back under the fraction.

    A list rather than a count, for the same reason `stops_unchecked` is one:
    a number on the heartbeat tells a reader something happened and not which
    symbol to go and look at.
    """
    return [w.symbol for w in widths if w.is_tight]


def unmeasured_stop_symbols(widths: Iterable[StopWidth]) -> list[str]:
    """Symbols whose stop could not be compared against an ATR at all.

    Kept apart from `tight_stop_symbols` deliberately. These are not stops that
    passed; they are stops nobody measured, and folding them into either bucket
    would make an outage look like an answer.
    """
    return [w.symbol for w in widths if w.is_unmeasured]
