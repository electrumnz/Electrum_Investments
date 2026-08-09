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

from .config import InstrumentRules, Rules
from .models import (
    AccountSnapshot,
    Direction,
    ExecutionMode,
    OrderProposal,
    Position,
    RiskVerdict,
    StandDownState,
    Tick,
    TradingActivity,
)
from .options import parse_occ_symbol


def proposal_class_label(instrument: InstrumentRules) -> str:
    """Readable name for an instrument class, taken from its strategy label.

    Rejection messages should say which class refused the trade, not just that
    something did.
    """
    return instrument.strategy if instrument.strategy != "unspecified" else "this instrument"


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
        execution_mode: ExecutionMode = ExecutionMode.PAPER,
        now: datetime | None = None,
    ) -> None:
        self._rules = rules
        self._equity_at_session_start = equity_at_session_start
        self._execution_mode = execution_mode
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
        stand_down: StandDownState | None = None,
    ) -> RiskVerdict:
        """Run every gate. Returns approve() only if all of them pass.

        This gates *opening* exposure only. Closing a position and moving a stop
        never come through here, which is deliberate: a stand-down that also
        froze position management would strand open trades with no way out.
        """
        activity = activity or TradingActivity()

        # Each asset class carries its own session window and symbol list, so
        # the gate resolves the class once and applies that class's rules. This
        # replaces special-casing crypto, which did not generalise.
        instrument = self._rules.for_symbol(proposal.symbol)

        checks: list[str | None] = [
            self._kill_switch(),
            self._stand_down(stand_down),
            self._equity_floor(account),
            self._symbol_allowed(proposal),
            self._within_session(instrument),
            self._news_blackout(proposal.symbol, news_windows or []),
            self._concurrent_positions(account),
            self._daily_loss(account),
            self._limit_price_sane(proposal, tick),
            self._stops_on_correct_side(proposal),
            self._per_trade_risk(proposal, account),
            self._position_size(proposal, account),
            self._total_risk(proposal, account),
            self._buying_power(proposal, account),
            self._gross_notional(proposal, account),
            self._trades_per_day(activity),
            self._trades_per_week(activity),
            self._symbol_cooldown(proposal, activity),
            self._option_expiry(proposal),
            self._instrument_capital_cap(proposal, account, instrument),
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

    def _stand_down(self, state: StandDownState | None) -> str | None:
        """Block live entries during a consecutive-loss stand-down.

        The rule is "can't trade money, only paper" — not "stop trading". So in
        paper mode a stand-down changes nothing: the bot keeps proposing, keeps
        being gated, and keeps journalling. Only live execution is withheld,
        which is the part that costs money and the part that revenge trading
        does damage with.
        """
        if state is None or not state.is_active(self._now()):
            return None
        if self._execution_mode == ExecutionMode.PAPER:
            return None
        days = state.days_remaining(self._now())
        ends = state.ends_at.date().isoformat() if state.ends_at else "unknown"
        return (
            f"stage {state.stage} stand-down after {state.consecutive_losses} "
            f"consecutive losses: live trading is suspended until {ends} "
            f"({days:.1f} days). Paper trading is unaffected."
        )

    def _equity_floor(self, account: AccountSnapshot) -> str | None:
        floor = self._rules.account.min_equity_floor_usd
        if account.equity_usd < floor:
            return f"equity {account.equity_usd:,.2f} is below floor {floor:,.2f}"
        return None

    def _symbol_allowed(self, proposal: OrderProposal) -> str | None:
        if not self._rules.is_symbol_allowed(proposal.symbol):
            return f"symbol {proposal.symbol} is not in the allowed list"
        return None

    def _within_session(self, instrument: InstrumentRules | None) -> str | None:
        """Session window comes from the instrument class, not a global setting.

        A 24/7 market configures `[[0, 24]]` across all seven days and is
        therefore always in session, rather than needing to be special-cased in
        code.

        The day is checked as well as the hour. Hours alone made Saturday at
        15:00 UTC a valid equity session, and Alpaca does not refuse an
        out-of-hours equity order: it queues it to the next session, so the
        fill arrives at Monday's open — the noisy half-hour `sessions_utc` is
        set to skip. That gap cost nothing while the loop placed no orders and
        became live the moment `--execute` went on.

        **Market holidays are still not covered.** Thanksgiving is a Thursday,
        so this passes it, and the order queues to the next open exactly as a
        weekend one used to. Closing that needs Alpaca's calendar endpoint, and
        it is a network call, which does not belong inside a gate that has to
        stay deterministic and cannot fail open. Named here rather than left as
        an absence: the guard is weekday-shaped, not market-open-shaped.
        """
        if instrument is None:
            return None  # the allowlist gate already rejected this
        now = self._now()
        label = proposal_class_label(instrument)

        if not instrument.is_trading_day(now):
            return (
                f"{now.strftime('%A')} is not a trading day for {label} "
                f"(an equity order placed now would queue to the next open)"
            )
        if instrument.is_within_hours(now):
            return None
        return (
            f"{now.hour:02d}:00 UTC is outside the trading sessions for {label}"
        )

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

    def _total_risk(self, proposal: OrderProposal, account: AccountSnapshot) -> str | None:
        """Cap combined risk across every open position plus this one.

        Risk, not position value: what would actually be lost if every stop
        filled. That makes the rule leverage-neutral — it means the same thing
        whether the exposure came from cash equities, margin, options or
        futures — and it composes with the per-trade cap, so a 2% total against
        a 1% per-trade allows two full-size trades or four half-size ones.
        """
        if account.equity_usd <= 0:
            return None
        risk_after = account.open_risk_usd + proposal.risk_usd
        pct_after = risk_after / account.equity_usd * 100
        cap = self._rules.account.max_total_risk_pct
        if pct_after > cap:
            return (
                f"total risk would reach {pct_after:.2f}% of equity "
                f"(max {cap:.2f}%)"
            )
        return None

    def _buying_power(self, proposal: OrderProposal, account: AccountSnapshot) -> str | None:
        """Stay well clear of an Intraday Margin Deficit call.

        Alpaca rejects deficit-creating orders in real time, and repeated
        non-compliance inside five business days costs a 90-day restriction.
        Leaving headroom is much cheaper than finding the edge of it.
        """
        if account.buying_power_usd <= 0:
            return "no buying power available"
        used_pct = proposal.notional_usd / account.buying_power_usd * 100
        cap = self._rules.margin.max_buying_power_utilisation_pct
        if used_pct > cap:
            return (
                f"order would use {used_pct:.1f}% of buying power (max {cap:.1f}%)"
            )
        return None

    def _gross_notional(self, proposal: OrderProposal, account: AccountSnapshot) -> str | None:
        """Cap total market exposure as a multiple of equity."""
        if account.equity_usd <= 0:
            return None
        notional_after = account.gross_exposure_usd + proposal.notional_usd
        pct_after = notional_after / account.equity_usd * 100
        cap = self._rules.margin.max_gross_notional_pct
        if pct_after > cap:
            return (
                f"gross notional would reach {pct_after:.0f}% of equity "
                f"(max {cap:.0f}%)"
            )
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

    def _option_expiry(self, proposal: OrderProposal) -> str | None:
        """Refuse to open an option position that is already near expiry.

        Buying into the last few days means inheriting Alpaca's automatic
        exercise, assignment and liquidation behaviour with very little room to
        change your mind — and "Do Not Exercise" cannot be filed through the
        API, so the only exit is closing the position in time.
        """
        contract = parse_occ_symbol(proposal.symbol)
        if contract is None:
            return None

        days = contract.days_to_expiry(self._now())
        minimum = self._rules.options.min_days_to_expiry_for_entry
        if days < minimum:
            return (
                f"{proposal.symbol} expires in {days:.2f} days, inside the "
                f"{minimum:.1f}-day minimum for opening an option position"
            )
        return None

    def _instrument_capital_cap(
        self,
        proposal: OrderProposal,
        account: AccountSnapshot,
        instrument: InstrumentRules | None,
    ) -> str | None:
        """Optional ceiling on one asset class's share of the portfolio.

        Keeps a volatile class from quietly growing into the whole account. Only
        applies where a cap is configured; classes without one are bounded by the
        portfolio-wide risk and exposure limits alone.
        """
        if instrument is None or instrument.capital_cap_pct is None:
            return None
        if account.equity_usd <= 0:
            return None

        symbols = set(instrument.allowed_symbols)
        held = sum(p.notional_usd for p in account.open_positions if p.symbol in symbols)
        pct_after = (held + proposal.notional_usd) / account.equity_usd * 100

        if pct_after > instrument.capital_cap_pct:
            return (
                f"{proposal_class_label(instrument)} allocation would reach "
                f"{pct_after:.1f}% of equity (cap {instrument.capital_cap_pct:.1f}%)"
            )
        return None


def aggregate_pnl_today(positions: list[Position], realised_pnl_today_usd: float) -> float:
    """Realised plus unrealised P&L for the day."""
    return realised_pnl_today_usd + sum(p.unrealised_pnl_usd for p in positions)
