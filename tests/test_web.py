"""Tests for the dashboard.

Two things matter here. It must render without a broker, a network or a real
journal, and it must be read-only: nothing on this page may place, close or
alter anything. The rest is presentation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.config import Env, load_rules
from bot.journal import Journal
from bot.models import Direction, StandDownState, Trade
from bot.web.app import build_app

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

ENTRY = datetime(2026, 5, 4, 15, 0, tzinfo=UTC)


def _env() -> Env:
    return Env(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "journal.db")


@pytest.fixture
def client(journal):
    app = build_app(journal=journal, rules=load_rules(), env=_env(), force_mock=True)
    return TestClient(app)


def _closed_trade(journal: Journal, pnl: float, *, minutes: int = 0,
                  mae: float = 0.0, mfe: float = 0.0) -> int:
    tid = journal.record_entry(
        Trade(
            symbol="SPY",
            strategy="mean_reversion",
            direction=Direction.BUY,
            qty=10,
            entry_time=ENTRY + timedelta(minutes=minutes),
            entry_price=580.0,
            planned_stop=570.0,
            planned_target=600.0,
            rationale="Reclaimed the prior day high; invalidated below 570.",
        )
    )
    if mae or mfe:
        journal.update_excursion(tid, mae)
        journal.update_excursion(tid, mfe)
    journal.record_exit(
        tid,
        exit_time=ENTRY + timedelta(minutes=minutes + 60),
        exit_price=590.0,
        realised_pnl_usd=pnl,
    )
    return tid


# ------------------------------------------------------------------- renders


def test_dashboard_renders_on_an_empty_journal(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "MUDHORN" in r.text
    assert "No closed trades yet." in r.text


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_dashboard_shows_metrics_once_trades_exist(client, journal):
    _closed_trade(journal, 200.0, minutes=0, mae=-40.0, mfe=300.0)
    _closed_trade(journal, -100.0, minutes=120)

    body = client.get("/").text
    assert "Profit factor" in body
    assert "Expectancy" in body
    assert "mean_reversion" in body
    # The rationale is the point of a journal, so it has to reach the page.
    assert "invalidated below 570" in body


def test_excursion_caveat_is_always_shown(client, journal):
    """The sampling limitation must travel with the number, not sit in a docstring."""
    _closed_trade(journal, 200.0, mae=-40.0, mfe=300.0)
    assert "sampled once per decision cycle" in client.get("/").text


def test_thin_sample_is_flagged_on_the_page(client, journal):
    _closed_trade(journal, 200.0)
    assert "thin sample" in client.get("/").text


# ------------------------------------------------------------------ banners


def test_clear_banner_when_nothing_needs_attention(client):
    assert "No stand-down, no expiries" in client.get("/").text


def test_stand_down_is_surfaced(client, journal):
    now = datetime.now(UTC)
    journal.save_stand_down(
        StandDownState(
            stage=2,
            started_at=now,
            ends_at=now + timedelta(days=6),
            consecutive_losses=3,
            last_triggered_at=now,
        )
    )
    body = client.get("/").text
    assert "Stage 2 stand-down" in body
    assert "Paper trading continues" in body


def test_untracked_position_warning(client, journal, tmp_path):
    """A held position the journal never saw makes open risk understated."""
    from bot.broker import MockBroker
    from bot.models import OrderProposal

    # Build an app whose broker already holds something unjournalled.
    broker = MockBroker(starting_equity=100_000.0)
    broker.connect()
    broker.set_price("SPY", bid=579.98, ask=580.02)
    broker.place_order(
        OrderProposal(
            symbol="SPY",
            direction=Direction.BUY,
            qty=3,
            limit_price=580.0,
            stop_loss_price=575.0,
            take_profit_price=590.0,
            rationale="Opened outside the journal entirely.",
        )
    )

    import bot.main as main_mod

    def _fixed_broker(env, force_mock=False):
        return broker

    app = build_app(journal=journal, rules=load_rules(), env=_env(), force_mock=True)
    original = main_mod.build_broker
    main_mod.build_broker = _fixed_broker
    try:
        body = TestClient(app).get("/").text
    finally:
        main_mod.build_broker = original

    assert "Open risk is understated" in body
    assert "higher than shown" in body


# ---------------------------------------------------------------- read-only


def test_dashboard_exposes_no_write_routes(client):
    """Nothing here may place, close or alter anything."""
    app = client.app
    methods = {m for route in app.routes for m in getattr(route, "methods", set())}
    assert methods <= {"GET", "HEAD"}, f"unexpected write methods: {methods}"


def test_rules_are_shown_not_editable(client):
    body = client.get("/").text
    assert "max_total_risk_pct" in body       # the rules are visible
    assert "Read-only" in body
    assert "<form" not in body.lower()        # and there is no way to change them
