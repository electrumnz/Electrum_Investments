"""Tests for the MCP tool layer.

The point of these is narrow but important: the MCP surface must not become a
way around the risk gate. A rejected proposal must not reach the broker.
"""

from __future__ import annotations

from typing import Any

import pytest

from bot import mcp_server
from bot.broker import MockBroker
from bot.config import load_rules
from bot.risk import RiskGate

from .conftest import INSIDE_SESSION, PAPER_EQUITY


@pytest.fixture(autouse=True)
def wired_session(monkeypatch):
    """Point the module-level session at a MockBroker seeded with prices.

    The gate's clock is pinned inside the allowed session window so these tests
    assert on the MCP layer rather than on what time it happens to be.
    """
    broker = MockBroker(starting_equity=PAPER_EQUITY)
    broker.connect()
    broker.set_price("SPY", bid=579.98, ask=580.02)
    broker.set_price("QQQ", bid=499.98, ask=500.02)
    # Priced but deliberately absent from allowed_symbols, so rejection tests
    # reach the allowlist gate rather than failing earlier on a missing quote.
    broker.set_price("GME", bid=579.98, ask=580.02)

    rules = load_rules()
    session = mcp_server._Session()
    session._broker = broker
    session._rules = rules
    session._gate = RiskGate(
        rules, equity_at_session_start=PAPER_EQUITY, now=INSIDE_SESSION
    )

    monkeypatch.setattr(mcp_server, "_session", session)
    return session


def _good_args(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "symbol": "SPY",
        "direction": "buy",
        "qty": 10,
        "limit_price": 580.00,
        "stop_loss_price": 575.00,
        "take_profit_price": 590.00,
        "rationale": "Reclaimed the prior day high; invalidated below 575.",
    }
    args.update(overrides)
    return args


def test_check_order_approves_a_sound_proposal():
    result = mcp_server.check_order(**_good_args())
    assert result["approved"], result["reasons"]
    assert result["risk_usd"] == pytest.approx(50.0)
    assert result["notional_usd"] == pytest.approx(5800.0)


def test_check_order_rejects_and_explains():
    result = mcp_server.check_order(**_good_args(symbol="GME"))
    assert not result["approved"]
    assert any("allowed list" in r for r in result["reasons"])


def test_check_order_handles_malformed_input_without_crashing():
    result = mcp_server.check_order(**_good_args(qty=-5))
    assert not result["approved"]
    assert any("invalid proposal" in r for r in result["reasons"])


def test_check_order_does_not_place_anything(wired_session):
    mcp_server.check_order(**_good_args())
    assert wired_session.broker.get_account().open_positions == []


def test_place_order_places_an_approved_proposal(wired_session):
    result = mcp_server.place_order(**_good_args())
    assert result["placed"], result
    positions = wired_session.broker.get_account().open_positions
    assert len(positions) == 1
    assert positions[0].symbol == "SPY"


def test_place_order_refuses_a_rejected_proposal(wired_session):
    """The critical test: a rule violation must never reach the broker."""
    result = mcp_server.place_order(**_good_args(symbol="GME"))
    assert not result["placed"]
    assert result["reasons"]
    assert wired_session.broker.get_account().open_positions == []


def test_place_order_refuses_oversized_position(wired_session):
    result = mcp_server.place_order(**_good_args(qty=1000))
    assert not result["placed"]
    assert wired_session.broker.get_account().open_positions == []


def test_place_order_respects_the_kill_switch(wired_session):
    wired_session.gate.trip_kill_switch()
    result = mcp_server.place_order(**_good_args())
    assert not result["placed"]
    assert any("kill-switch" in r for r in result["reasons"])
    assert wired_session.broker.get_account().open_positions == []


def test_close_position_round_trip(wired_session):
    mcp_server.place_order(**_good_args())
    assert len(wired_session.broker.get_account().open_positions) == 1

    result = mcp_server.close_position("SPY")
    assert result["closed"]
    assert wired_session.broker.get_account().open_positions == []


def test_get_risk_status_reports_limits_and_usage():
    status = mcp_server.get_risk_status()
    assert status["equity_usd"] == pytest.approx(PAPER_EQUITY)
    assert status["kill_switch_tripped"] is False
    assert status["limits"]["max_concurrent_positions"] > 0
    assert "trades_today" in status["frequency"]
    # A $100k paper account sits above the $25k PDT threshold.
    assert status["pattern_day_trader"]["currently_binding"] is False


def test_get_rules_exposes_the_active_config():
    rules = mcp_server.get_rules()
    assert rules["crypto_sleeve"]["enabled"] is False
    assert rules["pdt"]["enforce"] is True


def test_reset_session_clears_kill_switch(wired_session):
    wired_session.gate.trip_kill_switch()
    assert mcp_server.place_order(**_good_args())["placed"] is False

    result = mcp_server.reset_trading_session()
    assert result["reset"] is True
    assert result["kill_switch_tripped"] is False
    assert mcp_server.place_order(**_good_args())["placed"] is True
