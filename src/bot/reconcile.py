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

from .broker import Broker
from .config import Rules
from .journal import Journal
from .models import (
    AccountSnapshot,
    Direction,
    ExecutionMode,
    OrderProposal,
    OrderResult,
    Trade,
)
from .stand_down import evaluate_stand_down

log = structlog.get_logger()


@dataclass
class ReconcileResult:
    """What changed this cycle, for logging and for the audit trail."""

    opened: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    excursions_updated: int = 0
    estimated_exits: int = 0
    untracked_positions: list[str] = field(default_factory=list)
    stand_down_triggered: bool = False

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
    """
    if not result.accepted:
        return None

    entry_price = result.filled_price or proposal.limit_price
    trade = Trade(
        symbol=proposal.symbol,
        asset_class=proposal.asset_class,
        strategy=strategy,
        direction=proposal.direction,
        qty=result.filled_qty or proposal.qty,
        entry_time=now or datetime.now(UTC),
        entry_price=entry_price,
        planned_stop=proposal.stop_loss_price,
        planned_target=proposal.take_profit_price,
        rationale=proposal.rationale,
        execution_mode=execution_mode,
        entry_order_id=result.order_id,
        dream_id=dream_id,
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
    open_trades = journal.open_trades()

    # 1. Anything the journal thinks is open but the broker no longer holds has
    #    closed: by us, by a stop, or by Alpaca itself at an option expiry.
    for trade in open_trades:
        if trade.symbol in held:
            continue
        exit_price, estimated = _resolve_exit_price(broker, trade)
        if estimated:
            result.estimated_exits += 1

        pnl = _realised_pnl(trade, exit_price)
        journal.record_exit(
            trade.id or 0,
            exit_time=moment,
            exit_price=exit_price,
            realised_pnl_usd=pnl,
        )
        result.closed.append(trade.symbol)
        log.info(
            "trade_closed",
            symbol=trade.symbol,
            exit_price=round(exit_price, 4),
            realised_pnl_usd=round(pnl, 2),
            price_source="estimated" if estimated else "broker",
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
        by_symbol[trade.symbol] = by_symbol.get(trade.symbol, 0.0) + trade.planned_risk_usd
        # The stop LEVEL, so the model can be shown the price it is being asked
        # to manage. Aggregating risk across two trades in one symbol is
        # arithmetic; aggregating two stop levels is not, so the widest — the
        # furthest from entry, i.e. the one that would fill last — is the honest
        # single answer, and it is the one that describes the position's real
        # exposure rather than the tightest leg of it.
        held = stops.get(trade.symbol)
        stops[trade.symbol] = (
            trade.planned_stop
            if held is None
            else (
                max(held, trade.planned_stop)
                if trade.direction == Direction.SELL
                else min(held, trade.planned_stop)
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
