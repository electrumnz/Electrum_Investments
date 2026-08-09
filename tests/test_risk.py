"""Tests for the risk gate.

This is the load-bearing suite. The gate is the only thing standing between a
confidently-wrong model and the account, so each rule gets an explicit test that
proves it rejects, not merely that it exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.config import Rules
from bot.models import (
    AccountSnapshot,
    AssetClass,
    Direction,
    OrderProposal,
    Position,
    Tick,
    TradingActivity,
)
from bot.risk import NewsWindow, RiskGate

from .conftest import INSIDE_SESSION, PAPER_EQUITY


def _gate(
    rules: Rules,
    *,
    equity: float = PAPER_EQUITY,
    now: datetime | None = None,
) -> RiskGate:
    return RiskGate(
        rules,
        equity_at_session_start=equity,
        now=now or INSIDE_SESSION,
    )


def _reasons_mention(verdict, fragment: str) -> bool:
    return any(fragment.lower() in r.lower() for r in verdict.reasons)


# --------------------------------------------------------------- happy path


def test_approves_well_formed_proposal(rules, account, spy_tick, buy_proposal):
    verdict = _gate(rules).evaluate(buy_proposal, account=account, tick=spy_tick)
    assert verdict.approved, verdict.reasons


def test_collects_every_failure_not_just_the_first(rules, account, spy_tick):
    """A bad proposal should report all its problems in one pass."""
    bad = OrderProposal(
        symbol="TSLA",  # not allowed
        direction=Direction.BUY,
        qty=1000,  # oversized
        limit_price=580.00,
        stop_loss_price=600.00,  # wrong side
        take_profit_price=560.00,  # wrong side
        rationale="Deliberately broken in several ways at once.",
    )
    verdict = _gate(rules).evaluate(bad, account=account, tick=spy_tick)
    assert not verdict.approved
    assert len(verdict.reasons) >= 3, verdict.reasons


# ------------------------------------------------------------ symbol/session


def test_rejects_disallowed_symbol(rules, account, spy_tick):
    proposal = OrderProposal(
        symbol="GME",
        direction=Direction.BUY,
        qty=1,
        limit_price=580.00,
        stop_loss_price=575.00,
        take_profit_price=590.00,
        rationale="Not on the allowlist, should be refused outright.",
    )
    verdict = _gate(rules).evaluate(proposal, account=account, tick=spy_tick)
    assert not verdict.approved
    assert _reasons_mention(verdict, "not in the allowed list")


def test_rejects_outside_session(rules, account, spy_tick, buy_proposal):
    night = datetime(2026, 5, 4, 3, 0, tzinfo=UTC)  # outside [14, 21)
    verdict = _gate(rules, now=night).evaluate(buy_proposal, account=account, tick=spy_tick)
    assert not verdict.approved
    assert _reasons_mention(verdict, "outside the allowed trading sessions")


def test_rejects_during_news_blackout(rules, account, spy_tick, buy_proposal):
    window = NewsWindow(
        timestamp=INSIDE_SESSION + timedelta(minutes=5),
        affected_symbols=frozenset({"SPY", "QQQ"}),
    )
    verdict = _gate(rules).evaluate(
        buy_proposal, account=account, tick=spy_tick, news_windows=[window]
    )
    assert not verdict.approved
    assert _reasons_mention(verdict, "news blackout")


def test_news_window_for_other_symbol_does_not_block(rules, account, spy_tick, buy_proposal):
    window = NewsWindow(
        timestamp=INSIDE_SESSION + timedelta(minutes=5),
        affected_symbols=frozenset({"AAPL"}),
    )
    verdict = _gate(rules).evaluate(
        buy_proposal, account=account, tick=spy_tick, news_windows=[window]
    )
    assert verdict.approved, verdict.reasons


# ------------------------------------------------------------------ sizing


def test_rejects_oversized_risk(rules, account, spy_tick):
    """Stop 50 points away on 500 shares = $25,000 risk on a $100k account."""
    huge = OrderProposal(
        symbol="SPY",
        direction=Direction.BUY,
        qty=500,
        limit_price=580.00,
        stop_loss_price=530.00,
        take_profit_price=600.00,
        rationale="Oversized, should breach the per-trade risk cap.",
    )
    verdict = _gate(rules).evaluate(huge, account=account, tick=spy_tick)
    assert not verdict.approved
    assert _reasons_mention(verdict, "per-trade cap")


def test_rejects_position_larger_than_max_position_pct(rules, account, spy_tick):
    """A tight stop keeps risk small, but concentration is capped independently."""
    concentrated = OrderProposal(
        symbol="SPY",
        direction=Direction.BUY,
        qty=100,  # $58,000 = 58% of equity
        limit_price=580.00,
        stop_loss_price=579.50,  # only $50 of risk
        take_profit_price=590.00,
        rationale="Tiny stop but a huge position; concentration gate should catch it.",
    )
    verdict = _gate(rules).evaluate(concentrated, account=account, tick=spy_tick)
    assert not verdict.approved
    assert _reasons_mention(verdict, "of equity")


def test_rejects_when_cash_reserve_would_be_breached(rules, spy_tick, buy_proposal):
    poor_cash = AccountSnapshot(
        equity_usd=PAPER_EQUITY,
        cash_usd=PAPER_EQUITY * 0.21,  # 21% cash; the $5,800 order drops it below 20%
        buying_power_usd=PAPER_EQUITY * 0.21,
        open_positions=[],
    )
    verdict = _gate(rules).evaluate(buy_proposal, account=poor_cash, tick=spy_tick)
    assert not verdict.approved
    assert _reasons_mention(verdict, "reserve")


def test_rejects_when_gross_exposure_would_be_breached(rules, spy_tick, buy_proposal):
    heavy = AccountSnapshot(
        equity_usd=PAPER_EQUITY,
        cash_usd=PAPER_EQUITY,
        buying_power_usd=PAPER_EQUITY,
        open_positions=[
            Position(
                symbol="QQQ",
                direction=Direction.BUY,
                qty=160,
                entry_price=500.0,  # $80,000 = 80% of equity, already at the cap
                opened_at=INSIDE_SESSION,
                current_price=500.0,
            )
        ],
    )
    verdict = _gate(rules).evaluate(buy_proposal, account=heavy, tick=spy_tick)
    assert not verdict.approved
    assert _reasons_mention(verdict, "gross exposure")


def test_rejects_limit_price_far_from_market(rules, account, spy_tick):
    """A limit 20% below market is almost always a model arithmetic slip."""
    astray = OrderProposal(
        symbol="SPY",
        direction=Direction.BUY,
        qty=10,
        limit_price=464.00,
        stop_loss_price=460.00,
        take_profit_price=470.00,
        rationale="Limit price nowhere near the current market.",
    )
    verdict = _gate(rules).evaluate(astray, account=account, tick=spy_tick)
    assert not verdict.approved
    assert _reasons_mention(verdict, "away from the market")


# ------------------------------------------------------------------- stops


def test_rejects_buy_with_inverted_stop(rules, account, spy_tick):
    bad = OrderProposal(
        symbol="SPY",
        direction=Direction.BUY,
        qty=10,
        limit_price=580.00,
        stop_loss_price=585.00,  # above entry on a buy
        take_profit_price=590.00,
        rationale="Stop is on the wrong side of entry for a long.",
    )
    verdict = _gate(rules).evaluate(bad, account=account, tick=spy_tick)
    assert not verdict.approved
    assert _reasons_mention(verdict, "stop-loss")


def test_rejects_sell_with_inverted_stop(rules, account, spy_tick):
    bad = OrderProposal(
        symbol="SPY",
        direction=Direction.SELL,
        qty=10,
        limit_price=580.00,
        stop_loss_price=575.00,  # below entry on a sell
        take_profit_price=570.00,
        rationale="Stop is on the wrong side of entry for a short.",
    )
    verdict = _gate(rules).evaluate(bad, account=account, tick=spy_tick)
    assert not verdict.approved
    assert _reasons_mention(verdict, "stop-loss")


def test_accepts_well_formed_sell(rules, account, spy_tick):
    good = OrderProposal(
        symbol="SPY",
        direction=Direction.SELL,
        qty=10,
        limit_price=580.00,
        stop_loss_price=585.00,
        take_profit_price=570.00,
        rationale="Rejected the prior day high; invalidated above 585.",
    )
    verdict = _gate(rules).evaluate(good, account=account, tick=spy_tick)
    assert verdict.approved, verdict.reasons


# -------------------------------------------------------------- account state


def test_rejects_at_max_concurrent_positions(rules, spy_tick, buy_proposal):
    full = AccountSnapshot(
        equity_usd=PAPER_EQUITY,
        cash_usd=PAPER_EQUITY,
        buying_power_usd=PAPER_EQUITY,
        open_positions=[
            Position(
                symbol=sym,
                direction=Direction.BUY,
                qty=1,
                entry_price=100.0,
                opened_at=INSIDE_SESSION,
                current_price=100.0,
            )
            for sym in ("AAPL", "MSFT", "JNJ")[: rules.account.max_concurrent_positions]
        ],
    )
    verdict = _gate(rules).evaluate(buy_proposal, account=full, tick=spy_tick)
    assert not verdict.approved
    assert _reasons_mention(verdict, "positions")


def test_rejects_below_equity_floor(rules, spy_tick, buy_proposal):
    broke = AccountSnapshot(
        equity_usd=rules.account.min_equity_floor_usd - 1,
        cash_usd=50_000.0,
        buying_power_usd=50_000.0,
        open_positions=[],
    )
    verdict = _gate(rules, equity=PAPER_EQUITY).evaluate(
        buy_proposal, account=broke, tick=spy_tick
    )
    assert not verdict.approved
    assert _reasons_mention(verdict, "below floor")


def test_daily_kill_switch_trips_and_is_sticky(rules, spy_tick, buy_proposal):
    losing = AccountSnapshot(
        equity_usd=PAPER_EQUITY * (1 - rules.account.daily_loss_kill_pct / 100) - 1,
        cash_usd=PAPER_EQUITY,
        buying_power_usd=PAPER_EQUITY,
        open_positions=[],
    )
    gate = _gate(rules, equity=PAPER_EQUITY)

    first = gate.evaluate(buy_proposal, account=losing, tick=spy_tick)
    assert not first.approved
    assert _reasons_mention(first, "daily loss")

    # Equity recovers within the same session — the switch must stay tripped.
    recovered = AccountSnapshot(
        equity_usd=PAPER_EQUITY,
        cash_usd=PAPER_EQUITY,
        buying_power_usd=PAPER_EQUITY,
        open_positions=[],
    )
    second = gate.evaluate(buy_proposal, account=recovered, tick=spy_tick)
    assert not second.approved
    assert _reasons_mention(second, "kill-switch")


def test_reset_daily_clears_kill_switch(rules, account, spy_tick, buy_proposal):
    gate = _gate(rules)
    gate.trip_kill_switch()
    assert not gate.evaluate(buy_proposal, account=account, tick=spy_tick).approved

    gate.reset_daily(equity_at_session_start=PAPER_EQUITY)
    assert gate.evaluate(buy_proposal, account=account, tick=spy_tick).approved


# ---------------------------------------------------------------- frequency


def test_rejects_when_daily_trade_limit_reached(rules, account, spy_tick, buy_proposal):
    busy = TradingActivity(trades_today=rules.frequency.max_trades_per_day)
    verdict = _gate(rules).evaluate(
        buy_proposal, account=account, tick=spy_tick, activity=busy
    )
    assert not verdict.approved
    assert _reasons_mention(verdict, "trades today")


def test_rejects_when_weekly_trade_limit_reached(rules, account, spy_tick, buy_proposal):
    busy = TradingActivity(
        trades_today=0, trades_this_week=rules.frequency.max_trades_per_week
    )
    verdict = _gate(rules).evaluate(
        buy_proposal, account=account, tick=spy_tick, activity=busy
    )
    assert not verdict.approved
    assert _reasons_mention(verdict, "this week")


def test_rejects_inside_symbol_cooldown(rules, account, spy_tick, buy_proposal):
    just_traded = TradingActivity(
        last_trade_at_by_symbol={"SPY": INSIDE_SESSION - timedelta(seconds=60)}
    )
    verdict = _gate(rules).evaluate(
        buy_proposal, account=account, tick=spy_tick, activity=just_traded
    )
    assert not verdict.approved
    assert _reasons_mention(verdict, "cooldown")


def test_allows_after_cooldown_expires(rules, account, spy_tick, buy_proposal):
    cooldown = rules.frequency.min_seconds_between_trades_per_symbol
    long_ago = TradingActivity(
        last_trade_at_by_symbol={"SPY": INSIDE_SESSION - timedelta(seconds=cooldown + 60)}
    )
    verdict = _gate(rules).evaluate(
        buy_proposal, account=account, tick=spy_tick, activity=long_ago
    )
    assert verdict.approved, verdict.reasons


def test_cooldown_on_other_symbol_does_not_block(rules, account, spy_tick, buy_proposal):
    other = TradingActivity(
        last_trade_at_by_symbol={"QQQ": INSIDE_SESSION - timedelta(seconds=10)}
    )
    verdict = _gate(rules).evaluate(
        buy_proposal, account=account, tick=spy_tick, activity=other
    )
    assert verdict.approved, verdict.reasons


# ---------------------------------------------------------------------- PDT


def test_pdt_blocks_fourth_day_trade_below_threshold(rules, spy_tick, buy_proposal):
    small = AccountSnapshot(
        equity_usd=20_000.0,  # below the $25k threshold
        cash_usd=20_000.0,
        buying_power_usd=20_000.0,
        open_positions=[],
        daytrade_count=rules.pdt.max_day_trades_per_5_days,
    )
    # Equity floor is calibrated to a $100k paper account, so relax it here to
    # isolate the PDT gate rather than tripping the floor first.
    relaxed = rules.model_copy(deep=True)
    relaxed.account.min_equity_floor_usd = 1_000.0

    verdict = _gate(relaxed, equity=20_000.0).evaluate(
        buy_proposal, account=small, tick=spy_tick
    )
    assert not verdict.approved
    assert _reasons_mention(verdict, "pdt guard")


def test_pdt_does_not_bind_above_threshold(rules, spy_tick, buy_proposal):
    large = AccountSnapshot(
        equity_usd=PAPER_EQUITY,  # well above $25k
        cash_usd=PAPER_EQUITY,
        buying_power_usd=PAPER_EQUITY,
        open_positions=[],
        daytrade_count=99,
    )
    verdict = _gate(rules).evaluate(buy_proposal, account=large, tick=spy_tick)
    assert verdict.approved, verdict.reasons


def test_pdt_does_not_bind_on_crypto(rules, spy_tick):
    """Crypto is not a security, so PDT never applies to it."""
    relaxed = rules.model_copy(deep=True)
    relaxed.account.min_equity_floor_usd = 1_000.0
    relaxed.crypto_sleeve.enabled = True
    relaxed.crypto_sleeve.capital_cap_pct = 15.0
    relaxed.crypto_sleeve.allowed_symbols = ["BTC/USD"]

    small = AccountSnapshot(
        equity_usd=20_000.0,
        cash_usd=20_000.0,
        buying_power_usd=20_000.0,
        open_positions=[],
        daytrade_count=99,
    )
    btc_tick = Tick(symbol="BTC/USD", bid=64_990.0, ask=65_010.0, timestamp=INSIDE_SESSION)
    proposal = OrderProposal(
        symbol="BTC/USD",
        asset_class=AssetClass.CRYPTO,
        direction=Direction.BUY,
        qty=0.02,  # $1,300 notional = 6.5% of equity
        limit_price=65_000.0,
        stop_loss_price=63_000.0,
        take_profit_price=70_000.0,
        rationale="Crypto is PDT-exempt; this should clear the day-trade gate.",
    )
    verdict = _gate(relaxed, equity=20_000.0).evaluate(
        proposal, account=small, tick=btc_tick
    )
    assert verdict.approved, verdict.reasons


# ------------------------------------------------------------- crypto sleeve


def test_crypto_rejected_while_sleeve_disabled(rules, account):
    btc_tick = Tick(symbol="BTC/USD", bid=64_990.0, ask=65_010.0, timestamp=INSIDE_SESSION)
    proposal = OrderProposal(
        symbol="BTC/USD",
        asset_class=AssetClass.CRYPTO,
        direction=Direction.BUY,
        qty=0.01,
        limit_price=65_000.0,
        stop_loss_price=63_000.0,
        take_profit_price=70_000.0,
        rationale="Sleeve is disabled in the shipped rules, so this must be refused.",
    )
    verdict = _gate(rules).evaluate(proposal, account=account, tick=btc_tick)
    assert not verdict.approved


def test_crypto_sleeve_cap_is_enforced(rules, spy_tick):
    enabled = rules.model_copy(deep=True)
    enabled.crypto_sleeve.enabled = True
    enabled.crypto_sleeve.capital_cap_pct = 5.0
    enabled.crypto_sleeve.allowed_symbols = ["BTC/USD"]

    account = AccountSnapshot(
        equity_usd=PAPER_EQUITY,
        cash_usd=PAPER_EQUITY,
        buying_power_usd=PAPER_EQUITY,
        open_positions=[
            Position(
                symbol="BTC/USD",
                asset_class=AssetClass.CRYPTO,
                direction=Direction.BUY,
                qty=0.07,
                entry_price=65_000.0,  # $4,550 = 4.55% of equity
                opened_at=INSIDE_SESSION,
                current_price=65_000.0,
            )
        ],
    )
    btc_tick = Tick(symbol="BTC/USD", bid=64_990.0, ask=65_010.0, timestamp=INSIDE_SESSION)
    more = OrderProposal(
        symbol="BTC/USD",
        asset_class=AssetClass.CRYPTO,
        direction=Direction.BUY,
        qty=0.05,  # would take the sleeve past 5%
        limit_price=65_000.0,
        stop_loss_price=63_000.0,
        take_profit_price=70_000.0,
        rationale="Second crypto position that should breach the sleeve cap.",
    )
    verdict = _gate(enabled).evaluate(more, account=account, tick=btc_tick)
    assert not verdict.approved
    assert _reasons_mention(verdict, "sleeve cap")


def test_crypto_ignores_equity_session_window(rules):
    """Crypto trades 24/7, so the equities session gate must not apply."""
    enabled = rules.model_copy(deep=True)
    enabled.crypto_sleeve.enabled = True
    enabled.crypto_sleeve.capital_cap_pct = 15.0
    enabled.crypto_sleeve.allowed_symbols = ["BTC/USD"]

    account = AccountSnapshot(
        equity_usd=PAPER_EQUITY,
        cash_usd=PAPER_EQUITY,
        buying_power_usd=PAPER_EQUITY,
        open_positions=[],
    )
    middle_of_the_night = datetime(2026, 5, 4, 3, 0, tzinfo=UTC)
    btc_tick = Tick(
        symbol="BTC/USD", bid=64_990.0, ask=65_010.0, timestamp=middle_of_the_night
    )
    proposal = OrderProposal(
        symbol="BTC/USD",
        asset_class=AssetClass.CRYPTO,
        direction=Direction.BUY,
        qty=0.05,
        limit_price=65_000.0,
        stop_loss_price=63_000.0,
        take_profit_price=70_000.0,
        rationale="Crypto at 03:00 UTC should still be allowed through the gate.",
    )
    verdict = _gate(enabled, now=middle_of_the_night).evaluate(
        proposal, account=account, tick=btc_tick
    )
    assert verdict.approved, verdict.reasons


# ------------------------------------------------------------------- config


def test_crypto_sleeve_validation_rejects_inconsistent_config():
    from bot.config import CryptoSleeve

    with pytest.raises(ValueError, match="capital_cap_pct"):
        CryptoSleeve(enabled=True, capital_cap_pct=0, allowed_symbols=["BTC/USD"])

    with pytest.raises(ValueError, match="allowed_symbols"):
        CryptoSleeve(enabled=True, capital_cap_pct=10, allowed_symbols=[])


def test_frequency_rules_reject_weekly_below_daily():
    from bot.config import FrequencyRules

    with pytest.raises(ValueError, match="max_trades_per_week"):
        FrequencyRules(
            max_trades_per_day=10,
            max_trades_per_week=5,
            min_seconds_between_trades_per_symbol=0,
        )
