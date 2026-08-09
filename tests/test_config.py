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
    assert rules.crypto_sleeve.enabled is False, "crypto must default OFF for staged rollout"
    assert rules.frequency.max_trades_per_day > 0
    assert rules.pdt.enforce is True, "PDT guard must ship enabled"


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
        "max_position_pct": 2.0,
        "max_total_invested_pct": 2.0,
        "min_cash_reserve_pct": 20,
        "max_concurrent_positions": 1,
        "daily_loss_kill_pct": 1,
    }
    base.update(overrides)
    return AccountRules(**base)


def test_invalid_max_risk_pct_rejected():
    with pytest.raises(ValueError):
        _account_rules(max_risk_per_trade_pct=0)


def test_position_cap_above_total_cap_rejected():
    """A per-position cap larger than the total cap could never bind."""
    with pytest.raises(ValueError, match="max_total_invested_pct"):
        _account_rules(max_position_pct=10.0, max_total_invested_pct=2.0)


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


def test_is_crypto_uses_sleeve_symbols():
    rules = Rules.load(REPO_ROOT / "config" / "rules.yaml")
    enabled = rules.model_copy(deep=True)
    enabled.crypto_sleeve.allowed_symbols = ["BTC/USD"]
    assert enabled.is_crypto("BTC/USD")
    assert not enabled.is_crypto("SPY")
