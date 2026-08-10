"""Tests for the MCP tool layer.

The point of these is narrow but important: the MCP surface must not become a
way around the risk gate. A rejected proposal must not reach the broker.
"""

from __future__ import annotations

from typing import Any

import pytest

from bot import mcp_server
from bot.audit import AuditLog
from bot.broker import MockBroker
from bot.config import load_rules
from bot.journal import Journal
from bot.risk import RiskGate

from .conftest import INSIDE_SESSION, PAPER_EQUITY


@pytest.fixture(autouse=True)
def wired_session(monkeypatch, tmp_path):
    """Point the module-level session at a MockBroker seeded with prices.

    The gate's clock is pinned inside the allowed session window so these tests
    assert on the MCP layer rather than on what time it happens to be, and the
    journal goes to a temp file so the suite never touches a real one.

    The audit log goes to a temp directory for the same reason. `_Session`
    builds a default `AuditLog()` rooted at `audit/`, which the suite must
    neither read nor create.
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
    session._journal = Journal(tmp_path / "journal.db")
    session._audit = AuditLog(tmp_path / "audit")
    session._gate = RiskGate(
        rules, equity_at_session_start=PAPER_EQUITY, now=INSIDE_SESSION
    )

    monkeypatch.setattr(mcp_server, "_session", session)
    return session


def _good_args(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "symbol": "SPY",
        "direction": "buy",
        "qty": 3,
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
    assert result["risk_usd"] == pytest.approx(15.0)
    assert result["notional_usd"] == pytest.approx(1740.0)


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
    assert status["limits"]["max_total_risk_pct"] > 0
    assert "max_gross_notional_pct" in status["margin"]


def test_get_rules_exposes_the_active_config():
    rules = mcp_server.get_rules()
    assert rules["instruments"]["crypto"]["enabled"] is False
    assert rules["stand_down"]["consecutive_losses_trigger"] > 0


def test_reset_session_clears_kill_switch(wired_session):
    wired_session.gate.trip_kill_switch()
    assert mcp_server.place_order(**_good_args())["placed"] is False

    result = mcp_server.reset_trading_session()
    assert result["reset"] is True
    assert result["kill_switch_tripped"] is False
    assert mcp_server.place_order(**_good_args())["placed"] is True


def test_risk_status_reports_complete_when_journal_matches():
    assert mcp_server.get_risk_status()["open_risk_is_complete"] is True


def test_risk_status_flags_untracked_positions(wired_session):
    """A position the journal never saw makes reported open risk understated."""
    wired_session.broker.place_order(
        mcp_server._build_proposal(
            "SPY", "buy", 3, 580.0, 575.0, 590.0, "Opened outside the journal."
        )
    )
    status = mcp_server.get_risk_status()
    assert status["open_risk_is_complete"] is False
    assert status["untracked_positions"] == ["SPY"]
    assert "higher than reported" in status["open_risk_warning"]


# ------------------------------------------------------------ journal tools


def test_journal_stats_on_empty_journal_is_safe():
    stats = mcp_server.get_journal_stats()
    assert stats["summary"]["trade_count"] == 0
    assert stats["summary"]["profit_factor"] is None
    assert stats["readout"][0] == "No closed trades yet."


def test_journal_stats_reports_a_closed_trade(wired_session):
    from datetime import timedelta

    from bot.models import Direction, Trade

    entry = INSIDE_SESSION
    tid = wired_session.journal.record_entry(
        Trade(
            symbol="SPY",
            direction=Direction.BUY,
            qty=10,
            entry_time=entry,
            entry_price=580.0,
            planned_stop=570.0,  # $100 planned risk
            planned_target=600.0,
            rationale="Journalled trade for stats.",
        )
    )
    wired_session.journal.update_excursion(tid, -40.0)
    wired_session.journal.update_excursion(tid, 300.0)
    wired_session.journal.record_exit(
        tid,
        exit_time=entry + timedelta(hours=1),
        exit_price=595.0,
        realised_pnl_usd=150.0,
    )

    stats = mcp_server.get_journal_stats()
    assert stats["summary"]["trade_count"] == 1
    assert stats["summary"]["total_pnl_usd"] == pytest.approx(150.0)
    assert stats["summary"]["sample_is_thin"] is True
    # 150 realised against a 300 best-case excursion.
    assert stats["stops_and_targets"]["capture_ratio"] == pytest.approx(0.5)
    assert any("sampled once per decision cycle" in line for line in stats["readout"])


def test_get_trades_returns_rationale(wired_session):
    from datetime import timedelta

    from bot.models import Direction, Trade

    tid = wired_session.journal.record_entry(
        Trade(
            symbol="SPY",
            direction=Direction.BUY,
            qty=10,
            entry_time=INSIDE_SESSION,
            entry_price=580.0,
            planned_stop=570.0,
            planned_target=600.0,
            rationale="Reclaimed the prior day high.",
        )
    )
    wired_session.journal.record_exit(
        tid,
        exit_time=INSIDE_SESSION + timedelta(hours=1),
        exit_price=590.0,
        realised_pnl_usd=100.0,
    )
    trades = mcp_server.get_trades()
    assert len(trades) == 1
    assert trades[0]["rationale"] == "Reclaimed the prior day high."
    assert trades[0]["r_multiple"] == pytest.approx(1.0)


def test_stand_down_status_when_clear():
    status = mcp_server.get_stand_down_status()
    assert status["active"] is False
    assert status["stage"] == 0
    assert status["trigger_at"] > 0


# --------------------------------------------------------------- news recall


def _record_cycle(session, *, minutes_ago: int, **inputs):
    """Write one cycle into the temp audit log, `minutes_ago` before now."""
    from datetime import UTC, datetime, timedelta

    from bot.models import Decision, MarketInputs

    session.audit.record(
        Decision(
            timestamp=datetime.now(UTC) - timedelta(minutes=minutes_ago),
            inputs=MarketInputs(**inputs),
        )
    )


def test_get_recent_news_returns_what_the_loop_was_shown(wired_session):
    _record_cycle(
        wired_session,
        minutes_ago=10,
        headlines=["[SPY] Index closes at a record (2026-08-10)"],
        social_posts=["[@someone 14:20] a post"],
        news_windows=["2026-08-11T20:00 affects AAPL"],
    )

    news = mcp_server.get_recent_news()

    assert news["cycles_read"] == 1
    assert news["loop_recorded_nothing_in_window"] is False
    assert news["headlines"][0]["text"].startswith("[SPY]")
    assert news["social_posts"][0]["text"].startswith("[@someone")
    assert news["news_windows"][0]["text"] == "2026-08-11T20:00 affects AAPL"


def test_get_recent_news_attaches_an_age_to_every_item(wired_session):
    """A six-hour-old headline presented as current is the failure to avoid."""
    _record_cycle(wired_session, minutes_ago=90, headlines=["older story"])

    news = mcp_server.get_recent_news()

    item = news["headlines"][0]
    assert item["age_minutes"] == pytest.approx(90, abs=1)
    assert news["latest_cycle_age_minutes"] == pytest.approx(90, abs=1)
    assert news["reading_is_stale"] is True


def test_get_recent_news_says_a_silent_loop_is_not_a_quiet_market(wired_session):
    news = mcp_server.get_recent_news()

    assert news["cycles_read"] == 0
    assert news["loop_recorded_nothing_in_window"] is True
    assert news["headlines"] == []
    assert any("does NOT mean there was no news" in line for line in news["readout"])


def test_get_recent_news_declares_itself_a_recording(wired_session):
    """Nothing here was fetched: the Marketaux quota belongs to the loop."""
    _record_cycle(wired_session, minutes_ago=5, headlines=["something"])

    news = mcp_server.get_recent_news()

    assert "not a live news search" in news["source"].lower()
    assert any("not a live news search" in line for line in news["readout"])


def test_get_recent_news_carries_the_degraded_flags(wired_session):
    _record_cycle(
        wired_session, minutes_ago=5, social_degraded=True, calendar_degraded=True
    )

    news = mcp_server.get_recent_news()

    assert news["feeds_degraded"]["social"] is True
    assert news["feeds_degraded"]["calendar"] is True
    assert any("incomplete" in line for line in news["readout"])


def test_get_recent_news_respects_the_window(wired_session):
    _record_cycle(wired_session, minutes_ago=60 * 40, headlines=["last week"])
    _record_cycle(wired_session, minutes_ago=30, headlines=["today"])

    news = mcp_server.get_recent_news(hours=24)

    assert [h["text"] for h in news["headlines"]] == ["today"]


def test_get_recent_decisions_reads_across_days(wired_session):
    """Today's file does not exist until the loop writes; months may sit behind it."""
    from datetime import UTC, datetime, timedelta

    from bot.models import Decision, Direction, OrderProposal, RiskVerdict

    # Written straight to a dated file from three days ago, which is what the
    # old today-only implementation could never see.
    stamp = datetime.now(UTC) - timedelta(days=3)
    decision = Decision(
        timestamp=stamp,
        proposals=[
            OrderProposal(
                symbol="SPY",
                direction=Direction.BUY,
                qty=87,
                limit_price=580.0,
                stop_loss_price=567.0,
                take_profit_price=600.0,
                rationale="Reclaimed the prior day high; invalidated below 567.",
            )
        ],
        verdicts=[RiskVerdict.reject("risk 1,131.00 exceeds the per-trade cap 1,000.00")],
    )
    path = wired_session.audit._base / f"{stamp.date().isoformat()}.jsonl"
    path.write_text(decision.model_dump_json() + "\n", encoding="utf-8")

    result = mcp_server.get_recent_decisions()

    assert len(result["decisions"]) == 1
    entry = result["decisions"][0]
    assert entry["outcome"] == "rejected"
    assert entry["rejected"] == 1
    assert "exceeds the per-trade cap" in entry["decision"]["verdicts"][0]["reasons"][0]
    assert result["record_is_incomplete"] is False


def test_get_recent_decisions_counts_what_it_could_not_parse(wired_session):
    from datetime import UTC, datetime

    from bot.models import Decision

    wired_session.audit.record(Decision(timestamp=datetime.now(UTC)))
    path = next(iter(wired_session.audit._base.glob("*.jsonl")))
    with path.open("a", encoding="utf-8") as f:
        f.write("{ torn write\n")

    result = mcp_server.get_recent_decisions()

    assert result["malformed_lines"] == 1
    assert result["record_is_incomplete"] is True
    assert len(result["decisions"]) == 1
