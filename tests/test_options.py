"""Tests for option expiry tracking.

This is protective code, so the tests are about the scenarios that cost money:
an expiry arriving unnoticed, an in-the-money contract the account cannot fund,
and the final hour in which Alpaca decides for you.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from bot.config import Rules
from bot.models import AccountSnapshot, Direction, OrderProposal
from bot.options import (
    CONTRACT_MULTIPLIER,
    ExpiryUrgency,
    OptionRight,
    alerts_for_positions,
    assess_expiry,
    is_option_symbol,
    parse_occ_symbol,
    render_alerts,
)
from bot.risk import RiskGate

from .conftest import PAPER_EQUITY

# SPY $580 call expiring 18 Sep 2026.
SPY_CALL = "SPY260918C00580000"
SPY_PUT = "SPY260918P00500000"


# ------------------------------------------------------------------ parsing


def test_parses_a_call():
    c = parse_occ_symbol(SPY_CALL)
    assert c is not None
    assert c.underlying == "SPY"
    assert c.expiry == date(2026, 9, 18)
    assert c.right == OptionRight.CALL
    assert c.strike == pytest.approx(580.0)


def test_parses_a_put():
    c = parse_occ_symbol(SPY_PUT)
    assert c is not None
    assert c.right == OptionRight.PUT
    assert c.strike == pytest.approx(500.0)


def test_parses_fractional_strike():
    c = parse_occ_symbol("AAPL260320C00227500")
    assert c is not None
    assert c.strike == pytest.approx(227.5)


@pytest.mark.parametrize("symbol", ["SPY", "AAPL", "BTC/USD", "", "SPY2609", "NOTANOPTION"])
def test_rejects_non_option_symbols(symbol):
    assert parse_occ_symbol(symbol) is None
    assert not is_option_symbol(symbol)


def test_rejects_impossible_date():
    assert parse_occ_symbol("SPY261332C00580000") is None


def test_expiry_close_is_eastern_not_utc():
    """Expiry is defined in ET; an hour's DST error would delay the warning."""
    c = parse_occ_symbol(SPY_CALL)
    assert c is not None
    # 16:00 ET in September is EDT (UTC-4), so 20:00 UTC.
    assert c.expiry_close_utc == datetime(2026, 9, 18, 20, 0, tzinfo=UTC)


# ------------------------------------------------------------ moneyness


def test_call_is_itm_above_strike():
    c = parse_occ_symbol(SPY_CALL)
    assert c is not None
    assert c.is_itm(580.50)
    assert not c.is_itm(579.00)


def test_one_cent_itm_counts():
    """Alpaca's auto-exercise threshold is $0.01, not a round number."""
    c = parse_occ_symbol(SPY_CALL)
    assert c is not None
    assert c.is_itm(580.01)
    assert not c.is_itm(580.00)


def test_put_is_itm_below_strike():
    c = parse_occ_symbol(SPY_PUT)
    assert c is not None
    assert c.is_itm(499.00)
    assert not c.is_itm(501.00)


def test_exercise_cost_uses_the_contract_multiplier():
    c = parse_occ_symbol(SPY_CALL)
    assert c is not None
    # Two contracts of a $580 call = 200 shares = $116,000 to take delivery.
    assert c.exercise_cost_usd(2) == pytest.approx(580 * CONTRACT_MULTIPLIER * 2)


def test_puts_have_no_exercise_cost():
    c = parse_occ_symbol(SPY_PUT)
    assert c is not None
    assert c.exercise_cost_usd(5) == 0.0


# ------------------------------------------------------------------ urgency


def _assess(days_before: float, **kwargs):
    c = parse_occ_symbol(SPY_CALL)
    assert c is not None
    now = c.expiry_close_utc - timedelta(days=days_before)
    return assess_expiry(c, qty=1, now=now, warn_days=7.0, **kwargs)


def test_far_from_expiry_is_quiet():
    alert = _assess(30)
    assert alert.urgency == ExpiryUrgency.NONE
    assert not alert.needs_action


def test_inside_warning_window_is_watch():
    alert = _assess(5)
    assert alert.urgency == ExpiryUrgency.WATCH
    assert not alert.needs_action


def test_final_day_is_urgent():
    alert = _assess(0.5)
    assert alert.urgency == ExpiryUrgency.URGENT
    assert alert.needs_action


def test_final_hour_is_critical():
    """The window in which Alpaca liquidates un-fundable ITM positions."""
    alert = _assess(0.02)  # ~29 minutes
    assert alert.urgency == ExpiryUrgency.CRITICAL
    assert alert.needs_action


# -------------------------------------------------- the expensive scenarios


def test_itm_and_unfundable_warns_about_liquidation():
    """The exact scenario to avoid: broker sells you out in the final hour."""
    alert = _assess(0.5, underlying_price=590.0, buying_power_usd=1_000.0)
    assert alert.in_the_money is True
    assert alert.can_fund_exercise is False
    assert alert.exercise_cost_usd == pytest.approx(58_000.0)
    assert "LIQUIDATE" in alert.message
    assert alert.needs_action


def test_itm_and_fundable_warns_about_auto_exercise():
    alert = _assess(0.5, underlying_price=590.0, buying_power_usd=100_000.0)
    assert alert.in_the_money is True
    assert alert.can_fund_exercise is True
    assert "AUTO-EXERCISED" in alert.message
    assert "Do Not Exercise cannot be filed" in alert.message


def test_otm_near_expiry_still_warns():
    """Out of the money now is not out of the money at the close."""
    alert = _assess(0.5, underlying_price=560.0, buying_power_usd=100_000.0)
    assert alert.in_the_money is False
    assert alert.needs_action


def test_missing_price_still_warns_on_time():
    """Degraded detail must never mean a delayed warning."""
    alert = _assess(0.5)
    assert alert.in_the_money is None
    assert alert.urgency == ExpiryUrgency.URGENT
    assert alert.needs_action


# ------------------------------------------------------------ position sweep


def test_alerts_skip_non_option_positions():
    now = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    alerts = alerts_for_positions(
        [("SPY", 100), ("BTC/USD", 0.5), (SPY_CALL, 1)], now=now, warn_days=7.0
    )
    assert len(alerts) == 1
    assert alerts[0].symbol == SPY_CALL


def test_alerts_are_sorted_most_urgent_first():
    now = datetime(2026, 9, 18, 15, 0, tzinfo=UTC)  # expiry day for the September call
    alerts = alerts_for_positions(
        [("SPY261218C00580000", 1), (SPY_CALL, 1)],  # December, then September
        now=now,
        warn_days=7.0,
    )
    assert alerts[0].symbol == SPY_CALL  # nearer expiry leads
    assert alerts[0].urgency == ExpiryUrgency.URGENT
    assert alerts[1].urgency == ExpiryUrgency.NONE  # December is months away


def test_render_marks_actionable_alerts():
    now = datetime(2026, 9, 18, 15, 0, tzinfo=UTC)
    lines = render_alerts(alerts_for_positions([(SPY_CALL, 1)], now=now, warn_days=7.0))
    assert lines[0].startswith("- !!")


def test_render_handles_no_options():
    assert render_alerts([]) == ["- (no option positions)"]


# ------------------------------------------------------------- risk gate


def _option_proposal(symbol: str) -> OrderProposal:
    return OrderProposal(
        symbol=symbol,
        direction=Direction.BUY,
        qty=1,
        limit_price=5.00,
        stop_loss_price=2.50,
        take_profit_price=10.00,
        rationale="Option entry used to exercise the expiry gate.",
    )


def _account() -> AccountSnapshot:
    return AccountSnapshot(
        equity_usd=PAPER_EQUITY,
        cash_usd=PAPER_EQUITY,
        buying_power_usd=PAPER_EQUITY,
        open_positions=[],
    )


def _gate_at(rules: Rules, now: datetime) -> RiskGate:
    return RiskGate(rules, equity_at_session_start=PAPER_EQUITY, now=now)


def test_gate_refuses_option_entry_near_expiry(rules, spy_tick):
    contract = parse_occ_symbol(SPY_CALL)
    assert contract is not None
    one_day_out = contract.expiry_close_utc - timedelta(days=1)

    verdict = _gate_at(rules, one_day_out).evaluate(
        _option_proposal(SPY_CALL), account=_account(), tick=spy_tick
    )
    assert not verdict.approved
    assert any("expires in" in r for r in verdict.reasons)


def test_gate_allows_option_entry_well_before_expiry(rules, spy_tick):
    """Far from expiry the expiry gate is silent — other gates may still object."""
    contract = parse_occ_symbol(SPY_CALL)
    assert contract is not None
    far_out = contract.expiry_close_utc - timedelta(days=45)

    verdict = _gate_at(rules, far_out).evaluate(
        _option_proposal(SPY_CALL), account=_account(), tick=spy_tick
    )
    assert not any("expires in" in r for r in verdict.reasons), verdict.reasons


def test_expiry_gate_ignores_ordinary_equities(rules, account, spy_tick, buy_proposal):
    verdict = _gate_at(rules, datetime(2026, 5, 4, 15, 0, tzinfo=UTC)).evaluate(
        buy_proposal, account=account, tick=spy_tick
    )
    assert verdict.approved, verdict.reasons
