from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from bot.config import (
    CLAUDE_MODEL_IDS,
    CLAUDE_PRICING_USD_PER_MTOK,
    AccountRules,
    ClaudeTier,
    Env,
    InstrumentRules,
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


def test_the_open_window_is_derived_from_the_instruments_not_copied():
    """The loop's skip and the gate's rejection must never disagree.

    Both read the same `is_in_session`, so enabling a 24/7 class widens the
    loop's working hours without a second setting being touched. A separate
    copy of the equity hours would drift, and the symptom would be the loop
    skipping a session the gate would have allowed, with nothing to say why.
    """
    from datetime import UTC, datetime

    from bot.config import Rules

    from .conftest import RULES_PATH

    rules = Rules.load(RULES_PATH)
    monday_open = datetime(2026, 5, 4, 15, 0, tzinfo=UTC)
    saturday = datetime(2026, 5, 9, 15, 0, tzinfo=UTC)
    monday_night = datetime(2026, 5, 4, 3, 0, tzinfo=UTC)

    assert rules.classes_in_session(monday_open) == ["us_equity"]
    assert rules.any_class_in_session(monday_open)
    assert not rules.any_class_in_session(saturday)
    assert not rules.any_class_in_session(monday_night)

    # Enabling the 24/7 class makes the account a seven-day operation, and the
    # skip becomes a no-op, without any second setting being edited.
    rules.instruments["crypto"].enabled = True
    rules.instruments["crypto"].allowed_symbols = ["BTC/USD"]

    assert rules.any_class_in_session(saturday)
    assert rules.classes_in_session(saturday) == ["crypto"]


# ------------------------------------------------- per-day session windows


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# CME Globex under US daylight time: Sunday evening open, a daily maintenance
# break, an early Friday close, and Saturday dark. This is the schedule the flat
# form cannot express, and the reason the mapping form exists.
GLOBEX = {
    0: [(0, 21), (22, 24)],
    1: [(0, 21), (22, 24)],
    2: [(0, 21), (22, 24)],
    3: [(0, 21), (22, 24)],
    4: [(0, 21)],
    6: [(22, 24)],
}


def _globex() -> InstrumentRules:
    return InstrumentRules(enabled=True, allowed_symbols=["CL"], sessions_utc=GLOBEX)


@pytest.mark.parametrize(
    ("moment", "open_", "why"),
    [
        (_utc(2026, 5, 10, 21), False, "Sunday afternoon, before the evening open"),
        (_utc(2026, 5, 10, 22, 30), True, "Sunday evening, after the open"),
        (_utc(2026, 5, 4, 10), True, "Monday mid-session"),
        (_utc(2026, 5, 4, 21, 30), False, "inside the daily maintenance break"),
        (_utc(2026, 5, 4, 22, 30), True, "after the break reopens"),
        (_utc(2026, 5, 8, 20), True, "Friday, before the 16:00 CT close"),
        (_utc(2026, 5, 8, 22, 30), False, "Friday night, no reopen"),
        (_utc(2026, 5, 9, 12), False, "Saturday, dark all day"),
    ],
)
def test_a_globex_schedule_is_expressible_and_correct(moment, open_, why):
    """The whole point of the mapping form.

    Sunday is the case that proves it: under the flat form Sunday would inherit
    Monday's hours, and 12:00 on a Sunday would read as open. The gate would
    approve, and the broker would queue the fill into the next session.
    """
    assert _globex().is_in_session(moment) is open_, why


def test_the_trading_days_are_derived_from_the_mapping_keys():
    """Two places naming the trading days is two places to disagree."""
    assert _globex().session_days_utc == [0, 1, 2, 3, 4, 6]


def test_a_day_list_that_contradicts_the_mapping_is_refused():
    """A trading day with no window trades nothing; a window on a shut day never fires.

    Either reads as a bug in the bot rather than a typo in the config.
    """
    from bot.config import InstrumentRules

    with pytest.raises(ValueError, match="does not match"):
        InstrumentRules(
            enabled=True,
            allowed_symbols=["CL"],
            sessions_utc=GLOBEX,
            session_days_utc=[0, 1, 2, 3, 4, 5, 6],  # claims Saturday
        )


def test_a_weekday_listed_with_no_windows_is_refused():
    from bot.config import InstrumentRules

    with pytest.raises(ValueError, match="no windows"):
        InstrumentRules(
            enabled=True,
            allowed_symbols=["CL"],
            sessions_utc={0: [(0, 24)], 5: []},
        )


@pytest.mark.parametrize("window", [(21, 21), (22, 21), (0, 25), (-1, 4)])
def test_an_impossible_window_is_refused(window):
    from bot.config import InstrumentRules

    with pytest.raises(ValueError):
        InstrumentRules(
            enabled=True,
            allowed_symbols=["CL"],
            sessions_utc={0: [window]},
        )


def test_the_flat_form_still_works_and_is_what_the_shipped_config_uses():
    """The mapping must not have made the simple case harder to write."""
    from bot.config import Rules

    from .conftest import RULES_PATH

    equities = Rules.load(RULES_PATH).instruments["us_equity"]

    # 08:00 UTC is 04:00 New York in summer — the operator widened this to
    # cover pre-market and after-hours. The flat form is what is under test
    # here; the hours themselves are a config decision.
    assert equities.windows_by_day[0] == [(8, 24)]
    assert set(equities.windows_by_day) == {0, 1, 2, 3, 4}
    assert equities.render_sessions() == "08:00-24:00"


def test_a_per_day_schedule_renders_per_day():
    """Settings and the system prompt both show this, so it must read sensibly."""
    rendered = _globex().render_sessions()

    assert "Sun 22:00-24:00" in rendered
    assert "Fri 00:00-21:00" in rendered
    assert "Sat" not in rendered


def test_the_skip_defaults_to_on_and_is_not_a_risk_rule():
    """It can stop the model being asked. It can never widen what it may answer."""
    from bot.config import Rules

    from .conftest import RULES_PATH

    rules = Rules.load(RULES_PATH)

    assert rules.loop.skip_model_call_when_all_markets_closed is True
    assert not hasattr(rules.loop, "max_risk_per_trade_pct")


def test_the_shipped_rules_close_the_weekend_for_equities():
    """Guards the config, not just the gate. The rule is only as good as the file."""
    from bot.config import Rules

    from .conftest import RULES_PATH

    equities = Rules.load(RULES_PATH).instruments["us_equity"]

    assert equities.session_days_utc == [0, 1, 2, 3, 4]
    assert 5 not in equities.session_days_utc  # Saturday
    assert 6 not in equities.session_days_utc  # Sunday
