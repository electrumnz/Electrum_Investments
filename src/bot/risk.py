"""Risk gate: vets every order proposal against rules.yaml.

Claude proposes — this module disposes. Nothing in this codebase may place an
order without an approving verdict from here, and no prompt can talk this module
into changing its mind. That separation is the whole point: in the Alpha Arena
competition (six frontier LLMs, $10k of real money each, two weeks) every US
flagship model finished underwater, with win rates of 25-30% and fees dominating
P&L. The models were not reliably right, so correctness lives in code that the
model cannot reach.

Each gate returns either None (pass) or a string explaining the rejection. Every
reason is surfaced at once rather than short-circuiting, so a rejected proposal
tells you everything that was wrong with it in one pass.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from .config import Rules
from .models import (
    AccountSnapshot,
    AssetClass,
    Direction,
    OrderProposal,
    Position,
    RiskVerdict,
    Tick,
    TradingActivity,
)


class NewsWindow(NamedTuple):
    """A high-impact news event affecting one or more symbols."""

    timestamp: datetime
    affected_symbols: frozenset[str]


class RiskGate:
    """Stateful risk gate.

    State is limited to the daily kill switch and the session's starting equity;
    everything else is derived from the account snapshot and trading activity
    passed in per call, so the gate stays straightforward to test.
    """

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

    # ---------------------------------------------------------------- public

    def evaluate(
        self,
        proposal: OrderProposal,
        *,
        account: AccountSnapshot,
        tick: Tick,
        activity: TradingActivity | None = None,
        news_windows: list[NewsWindow] | None = None,
    ) -> RiskVerdict:
        """Run every gate. Returns approve() only if all of them pass."""
        activity = activity or TradingActivity()
        is_crypto = self._is_crypto(proposal)

        checks: list[str | None] = [
            self._kill_switch(),
            self._equity_floor(account),
            self._symbol_allowed(proposal),
            # Crypto trades 24/7, so the equities session window does not apply.
            None if is_crypto else self._within_session(),
            self._news_blackout(proposal.symbol, news_windows or []),
            self._concurrent_positions(account),
            self._daily_loss(account),
            self._limit_price_sane(proposal, tick),
            self._stops_on_correct_side(proposal),
            self._per_trade_risk(proposal, account),
            self._position_size(proposal, account),
            self._cash_reserve(proposal, account),
            self._gross_exposure(proposal, account),
            self._trades_per_day(activity),
            self._trades_per_week(activity),
            self._symbol_cooldown(proposal, activity),
            # PDT binds on securities only; crypto is not a security.
            None if is_crypto else self._pattern_day_trader(account),
            self._crypto_sleeve_cap(proposal, account) if is_crypto else None,
        ]

        reasons = [c for c in checks if c is not None]
        return RiskVerdict.reject(*reasons) if reasons else RiskVerdict.approve()

    def trip_kill_switch(self) -> None:
        self._kill_switch_tripped = True

    def reset_daily(self, equity_at_session_start: float) -> None:
        self._equity_at_session_start = equity_at_session_start
        self._kill_switch_tripped = False

    @property
    def kill_switch_tripped(self) -> bool:
        return self._kill_switch_tripped

    # ----------------------------------------------------------------- gates

    def _kill_switch(self) -> str | None:
        if self._kill_switch_tripped:
            return "daily loss kill-switch is tripped; no new positions until reset"
        return None

    def _equity_floor(self, account: AccountSnapshot) -> str | None:
        floor = self._rules.account.min_equity_floor_usd
        if account.equity_usd < floor:
            return f"equity {account.equity_usd:,.2f} is below floor {floor:,.2f}"
        return None

    def _symbol_allowed(self, proposal: OrderProposal) -> str | None:
        if not self._rules.is_symbol_allowed(proposal.symbol):
            return f"symbol {proposal.symbol} is not in the allowed list"
        return None

    def _within_session(self) -> str | None:
        hour = self._now().hour
        if any(start <= hour < end for start, end in self._rules.sessions_utc):
            return None
        return f"{hour:02d}:00 UTC is outside the allowed trading sessions"

    def _news_blackout(self, symbol: str, windows: list[NewsWindow]) -> str | None:
        before = timedelta(minutes=self._rules.news_blackout_minutes_before)
        after = timedelta(minutes=self._rules.news_blackout_minutes_after)
        now = self._now()
        for w in windows:
            if symbol in w.affected_symbols and w.timestamp - before <= now <= w.timestamp + after:
                return f"inside news blackout window around {w.timestamp.isoformat()}"
        return None

    def _concurrent_positions(self, account: AccountSnapshot) -> str | None:
        cap = self._rules.account.max_concurrent_positions
        if len(account.open_positions) >= cap:
            return f"already holding {len(account.open_positions)} positions (max {cap})"
        return None

    def _daily_loss(self, account: AccountSnapshot) -> str | None:
        """Sticky: once breached the switch stays tripped for the session."""
        if self._equity_at_session_start <= 0:
            return None
        loss_pct = (
            (self._equity_at_session_start - account.equity_usd)
            / self._equity_at_session_start
            * 100
        )
        limit = self._rules.account.daily_loss_kill_pct
        if loss_pct >= limit:
            self._kill_switch_tripped = True
            return f"daily loss {loss_pct:.2f}% has reached the {limit:.2f}% limit"
        return None

    def _limit_price_sane(self, proposal: OrderProposal, tick: Tick) -> str | None:
        """Reject limit prices far from the market — usually a model arithmetic slip."""
        reference = tick.ask if proposal.direction == Direction.BUY else tick.bid
        if reference <= 0:
            return f"no usable market price for {proposal.symbol}"
        drift_pct = abs(proposal.limit_price - reference) / reference * 100
        if drift_pct > 5.0:
            return (
                f"limit price {proposal.limit_price:,.4f} is {drift_pct:.1f}% away "
                f"from the market at {reference:,.4f}"
            )
        return None

    def _stops_on_correct_side(self, proposal: OrderProposal) -> str | None:
        """Stop-loss must sit on the losing side of entry, take-profit on the winning side."""
        entry = proposal.limit_price
        if proposal.direction == Direction.BUY:
            if proposal.stop_loss_price >= entry:
                return f"buy stop-loss {proposal.stop_loss_price} is not below entry {entry}"
            if proposal.take_profit_price <= entry:
                return f"buy take-profit {proposal.take_profit_price} is not above entry {entry}"
        else:
            if proposal.stop_loss_price <= entry:
                return f"sell stop-loss {proposal.stop_loss_price} is not above entry {entry}"
            if proposal.take_profit_price >= entry:
                return f"sell take-profit {proposal.take_profit_price} is not below entry {entry}"
        return None

    def _per_trade_risk(self, proposal: OrderProposal, account: AccountSnapshot) -> str | None:
        """Loss if the stop fills must stay within max_risk_per_trade_pct of equity.

        Shares and coin units are 1:1 with price, so this is exact — unlike the
        FX version this replaces, which had to approximate contract sizes.
        """
        risk_usd = abs(proposal.limit_price - proposal.stop_loss_price) * proposal.qty
        cap_usd = account.equity_usd * self._rules.account.max_risk_per_trade_pct / 100
        if risk_usd > cap_usd:
            return (
                f"risk {risk_usd:,.2f} exceeds the per-trade cap {cap_usd:,.2f} "
                f"({self._rules.account.max_risk_per_trade_pct:.2f}% of equity)"
            )
        return None

    def _position_size(self, proposal: OrderProposal, account: AccountSnapshot) -> str | None:
        if account.equity_usd <= 0:
            return "account equity is zero or negative"
        pct = proposal.notional_usd / account.equity_usd * 100
        cap = self._rules.account.max_position_pct
        if pct > cap:
            return (
                f"position {proposal.notional_usd:,.2f} is {pct:.1f}% of equity "
                f"(max {cap:.1f}%)"
            )
        return None

    def _cash_reserve(self, proposal: OrderProposal, account: AccountSnapshot) -> str | None:
        if account.equity_usd <= 0:
            return None
        cash_after = account.cash_usd - proposal.notional_usd
        pct_after = cash_after / account.equity_usd * 100
        floor = self._rules.account.min_cash_reserve_pct
        if pct_after < floor:
            return (
                f"cash would fall to {pct_after:.1f}% of equity, below the "
                f"{floor:.1f}% reserve"
            )
        return None

    def _gross_exposure(self, proposal: OrderProposal, account: AccountSnapshot) -> str | None:
        if account.equity_usd <= 0:
            return None
        exposure_after = account.gross_exposure_usd + proposal.notional_usd
        pct_after = exposure_after / account.equity_usd * 100
        cap = self._rules.account.max_gross_exposure_pct
        if pct_after > cap:
            return f"gross exposure would reach {pct_after:.1f}% of equity (max {cap:.1f}%)"
        return None

    def _trades_per_day(self, activity: TradingActivity) -> str | None:
        cap = self._rules.frequency.max_trades_per_day
        if activity.trades_today >= cap:
            return f"already made {activity.trades_today} trades today (max {cap})"
        return None

    def _trades_per_week(self, activity: TradingActivity) -> str | None:
        cap = self._rules.frequency.max_trades_per_week
        if activity.trades_this_week >= cap:
            return f"already made {activity.trades_this_week} trades this week (max {cap})"
        return None

    def _symbol_cooldown(
        self, proposal: OrderProposal, activity: TradingActivity
    ) -> str | None:
        cooldown = self._rules.frequency.min_seconds_between_trades_per_symbol
        if cooldown <= 0:
            return None
        elapsed = activity.seconds_since_last_trade(proposal.symbol, self._now())
        if elapsed is not None and elapsed < cooldown:
            return (
                f"{proposal.symbol} traded {elapsed:.0f}s ago; cooldown is {cooldown}s"
            )
        return None

    def _pattern_day_trader(self, account: AccountSnapshot) -> str | None:
        """Block the trade that would risk a PDT flag.

        Conservative by design: opening a position is not itself a day trade, but
        once the count is at the limit any new equity entry could become the
        fourth if it closes the same day. A 90-day restriction is a far worse
        outcome than a skipped trade.
        """
        pdt = self._rules.pdt
        if not pdt.enforce or account.equity_usd >= pdt.equity_threshold_usd:
            return None
        if account.daytrade_count >= pdt.max_day_trades_per_5_days:
            return (
                f"PDT guard: {account.daytrade_count} day trades in the last 5 "
                f"business days (max {pdt.max_day_trades_per_5_days} below "
                f"${pdt.equity_threshold_usd:,.0f} equity)"
            )
        return None

    def _crypto_sleeve_cap(
        self, proposal: OrderProposal, account: AccountSnapshot
    ) -> str | None:
        sleeve = self._rules.crypto_sleeve
        if not sleeve.enabled:
            return "crypto sleeve is disabled"
        if account.equity_usd <= 0:
            return None
        crypto_now = sum(
            p.notional_usd for p in account.open_positions if p.asset_class == AssetClass.CRYPTO
        )
        pct_after = (crypto_now + proposal.notional_usd) / account.equity_usd * 100
        if pct_after > sleeve.capital_cap_pct:
            return (
                f"crypto allocation would reach {pct_after:.1f}% of equity "
                f"(sleeve cap {sleeve.capital_cap_pct:.1f}%)"
            )
        return None

    # ---------------------------------------------------------------- helpers

    def _is_crypto(self, proposal: OrderProposal) -> bool:
        return (
            proposal.asset_class == AssetClass.CRYPTO
            or self._rules.is_crypto(proposal.symbol)
        )


def aggregate_pnl_today(positions: list[Position], realised_pnl_today_usd: float) -> float:
    """Realised plus unrealised P&L for the day."""
    return realised_pnl_today_usd + sum(p.unrealised_pnl_usd for p in positions)
