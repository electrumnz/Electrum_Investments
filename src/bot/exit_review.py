"""Why a position closed — grading the PLAN, and never the profit.

`Journal.record_exit` takes a price, a time and a realised figure, so once a
trade was closed, **stop-hit, target-hit, closed-by-hand and expiry were
indistinguishable afterwards**. Every one of them looks identical in the
journal: a row with an exit price on it. This module is the classification.

## What is being measured

Whether the trade ended the way it was designed to. Six answers, and the
distinctions are the whole value:

- `STOP_HIT` and `TARGET_HIT` — the plan working. The level the position was
  sized against is where it ended.
- **`CLOSED_EARLY` — the plan being ABANDONED.** Somebody closed it by hand
  with the price at neither level. This is the bucket that matters, and it is
  the only one here that says something about the operator or the agent rather
  than about the market: it is discipline, or the lack of it, rather than luck.
- `CLOSED_AT_LEVEL` — closed by hand, but a level had already been reached.
  The plan followed, with somebody pressing the button instead of the broker's
  leg firing.
- `EXPIRY` — the broker resolved an option itself. Alpaca auto-exercises
  anything in the money by $0.01, auto-assigns a short in-the-money position,
  and liquidates one the account cannot fund. None of those is a decision
  anybody here made.
- `UNKNOWN` — it could not be established. A real answer, and never a guess.

## Why the P&L half stays out, permanently

"Review the trade so it can learn" is the reasonable-sounding request this
repository exists to refuse. Forty trades is noise; a model shown three losses
will confidently change approach; that is the Alpha Arena failure arriving as a
feature request. So this sits beside `triggers.py` and `DreamLedger` — which
grade plan-following and reasoning quality, facts that are true regardless of
how a trade went and therefore have no outcome sample to overfit to — and NOT
beside `metrics.py`.

Concretely, and `tests/test_exit_review.py` pins all three:

- **Nothing here reads `realised_pnl_usd`, `net_pnl_usd`, `r_multiple`,
  `mae_usd` or `mfe_usd`**, and nothing here imports `metrics`.
- A review carries no figure that would let one be reconstructed.
- **It reaches the operator and never the prompt.** Same rule as `triggers.py`:
  a model shown "you abandoned the plan four times this month" will change
  behaviour to score better on that number, on a sample far too small to mean
  anything.

## Missing is missing, again

Two separate absences, kept apart on purpose:

- `Trade.exit_reason is None` — nothing ever classified this exit. A row
  written before this module existed.
- `ExitReason.UNKNOWN` — the question was asked and could not be answered.

And the reason it so often cannot be answered is worth stating rather than
hiding: `reconcile._resolve_exit_price` falls back to the ENTRY price when the
broker cannot supply a mark, which makes the trade look flat rather than
inventing a plausible result. That fallback puts the exit at neither level, so
a classifier that did not know the price was estimated would report a confident
`CLOSED_EARLY` off no evidence at all. `Trade.exit_price_estimated` is what
stops it, and where that was never recorded the answer is `UNKNOWN` rather than
the flattering reading.

**The exit price is where the market was when the close was DETECTED**, not
where the fill happened — `reconcile` runs on the loop's fifteen-minute pulse
and takes the mid. It is usually beyond the level that fired, because that is
the direction price was going, which is why a strict comparison works at all.
Where it is not, the answer is `UNKNOWN`, which is the honest reading of an
exit price that does not establish anything. No tolerance band is applied: a
percentage nobody agreed to would turn a measurement into an opinion.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from .models import (
    Direction,
    ExitReason,
    PositionAction,
    PositionActionRecord,
    Trade,
)
from .options import parse_occ_symbol


@dataclass(frozen=True)
class ExitReview:
    """One closed trade, classified. Carries no profit or loss, deliberately."""

    symbol: str
    trade_id: int | None
    reason: ExitReason

    # One sentence saying why this answer and not another, so the verdict can be
    # checked rather than trusted. The same principle as `WatchOutcome`
    # carrying the reading that met its trigger.
    detail: str

    # Whether a close was RECORDED at the moment of the move, with a reason, by
    # `position_actions.close_with_reason`. Kept apart from `reason` because the
    # two answer different questions: this is who acted, `reason` is what the
    # plan did. A hand close whose price provenance was never recorded is a
    # `CLOSED_EARLY` that cannot be established — and losing the fact that a
    # human closed it would be worse than reporting the plan verdict as unknown.
    closed_by_hand: bool = False

    # The words recorded at the moment of the close. Empty when nothing was.
    # This is the one thing no amount of later arithmetic could reconstruct.
    close_reason: str = ""

    # Carried through so a reader can see WHY an answer is unknown rather than
    # being told only that it is.
    exit_price_estimated: bool | None = None
    entry_never_confirmed: bool = False

    @property
    def plan_abandoned(self) -> bool:
        """Closed by hand, before either level. The bucket that matters.

        Two-valued rather than three, and only because the third state has its
        own home: a hand close whose level could not be established comes back
        `False` here and `True` on `closed_by_hand`, so the pair still tells a
        reader that somebody closed it and that the plan verdict is unknown.
        """
        return self.reason is ExitReason.CLOSED_EARLY

    @property
    def ended_as_designed(self) -> bool | None:
        """Did the trade end at a level it was built around? `None` if unknown.

        Three-valued for the same reason `WatchOutcome.followed_through` is:
        reporting an unclassifiable exit as "not as designed" would count a
        missing reading against the bot.
        """
        if self.reason is ExitReason.UNKNOWN:
            return None
        return self.reason in (
            ExitReason.STOP_HIT,
            ExitReason.TARGET_HIT,
            ExitReason.CLOSED_AT_LEVEL,
        )


@dataclass
class ExitReport:
    """Every classified exit in a set of closed trades, plus what was not."""

    reviews: list[ExitReview] = field(default_factory=list)

    # Exits whose price provenance was never recorded — rows closed before
    # `exit_price_estimated` existed. Counted rather than assumed to be real
    # readings, because assuming would turn every one of them into evidence.
    exits_without_price_provenance: int = 0

    @property
    def reviewed(self) -> int:
        return len(self.reviews)

    @property
    def established(self) -> int:
        return sum(1 for r in self.reviews if r.reason is not ExitReason.UNKNOWN)

    @property
    def counts(self) -> dict[ExitReason, int]:
        """One entry per reason, INCLUDING the zeros.

        Every bucket is listed even when empty, because a zero next to
        `closed_early` is a stated fact each time this is read, and the absence
        of a line is what an outage looks like. Same reasoning as the breach
        count on the `cycle_complete` line.
        """
        tally = {reason: 0 for reason in ExitReason}
        for review in self.reviews:
            tally[review.reason] += 1
        return tally

    @property
    def plan_abandoned(self) -> list[ExitReview]:
        """The list the operator actually wants: the plan put aside by hand."""
        return [r for r in self.reviews if r.plan_abandoned]

    @property
    def closed_by_hand(self) -> int:
        """Every recorded hand close, whatever the plan verdict came out as."""
        return sum(1 for r in self.reviews if r.closed_by_hand)

    @property
    def can_grade_anything(self) -> bool:
        """Whether this report rests on any evidence at all.

        Deliberately separate from "is the list empty", the same way
        `has_cycles` is in `news_history` and `can_grade_anything` is in
        `triggers`. **No trades have closed** and **trades closed and not one
        could be established** are opposite findings, and a caller handed only
        an empty tally reaches for the wrong one every time. `reviewed` and
        `established` separate them.
        """
        return self.established > 0


def classify_exit(
    trade: Trade,
    *,
    actions: Sequence[PositionActionRecord] = (),
    exit_price: float | None = None,
    exit_time: datetime | None = None,
    exit_price_estimated: bool | None = None,
) -> ExitReview:
    """Work out why one position closed. Pure; reads no database and no network.

    The three overrides exist for `reconcile`, which has to classify at the
    moment it writes the exit — the trade in its hand is still open, so its own
    exit fields are empty. Every one of them falls back to the trade's own
    value, so a caller reading a closed row passes none of them.

    `actions` may be any slice of the position-action log; the ones belonging to
    this trade are selected here. Newest first is the order
    `Journal.position_actions` returns and the order this relies on.
    """
    price = exit_price if exit_price is not None else trade.exit_price
    when = exit_time if exit_time is not None else trade.exit_time
    estimated = (
        exit_price_estimated
        if exit_price_estimated is not None
        else trade.exit_price_estimated
    )
    never_confirmed = not trade.fill_is_confirmed

    closes = [a for a in actions if _belongs(a, trade, when)]
    by_hand = bool(closes)
    close_reason = closes[0].reason if closes else ""

    def review(reason: ExitReason, detail: str) -> ExitReview:
        return ExitReview(
            symbol=trade.symbol,
            trade_id=trade.id,
            reason=reason,
            detail=detail,
            closed_by_hand=by_hand,
            close_reason=close_reason,
            exit_price_estimated=estimated,
            entry_never_confirmed=never_confirmed,
        )

    # An exit price that is a fallback, or one that was never recorded as being
    # a reading, is not evidence about a level. It is the ENTRY price, which
    # sits between the stop and the target by construction, so believing it
    # would report "at neither level" on every single one of them. `None` here
    # means "do not compare against this", which is why it is a price rather
    # than a flag: a flag leaves the unusable number in scope.
    evidence: float | None = price if estimated is False else None

    if by_hand:
        if evidence is None:
            return review(
                ExitReason.UNKNOWN,
                _why_the_price_is_not_evidence(
                    price,
                    estimated,
                    prefix=(
                        "a close was recorded by hand, so who ended this is "
                        "known — but whether either level had been reached "
                        "cannot be established: "
                    ),
                ),
            )
        stop_reached, target_reached = _levels_reached(trade, evidence)
        if stop_reached or target_reached:
            level = "the stop" if stop_reached else "the target"
            return review(
                ExitReason.CLOSED_AT_LEVEL,
                f"closed by hand at {evidence:,.4f}, with {level} already reached. "
                "The plan was followed; somebody pressed the button rather "
                "than the resting leg firing.",
            )
        return review(
            ExitReason.CLOSED_EARLY,
            f"closed by hand at {evidence:,.4f}, with neither the stop "
            f"({trade.effective_stop:,.4f}) nor "
            + (
                f"the target ({trade.planned_target:,.4f}) reached"
                if trade.planned_target is not None
                else "any target — none was set — reached"
            )
            + ". The plan was abandoned rather than completed.",
        )

    # Nothing on file to say anybody closed it. The broker's own resolution of
    # an option comes first, because it needs no price at all and because an
    # assigned contract can land past a level it never traded to.
    expiry = _expiry_reached(trade, when)
    if expiry is not None:
        return review(
            ExitReason.EXPIRY,
            f"an option contract expiring {expiry.isoformat()}, gone from the "
            "account at or after its expiry close. Alpaca resolves these "
            "itself — auto-exercise, assignment, or liquidating a position the "
            "account cannot fund — so this is not a decision anybody here made.",
        )

    if evidence is None:
        return review(
            ExitReason.UNKNOWN,
            _why_the_price_is_not_evidence(
                price,
                estimated,
                prefix="nothing on file says who closed this, and ",
            ),
        )

    stop_reached, target_reached = _levels_reached(trade, evidence)
    if stop_reached:
        return review(
            ExitReason.STOP_HIT,
            f"gone from the account with the price at {evidence:,.4f}, at or "
            f"through the stop in force ({trade.effective_stop:,.4f}). The "
            "trade ended where it was sized to end.",
        )
    if target_reached and trade.planned_target is not None:
        return review(
            ExitReason.TARGET_HIT,
            f"gone from the account with the price at {evidence:,.4f}, at or "
            f"through the target ({trade.planned_target:,.4f}).",
        )
    return review(
        ExitReason.UNKNOWN,
        f"gone from the account at {evidence:,.4f}, which is at neither the stop "
        f"({trade.effective_stop:,.4f}) nor the target, and nothing was "
        "recorded to say who closed it. A close made outside this bot leaves "
        "no reason on file, and one is not invented here.",
    )


def review_closed_trades(
    trades: Sequence[Trade],
    actions: Sequence[PositionActionRecord] = (),
) -> ExitReport:
    """Classify a set of closed trades. Open ones are skipped, not guessed at.

    One read of the action log serves every trade, the same way
    `apply_journal_state` takes one journal read for its three figures: two
    reads would eventually describe two different sets of moves.
    """
    report = ExitReport()
    for trade in trades:
        if trade.is_open:
            continue
        review = classify_exit(trade, actions=actions)
        report.reviews.append(review)
        if review.exit_price_estimated is None:
            report.exits_without_price_provenance += 1
    return report


def render(report: ExitReport) -> list[str]:
    """Operator-facing lines. **Never rendered into a prompt** — see the module
    docstring: this is a track record, and a model shown one starts trading to
    improve it."""
    if not report.reviews:
        return [
            "No closed trades in this window, so nothing is graded. That is not "
            "the same as every trade ending as designed."
        ]

    lines = [f"Exits reviewed: {report.reviewed}, established: {report.established}"]
    for reason, count in report.counts.items():
        lines.append(f"  {reason.value}: {count}")

    if not report.can_grade_anything:
        lines.append(
            "Not one exit could be established. Trades closed and the record "
            "cannot say how — which is a finding about the record, not about "
            "the trading."
        )
    if report.exits_without_price_provenance:
        lines.append(
            f"{report.exits_without_price_provenance} exit(s) predate the "
            "recording of whether the exit price was a broker reading or a "
            "fallback, so their price is not treated as evidence."
        )
    for review in report.plan_abandoned:
        lines.append(
            f"PLAN ABANDONED — {review.symbol}: {review.detail}"
            + (f" Reason given: {review.close_reason}" if review.close_reason else "")
        )
    return lines


# ------------------------------------------------------------------ internals


def _belongs(
    action: PositionActionRecord, trade: Trade, exit_time: datetime | None
) -> bool:
    """Is this recorded CLOSE the one that ended this trade?

    Matched on `trade_id` whenever both sides have one, which is exact. The
    fallback — same symbol, inside the trade's own life — exists for a move on a
    position the journal had never seen when it was made, and it is bounded at
    both ends so a close from an EARLIER trade in the same symbol cannot be read
    as this one's.
    """
    if action.action is not PositionAction.CLOSE:
        return False
    if action.trade_id is not None and trade.id is not None:
        return action.trade_id == trade.id
    if action.symbol != trade.symbol:
        return False
    if action.at < trade.entry_time:
        return False
    return exit_time is None or action.at <= exit_time


def _levels_reached(trade: Trade, price: float) -> tuple[bool, bool]:
    """Did the exit price reach the stop, and did it reach the target?

    Measured against `effective_stop` rather than `planned_stop`, so a stop the
    agent pulled in is the level a hit is judged against — handing back the
    level it moved away from would make its own action invisible.

    Both can never be true: a stop and a target sit on opposite sides of entry,
    which `RiskGate._stops_on_correct_side` is what guarantees.
    """
    stop = trade.effective_stop
    target = trade.planned_target
    if trade.direction == Direction.BUY:
        return price <= stop, target is not None and price >= target
    return price >= stop, target is not None and price <= target


def _expiry_reached(trade: Trade, when: datetime | None) -> date | None:
    """The contract's expiry date, when this is an option gone at or after it.

    `None` for anything that is not an option, and for an option that
    disappeared BEFORE its expiry close — that one was closed or stopped out
    like any other position and is classified on its price.
    """
    if when is None:
        return None
    contract = parse_occ_symbol(trade.symbol)
    if contract is None:
        return None
    if when < contract.expiry_close_utc:
        return None
    return contract.expiry


def _why_the_price_is_not_evidence(
    price: float | None, estimated: bool | None, *, prefix: str
) -> str:
    if price is None:
        return prefix + "no exit price was recorded at all."
    if estimated is None:
        return (
            prefix
            + f"the exit price ({price:,.4f}) predates the recording of whether "
            "it was a broker reading or the fallback to entry price, so it "
            "establishes nothing about a level."
        )
    return (
        prefix
        + f"the exit price ({price:,.4f}) is the fallback to the entry price, "
        "used because the broker could not supply a mark. It sits between the "
        "stop and the target by construction, so reading it as 'neither level' "
        "would be reading the fallback rather than the trade."
    )
