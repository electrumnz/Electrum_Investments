from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.config import Rules
from bot.models import (
    AccountSnapshot,
    Direction,
    OrderProposal,
    Position,
    Tick,
)
from bot.risk import NewsWindow, RiskGate


def _gate(rules: Rules, *, equity: float = 5000.0, now: datetime | None = None) -> RiskGate:
    return RiskGate(
        rules,
        equity_at_session_start=equity,
        now=now or datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
    )


def test_approves_well_formed_proposal(rules, empty_account, eurusd_tick, buy_proposal):
    gate = _gate(rules)
    verdict = gate.evaluate(buy_proposal, account=empty_account, tick=eurusd_tick)
    assert verdict.approved, verdict.reasons


def test_rejects_disallowed_symbol(rules, empty_account, eurusd_tick):
    gate = _gate(rules)
    proposal = OrderProposal(
        symbol="BTCUSD",
        direction=Direction.BUY,
        size_lots=0.01,
        stop_loss_price=60000,
        take_profit_price=70000,
        rationale="Crypto sleeve disabled but proposal sent anyway.",
    )
    btc_tick = Tick(symbol="BTCUSD", bid=65000, ask=65010, timestamp=eurusd_tick.timestamp)
    verdict = gate.evaluate(proposal, account=empty_account, tick=btc_tick)
    assert not verdict.approved
    assert any("not in allowed list" in r for r in verdict.reasons)


def test_rejects_outside_session(rules, empty_account, eurusd_tick, buy_proposal):
    night = datetime(2026, 5, 2, 3, 0, tzinfo=UTC)  # outside [7,21)
    gate = _gate(rules, now=night)
    verdict = gate.evaluate(buy_proposal, account=empty_account, tick=eurusd_tick)
    assert not verdict.approved
    assert any("session" in r for r in verdict.reasons)


def test_rejects_during_news_blackout(rules, empty_account, eurusd_tick, buy_proposal):
    now = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    gate = _gate(rules, now=now)
    window = NewsWindow(
        timestamp=now + timedelta(minutes=5),
        affected_symbols=frozenset({"EURUSD", "USDJPY"}),
    )
    verdict = gate.evaluate(
        buy_proposal,
        account=empty_account,
        tick=eurusd_tick,
        news_windows=[window],
    )
    assert not verdict.approved
    assert any("news blackout" in r for r in verdict.reasons)


def test_rejects_when_at_max_concurrent(rules, eurusd_tick, buy_proposal):
    open_positions = [
        Position(
            ticket=i,
            symbol="EURUSD",
            direction=Direction.BUY,
            size_lots=0.01,
            open_price=1.0850,
            open_time=datetime(2026, 5, 2, 11, 0, tzinfo=UTC),
        )
        for i in range(rules.account.max_concurrent_positions)
    ]
    account = AccountSnapshot(
        equity_usd=5000.0,
        balance_usd=5000.0,
        margin_used_usd=200.0,
        free_margin_usd=4800.0,
        open_positions=open_positions,
    )
    gate = _gate(rules)
    verdict = gate.evaluate(buy_proposal, account=account, tick=eurusd_tick)
    assert not verdict.approved
    assert any("max_concurrent_positions" in r for r in verdict.reasons)


def test_rejects_buy_with_inverted_sl(rules, empty_account, eurusd_tick):
    bad = OrderProposal(
        symbol="EURUSD",
        direction=Direction.BUY,
        size_lots=0.01,
        stop_loss_price=1.0900,  # above entry — wrong side
        take_profit_price=1.1000,
        rationale="Intentionally inverted to test the SL/TP gate.",
    )
    gate = _gate(rules)
    verdict = gate.evaluate(bad, account=empty_account, tick=eurusd_tick)
    assert not verdict.approved
    assert any("SL" in r for r in verdict.reasons)


def test_rejects_oversized_risk(rules, empty_account, eurusd_tick):
    huge = OrderProposal(
        symbol="EURUSD",
        direction=Direction.BUY,
        size_lots=10.0,  # 10 lots × 30 pips × $10 = $3000 risk on a $5k account
        stop_loss_price=1.0820,
        take_profit_price=1.0900,
        rationale="Oversized — should be rejected by per-trade risk cap.",
    )
    gate = _gate(rules)
    verdict = gate.evaluate(huge, account=empty_account, tick=eurusd_tick)
    assert not verdict.approved
    assert any("per-trade cap" in r for r in verdict.reasons)


def test_rejects_below_equity_floor(rules, eurusd_tick, buy_proposal):
    broke = AccountSnapshot(
        equity_usd=rules.account.min_equity_floor_usd - 1,
        balance_usd=4000.0,
        margin_used_usd=0.0,
        free_margin_usd=4000.0,
        open_positions=[],
    )
    gate = _gate(rules, equity=5000.0)
    verdict = gate.evaluate(buy_proposal, account=broke, tick=eurusd_tick)
    assert not verdict.approved
    assert any("floor" in r for r in verdict.reasons)


def test_daily_kill_switch_trips_and_persists(rules, eurusd_tick, buy_proposal):
    losing = AccountSnapshot(
        equity_usd=5000.0 * (1 - rules.account.daily_loss_kill_pct / 100) - 1,
        balance_usd=5000.0,
        margin_used_usd=0.0,
        free_margin_usd=4500.0,
        open_positions=[],
    )
    gate = _gate(rules, equity=5000.0)
    first = gate.evaluate(buy_proposal, account=losing, tick=eurusd_tick)
    assert not first.approved
    assert any("daily loss" in r for r in first.reasons)

    # Now equity recovers — but kill switch is sticky for the day.
    recovered = AccountSnapshot(
        equity_usd=5000.0,
        balance_usd=5000.0,
        margin_used_usd=0.0,
        free_margin_usd=5000.0,
        open_positions=[],
    )
    second = gate.evaluate(buy_proposal, account=recovered, tick=eurusd_tick)
    assert not second.approved
    assert any("kill-switch" in r for r in second.reasons)


def test_reset_daily_clears_kill_switch(rules, eurusd_tick, buy_proposal):
    gate = _gate(rules)
    gate.trip_kill_switch()
    bad = gate.evaluate(buy_proposal, account=_fresh_account(), tick=eurusd_tick)
    assert not bad.approved

    gate.reset_daily(equity_at_session_start=5000.0)
    good = gate.evaluate(buy_proposal, account=_fresh_account(), tick=eurusd_tick)
    assert good.approved, good.reasons


def _fresh_account() -> AccountSnapshot:
    return AccountSnapshot(
        equity_usd=5000.0,
        balance_usd=5000.0,
        margin_used_usd=0.0,
        free_margin_usd=5000.0,
        open_positions=[],
    )


def test_crypto_sleeve_disabled_blocks_btc(rules):
    assert not rules.is_symbol_allowed("BTCUSD")


def test_crypto_sleeve_validation_rejects_inconsistent_config():
    from bot.config import CryptoSleeve

    with pytest.raises(ValueError, match="capital_cap_pct"):
        CryptoSleeve(enabled=True, capital_cap_pct=0, allowed_symbols=["BTCUSD"])

    with pytest.raises(ValueError, match="allowed_symbols"):
        CryptoSleeve(enabled=True, capital_cap_pct=10, allowed_symbols=[])
