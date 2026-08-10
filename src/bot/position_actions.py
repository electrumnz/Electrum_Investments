"""Acting on a position that is already open, and recording why.

## The asymmetry, which is the whole safety argument

`RiskGate.evaluate` vets proposals that **open** exposure. It never sees a close
and it never sees a stop move, deliberately: a stand-down that froze position
management would strand open trades with no way out, which is worse than the
losing streak that caused it. Closing a position and moving a stop are outside
the gated path on purpose.

That exemption is safe for exactly one class of move — the ones that cannot
increase what is at risk — and this module is where that class is enforced,
because nothing downstream of here does:

- **CLOSE is permitted.** It reduces exposure to zero. There is no arrangement
  of the rules under which being out is worse than being in.
- **TIGHTENING a stop is permitted.** Moving it toward entry strictly reduces
  `|entry - stop| x qty`, so it can only lower the loss the position stands to
  take. Past entry it reduces that loss to nothing and starts locking in a gain,
  which is the end state of tightening rather than a different act.
- **WIDENING a stop is REFUSED, always, in code.** Moving it away from entry
  increases the loss at unchanged size, on a live position, with **no gate
  anywhere in this system that would catch it**. The prompt already tells the
  model never to widen a stop to compensate; prose is the wrong place for the
  only thing standing between a position and a larger loss. `classify_stop_move`
  is deterministic and cannot be argued with, in the same way and for the same
  reason `RiskGate` cannot.

**The direction test comes from the position, never from an assumption.** On a
long, tighter means a HIGHER stop; on a short, tighter means a LOWER one. Half
of the trades this repository can hold are shorts — the live one is — so a
comparison written for the long case would silently invert on exactly the
position it was needed for.

## Every move carries a reason, and the absence of one is visible

`record_exit` takes a price, a time and a realised figure, so stop-hit,
target-hit, closed-by-hand and expiry are indistinguishable afterwards. A reason
captured at the moment of the move is the only version anybody actually knows;
reconstructed later it is a guess dressed as a record.

So a move with no reason is refused, at three layers — `PositionActionRecord`,
`Journal.record_position_action`, and the entry points here. And the inverse is
reported rather than hidden: `detect_unexplained_moves` finds a stop or a
quantity that changed at the broker with **nothing on file to explain it**. A
record that only showed the moves it managed to capture would hide its own
failures, which is the same rule as `FinnhubCalendar.is_degraded` and
`stops_unchecked` arriving on the position-management side.

## Nothing here is a proposal, and nothing here fires unattended by default

The functions below are called by the MCP tools — the attended path, with a
person in the conversation — and by `execute_position_plan`, which refuses
outright unless `position_actions.enabled` is true in `config/rules.yaml`. It
ships false. A loop that closes a position at 3am with nobody watching is a new
live execution path and it is the operator's switch to throw, not a side effect
of building the record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

import structlog

from .broker import Broker, is_crypto_symbol
from .config import Rules
from .journal import Journal
from .models import (
    Direction,
    Position,
    PositionAction,
    PositionActionRecord,
    PositionPlan,
    Trade,
    WorkingOrder,
)

log = structlog.get_logger()

__all__ = [
    "ActionOutcome",
    "StopMove",
    "UnexplainedMove",
    "UnexplainedMoveReport",
    "classify_stop_move",
    "close_with_reason",
    "detect_unexplained_moves",
    "execute_position_plan",
    "loop_execution_state",
    "resting_stop_legs",
    "tighten_stop",
]

# Prices are carried to four decimal places everywhere in this repository, so
# anything under half a ten-thousandth is the same price written twice rather
# than a move. Deliberately not zero: an exact float comparison between a level
# read back from a broker and one computed here would report a move that is
# entirely an artefact of binary representation.
PRICE_TOLERANCE = 5e-5

# Crypto quantities are fractional, so this cannot be a whole unit. A millionth
# of a coin is below anything Alpaca will fill.
QTY_TOLERANCE = 1e-6


class StopMove(StrEnum):
    """Which way a proposed stop level moves, relative to entry.

    Measured along the price axis in the direction that REDUCES risk for this
    position's side, which is the only definition that works for a long and a
    short at once.
    """

    TIGHTER = "tighter"
    WIDER = "wider"
    UNCHANGED = "unchanged"


def classify_stop_move(
    *,
    direction: Direction,
    current_stop: float,
    new_stop: float,
) -> StopMove:
    """Is this move toward entry, away from it, or nowhere?

    **The one function in this module that decides whether a move is allowed**,
    and it is pure so it can be reasoned about and tested on its own.

    On a LONG the stop sits below entry, so raising it reduces the distance to
    entry and therefore the loss. On a SHORT the stop sits above entry, so
    lowering it does. The rule is stated in terms of the side rather than in
    terms of the entry price on purpose: it stays correct for a stop moved past
    entry into profit, where "distance from entry" starts growing again while
    the risk is already zero.

    `entry_price` is deliberately not a parameter. A comparison against entry
    would call a move from 810 to 800 on a short "tighter" and a move from 780
    to 770 — below the 773.32 entry, so further from it — "wider", when both are
    the same act of pulling the stop down. Side and sign are all this needs.
    """
    if abs(new_stop - current_stop) <= PRICE_TOLERANCE:
        return StopMove.UNCHANGED
    if direction == Direction.BUY:
        return StopMove.TIGHTER if new_stop > current_stop else StopMove.WIDER
    return StopMove.TIGHTER if new_stop < current_stop else StopMove.WIDER


def risk_at(trade: Trade, stop: float) -> float:
    """What `trade` would lose with its stop at `stop`. Never negative.

    Floored at zero for the reason `Trade.current_risk_usd` is: a stop past
    entry is a locked-in gain, and `abs()` would report it as risk growing the
    further into profit it went.
    """
    if trade.direction == Direction.BUY:
        return max(0.0, trade.entry_price - stop) * trade.qty
    return max(0.0, stop - trade.entry_price) * trade.qty


def resting_stop_legs(orders: list[WorkingOrder], symbol: str) -> list[WorkingOrder]:
    """Every working order on `symbol` that TRIGGERS at a price.

    A list rather than a single order, because "there are two" and "there is
    one" are different facts and only one of them can be acted on
    unambiguously. `WorkingOrder.is_stop` reads the broker's own order type, so
    a `stop`, a `stop_limit` and a `trailing_stop` all count.
    """
    clean = symbol.strip().upper()
    return [o for o in orders if o.symbol.strip().upper() == clean and o.is_stop]


@dataclass
class ActionOutcome:
    """What happened, or every reason it did not.

    `reasons` collects ALL the refusals rather than short-circuiting on the
    first, which is the property `RiskGate` is built around and it is worth
    here for the same reason: a caller told one thing wrong, fixing it and being
    told the second has been made to ask twice for information that existed at
    once.

    `warnings` is separate and never blocks. A move that went through with the
    stop landing in the journal alone is a real move with a real caveat, and
    folding those into `reasons` would make a refusal and a caveat look alike.
    """

    ok: bool
    action: PositionAction
    symbol: str
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    record: PositionActionRecord | None = None
    broker_order_id: str | None = None
    reached_broker: bool = False
    risk_before_usd: float | None = None
    risk_after_usd: float | None = None
    detail: str = ""

    @property
    def risk_reduced_usd(self) -> float | None:
        if self.risk_before_usd is None or self.risk_after_usd is None:
            return None
        return self.risk_before_usd - self.risk_after_usd

    @classmethod
    def refused(
        cls, action: PositionAction, symbol: str, *reasons: str
    ) -> ActionOutcome:
        return cls(ok=False, action=action, symbol=symbol, reasons=list(reasons))


def _reason_refusal(reason: str) -> str | None:
    if reason.strip():
        return None
    return (
        "a reason is required and this one is blank. The record exists so that "
        "a move with no reason is VISIBLE; storing an empty one would silence "
        "the unexplained-move report while explaining nothing. It does not have "
        "to be a good reason — nothing here judges it — it has to exist."
    )


def tighten_stop(
    journal: Journal,
    broker: Broker,
    *,
    symbol: str,
    new_stop: float,
    reason: str,
    actor: str,
    now: datetime | None = None,
) -> ActionOutcome:
    """Move a position's stop CLOSER to entry, at the broker and in the journal.

    Refuses, with every reason at once:

    - a blank reason;
    - a non-positive stop;
    - no open journal row for the symbol, because the stop that is in force is
      unknowable without one and a move measured from an unknown level is a
      number nobody can check;
    - **a move that widens the stop, on either side of the market**;
    - a move that changes nothing;
    - more than one resting stop leg, because which one to replace is then a
      guess;
    - a broker read that failed, because a stop leg that could not be listed is
      not a stop leg that is absent.

    **The broker moves first, and the journal only follows a confirmed replace.**
    A journal saying 800 while 820 rests at Alpaca is worse than no record at
    all: the caps would count a risk figure that does not exist and the model
    would be shown a level it cannot rely on. A failed replace leaves the
    original leg resting and writes nothing.

    A symbol with **no** resting stop leg is not refused. Crypto has never had
    one — Alpaca accepts no bracket on it — so its stop has always been a
    journal figure that `stop_watch` reports a breach against, and an equity
    whose bracket was cancelled by hand is in the same position. The move is
    recorded with `reached_broker` false and the outcome says so out loud,
    because "the stop moved" and "the stop moved at the broker" are different
    claims and only one of them puts an order between the position and a loss.
    """
    moment = now or datetime.now(UTC)
    clean = symbol.strip().upper()
    reasons: list[str] = []
    warnings: list[str] = []

    blank = _reason_refusal(reason)
    if blank:
        reasons.append(blank)
    if new_stop <= 0:
        reasons.append(f"{new_stop} is not a price; a stop has to be positive.")

    trade = journal.open_trade_for(clean)
    if trade is None:
        reasons.append(
            f"no open journal row for {clean}, so the stop currently in force "
            "is unknown and a move cannot be measured against it. A held "
            "position the journal has never seen has an unknowable stop — that "
            "is the same fact reported as symbols_with_unknown_risk."
        )
        return ActionOutcome.refused(PositionAction.TIGHTEN_STOP, clean, *reasons)

    in_force = trade.effective_stop
    move = classify_stop_move(
        direction=trade.direction, current_stop=in_force, new_stop=new_stop
    )
    if move is StopMove.WIDER:
        side = "below" if trade.direction == Direction.BUY else "above"
        reasons.append(
            f"REFUSED: {new_stop:,.4f} is further from entry than the stop in "
            f"force at {in_force:,.4f} on a {trade.direction.value} position "
            f"(a long's stop tightens upward, a short's downward; this one sits "
            f"{side} entry at {trade.entry_price:,.4f}). Widening a stop on an "
            f"open position increases the loss at unchanged size — "
            f"{risk_at(trade, in_force):,.2f} becomes "
            f"{risk_at(trade, new_stop):,.2f} — and no gate in this system sees "
            "a position move. It is refused here because here is the only place "
            "it can be. Close some of the position instead if the risk is the "
            "problem; do not buy room by moving the stop."
        )
    elif move is StopMove.UNCHANGED:
        reasons.append(
            f"the stop is already at {in_force:,.4f}; there is nothing to move "
            "and nothing worth recording."
        )

    if reasons:
        return ActionOutcome.refused(PositionAction.TIGHTEN_STOP, clean, *reasons)

    orders = broker.get_open_orders()
    if broker.orders_degraded:
        return ActionOutcome.refused(
            PositionAction.TIGHTEN_STOP,
            clean,
            "the resting orders could not be read, so which leg to replace is "
            "unknown. An empty order list from a failed fetch looks exactly "
            "like an account with nothing resting, and moving a stop on that "
            "reading could cancel the wrong thing. Try again.",
        )

    legs = resting_stop_legs(orders, clean)
    if len(legs) > 1:
        return ActionOutcome.refused(
            PositionAction.TIGHTEN_STOP,
            clean,
            f"{len(legs)} resting stop legs on {clean} "
            f"({', '.join(o.order_id for o in legs)}). Which one protects the "
            "position is a guess, and replacing the wrong one would leave the "
            "other at its old trigger. Resolve it at the broker first.",
        )

    risk_before = risk_at(trade, in_force)
    risk_after = risk_at(trade, new_stop)
    broker_order_id: str | None = None
    reached_broker = False

    if legs:
        leg = legs[0]
        if leg.stop_price is None:
            warnings.append(
                f"the resting leg {leg.order_id} reported no trigger price, so "
                f"what it was protecting at is unknown. The journal said "
                f"{in_force:,.4f} and the replacement is being sent against "
                "that; read the leg back afterwards."
            )
        result = broker.replace_stop(leg.order_id, stop_price=new_stop)
        if not result.accepted:
            return ActionOutcome.refused(
                PositionAction.TIGHTEN_STOP,
                clean,
                f"the broker refused the replace: {result.error}. The original "
                f"stop is still resting at {in_force:,.4f} and the journal was "
                "not touched — a record of a move that did not happen is worse "
                "than no record.",
            )
        broker_order_id = result.order_id
        reached_broker = True
    else:
        warnings.append(
            f"no stop order is resting on {clean}, so this move is a JOURNAL "
            "FIGURE ONLY and nothing at the broker will act on it. "
            + (
                "Alpaca accepts no bracket on crypto, so that is the normal "
                "arrangement here."
                if is_crypto_symbol(clean)
                else "That is not normal for an equity — the bracket may have "
                "been cancelled, or the position adopted from the broker. Rule "
                "3 is not being met on this position."
            )
            + " stop_watch reports a breach against it on the loop's pulse."
        )

    record = PositionActionRecord(
        trade_id=trade.id,
        symbol=clean,
        action=PositionAction.TIGHTEN_STOP,
        actor=actor,
        at=moment,
        reason=reason,
        before_stop=in_force,
        after_stop=new_stop,
        broker_order_id=broker_order_id,
        reached_broker=reached_broker,
    )
    record.id = journal.record_stop_move(record)

    log.info(
        "position_stop_tightened",
        symbol=clean,
        before_stop=round(in_force, 4),
        after_stop=round(new_stop, 4),
        reached_broker=reached_broker,
        risk_before_usd=round(risk_before, 2),
        risk_after_usd=round(risk_after, 2),
        actor=actor,
    )
    return ActionOutcome(
        ok=True,
        action=PositionAction.TIGHTEN_STOP,
        symbol=clean,
        warnings=warnings,
        record=record,
        broker_order_id=broker_order_id,
        reached_broker=reached_broker,
        risk_before_usd=risk_before,
        risk_after_usd=risk_after,
        detail=(
            f"{clean} stop {in_force:,.4f} -> {new_stop:,.4f}; risk "
            f"{risk_before:,.2f} -> {risk_after:,.2f}."
        ),
    )


def close_with_reason(
    journal: Journal,
    broker: Broker,
    *,
    symbol: str,
    reason: str,
    actor: str,
    now: datetime | None = None,
) -> ActionOutcome:
    """Close the whole position in a symbol, recording why it was closed.

    Closing is never gated and must never become gated — a stand-down that
    could not be exited is worse than the losing streak that caused it — so the
    only thing refused here is a blank reason and a broker refusal.

    **A position with no journal row is still closed.** Refusing would be
    refusing to reduce exposure because the paperwork is incomplete, which is
    the wrong way round; the record is written with `trade_id` absent and the
    outcome names it.

    The exit itself is left to `reconcile`, which squares journal against broker
    every cycle and writes the exit price, the time and the realised figure.
    This adds the one thing that read has never been able to supply: WHY. That
    is the front half of the exit review — the plan being abandoned, kept apart
    from what it earned.
    """
    moment = now or datetime.now(UTC)
    clean = symbol.strip().upper()

    blank = _reason_refusal(reason)
    if blank:
        return ActionOutcome.refused(PositionAction.CLOSE, clean, blank)

    trade = journal.open_trade_for(clean)
    result = broker.close_position(clean)
    if not result.accepted:
        return ActionOutcome.refused(
            PositionAction.CLOSE,
            clean,
            f"the broker refused the close: {result.error}. Nothing was "
            "recorded, because a close that did not happen is not a close.",
        )

    warnings: list[str] = []
    if trade is None:
        warnings.append(
            f"{clean} had no open journal row, so this move is recorded against "
            "no trade. Its planned stop and therefore its risk were never known "
            "— the same fact reported as symbols_with_unknown_risk."
        )

    record = PositionActionRecord(
        trade_id=trade.id if trade else None,
        symbol=clean,
        action=PositionAction.CLOSE,
        actor=actor,
        at=moment,
        reason=reason,
        before_qty=result.filled_qty or (trade.qty if trade else None),
        after_qty=0.0,
        broker_order_id=result.order_id,
        reached_broker=True,
    )
    record.id = journal.record_position_action(record)

    log.info(
        "position_closed_with_reason",
        symbol=clean,
        order_id=result.order_id,
        actor=actor,
        trade_id=trade.id if trade else None,
    )
    return ActionOutcome(
        ok=True,
        action=PositionAction.CLOSE,
        symbol=clean,
        warnings=warnings,
        record=record,
        broker_order_id=result.order_id,
        reached_broker=True,
        risk_before_usd=trade.current_risk_usd if trade else None,
        risk_after_usd=0.0,
        detail=(
            f"{clean} closed. The exit price, time and realised figure are "
            "written by reconcile on the next cycle; this records the reason, "
            "which is the part nothing else captures."
        ),
    )


# --------------------------------------------------------- the loop's switch


def loop_execution_state(rules: Rules) -> tuple[bool, str]:
    """May the LOOP act on its own `position_plans`? And the sentence to show.

    Separate from the tools on purpose, and the distinction is the operator's:
    the agent having control of its position is satisfied by the attended
    path — chat, MCP, a person in the conversation. A loop that closes a
    position or moves a stop at 3am with nobody watching is a different
    proposition and a new unattended execution path.

    Returns the sentence as well as the flag so that every surface reporting
    this says the same thing rather than each inventing its own wording.
    """
    if rules.position_actions.enabled:
        return True, (
            "position_actions.enabled is TRUE in config/rules.yaml: the loop "
            "may act on its own position plans without anyone watching. Closes "
            "and stop tightenings only — a widen is refused whatever this is "
            "set to."
        )
    return False, (
        "position_actions.enabled is false in config/rules.yaml, so the loop "
        "records its position plans and acts on none of them. The attended "
        "path — tighten_stop and close_position_with_reason over MCP, with a "
        "person in the conversation — is unaffected and is what the operator "
        "asked for."
    )


def execute_position_plan(
    plan: PositionPlan,
    *,
    rules: Rules,
    journal: Journal,
    broker: Broker,
    actor: str = "loop",
    now: datetime | None = None,
) -> ActionOutcome:
    """Act on one `PositionPlan`, if and only if the operator has switched it on.

    **The gate is the first thing checked and it fails closed.** A caller that
    forgets to look at the flag still gets a refusal, which is why the flag is
    read here rather than by whoever calls this: a switch enforced at the call
    site is a switch one new call site can miss.

    HOLD does nothing and is not recorded. A hold is already in the audit log on
    every cycle through `Decision.position_plans`, and writing a row every
    fifteen minutes for every open position would bury the moves that matter in
    the ones that are not moves at all.
    """
    enabled, sentence = loop_execution_state(rules)
    if not enabled:
        return ActionOutcome.refused(plan.action, plan.symbol, sentence)

    if plan.action is PositionAction.HOLD:
        return ActionOutcome(
            ok=True,
            action=PositionAction.HOLD,
            symbol=plan.symbol,
            detail=(
                "Hold: nothing to execute. The reasoning is already recorded in "
                "the audit log with the rest of this cycle's decision."
            ),
        )

    if plan.action is PositionAction.CLOSE:
        return close_with_reason(
            journal,
            broker,
            symbol=plan.symbol,
            reason=plan.reasoning,
            actor=actor,
            now=now,
        )

    if plan.new_stop_price is None:
        return ActionOutcome.refused(
            PositionAction.TIGHTEN_STOP,
            plan.symbol,
            "the plan says tighten_stop and names no level. `new_stop_price` is "
            "empty, and a stop invented to fill it would be an exit nobody "
            "chose — the same failure as an invented take-profit, arriving on "
            "the stop side where it is worse. Nothing was moved.",
        )
    return tighten_stop(
        journal,
        broker,
        symbol=plan.symbol,
        new_stop=plan.new_stop_price,
        reason=plan.reasoning,
        actor=actor,
        now=now,
    )


# ------------------------------------------------- the inverse: what moved
#
# A record that only showed the moves it managed to capture would hide its own
# failures. This is the half that makes the feature honest.


@dataclass(frozen=True)
class UnexplainedMove:
    """A stop or a quantity that changed at the broker with no reason on file.

    `journal_value` is what this repository believes and `broker_value` is what
    is actually there. Neither is presented as the right one: the broker is
    authoritative about what exists, the journal is the only thing that knows
    what was intended, and the whole point of the tag is that they disagree with
    nothing recorded to say why.
    """

    symbol: str
    kind: str  # "stop" | "quantity"
    journal_value: float
    broker_value: float

    def describe(self) -> str:
        return (
            f"{self.symbol}: {self.kind} is {self.broker_value:,.4f} at the "
            f"broker and {self.journal_value:,.4f} in the journal, with no "
            "recorded action explaining the change."
        )


@dataclass(frozen=True)
class UnexplainedMoveReport:
    """What was found, and — separately — what could not be looked at.

    Three lists rather than one, because they are three different findings and
    a caller handed only the first would read the other two as "all clear":

    - `moves` — changed, and nothing on file says why.
    - `unreadable_stops` — a stop leg is resting and the broker did not report
      its trigger, so whether it moved is UNKNOWN. Never "fine". This is the
      `WorkingOrder.trigger_price_unknown` state reaching a caller.
    - `positions_without_a_resting_stop` — the position is there and no stop
      order is. Expected for crypto, which Alpaca accepts no bracket on, so
      crypto is excluded; for an equity it means rule 3 is not being met.

    `can_check` is false when the resting orders could not be read at all, and
    an empty `moves` means nothing whatever in that case. Same rule as
    `has_cycles` in `news_history` and `can_grade_anything` in `triggers`: the
    absence of evidence is its own state and must not share a shape with the
    good outcome.
    """

    moves: list[UnexplainedMove] = field(default_factory=list)
    unreadable_stops: list[str] = field(default_factory=list)
    positions_without_a_resting_stop: list[str] = field(default_factory=list)
    can_check: bool = True

    @property
    def anything_to_report(self) -> bool:
        return bool(
            self.moves
            or self.unreadable_stops
            or self.positions_without_a_resting_stop
            or not self.can_check
        )


def _explained(
    actions: list[PositionActionRecord], symbol: str, attr: str, value: float, tol: float
) -> bool:
    """Is there a recorded action whose result is the value now at the broker?

    Matched on the AFTER value rather than on a timestamp, so a move recorded
    before the journal row was updated — a torn write, or a replace confirmed
    while the process died — still counts as explained. The reason exists; that
    is the question being asked.
    """
    clean = symbol.strip().upper()
    for action in actions:
        if action.symbol.strip().upper() != clean:
            continue
        after = getattr(action, attr, None)
        if after is not None and abs(float(after) - value) <= tol:
            return True
    return False


def detect_unexplained_moves(
    *,
    positions: list[Position],
    orders: list[WorkingOrder],
    open_trades: list[Trade],
    actions: list[PositionActionRecord],
    orders_degraded: bool = False,
) -> UnexplainedMoveReport:
    """Compare what the broker holds against what the journal says, and why.

    Pure, so the loop can call it, a test can drive it and a renderer can ask it
    without any of them touching a broker.

    Only positions the journal knows about are compared. A held position with no
    journal row has nothing to disagree with — it is already reported, by name,
    as `symbols_with_unknown_risk`, and counting it here would be the same fact
    told twice under a tag that means something else.
    """
    by_symbol = {t.symbol.strip().upper(): t for t in open_trades if t.is_open}
    moves: list[UnexplainedMove] = []
    unreadable: list[str] = []
    stopless: list[str] = []

    for position in positions:
        symbol = position.symbol.strip().upper()
        trade = by_symbol.get(symbol)
        if trade is None:
            continue

        if abs(position.qty - trade.qty) > QTY_TOLERANCE and not _explained(
            actions, symbol, "after_qty", position.qty, QTY_TOLERANCE
        ):
            moves.append(
                UnexplainedMove(
                    symbol=symbol,
                    kind="quantity",
                    journal_value=trade.qty,
                    broker_value=position.qty,
                )
            )

        if orders_degraded:
            # The stop half cannot be established at all. Reported through
            # `can_check` rather than as a clean result.
            continue

        legs = resting_stop_legs(orders, symbol)
        if not legs:
            if not is_crypto_symbol(symbol):
                stopless.append(symbol)
            continue

        for leg in legs:
            if leg.stop_price is None:
                unreadable.append(symbol)
                continue
            if abs(leg.stop_price - trade.effective_stop) <= PRICE_TOLERANCE:
                continue
            if _explained(actions, symbol, "after_stop", leg.stop_price, PRICE_TOLERANCE):
                continue
            moves.append(
                UnexplainedMove(
                    symbol=symbol,
                    kind="stop",
                    journal_value=trade.effective_stop,
                    broker_value=leg.stop_price,
                )
            )

    return UnexplainedMoveReport(
        moves=moves,
        unreadable_stops=sorted(set(unreadable)),
        positions_without_a_resting_stop=sorted(set(stopless)),
        can_check=not orders_degraded,
    )
