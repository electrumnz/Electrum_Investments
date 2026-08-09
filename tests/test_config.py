from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bot.config import (
    CLAUDE_MODEL_IDS,
    CLAUDE_PRICING_USD_PER_MTOK,
    AccountRules,
    ClaudeTier,
    Env,
    LiveTradingRefused,
    Rules,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_rules_load_from_yaml():
    rules = Rules.load(REPO_ROOT / "config" / "rules.yaml")
    assert rules.account.max_concurrent_positions > 0
    assert rules.allowed_symbols, "allowed_symbols must not be empty"
    assert rules.instruments["crypto"].enabled is False, "crypto must default OFF"
    assert rules.frequency.max_trades_per_day > 0
    assert rules.margin.max_gross_notional_pct > 0, "margin guards must ship configured"


def test_claude_model_ids_and_pricing_complete():
    for tier in ClaudeTier:
        assert tier in CLAUDE_MODEL_IDS
        assert CLAUDE_MODEL_IDS[tier].startswith("claude-")
        assert tier in CLAUDE_PRICING_USD_PER_MTOK
        base_in, out, cache_read = CLAUDE_PRICING_USD_PER_MTOK[tier]
        assert 0 < base_in < out
        # Cache reads bill at 10% of base input.
        assert cache_read == pytest.approx(base_in * 0.1)


def _account_rules(**overrides: Any) -> AccountRules:
    base: dict[str, Any] = {
        "min_equity_floor_usd": 90_000,
        "max_risk_per_trade_pct": 1.0,
        "max_position_pct": 50.0,
        "max_total_risk_pct": 2.0,
        "max_concurrent_positions": 1,
        "daily_loss_kill_pct": 1,
    }
    base.update(overrides)
    return AccountRules(**base)


def test_invalid_max_risk_pct_rejected():
    with pytest.raises(ValueError):
        _account_rules(max_risk_per_trade_pct=0)


def test_total_risk_below_per_trade_risk_rejected():
    """A total cap under the per-trade cap would block every possible trade."""
    with pytest.raises(ValueError, match="max_risk_per_trade_pct"):
        _account_rules(max_risk_per_trade_pct=2.0, max_total_risk_pct=1.0)


def test_stand_down_stage_two_must_exceed_stage_one():
    from bot.config import StandDownRules

    with pytest.raises(ValueError, match="stage_one_days"):
        StandDownRules(stage_one_days=10, stage_two_days=3)


def test_execution_mode_follows_paper_flag():
    from bot.models import ExecutionMode

    assert _env().execution_mode == ExecutionMode.PAPER
    assert _env(ALPACA_PAPER_TRADE=False).execution_mode == ExecutionMode.LIVE


# --------------------------------------------------------- paper-only guard


def _env(**overrides: Any) -> Env:
    """Build an Env ignoring any developer .env file, so these tests are hermetic."""
    return Env(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_paper_mode_is_the_default():
    env = _env()
    assert env.alpaca_paper_trade is True
    env.assert_paper_only()  # must not raise


def test_live_mode_is_refused():
    with pytest.raises(LiveTradingRefused, match="paper-only"):
        _env(ALPACA_PAPER_TRADE=False).assert_paper_only()


def test_base_url_follows_paper_flag():
    assert "paper-api" in _env().alpaca_base_url
    assert "paper-api" not in _env(ALPACA_PAPER_TRADE=False).alpaca_base_url


def test_alpaca_broker_refuses_live_env():
    """Second line of defence: the broker re-checks rather than trusting startup."""
    from bot.broker import AlpacaBroker

    env = _env(ALPACA_PAPER_TRADE=False, ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s")
    with pytest.raises(LiveTradingRefused):
        AlpacaBroker(env)


def test_allowed_symbols_is_derived_from_enabled_classes():
    """Disabled classes contribute nothing, so their symbols are not tradeable."""
    rules = Rules.load(REPO_ROOT / "config" / "rules.yaml")
    assert "SPY" in rules.allowed_symbols
    assert rules.is_symbol_allowed("SPY")
    assert not rules.is_symbol_allowed("BTC/USD")   # crypto class is disabled

    enabled = rules.model_copy(deep=True)
    enabled.instruments["crypto"].enabled = True
    enabled.instruments["crypto"].allowed_symbols = ["BTC/USD"]
    assert enabled.is_symbol_allowed("BTC/USD")
    assert enabled.class_name_for("BTC/USD") == "crypto"
    assert enabled.strategy_for("BTC/USD") == "momentum"


def test_enabled_instrument_must_have_symbols_and_sessions():
    """An enabled class with no session window could never trade."""
    from bot.config import InstrumentRules

    with pytest.raises(ValueError, match="sessions_utc"):
        InstrumentRules(enabled=True, allowed_symbols=["SPY"], sessions_utc=[])

    with pytest.raises(ValueError, match="allowed_symbols"):
        InstrumentRules(enabled=True, allowed_symbols=[], sessions_utc=[(14, 21)])


def test_enabled_instrument_must_declare_its_trading_days():
    """Required rather than defaulted, because both defaults are wrong somewhere.

    Monday-to-Friday would silently shut crypto at weekends; all seven would
    leave equities tradeable on a Saturday, where Alpaca queues the order to
    Monday's open rather than refusing it. Failing at startup beats either.
    """
    from bot.config import InstrumentRules

    with pytest.raises(ValueError, match="session_days_utc"):
        InstrumentRules(
            enabled=True,
            allowed_symbols=["SPY"],
            sessions_utc=[(14, 21)],
            session_days_utc=[],
        )

    with pytest.raises(ValueError, match="0-6"):
        InstrumentRules(
            enabled=True,
            allowed_symbols=["SPY"],
            sessions_utc=[(14, 21)],
            session_days_utc=[1, 7],
        )


def test_the_shipped_rules_close_the_weekend_for_equities():
    """Guards the config, not just the gate. The rule is only as good as the file."""
    from bot.config import Rules

    from .conftest import RULES_PATH

    equities = Rules.load(RULES_PATH).instruments["us_equity"]

    assert equities.session_days_utc == [0, 1, 2, 3, 4]
    assert 5 not in equities.session_days_utc  # Saturday
    assert 6 not in equities.session_days_utc  # Sunday
