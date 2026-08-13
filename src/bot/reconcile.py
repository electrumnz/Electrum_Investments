"""Keep the journal in step with what the broker actually holds.

Two sources of truth have to be reconciled every cycle, because neither is
complete on its own:

- **The broker** knows what is actually held right now. It does not know what
  any of it was *meant* to do: Alpaca reports no stop-loss on a position (stops
  are separate orders) and no open time.
- **The journal** knows the intent — planned stop, planned target, the rationale
  Claude gave — but only for trades this bot opened, and only until something
  closes them behind its back.

That gap is not academic. `AccountSnapshot.open_risk_usd` is what the 2%
total-risk cap counts against, and it can only be computed from planned stops,
which only the journal has. Without this module the cap has nothing to count and
silently never binds.

Deliberately conservative in two places, both about not inventing data:

- An exit price that could not be confirmed from the broker is recorded as
  estimated, not passed off as a fill. R-multiples feed every downstream metric,
  and a quietly wrong one corrupts all of them.
- A position the journal has never seen has an unknowable planned stop, so its
  risk cannot be counted. It is reported rather than guessed at, because a cap
  running blind should say so instead of returning a confident wrong number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog

from .broker import Broker, OrderDisposition
from .config import Rules
from .exit_review import ExitReview, classify_exit
from .journal import Journal
from .models import (
    AccountSnapshot,
    Direction,
    ExecutionMode,
    FillState,
    OrderProposal,
    OrderResult,
    Position,
    Trade,
    WorkingOrder,
)
from .position_actions import UnexplainedMoveReport, detect_unexplained_moves
from .stand_down import evaluate_stand_down

log = structlog.get_logger()

# Float slack for comparing a quantity or a price read back from the broker
# against one this journal wrote. Both round-trip through SQLite as doubles, so
# this is about representation and nothing else — it is deliberately NOT a
# tolerance band on whether a fill "counts". A percentage nobody agreed to is
# how a measurement becomes an opinion.
_TOL = 1e-9

# How far back to read the action log when classifying one symbol's exit. The
# query is already filtered to the symbol and ordered newest first, so this is a
# ceiling on a pathological history rather than a window anybody has to tune.
_ACTION_LOOKBACK = 200


@dataclass(frozen=True)
class FillCorrection:
    """One entry squared against what the broker actually did.

    Recorded rather than applied silently: the whole defect being fixed is a
    journal that stated an intention and called it an outcome, and a correction
    nobody can see would be the same fault with better numbers.
    """

    symbol: str
    trade_id: int
    fill_state: FillState
    qty_before: float
    qty_after: float
    entry_before: float
    entry_after: float

    @property
    def qty_changed(self) -> bool:
        return abs(self.qty_after - self.qty_before) > _TOL

    @property
    def price_changed(self) -> bool:
        return abs(self.entry_after - self.entry_before) > _TOL

    def describe(self) -> str:
        parts = []
        if self.qty_changed:
            parts.append(f"qty {self.qty_before:,.4f} -> {self.qty_after:,.4f}")
        if self.price_changed:
            parts.append(
                f"entry {self.entry_before:,.4f} -> {self.entry_after:,.4f}"
            )
        what = ", ".join(parts) if parts else "figures unchanged"
        return f"{self.symbol}: {what} ({self.fill_state.value})"


@dataclass
class ReconcileResult:
    """What changed this cycle, for logging and for the audit trail."""

    opened: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    excursions_updated: int = 0
    estimated_exits: int = 0
    untracked_positions: list[str] = field(default_factory=list)
    stand_down_triggered: bool = False

    # Stops and quantities that changed at the broker with NO reason on file,
    # plus the two states that are not findings and must not be read as clean
    # ones: a stop leg whose trigger the broker would not report, and a position
    # with no stop resting behind it at all.
    #
    # Optional with a default, like everything else added here after the fact.
    # Populated by `position_actions.detect_unexplained_moves`, which is pure.
    unexplained: UnexplainedMoveReport = field(default_factory=UnexplainedMoveReport)

    # ---- what the entry actually did, squared against what was submitted ----
    #
    # `record_fill` writes the PROPOSAL's quantity and limit price, because
    # waiting for a terminal order state would leave a live position
    # unjournalled in the meantime. These are the corrections that follow.

    # Entries rewritten from the position the broker actually holds.
    entries_corrected: list[FillCorrection] = field(default_factory=list)

    # The entry order is still working with nothing filled. There is no
    # position yet and there may never be one — this is what an out-of-hours
    # entry looks like, and it is the state that used to be closed as a trade.
    entries_resting: list[str] = field(default_factory=list)

    # The entry order is still working and PART of it has filled. **A reading,
    # never an outcome.** A poll during the first real order returned
    # `FILLED 3.0` of 21 and the order completed moments later, so nothing is
    # written from this state.
    entries_mid_fill: list[str] = field(default_factory=list)

    # Entries whose order could not be looked up: no order id on the row, or
    # the order read was degraded. Named rather than treated as terminal —
    # `get_open_orders` returns `[]` on its own failure, so an empty list is
    # "could not ask" and never "nothing resting".
    entries_unchecked: list[str] = field(default_factory=list)

    # The entry order is not in the resting list, nothing is held, AND the
    # broker was asked about the order directly and still could not settle it:
    # the lookup failed, or it came back in a status this build does not
    # classify, or the answer contradicts what is held. **Asked and unanswered**,
    # which is a narrower claim than this field used to make — before
    # `Broker.get_order` existed it also covered every case now named below.
    # Overstates risk for as long as it lasts, which is the safe direction and
    # the one this repository picks.
    entries_unresolved: list[str] = field(default_factory=list)

    # The broker says the entry order is terminal with NOTHING filled —
    # cancelled, expired or rejected. **The trade never opened**, so there is no
    # position, no exit, and no realised figure, and the row is a proposal that
    # went nowhere rather than a trade that ended.
    #
    # It is named and NOT closed. `Journal.record_exit` requires a float exit
    # price and a float realised P&L, and `ExitReason` has no member meaning
    # "never opened", so closing the row here would manufacture all three — and
    # they reach `metrics.py` and `journal.consecutive_losses`, which feeds
    # `evaluate_stand_down`, the operator's fourth rule. A fabricated loss can
    # move a real breaker, and a fabricated scratch is worse: it neither counts
    # nor resets, so a losing streak carries straight across it.
    #
    # So this settles the QUESTION without settling the row: the answer is known
    # and stated, the risk stays counted, and an operator retires it. Settling
    # it in code needs a journal that can record "this never opened" with no
    # price and no P&L, which is a schema change plus a rule in `metrics.py` and
    # in `consecutive_losses` about not counting such a row.
    entries_never_opened: list[str] = field(default_factory=list)

    # A position exists and cannot be attributed to one journal row: two open
    # trades in the symbol, a direction that disagrees, or more held than was
    # ever ordered. Alpaca aggregates per symbol, so its average entry is not
    # this row's fill, and correcting from it would be a plausible wrong figure.
    entries_ambiguous: list[str] = field(default_factory=list)

    # Closes withheld because the entry has never been confirmed as a position.
    # Without this, an order placed out of hours — which rests and fills at the
    # next open — was closed as a trade on the very next cycle, and then filled
    # into a position the journal no longer had a row for.
    closes_deferred: list[str] = field(default_factory=list)

    # Why each position that closed this cycle closed. The plan, never the
    # profit — see `exit_review`.
    exits_reviewed: list[ExitReview] = field(default_factory=list)

    @property
    def plan_abandoned(self) -> list[ExitReview]:
        """Closes made by hand before either level. The bucket that matters."""
        return [r for r in self.exits_reviewed if r.plan_abandoned]

    @property
    def entry_figures_are_the_proposal(self) -> list[str]:
        """Symbols whose journalled entry is still what was ASKED for.

        The honest name for `open_risk_usd` being computed off a limit price
        rather than a fill. Overstated is the safe direction — the first real
        order read $990.36 against a real $980.19 — and it is still a figure
        that does not describe the account.
        """
        return sorted(
            set(
                self.entries_resting
                + self.entries_mid_fill
                + self.entries_unchecked
                + self.entries_unresolved
                + self.entries_never_opened
            )
        )

    @property
    def risk_is_understated(self) -> bool:
        """True when positions exist whose planned risk cannot be known.

        The total-risk cap counts only what the journal can see, so any
        untracked position means the reported figure is lower than reality.
        """
        return bool(self.untracked_positions)


def record_fill(
    journal: Journal,
    proposal: OrderProposal,
    result: OrderResult,
    *,
    execution_mode: ExecutionMode,
    strategy: str = "unspecified",
    now: datetime | None = None,
    dream_id: int | None = None,
) -> int | None:
    """Journal a filled order as an open trade. Returns its id, or None.

    Carries the planned stop across, which is the whole point: without it the
    trade has no `planned_risk_usd` and contributes nothing to the total-risk
    cap.

    `dream_id` records that this symbol was tradeable only because an adopted
    dream granted it. **Provenance, never endorsement**, and it is passed in
    rather than looked up here: the caller is the only thing that knows whether
    the grant was what let the proposal through, because that is the risk gate's
    finding and it travels on the verdict. Defaulting to `None` is correct for
    every trade in a symbol `config/rules.yaml` already allows.

    ## It records the PROPOSAL, and that is a decision rather than an oversight

    This runs immediately after `broker.place_order`, so what it can write is
    what was submitted. The alternative — waiting for a terminal order state —
    leaves a live position unjournalled in the meantime, which is the `14b88c8`
    hole exactly: an order at the broker the journal has never heard of, with
    `open_risk_usd` unable to count it and the 2% cap blind to it. Recording
    early and correcting in `reconcile` overstates risk for at most one cycle;
    recording late understates it to zero for as long as the order takes.

    So the submitted figures are kept beside the recorded ones —
    `submitted_qty` and `submitted_price` — and `fill_state` says how much of it
    anybody has actually read back. `reconcile` squares them once the entry
    order is terminal.

    **A partial reading at submission is not a partial fill**, and this is where
    that is enforced. `filled_qty` short of what was asked for is a snapshot of
    an order still filling: the first real order polled back `FILLED 3.0` of 21
    and completed moments later. Writing 3 would understate the position's risk
    by six sevenths, which is the unsafe direction, so the proposal's quantity
    stands and the row is marked `UNCONFIRMED` until the order settles.
    """
    if not result.accepted:
        return None

    # Only a fill that is complete AND priced is taken as an outcome. Either
    # half missing means the broker has not finished telling us what happened.
    filled_qty = result.filled_qty
    complete = (
        filled_qty is not None
        and filled_qty + _TOL >= proposal.qty
        and result.filled_price is not None
    )
    trade = Trade(
        symbol=proposal.symbol,
        asset_class=proposal.asset_class,
        strategy=strategy,
        direction=proposal.direction,
        qty=filled_qty if complete and filled_qty is not None else proposal.qty,
        entry_time=now or datetime.now(UTC),
        entry_price=(
            result.filled_price
            if complete and result.filled_price is not None
            else proposal.limit_price
        ),
        planned_stop=proposal.stop_loss_price,
        planned_target=proposal.take_profit_price,
        rationale=proposal.rationale,
        execution_mode=execution_mode,
        entry_order_id=result.order_id,
        dream_id=dream_id,
        submitted_qty=proposal.qty,
        submitted_price=proposal.limit_price,
        fill_state=FillState.COMPLETE if complete else FillState.UNCONFIRMED,
    )
    return journal.record_entry(trade)


def reconcile(
    journal: Journal,
    broker: Broker,
    rules: Rules,
    *,
    account: AccountSnapshot | None = None,
    now: datetime | None = None,
) -> ReconcileResult:
    """Bring the journal in line with broker state. Call once per cycle."""
    moment = now or datetime.now(UTC)
    snapshot = account or broker.get_account()
    result = ReconcileResult()

    held = {p.symbol: p for p in snapshot.open_positions}

    # One order read, used by the entry correction below and by the
    # unexplained-move detection at the end. Two reads would eventually describe
    # two different sets of resting orders, and the second of them would be
    # compared against the first's conclusions.
    orders = broker.get_open_orders()
    orders_degraded = broker.orders_degraded

    # 0. Square each ENTRY against what the broker actually did, BEFORE anything
    #    reads the figures. `record_fill` wrote the proposal's quantity and limit
    #    price; this is where they become what happened.
    deferred_closes = _confirm_entries(
        journal,
        broker=broker,
        held=held,
        open_trades=journal.open_trades(),
        orders=orders,
        orders_degraded=orders_degraded,
        result=result,
    )
    # Re-read so every step below sees corrected entries rather than the
    # proposals they replaced.
    open_trades = journal.open_trades()

    # 1. Anything the journal thinks is open but the broker no longer holds has
    #    closed: by us, by a stop, or by Alpaca itself at an option expiry.
    for trade in open_trades:
        if trade.symbol in held:
            continue
        if trade.id is not None and trade.id in deferred_closes:
            # Not a close, and the test is what the journal can ESTABLISH rather
            # than what it can guess. This row's fill has never been confirmed,
            # so nothing has ever read back a position behind it — and a row
            # that was never a position cannot have closed. The entry may be
            # resting, the order read may have failed, or the order may simply
            # not have come back in a list that is status-filtered and bounded;
            # `_confirm_entries` says which, and none of the three is a fill.
            #
            # Without this an out-of-hours entry, which rests until the next
            # regular open, was written off as a trade on the very next cycle
            # and then filled into a position with no journal row behind it.
            result.closes_deferred.append(trade.symbol)
            continue
        exit_price, estimated = _resolve_exit_price(broker, trade)
        if estimated:
            result.estimated_exits += 1

        # WHY it closed, captured at the only moment the evidence is all in one
        # place. A price, a time and a realised figure cannot tell stop-hit from
        # target-hit from closed-by-hand afterwards.
        review = classify_exit(
            trade,
            actions=journal.position_actions(
                symbol=trade.symbol, limit=_ACTION_LOOKBACK
            ),
            exit_price=exit_price,
            exit_time=moment,
            exit_price_estimated=estimated,
        )
        result.exits_reviewed.append(review)

        pnl = _realised_pnl(trade, exit_price)
        journal.record_exit(
            trade.id or 0,
            exit_time=moment,
            exit_price=exit_price,
            realised_pnl_usd=pnl,
            exit_reason=review.reason,
            exit_price_estimated=estimated,
        )
        result.closed.append(trade.symbol)
        log.info(
            "trade_closed",
            symbol=trade.symbol,
            exit_price=round(exit_price, 4),
            realised_pnl_usd=round(pnl, 2),
            price_source="estimated" if estimated else "broker",
            exit_reason=review.reason.value,
            closed_by_hand=review.closed_by_hand,
        )

    if result.plan_abandoned:
        log.warning(
            "plan_abandoned_on_close",
            symbols=[r.symbol for r in result.plan_abandoned],
            detail=(
                "A position was closed by hand with the price at neither the "
                "stop nor the target. That is the plan being put aside rather "
                "than completed, which is the one exit worth looking at "
                "whatever it made."
            ),
        )

    # 2. Widen MAE/MFE on everything still open.
    for trade in open_trades:
        position = held.get(trade.symbol)
        if position is None or trade.id is None:
            continue
        journal.update_excursion(trade.id, position.unrealised_pnl_usd)
        result.excursions_updated += 1

    # 3. Positions we have no record of. Their planned stop is unknowable, so
    #    their risk cannot be counted — say so rather than guess.
    result.untracked_positions.extend(untracked_positions(snapshot, open_trades))

    if result.untracked_positions:
        log.warning(
            "untracked_positions_total_risk_understated",
            symbols=result.untracked_positions,
            detail=(
                "These positions have no journal entry, so their planned stop "
                "and therefore their risk is unknown. The total-risk cap is "
                "counting less than is actually at risk."
            ),
        )

    # 3b. Anything that changed at the broker with no reason on file.
    #
    #     The inverse of the action record, and the half that makes it honest: a
    #     feature that only showed the moves it managed to capture would hide
    #     its own failures. A stop pulled in through the Alpaca web UI, a
    #     partial fill nobody journalled, a bracket leg cancelled by hand — all
    #     of them show here and none of them shows anywhere else.
    #
    #     `get_open_orders` catches its own failures and returns `[]`, so the
    #     degraded flag has to travel with it or an outage would render as an
    #     account with nothing resting and therefore nothing wrong.
    still_open = [t for t in journal.open_trades() if t.is_open]
    result.unexplained = detect_unexplained_moves(
        positions=snapshot.open_positions,
        orders=orders,
        open_trades=still_open,
        # Newest first and bounded: an explanation for a level resting NOW is a
        # recent move, and reading the whole history to find one would grow with
        # the log for no gain.
        actions=journal.position_actions(limit=_ACTION_LOOKBACK),
        orders_degraded=orders_degraded,
    )
    if result.unexplained.moves:
        log.warning(
            "unexplained_position_moves",
            moves=[m.describe() for m in result.unexplained.moves],
            detail=(
                "A stop or a quantity differs from what the journal records, "
                "and no position action explains it. The broker is "
                "authoritative about what exists; the journal is the only "
                "thing that knows what was intended."
            ),
        )
    if result.unexplained.unreadable_stops:
        log.warning(
            "stop_trigger_unreadable",
            symbols=result.unexplained.unreadable_stops,
            detail=(
                "A stop leg is resting and the broker reported no trigger "
                "price, so whether it has moved is unknown rather than fine."
            ),
        )

    # 4. A close may have completed a losing streak.
    if result.closed:
        state = evaluate_stand_down(journal, rules.stand_down, now=moment)
        if state.is_active(moment):
            result.stand_down_triggered = True
            log.warning(
                "stand_down_active",
                stage=state.stage,
                consecutive_losses=state.consecutive_losses,
                days_remaining=round(state.days_remaining(moment), 2),
            )

    # 5. One equity point per day for the curve.
    journal.record_equity(snapshot.equity_usd, when=moment)

    return result


def _confirm_entries(
    journal: Journal,
    *,
    broker: Broker,
    held: dict[str, Position],
    open_trades: list[Trade],
    orders: list[WorkingOrder],
    orders_degraded: bool,
    result: ReconcileResult,
) -> set[int]:
    """Replace each proposed entry with what the broker did. Returns ids NOT to close.

    The other half of the decision recorded on `record_fill`: the journal writes
    the proposal at submission so a live position is never unjournalled, and
    this is where it stops being a proposal. `reconcile` already squares journal
    against broker every cycle, so it is the one place that sees both sides.

    Five properties, and none of them is padding:

    - **Nothing is written from an order that is still working.** A fill is not
      atomic. The first real order polled back `FILLED 3.0` of 21 and completed
      moments later, so anything sampling an in-flight order has a snapshot
      rather than an outcome. Those are named in `entries_mid_fill` and left
      alone.
    - **A confirmed entry is never rewritten.** Squaring happens once, when the
      entry order goes terminal. Afterwards a quantity that disagrees with the
      broker is a POSITION change — a hand close, a partial exit — which
      `detect_unexplained_moves` reports and which must not be quietly absorbed
      into the entry.
    - **A position that cannot be attributed to one row is left alone.** Alpaca
      aggregates all exposure to a symbol into a single position, so with two
      open trades in it the broker's average entry is not either row's fill.
      Correcting from it would be a plausible wrong figure, which is the exact
      failure this repository exists to refuse.
    - **More held than was ordered is never absorbed.** `submitted_qty` is a
      ceiling: an order cannot fill for more than it asked for, so a larger
      position contains something this row did not buy.
    - **A degraded order read defers rather than concludes.**
      `Broker.get_open_orders` catches its own failures and returns `[]`, so an
      empty list means "could not ask" and never "nothing resting" — the same
      lesson as `FinnhubCalendar.is_degraded`. Concluding "terminal" from it
      would close a resting entry as a trade.
    - **An entry that is neither resting nor held is ASKED ABOUT, never assumed.**
      This is the one that was wrong, and it cost a live position. Read on.

    ## Absence from the resting list is not evidence of a fill

    The degraded flag above guards the whole list failing. It does NOT guard one
    order going missing from a list that came back fine, and that is the same
    missing-versus-absent distinction one level down:
    `Broker.get_open_orders` asks Alpaca for the orders it classifies as OPEN,
    bounded at 100, so an order id that is not in the answer may be filled,
    cancelled, expired, rejected, past the bound, or in a status that query does
    not return. Only the first of those is a position.

    **And even a genuinely terminal entry that never filled is not a close.** A
    cancelled order means the trade never opened; closing the row invents an
    exit price and a realised P&L for a position that never existed, and both go
    on to reach `metrics.py`.

    Read off `data/journal.db` on the droplet, 11 Aug 2026: 107 AAPL submitted
    09:00:03 UTC — 05:00 New York, so a bracket that could not fill and rested —
    was marked CLOSED at 09:15:04, one cycle later, with a realised figure
    nobody had traded. The order then filled at the regular open, leaving 107
    shares at the broker with no open journal row: `open_risk_usd` read 1,487.19
    against a true 2,396.69, which is 2.42% of equity against a 2% cap, and
    `stop_watch` no longer looked at it.

    **This branch is the only way into that close, and that is established by
    running the other four rather than by reasoning about Alpaca.** With the
    order in the resting list the row defers; with the order read degraded it
    defers; with the fill confirmed the close is a real one; and a row with no
    `entry_order_id` cannot arise from `record_fill`, which takes the id off an
    accepted `OrderResult`. So whatever Alpaca's reason for leaving that id out
    of the answer, the defect is here: this function cannot tell "not in the
    list" from "filled", and it must not try.

    ## So it asks, and the deferral is what happens when the answer does not come

    Deferring instead of closing fails closed, and a deferral is still an
    inference: "not in that list" was replaced by "cannot be established", which
    left a genuinely cancelled entry holding planned risk with nothing able to
    say so. `Broker.get_order` is the direct question, and it is asked HERE and
    nowhere else in this function — only when the order is absent from the
    resting list and no position is held, which is the state that cost the
    position. Every other branch already has its evidence.

    Five answers, and only one of them writes anything:

    - **FILLED** — the entry did become a position, and the position's absence
      is therefore a CLOSE. The row is squared against the fill the broker
      reports and released, so step 1 closes it with a confirmed entry price
      rather than the proposal's limit. Before this it deferred for ever: an
      entry that filled and was then stopped out between two cycles could never
      leave the journal.
    - **NEVER_FILLED** — cancelled, expired or rejected with nothing filled. The
      trade never opened. Named in `entries_never_opened` and **still not
      closed**: see that field for why manufacturing an exit price and a
      realised figure here would reach the stand-down breaker.
    - **WORKING** — alive after all, past the resting list's bound or in a
      status that query does not return. Identical treatment to being in the
      list: RESTING, and deferred.
    - **PART_FILLED**, and FILLED where the figures do not support it — the
      broker's answer contradicts an account holding nothing. Unresolved.
    - **`None`, or a status this build cannot classify** — the question could
      not be answered. Unresolved, which is exactly where this stood before
      asking, so a broker outage costs the answer and never the safety.

    Nothing is inferred from the ABSENCE of an answer, in either direction. A
    lookup that failed and a lookup that said "cancelled" must not share a
    branch, or the original defect returns with a network error as its trigger.
    """
    deferred: set[int] = set()
    working = {o.order_id: o for o in orders}
    trades_per_symbol: dict[str, int] = {}
    for trade in open_trades:
        trades_per_symbol[trade.symbol] = trades_per_symbol.get(trade.symbol, 0) + 1

    for trade in open_trades:
        if trade.id is None or trade.fill_is_confirmed:
            continue

        if trade.entry_order_id is None:
            # A row written by hand or adopted from the broker. There is no
            # order to look up, so nothing can be established — and nothing is
            # deferred either, which keeps the behaviour of such a row exactly
            # what it was before this step existed.
            result.entries_unchecked.append(trade.symbol)
            continue

        if orders_degraded:
            result.entries_unchecked.append(trade.symbol)
            deferred.add(trade.id)
            continue

        order = working.get(trade.entry_order_id)
        if order is not None:
            deferred.add(trade.id)
            if order.filled_qty > _TOL:
                result.entries_mid_fill.append(trade.symbol)
                continue
            result.entries_resting.append(trade.symbol)
            if trade.fill_state is not FillState.RESTING:
                journal.confirm_fill(trade.id, fill_state=FillState.RESTING)
            continue

        # The entry order is not in the resting list. That is WEAKER than
        # "terminal", and reading it as terminal is what closed AAPL row 2.
        ceiling = trade.submitted_qty if trade.submitted_qty is not None else trade.qty
        position = held.get(trade.symbol)
        if position is None:
            # Nothing is held and nothing was ever confirmed, so nothing HERE is
            # evidence this row was ever a position. Ask the broker about the
            # order itself rather than inferring from a list that answers a
            # different question. See the docstring for all five answers.
            lookup = broker.get_order(trade.entry_order_id)
            if lookup is None or lookup.disposition is OrderDisposition.UNCLASSIFIED:
                # Could not ask, or the broker answered with a word this build
                # does not classify. Both are "still not established", and both
                # leave this exactly where it was before asking.
                result.entries_unresolved.append(trade.symbol)
                deferred.add(trade.id)
                continue

            if lookup.disposition is OrderDisposition.WORKING:
                # Alive, and simply not in a bounded, status-filtered list.
                # Treated identically to being in it, right down to the split
                # below, because it is the same fact arrived at by a different
                # route — and two routes to one fact that behaved differently
                # would be worth less than the one route.
                deferred.add(trade.id)
                if lookup.filled_qty > _TOL:
                    # A reading, never an outcome. `RESTING` asserts that
                    # nothing has filled, which this answer contradicts, and
                    # `PARTIAL` asserts the rest is not coming, which a working
                    # order has not said. Nothing is written from either.
                    result.entries_mid_fill.append(trade.symbol)
                    continue
                result.entries_resting.append(trade.symbol)
                if trade.fill_state is not FillState.RESTING:
                    journal.confirm_fill(trade.id, fill_state=FillState.RESTING)
                continue

            if lookup.disposition is OrderDisposition.NEVER_FILLED:
                # Settled, and settled as NOT A TRADE. The row keeps counting
                # its planned risk and an operator retires it; nothing here
                # invents the exit price and realised figure a close would need.
                result.entries_never_opened.append(trade.symbol)
                deferred.add(trade.id)
                continue

            if (
                lookup.disposition is OrderDisposition.FILLED
                and lookup.filled_avg_price is not None
                and lookup.filled_qty + _TOL >= ceiling
            ):
                # The entry DID become a position, so the position's absence is
                # a close after all — and now it is a close with a confirmed
                # entry price behind it rather than the proposal's limit. Not
                # deferred: step 1 records the exit.
                correction = FillCorrection(
                    symbol=trade.symbol,
                    trade_id=trade.id,
                    fill_state=FillState.COMPLETE,
                    qty_before=trade.qty,
                    qty_after=lookup.filled_qty,
                    entry_before=trade.entry_price,
                    entry_after=lookup.filled_avg_price,
                )
                journal.confirm_fill(
                    trade.id,
                    fill_state=FillState.COMPLETE,
                    qty=lookup.filled_qty,
                    entry_price=lookup.filled_avg_price,
                )
                result.entries_corrected.append(correction)
                log.info(
                    "entry_squared_against_the_order_itself",
                    symbol=trade.symbol,
                    trade_id=trade.id,
                    change=correction.describe(),
                    broker_status=lookup.broker_status,
                    detail=(
                        "The entry order reports FILLED and no position is "
                        "held, so this trade opened and has since closed. The "
                        "entry is squared against the order's own average fill "
                        "before the close is recorded, so the realised figure "
                        "is measured from what filled rather than from what "
                        "was asked for."
                    ),
                )
                continue

            # A terminal order with part of it filled, or a FILLED one whose
            # figures do not support the claim, against an account holding
            # nothing. The answer contradicts itself, so nothing is written.
            result.entries_unresolved.append(trade.symbol)
            deferred.add(trade.id)
            log.warning(
                "entry_answer_contradicts_the_account",
                symbol=trade.symbol,
                order=lookup.describe(),
                filled_qty=lookup.filled_qty,
                submitted_qty=ceiling,
                detail=(
                    "The broker's answer about this entry order does not "
                    "square with holding no position in the symbol, so it "
                    "settles nothing and the close stays withheld."
                ),
            )
            continue

        if (
            trades_per_symbol[trade.symbol] != 1
            or position.direction != trade.direction
            or position.qty > ceiling + _TOL
        ):
            result.entries_ambiguous.append(trade.symbol)
            continue

        state = (
            FillState.COMPLETE
            if position.qty + _TOL >= ceiling
            else FillState.PARTIAL
        )
        correction = FillCorrection(
            symbol=trade.symbol,
            trade_id=trade.id,
            fill_state=state,
            qty_before=trade.qty,
            qty_after=position.qty,
            entry_before=trade.entry_price,
            entry_after=position.entry_price,
        )
        journal.confirm_fill(
            trade.id,
            fill_state=state,
            qty=position.qty,
            entry_price=position.entry_price,
        )
        result.entries_corrected.append(correction)
        log.info(
            "entry_squared_against_broker",
            symbol=trade.symbol,
            trade_id=trade.id,
            change=correction.describe(),
            detail=(
                "The journal held the proposal's quantity and limit price. "
                "These are the position the broker actually reports, so open "
                "risk now describes the account rather than the intention."
            ),
        )

    if result.entries_mid_fill:
        log.info(
            "entry_still_filling",
            symbols=result.entries_mid_fill,
            detail=(
                "Part of this order has filled and the rest is still working. "
                "That is a reading, not an outcome, so nothing is written from "
                "it — the journal keeps the submitted quantity, which "
                "overstates risk rather than understating it."
            ),
        )
    if result.entries_unresolved:
        log.warning(
            "entry_neither_resting_nor_held",
            symbols=result.entries_unresolved,
            detail=(
                "This entry is not in the broker's resting orders, no position "
                "is held, and the broker was asked about the order directly "
                "and still settled nothing — the lookup failed, or the status "
                "is one this build does not classify, or the answer "
                "contradicts holding nothing. The close is WITHHELD rather "
                "than recorded: closing on an unestablished order would write "
                "an exit price and a realised P&L for a position that may "
                "never have existed. The row keeps counting its planned risk, "
                "which overstates the total rather than understating it, and "
                "it settles itself once the broker can answer."
            ),
        )
    if result.entries_never_opened:
        log.warning(
            "entry_never_became_a_position",
            symbols=result.entries_never_opened,
            detail=(
                "The broker reports this entry order as cancelled, expired or "
                "rejected with nothing filled, so the trade never opened. It "
                "is NOT closed here: a close needs an exit price and a "
                "realised figure, and inventing those for a position that "
                "never existed puts a fabricated sample into metrics and into "
                "the consecutive-loss breaker. The row still counts its "
                "planned risk against the 2% cap — overstated, which is the "
                "safe direction — until an operator retires it."
            ),
        )
    if result.entries_ambiguous:
        log.warning(
            "entry_cannot_be_attributed",
            symbols=result.entries_ambiguous,
            detail=(
                "A held position cannot be matched to a single journal row, so "
                "the broker's average entry is not this trade's fill and is not "
                "copied onto it."
            ),
        )
    return deferred


def untracked_positions(
    snapshot: AccountSnapshot, open_trades: list[Trade]
) -> list[str]:
    """Held symbols the journal has no open row for. One definition, two callers.

    `reconcile` reports these so an operator knows the total-risk figure is
    understated; `apply_journal_state` carries them onto the snapshot so
    `RiskGate._class_total_risk` can refuse rather than count an unknown as
    zero. Both are the same fact and it is computed once, because two
    implementations of "untracked" would eventually disagree about which
    positions the caps are blind to.
    """
    journalled = {t.symbol for t in open_trades}
    return [p.symbol for p in snapshot.open_positions if p.symbol not in journalled]


def apply_journal_state(
    snapshot: AccountSnapshot, journal: Journal
) -> AccountSnapshot:
    """Fill in the open risk the broker cannot report.

    The single place this happens: `_Session.account()` in `mcp_server.py` calls
    straight through to here rather than keeping its own copy. Every path that
    feeds an account snapshot to the risk gate must go through it, or the caps
    silently have nothing to count.

    **One journal read supplies all three figures.** The portfolio total, the
    per-symbol breakdown the class caps need, and the list of positions whose
    risk is unknowable all describe the same set of open trades, so reading the
    journal once is what keeps them from describing different ones.
    """
    open_trades = journal.open_trades()

    by_symbol: dict[str, float] = {}
    stops: dict[str, float] = {}
    for trade in open_trades:
        # `current_risk_usd`, not `planned_risk_usd`. The cap is a statement
        # about what the stops would cost if they filled, so a stop that has
        # been tightened costs less and the cap should count less. The two
        # figures are identical for every trade whose stop has never moved.
        #
        # `planned_risk_usd` stays fixed at what the position was SIZED against,
        # because that is the denominator of R and redefining it after the fact
        # would make a trade that lost half what it risked read as a full
        # stop-out.
        by_symbol[trade.symbol] = by_symbol.get(trade.symbol, 0.0) + trade.current_risk_usd
        # The stop LEVEL, so the model can be shown the price it is being asked
        # to manage. Aggregating risk across two trades in one symbol is
        # arithmetic; aggregating two stop levels is not, so the widest — the
        # furthest from entry, i.e. the one that would fill last — is the honest
        # single answer, and it is the one that describes the position's real
        # exposure rather than the tightest leg of it.
        #
        # `effective_stop`, so a stop the agent has pulled in is the level the
        # agent is then shown. Handing it back the level it moved away from
        # would make its own action invisible to it on the next cycle.
        in_force = trade.effective_stop
        held = stops.get(trade.symbol)
        stops[trade.symbol] = (
            in_force
            if held is None
            else (
                max(held, in_force)
                if trade.direction == Direction.SELL
                else min(held, in_force)
            )
        )

    snapshot.open_risk_usd = sum(by_symbol.values())
    snapshot.open_risk_by_symbol = by_symbol
    snapshot.planned_stop_by_symbol = stops
    # Missing, never zero. An empty entry above would read as "risks nothing".
    snapshot.symbols_with_unknown_risk = untracked_positions(snapshot, open_trades)
    return snapshot


def _resolve_exit_price(broker: Broker, trade: Trade) -> tuple[float, bool]:
    """Best available exit price, and whether it had to be estimated.

    Prefers a live mark from the broker. Falls back to the entry price, which
    makes the trade look flat rather than inventing a plausible-looking result
    — an approximation that reads as real would quietly corrupt every metric
    derived from R-multiples.
    """
    try:
        tick = broker.get_tick(trade.symbol)
        return tick.mid, False
    except (KeyError, RuntimeError):
        return trade.entry_price, True


def _realised_pnl(trade: Trade, exit_price: float) -> float:
    move = exit_price - trade.entry_price
    if trade.direction.value == "sell":
        move = -move
    return move * trade.qty
