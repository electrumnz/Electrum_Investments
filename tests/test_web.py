"""Tests for the operator command centre.

Two things matter here. It must render without a broker, a network or a real
journal, and nothing it exposes may place, close or alter a position. The rest
is presentation.

The read-only guarantee is now asserted at the ROUTE level rather than by
grepping one page for `<form`. The dashboard is several pages and the chat page
legitimately has an input, so the old proxy would have failed for the wrong
reason. What actually matters is unchanged and is pinned harder: exactly one
non-GET route exists in the whole application, and it is `POST /chat`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.config import Env, load_rules
from bot.journal import Journal
from bot.models import (
    Decision,
    Direction,
    OrderProposal,
    OrderResult,
    RiskVerdict,
    StandDownState,
    Trade,
)
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


PAGES = ["/", "/decisions", "/trades", "/analytics", "/settings", "/chat"]


@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders_on_an_empty_journal(client, path):
    """A fresh box has no trades and no audit log. Nothing may 500 over that."""
    r = client.get(path)
    assert r.status_code == 200
    assert "MUDHORN" in r.text


def test_board_renders_on_an_empty_journal(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Nothing open" in r.text


def test_trades_page_says_so_when_there_are_none(client):
    assert "No closed trades yet" in client.get("/trades").text


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_analytics_shows_metrics_once_trades_exist(client, journal):
    _closed_trade(journal, 200.0, minutes=0, mae=-40.0, mfe=300.0)
    _closed_trade(journal, -100.0, minutes=120)

    body = client.get("/analytics").text
    assert "Profit factor" in body
    assert "Expectancy" in body
    assert "mean_reversion" in body


def test_the_rationale_reaches_the_trades_page(client, journal):
    """The reasoning is the point of a journal. Without it this is a statement."""
    _closed_trade(journal, 200.0)
    assert "invalidated below 570" in client.get("/trades").text


def test_excursion_caveat_is_always_shown(client, journal):
    """The sampling limitation must travel with the number, not sit in a docstring."""
    _closed_trade(journal, 200.0, mae=-40.0, mfe=300.0)
    assert "sampled once per decision cycle" in client.get("/analytics").text


def test_thin_sample_is_flagged_on_the_page(client, journal):
    _closed_trade(journal, 200.0)
    assert "thin sample" in client.get("/analytics").text


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


def test_chat_is_the_only_write_route(client):
    """The dashboard was wholly read-only and this test enforced it.

    That changed deliberately when the chat panel was added, so the assertion
    changed with it rather than being deleted: exactly one POST, and it is
    `/chat`. Anything else appearing here means a write route arrived without
    anyone deciding it should.
    """
    app = client.app
    writes = {
        (r.path, m)
        for r in app.routes
        for m in getattr(r, "methods", set())
        if m not in {"GET", "HEAD"}
    }
    assert writes == {("/chat", "POST")}, f"unexpected write routes: {writes}"


def test_chat_is_off_unless_a_token_is_set(client):
    """Fail closed. A deploy must not switch on the ability to drive an agent."""
    assert client.post("/chat", json={"message": "hello"}).status_code == 404


def test_chat_rejects_a_wrong_token(tmp_path, journal):
    from bot.web.app import build_app

    env = Env(_env_file=None)  # type: ignore[call-arg]
    env.dashboard_chat_token = "correct-horse"
    app = build_app(journal=journal, rules=load_rules(), env=env, force_mock=True)

    r = TestClient(app).post("/chat", json={"token": "wrong", "message": "hi"})
    assert r.status_code == 403


def test_chat_page_says_why_it_is_off_rather_than_rendering_nothing(client):
    """A blank space reads as a broken feature; a sentence reads as a choice."""
    body = client.get("/chat").text
    assert "DASHBOARD_CHAT_TOKEN" in body
    assert "risks action rather than" in body


def test_settings_shows_the_limits_without_offering_to_change_them(client):
    """A settings screen that could widen a limit would be used to widen one
    during a losing run, which is exactly when the limit is doing its job."""
    body = client.get("/settings").text

    assert "Total open" in body
    assert "2.00%" in body                     # the real cap from rules.yaml
    assert "they change in a commit" in body
    # No control of any kind that could submit a change.
    for control in ("<form", "<input", "<select", "contenteditable"):
        assert control not in body.lower(), f"settings page carries a {control}"


def test_settings_never_renders_a_credential(tmp_path, journal):
    """Loopback-bound is not the same as private. A screenshot travels."""
    env = Env(_env_file=None)  # type: ignore[call-arg]
    env.alpaca_api_key = "PK-SUPER-SECRET-KEY"
    env.finnhub_api_key = "fh-secret"
    app = build_app(journal=journal, rules=load_rules(), env=env, force_mock=True)

    body = TestClient(app).get("/settings").text

    assert "PK-SUPER-SECRET-KEY" not in body
    assert "fh-secret" not in body
    assert "configured" in body


# --------------------------------------------------------------- decisions
# The decision trail is the only surface on which a REJECTED proposal is
# visible. It never becomes a trade, so it reaches neither the journal nor the
# broker: if it does not render here, the reasoning is gone.


@pytest.fixture
def audited(tmp_path, journal):
    from bot.audit import AuditLog

    log = AuditLog(tmp_path / "audit")
    app = build_app(
        journal=journal, rules=load_rules(), env=_env(), audit_log=log, force_mock=True
    )
    return log, TestClient(app)


def _decision(**kw: object) -> Decision:
    return Decision(timestamp=datetime(2026, 5, 4, 15, 0, tzinfo=UTC), **kw)  # type: ignore[arg-type]


def _proposal(symbol: str = "SPY") -> OrderProposal:
    return OrderProposal(
        symbol=symbol,
        direction=Direction.BUY,
        qty=3,
        limit_price=580.0,
        stop_loss_price=575.0,
        take_profit_price=590.0,
        rationale="Stretched 1.4 ATR below the 20-day. Invalidated below 575.",
    )


def test_decisions_page_is_empty_but_explains_itself(client):
    body = client.get("/decisions").text
    assert "No decisions recorded yet" in body
    assert "audit/" in body


def test_a_rejection_shows_every_reason_the_gate_gave(audited):
    """The gate collects all reasons rather than short-circuiting. If the page
    renders only the first, that property is invisible to the operator."""
    log, client = audited
    log.record(
        _decision(
            proposals=[_proposal()],
            verdicts=[
                RiskVerdict.reject(
                    "risk 1.4% exceeds the 1.0% per-trade cap",
                    "total open risk would reach 2.6% against a 2.0% cap",
                    "SPY traded 4 minutes ago, inside the 1800s cooldown",
                )
            ],
        )
    )

    body = client.get("/decisions").text

    assert "rejected" in body
    assert "exceeds the 1.0% per-trade cap" in body
    assert "against a 2.0% cap" in body
    assert "inside the 1800s cooldown" in body


def test_the_proposal_and_its_reasoning_both_render(audited):
    log, client = audited
    log.record(_decision(proposals=[_proposal()], verdicts=[_approve()]))

    body = client.get("/decisions").text

    assert "BUY 3 SPY" in body
    assert "Invalidated below 575" in body
    assert "575.0000" in body


def _approve() -> RiskVerdict:
    return RiskVerdict.approve()


def test_doing_nothing_renders_as_a_result_not_a_failure(audited):
    """Most days the correct action is none, and the page must not cry wolf."""
    log, client = audited
    log.record(_decision(notes="Nothing worth taking today."))

    body = client.get("/decisions").text

    assert "held" in body
    assert "Nothing worth taking today." in body
    assert "Doing nothing is" in body


def test_an_execution_is_shown_with_its_fill(audited):
    log, client = audited
    log.record(
        _decision(
            proposals=[_proposal()],
            verdicts=[_approve()],
            executed=[
                OrderResult(
                    accepted=True, order_id="uuid-1", filled_price=580.02, filled_qty=3
                )
            ],
        )
    )

    body = client.get("/decisions").text

    assert "executed" in body
    assert "filled 3 at 580.0200" in body


def test_a_broker_refusal_is_not_dressed_up_as_success(audited):
    log, client = audited
    log.record(
        _decision(
            proposals=[_proposal()],
            verdicts=[_approve()],
            executed=[OrderResult(accepted=False, error="insufficient buying power")],
        )
    )

    assert "broker refused: insufficient buying power" in client.get("/decisions").text


def test_a_damaged_log_is_declared_rather_than_silently_shortened(audited):
    log, client = audited
    log.record(_decision(notes="a real cycle"))
    with log.files()[0].open("a", encoding="utf-8") as f:
        f.write("{not json at all")

    body = client.get("/decisions").text

    assert "Decision log is incomplete" in body
    assert "missing entries" in body


def test_a_journal_trade_the_broker_does_not_hold_is_flagged(client, journal):
    """The mirror of the untracked-position warning, and easier to miss.

    Open risk counts from the journal, so a trade the broker no longer holds
    makes the headline figure OVERSTATED. Without this the Board shows risk
    against an empty position list and nothing explains the contradiction.
    """
    journal.record_entry(
        Trade(
            symbol="SPY",
            direction=Direction.BUY,
            qty=10,
            entry_time=ENTRY,
            entry_price=580.0,
            planned_stop=570.0,
            planned_target=600.0,
            rationale="Open in the journal, absent at the broker.",
        )
    )

    body = client.get("/").text

    assert "Open risk may be overstated" in body
    assert "SPY" in body
    assert "the real figure is lower" in body
