"""Risk gate: vets every order proposal against rules.yaml.

Claude proposes — this module disposes. The bot must NOT bypass this.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from .config import Rules
from .models import (
    AccountSnapshot,
    Direction,
    OrderProposal,
    Position,
    RiskVerdict,
    Tick,
)


class NewsWindow(NamedTuple):
    """A high-impact news event that affects one or more currencies/symbols."""

    timestamp: datetime
    affected_symbols: frozenset[str]


class RiskGate:
    """Stateful risk gate. Holds rules + recent state required for daily kill-switch etc."""

    def __init__(
        self,
        rules: Rules,
        *,
        equity_at_session_start: float,
        now: datetime | None = None,
    ) -> None:
        self._rules = rules
        self._equity_at_session_start = equity_at_session_start
        self._kill_switch_tripped = False
        self._now_override = now

    def _now(self) -> datetime:
        return self._now_override or datetime.now(UTC)

    def evaluate(
        self,
        proposal: OrderProposal,
        *,
        account: AccountSnapshot,
        tick: Tick,
        news_windows: list[NewsWindow] | None = None,
    ) -> RiskVerdict:
        """Run every gate. Returns approve() if all pass, reject(reasons) otherwise."""
        reasons: list[str] = []

        if self._kill_switch_tripped:
            reasons.append("daily loss kill-switch is tripped")

        if account.equity_usd < self._rules.account.min_equity_floor_usd:
            reasons.append(
                f"equity {account.equity_usd:.2f} < floor "
                f"{self._rules.account.min_equity_floor_usd:.2f}"
            )

        if not self._rules.is_symbol_allowed(proposal.symbol):
            reasons.append(f"symbol {proposal.symbol} not in allowed list")

        if not self._within_session():
            reasons.append("outside allowed UTC trading sessions")

        if self._in_news_blackout(proposal.symbol, news_windows or []):
            reasons.append("inside news blackout window")

        if len(account.open_positions) >= self._rules.account.max_concurrent_positions:
            reasons.append(
                f"already at max_concurrent_positions={self._rules.account.max_concurrent_positions}"
            )

        if self._daily_loss_breached(account):
            reasons.append("daily loss limit breached")
            self._kill_switch_tripped = True

        sl_reason = self._sl_consistent(proposal, tick)
        if sl_reason:
            reasons.append(sl_reason)

        risk_reason = self._risk_within_per_trade_cap(proposal, account, tick)
        if risk_reason:
            reasons.append(risk_reason)

        return RiskVerdict.reject(*reasons) if reasons else RiskVerdict.approve()

    def trip_kill_switch(self) -> None:
        self._kill_switch_tripped = True

    def reset_daily(self, equity_at_session_start: float) -> None:
        self._equity_at_session_start = equity_at_session_start
        self._kill_switch_tripped = False

    def _within_session(self) -> bool:
        hour = self._now().hour
        return any(start <= hour < end for start, end in self._rules.sessions_utc)

    def _in_news_blackout(self, symbol: str, windows: list[NewsWindow]) -> bool:
        before = timedelta(minutes=self._rules.news_blackout_minutes_before)
        after = timedelta(minutes=self._rules.news_blackout_minutes_after)
        now = self._now()
        for w in windows:
            if symbol not in w.affected_symbols:
                continue
            if w.timestamp - before <= now <= w.timestamp + after:
                return True
        return False

    def _daily_loss_breached(self, account: AccountSnapshot) -> bool:
        loss_pct = (
            (self._equity_at_session_start - account.equity_usd)
            / self._equity_at_session_start
            * 100
        )
        return loss_pct >= self._rules.account.daily_loss_kill_pct

    def _sl_consistent(self, proposal: OrderProposal, tick: Tick) -> str | None:
        """Stop-loss must be on the loss side of entry, take-profit on the win side."""
        ref = tick.ask if proposal.direction == Direction.BUY else tick.bid
        if proposal.direction == Direction.BUY:
            if proposal.stop_loss_price >= ref:
                return f"buy SL {proposal.stop_loss_price} not below entry {ref}"
            if proposal.take_profit_price <= ref:
                return f"buy TP {proposal.take_profit_price} not above entry {ref}"
        else:
            if proposal.stop_loss_price <= ref:
                return f"sell SL {proposal.stop_loss_price} not above entry {ref}"
            if proposal.take_profit_price >= ref:
                return f"sell TP {proposal.take_profit_price} not below entry {ref}"
        return None

    def _risk_within_per_trade_cap(
        self,
        proposal: OrderProposal,
        account: AccountSnapshot,
        tick: Tick,
    ) -> str | None:
        """Estimated $ loss if SL is hit must be <= max_risk_per_trade_pct of equity.

        Approximation: loss ≈ |entry - SL| × size_lots × contract_size.
        Uses contract_size=100_000 for FX majors, 100 for gold/indices as a rough default.
        Production should look up symbol specs from MT5; this is conservative for demo.
        """
        entry = tick.ask if proposal.direction == Direction.BUY else tick.bid
        contract_size = _approx_contract_size(proposal.symbol)
        loss_quote = abs(entry - proposal.stop_loss_price) * proposal.size_lots * contract_size
        cap_usd = account.equity_usd * self._rules.account.max_risk_per_trade_pct / 100
        if loss_quote > cap_usd:
            return (
                f"risk {loss_quote:.2f} > per-trade cap {cap_usd:.2f} "
                f"({self._rules.account.max_risk_per_trade_pct:.2f}%)"
            )
        return None


def _approx_contract_size(symbol: str) -> float:
    """Conservative contract-size approximation for risk math.

    Real values come from MT5's `symbol_info` at runtime; this constant is good
    enough for the gate to refuse oversized trades without a live broker connection.
    """
    upper = symbol.upper()
    if upper.startswith(("XAU", "XAG")):
        return 100.0
    if upper.startswith(("US", "NAS", "SPX", "DJ")):
        return 1.0
    return 100_000.0


def aggregate_pnl_today(positions: list[Position], realised_pnl_today_usd: float) -> float:
    """Realised + unrealised P&L for the day."""
    return realised_pnl_today_usd + sum(p.current_pnl_usd for p in positions)
