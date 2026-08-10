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

**This module is deterministic and must stay that way.** It reads no database,
opens no file and makes no network call; everything it needs arrives as an
argument. That is why the earnings calendar arrives as `news_windows` and why a
dream's symbol grant arrives as `granted_symbols` — resolved in `grants.py`,
which fails to an empty mapping, because a gate that can fail is a gate that can
fail OPEN. Nothing here imports `grants` or `dreaming`, and nothing there
imports this.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from .config import InstrumentRules, Rules
from .market_clock import MarketPhase, market_state
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


class ResolvedClass(NamedTuple):
    """Which instrument class a proposal's symbol trades under, and how.

    Three facts that have to travel together, because every downstream gate
    needs at least one of them:

    - `instrument` is the block whose limits apply. `None` means no enabled
      class claims this symbol, and every gate that takes it returns early —
      the allowlist gate has already refused the proposal.
    - `class_key` is the `instruments:` key, which is what `allowed_symbols`
      alone cannot supply for a granted symbol. It is how a grant is matched
      back to the class whose caps it counts against.
    - `granted_class` is set only when a dream grant is the reason the symbol
      resolved at all. `None` for a symbol `config/rules.yaml` already allows,
      even if a grant also names it — the verdict must not claim a dream was
      load-bearing on a trade the allowlist would have permitted anyway.
    """

    instrument: InstrumentRules | None
    class_key: str | None
    granted_class: str | None


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
        granted_symbols: Mapping[str, str] | None = None,
    ) -> RiskVerdict:
        """Run every gate. Returns approve() only if all of them pass.

        This gates *opening* exposure only. Closing a position and moving a stop
        never come through here, which is deliberate: a stand-down that also
        froze position management would strand open trades with no way out.

        `granted_symbols` maps a symbol to the instrument-class key an adopted
        dream permits it under, in the same shape as `news_windows` and for the
        same reason: **it is resolved outside and passed in.** This gate reads
        no database, opens no file and makes no network call, because a gate
        that can fail is a gate that can fail OPEN. `grants.py` does the
        resolution and answers `{}` for every failure, so an unavailable dream
        store costs a permission rather than a rule.

        A grant buys **entry to the allowlist and nothing else.** The symbol is
        resolved to the class named in the grant and then faces every gate below
        exactly as a listed symbol in that class would, including the ones that
        count what the class already holds. See `_class_symbols`.
        """
        activity = activity or TradingActivity()
        grants = granted_symbols or {}

        # Each asset class carries its own session window and symbol list, so
        # the gate resolves the class once and applies that class's rules. This
        # replaces special-casing crypto, which did not generalise.
        resolved = self._resolve_class(proposal.symbol, grants)
        instrument = resolved.instrument
        # Which symbols count AS this class, listed or granted. Computed once
        # and handed to the three gates that measure what the class is already
        # carrying, so a granted symbol cannot be invisible to the caps it is
        # supposed to be subject to.
        class_symbols = self._class_symbols(resolved, grants)

        checks: list[str | None] = [
            self._kill_switch(),
            self._stand_down(stand_down),
            self._equity_floor(account),
            self._symbol_allowed(proposal, resolved, grants),
            self._within_session(instrument),
            self._premarket(instrument),
            self._news_blackout(proposal.symbol, news_windows or []),
            self._concurrent_positions(account, instrument, class_symbols),
            self._daily_loss(account),
            self._limit_price_sane(proposal, tick),
            self._stops_on_correct_side(proposal),
            self._per_trade_risk(proposal, account, instrument),
            self._position_size(proposal, account, instrument),
            self._total_risk(proposal, account),
            self._class_total_risk(proposal, account, instrument, class_symbols),
            self._buying_power(proposal, account),
            self._gross_notional(proposal, account),
            self._trades_per_day(activity),
            self._trades_per_week(activity),
            self._symbol_cooldown(proposal, activity),
            self._option_expiry(proposal),
            self._instrument_capital_cap(proposal, account, instrument, class_symbols),
        ]

        reasons = [c for c in checks if c is not None]
        granted_by = resolved.granted_class
        if reasons:
            return RiskVerdict.reject(*reasons, granted_by_dream_class=granted_by)
        return RiskVerdict.approve(granted_by_dream_class=granted_by)

    # ------------------------------------------------------- class resolution

    def _resolve_class(
        self, symbol: str, grants: Mapping[str, str]
    ) -> ResolvedClass:
        """Which class's limits apply to this symbol, and whether a dream did it.

        `Rules.for_symbol` cannot find a granted symbol — it is in no
        `allowed_symbols` list — so the class comes from the grant itself. Two
        properties are load-bearing:

        - **The listed answer wins.** A symbol already in an enabled class is
          resolved that way whether or not a grant also names it, so a stale or
          duplicated grant can never move a listed symbol into a different
          class's limits, and the verdict does not claim a dream permitted a
          trade the allowlist already permitted.
        - **A grant naming a class that is not enabled resolves to NOTHING**,
          which makes the symbol unallowed and the proposal refused. That is the
          class hard block, and it is checked here as well as in `grants.py`:
          the resolver is the useful error message and this is the guarantee, in
          the same arrangement as `mode=ro` plus the statement guard in
          `insight.py`. A gate that trusted its input to have been filtered
          would be a gate whose safety lived in another file.
        """
        listed = self._rules.for_symbol(symbol)
        if listed is not None:
            return ResolvedClass(listed, self._rules.class_name_for(symbol), None)

        class_key = grants.get(symbol)
        if class_key is None:
            return ResolvedClass(None, None, None)

        instrument = self._rules.enabled_instruments.get(class_key)
        if instrument is None:
            return ResolvedClass(None, None, None)
        return ResolvedClass(instrument, class_key, class_key)

    @staticmethod
    def _class_symbols(
        resolved: ResolvedClass, grants: Mapping[str, str]
    ) -> set[str]:
        """Every symbol that counts as this class: listed plus currently granted.

        **This is what stops a grant bypassing the caps that are not about the
        symbol.** `_concurrent_positions`, `_class_total_risk` and
        `_instrument_capital_cap` all ask "how much of this class am I already
        carrying", and all three answer it by SYMBOL MEMBERSHIP of
        `allowed_symbols` rather than by the position's `asset_class` enum — one
        source, so the two cannot drift into different answers.

        A granted symbol is in no `allowed_symbols` list, so without this a
        position held under a grant would be invisible to its own class's
        concurrency cap, class total-risk cap and capital cap: the grant would
        have bought entry to the allowlist AND a quiet exemption from three
        limits. Adoption buys the first and must not buy the second.

        The membership is computed from the grants in force NOW, which has a
        consequence worth stating rather than discovering: a position still held
        under a grant that has since expired drops back out of the class's
        counts. That is not new — before grants existed it was never in them —
        and it is the same shape as the `dream-expired-holding` case, where the
        permission to open ends while the position stands.
        """
        if resolved.instrument is None:
            return set()
        symbols = set(resolved.instrument.allowed_symbols)
        if resolved.class_key is not None:
            symbols |= {s for s, key in grants.items() if key == resolved.class_key}
        return symbols

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

    def _symbol_allowed(
        self,
        proposal: OrderProposal,
        resolved: ResolvedClass,
        grants: Mapping[str, str],
    ) -> str | None:
        """The allowlist, plus whatever an adopted dream currently widens it by.

        A grant only passes here once it has RESOLVED — that is, once its class
        key names an enabled instrument block. A grant naming a disabled class
        is refused with its own sentence, because "not in the allowed list" on a
        symbol a dream visibly granted would send an operator looking for the
        wrong problem.
        """
        if self._rules.is_symbol_allowed(proposal.symbol):
            return None
        if resolved.granted_class is not None:
            return None

        claimed = grants.get(proposal.symbol)
        if claimed is not None:
            return (
                f"symbol {proposal.symbol} is not in the allowed list: an "
                f"adopted dream grants it under '{claimed}', which is not an "
                f"enabled instrument class. A dream may widen the symbols "
                f"inside an enabled class and can never enable a class."
            )
        return f"symbol {proposal.symbol} is not in the allowed list"

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

    def _premarket(self, instrument: InstrumentRules | None) -> str | None:
        """The operator's rule: no pre-market. After hours is fine.

        Separate from `_within_session` because it answers a question a UTC
        window structurally cannot. `sessions_utc` is fixed hours; the US
        session is defined in New York time and moves an hour twice a year, so
        the configured `[[14, 21]]` opens at 09:00 New York through the winter
        — half an hour of pre-market, every day, with nothing in the window
        able to notice.

        That gap used to cost nothing, because an out-of-hours equity order was
        queued to the next open rather than filled. It costs something now:
        Alpaca runs a pre-market session from 04:00 ET, so an order placed into
        it **trades**, in a thinner book than anybody chose.

        `market_clock` is a pure function over the clock — no network, no
        calendar fetch — so this stays deterministic and cannot fail open, and
        it only ever adds a reason to refuse.

        **Holidays are still not covered**, exactly as in `_within_session`.
        Thanksgiving reads as an ordinary Thursday to both.
        """
        if instrument is None or not instrument.refuse_premarket:
            return None
        phase = market_state(self._now()).phase
        if phase is not MarketPhase.PRE:
            return None
        return (
            "pre-market session (04:00-09:30 New York); this account does not "
            "trade pre-market, and Alpaca would fill this rather than queue it"
        )

    def _news_blackout(self, symbol: str, windows: list[NewsWindow]) -> str | None:
        before = timedelta(minutes=self._rules.news_blackout_minutes_before)
        after = timedelta(minutes=self._rules.news_blackout_minutes_after)
        now = self._now()
        for w in windows:
            if symbol in w.affected_symbols and w.timestamp - before <= now <= w.timestamp + after:
                return f"inside news blackout window around {w.timestamp.isoformat()}"
        return None

    def _concurrent_positions(
        self,
        account: AccountSnapshot,
        instrument: InstrumentRules | None = None,
        class_symbols: set[str] | None = None,
    ) -> str | None:
        """The portfolio cap always, and this class's own cap if it has one.

        Both, not either. The portfolio limit bounds the account and a class
        limit bounds the class, so a class that gets loud cannot fill every
        slot the portfolio has — which is the failure a single global count
        cannot express.
        """
        cap = self._rules.account.max_concurrent_positions
        if len(account.open_positions) >= cap:
            return f"already holding {len(account.open_positions)} positions (max {cap})"

        if instrument is None or instrument.max_concurrent_positions is None:
            return None
        # Counted WITHIN the class, so the two caps measure different things.
        # By SYMBOL membership rather than by the position's `asset_class`: the
        # instrument block's own `allowed_symbols` is what defines the class
        # here, and reading the enum instead would ask two different sources
        # the same question and eventually get two answers.
        #
        # `class_symbols` is that list PLUS anything an adopted dream currently
        # grants under this class, so a position held under a grant is not
        # invisible to the cap it is subject to. See `_class_symbols`.
        symbols = (
            class_symbols
            if class_symbols is not None
            else set(instrument.allowed_symbols)
        )
        held = sum(1 for p in account.open_positions if p.symbol in symbols)
        class_cap = instrument.max_concurrent_positions
        if held >= class_cap:
            label = proposal_class_label(instrument)
            return (
                f"already holding {held} {label} position(s) "
                f"(max {class_cap} for this instrument class)"
            )
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
        target = proposal.take_profit_price
        if proposal.direction == Direction.BUY:
            if proposal.stop_loss_price >= entry:
                return f"buy stop-loss {proposal.stop_loss_price} is not below entry {entry}"
            # Only when one was given. A trade with no target is a normal trade
            # — the stop is what the operator's rules require, never the exit.
            if target is not None and target <= entry:
                return f"buy take-profit {target} is not above entry {entry}"
        else:
            if proposal.stop_loss_price <= entry:
                return f"sell stop-loss {proposal.stop_loss_price} is not above entry {entry}"
            if target is not None and target >= entry:
                return f"sell take-profit {target} is not below entry {entry}"
        return None

    def _per_trade_risk(
        self,
        proposal: OrderProposal,
        account: AccountSnapshot,
        instrument: InstrumentRules | None = None,
    ) -> str | None:
        """Loss if the stop fills must stay within the per-trade cap.

        Shares and coin units are 1:1 with price, so this is exact — unlike the
        FX version this replaces, which had to approximate contract sizes.

        A class limit OVERRIDES the portfolio one, in either direction —
        `account:` is the default rather than a ceiling. It is deliberately not
        floored back with a `min`: a file saying 3% while the gate quietly
        applied 1% would be a limit nobody could read off the config, which is
        worse than either number on its own.

        Pushing back on a limit getting looser is a job for the settings agent
        (see docs), which argues and slows the operator down without denying
        the change. It is not a job for a validator that refuses to start.
        """
        pct = self._rules.account.max_risk_per_trade_pct
        if instrument is not None and instrument.max_risk_per_trade_pct is not None:
            pct = instrument.max_risk_per_trade_pct

        risk_usd = abs(proposal.limit_price - proposal.stop_loss_price) * proposal.qty
        cap_usd = account.equity_usd * pct / 100
        if risk_usd > cap_usd:
            return (
                f"risk {risk_usd:,.2f} exceeds the per-trade cap {cap_usd:,.2f} "
                f"({pct:.2f}% of equity)"
            )
        return None

    def _position_size(
        self,
        proposal: OrderProposal,
        account: AccountSnapshot,
        instrument: InstrumentRules | None = None,
    ) -> str | None:
        if account.equity_usd <= 0:
            return "account equity is zero or negative"
        pct = proposal.notional_usd / account.equity_usd * 100
        cap = self._rules.account.max_position_pct
        if instrument is not None and instrument.max_position_pct is not None:
            # Overrides rather than floors. See `_per_trade_risk`.
            cap = instrument.max_position_pct
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

    def _class_total_risk(
        self,
        proposal: OrderProposal,
        account: AccountSnapshot,
        instrument: InstrumentRules | None,
        class_symbols: set[str] | None = None,
    ) -> str | None:
        """Cap the combined open risk held by ONE instrument class.

        `_total_risk` bounds the portfolio and `_per_trade_risk` bounds a single
        trade. Neither can say "this class may hold no more than X at once", and
        a class allowed 0.5% per trade with nothing else in the way is a class
        that can quietly accumulate four of them.

        **Unrealised profit does not offset open risk.** That is the operator's
        rule stated outright, and it follows from the unit the rest of this file
        rests on: risk is `|entry - stop| * qty`, what the position loses if the
        stop fills as planned. A position being up today does not change what
        its stop costs tomorrow. Netting a paper gain against a real stop
        distance would make the cap loosest exactly when the class had run
        furthest — and a mark is the one input here that can reverse before
        anything is acted on.

        So the consequence is deliberate rather than incidental: at the cap, an
        existing position in the class has to be **closed** before another can
        be opened. The gate will not size the new trade down to fit and will not
        let a winner count for less. The rejection says so, because a bare
        number would leave an operator looking for a limit to widen.

        **An unknown fails CLOSED.** A held position with no journal row has an
        unknowable planned stop, so the class total cannot be computed at all —
        and computing it without that position would report a figure lower than
        reality. `reconcile` already calls that `risk_is_understated` and warns;
        here it is a rejection, because this gate is what the figure is for and
        approving against an understated total is how a cap silently stops
        binding.

        Membership is by the class's own `allowed_symbols` rather than the
        position's `asset_class` enum, exactly as in `_concurrent_positions` and
        `_instrument_capital_cap`: one source answers the question, so the two
        cannot drift into different answers. A symbol an adopted dream currently
        grants under this class counts too — a grant buys entry to the allowlist
        and must not buy an exemption from the cap that entry falls under.
        """
        if instrument is None or instrument.max_class_total_risk_pct is None:
            return None
        if account.equity_usd <= 0:
            return None

        pct = instrument.max_class_total_risk_pct
        label = proposal_class_label(instrument)
        symbols = (
            class_symbols
            if class_symbols is not None
            else set(instrument.allowed_symbols)
        )
        held_symbols = [p.symbol for p in account.open_positions if p.symbol in symbols]

        unknown = sorted(set(held_symbols) & set(account.symbols_with_unknown_risk))
        if unknown:
            return (
                f"{label} open risk cannot be established, so the "
                f"{pct:.2f}%-of-equity class cap cannot be enforced. Held with no "
                f"journal row: {', '.join(unknown)}. A position the journal has "
                f"never seen has an unknowable planned stop, so its risk is "
                f"missing rather than zero. Close or journal it before opening "
                f"another {label} position."
            )

        held = sum(account.open_risk_by_symbol.get(s, 0.0) for s in held_symbols)
        after = held + proposal.risk_usd
        cap = account.equity_usd * pct / 100
        if after > cap:
            return (
                f"{label} open risk would reach {after:,.2f} against the class cap "
                f"{cap:,.2f} ({pct:.2f}% of equity): {held:,.2f} already at risk in "
                f"this class plus {proposal.risk_usd:,.2f} for this trade. "
                f"Unrealised profit does not offset open risk — risk is "
                f"|entry - stop| x qty, which is what an open stop still costs if "
                f"it fills. Close an existing {label} position before opening "
                f"another."
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
        class_symbols: set[str] | None = None,
    ) -> str | None:
        """Optional ceiling on one asset class's share of the portfolio.

        Keeps a volatile class from quietly growing into the whole account. Only
        applies where a cap is configured; classes without one are bounded by the
        portfolio-wide risk and exposure limits alone.

        Membership includes symbols an adopted dream currently grants under this
        class, exactly as in `_concurrent_positions` and `_class_total_risk`.
        """
        if instrument is None or instrument.capital_cap_pct is None:
            return None
        if account.equity_usd <= 0:
            return None

        symbols = (
            class_symbols
            if class_symbols is not None
            else set(instrument.allowed_symbols)
        )
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
