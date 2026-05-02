from __future__ import annotations

from pathlib import Path

import pytest

from bot.config import CLAUDE_MODEL_IDS, BotMode, ClaudeTier, Rules


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_rules_load_from_yaml():
    rules = Rules.load(REPO_ROOT / "config" / "rules.yaml")
    assert rules.account.max_concurrent_positions > 0
    assert rules.allowed_symbols, "allowed_symbols must not be empty"
    assert rules.crypto_sleeve.enabled is False, "crypto must default OFF for staged rollout"


def test_claude_model_ids_complete():
    for tier in ClaudeTier:
        assert tier in CLAUDE_MODEL_IDS
        assert CLAUDE_MODEL_IDS[tier].startswith("claude-")


def test_bot_mode_enum_values():
    assert BotMode.DEMO.value == "demo"
    assert BotMode.LIVE.value == "live"


def test_invalid_max_risk_pct_rejected():
    from bot.config import AccountRules

    with pytest.raises(ValueError):
        AccountRules(
            min_equity_floor_usd=4500,
            max_risk_per_trade_pct=0,
            max_concurrent_positions=1,
            max_gross_leverage=1,
            daily_loss_kill_pct=1,
        )
