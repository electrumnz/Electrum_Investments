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
    MarketInputs,
    OrderProposal,
    OrderResult,
    PositionAction,
    PositionPlan,
    RiskVerdict,
    Stance,
    StandDownState,
    SymbolAssessment,
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


def test_the_write_routes_are_exactly_the_two_that_were_decided_on(client):
    """The dashboard was wholly read-only and this test enforced it.

    It has been widened twice, each time deliberately and each time by editing
    this set rather than loosening the assertion: `POST /chat` when the agent
    panel landed, and `POST /login` when the operator chose to expose the
    dashboard publicly. Anything else appearing here means a write route
    arrived without anyone deciding it should.

    Neither of these writes to the journal or reaches an order path. `/login`
    mints a session; `/chat` is gated by its own separate token on top.
    """
    app = client.app
    writes = {
        (r.path, m)
        for r in app.routes
        for m in getattr(r, "methods", set())
        if m not in {"GET", "HEAD"}
    }
    assert writes == {
        ("/chat", "POST"),
        ("/login", "POST"),
    }, f"unexpected write routes: {writes}"


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


def test_chat_page_says_what_its_news_actually_is():
    """The operator asked for news and got a refusal, which read as a fault.

    It was neither: Hermes has no web access on purpose, and what it can read
    is the loop's own recording. The page has to say so, and has to say the age
    matters — a recorded headline offered as current is the confident partial
    answer this project exists to avoid.

    Rendered directly rather than fetched, because the live panel only appears
    once Hermes is installed and this asserts on the copy, not the wiring.
    """
    import re

    from bot.web.render import chat_page

    # Collapsed, because the copy is hard-wrapped in the template and a phrase
    # split across two source lines is the same sentence to a reader.
    body = re.sub(r"\s+", " ", chat_page(enabled=True, token="tok", hermes_available=True))

    assert "recording, not a search" in body
    assert "no web access" in body
    assert "age of each attached" in body
    # The quota is the reason it cannot simply fetch, and it belongs on the page
    # rather than only in a docstring somebody would have to go looking for.
    assert "100 requests a day" in body

    # And the searchable history, which is a different claim from the window:
    # the page has to say the index is derived or somebody will treat a stale
    # index as the record.
    assert "whole history is searchable" in body
    assert "read-only SQL" in body
    assert "rebuilt from it" in body


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


def test_a_breakdown_row_carries_the_same_hedge_as_the_headline():
    """A three-trade row showing 67% reads as a result. The headline says noise.

    Two figures from the same module disagreeing about how far they can be
    trusted is the sma_200-over-40-bars error moved into a table.
    """
    from datetime import UTC, datetime, timedelta

    from bot.metrics import build_report
    from bot.models import Direction, Trade
    from bot.web.render import analytics_page

    start = datetime(2026, 5, 4, 15, 0, tzinfo=UTC)
    trades = [
        Trade(
            symbol="SPY",
            strategy="trend_break",
            direction=Direction.BUY,
            qty=10,
            entry_time=start + timedelta(minutes=i * 30),
            entry_price=580.0,
            planned_stop=575.0,
            planned_target=600.0,
            exit_time=start + timedelta(minutes=i * 30 + 20),
            exit_price=585.0 if i < 2 else 574.0,
            realised_pnl_usd=50.0 if i < 2 else -60.0,
        )
        for i in range(3)
    ]

    body = analytics_page(build_report(trades))

    assert "trend_break" in body
    assert "Reading" in body
    # Three trades is well under THIN_SAMPLE_THRESHOLD, so the row must say so
    # in the same words the headline uses.
    assert "only 3 trades so treat as noise" in body


def test_settings_shows_the_loop_controls_and_marks_them_as_not_limits(client):
    """A reader must not mistake a cost control for a risk rule."""
    body = client.get("/settings").text

    assert "Skip the model call when everything is shut" in body
    assert "Open right now" in body
    assert "Not a risk limit" in body


def test_settings_shows_trading_days_beside_the_session_hours(client):
    """Hours alone read as "this is when it trades", and miss by two days a week."""
    body = client.get("/settings").text

    assert "Trading days" in body
    assert "Mon, Tue, Wed, Thu, Fri" in body


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


# ------------------------------------------------ what it considered and holds


def test_symbols_considered_but_not_proposed_still_reach_the_page(audited):
    """A quiet cycle must record what was examined.

    Otherwise "nothing met the conditions" and "the loop never looked at QQQ"
    are the same entry afterwards, and only one of them is a working bot.
    """
    log, client = audited
    log.record(
        _decision(
            notes="Nothing stretched far enough.",
            assessments=[
                SymbolAssessment(
                    symbol="QQQ",
                    stance=Stance.WATCH,
                    reasoning="0.4 ATR under the 20-day, not the 1.0 the entry needs.",
                    waiting_for="QQQ closing below 771.20, about 1 ATR under the 20-day",
                ),
                SymbolAssessment(
                    symbol="KO",
                    stance=Stance.PASS,
                    reasoning="Below its 200-day average, so the trend filter fails.",
                ),
            ],
        )
    )

    body = client.get("/decisions").text

    assert "Considered" in body
    assert "QQQ" in body and "KO" in body
    assert "watch" in body and "pass" in body
    assert "closing below 771.20" in body
    assert "trend filter fails" in body


def test_a_watch_with_no_trigger_is_called_out_as_empty(audited):
    """"Waiting for more confirmation" is not a plan and must not read as one."""
    log, client = audited
    log.record(
        _decision(
            assessments=[
                SymbolAssessment(
                    symbol="SPY",
                    stance=Stance.WATCH,
                    reasoning="Setup forming but not there yet.",
                    waiting_for="   ",
                )
            ]
        )
    )

    assert "no condition named" in client.get("/decisions").text


def test_the_reasoning_for_staying_in_a_position_reaches_the_page(audited):
    log, client = audited
    log.record(
        _decision(
            position_plans=[
                PositionPlan(
                    symbol="SPY",
                    action=PositionAction.HOLD,
                    thesis_intact=True,
                    reasoning="Reverted half the distance to the 20-day; thesis working.",
                    waiting_for="the 20-day average at 651.40, or the stop at 631.40",
                    invalidation="a daily close below 631.40",
                )
            ]
        )
    )

    body = client.get("/decisions").text

    assert "Open positions reviewed" in body
    assert "thesis working" in body
    assert "20-day average at 651.40" in body
    assert "close below 631.40" in body
    # It must be unmistakable that nothing here was acted on.
    assert "Advisory only" in body


def test_the_news_the_model_read_is_recorded_with_the_decision(audited):
    """A snapshot taken later answers a different question from the one an old
    cycle raises."""
    log, client = audited
    log.record(
        _decision(
            inputs=MarketInputs(
                headlines=["Fed holds rates steady", "Chip orders soften"],
                news_windows=["2026-05-04T15:30 affects KO"],
                indicators={"SPY": "close 648.20, sma20 651.40, trend above"},
                symbols_without_history=["JNJ"],
            )
        )
    )

    body = client.get("/decisions").text

    assert "What it read" in body
    assert "Fed holds rates steady" in body
    assert "affects KO" in body
    assert "close 648.20" in body
    assert "JNJ" in body


def test_a_degraded_calendar_is_not_shown_as_no_announcements(audited):
    """Zero windows from a broken feed is indistinguishable from a quiet week
    unless the page says which it was."""
    log, client = audited
    log.record(_decision(inputs=MarketInputs(news_windows=[], calendar_degraded=True)))

    body = client.get("/decisions").text

    assert "DEGRADED" in body
    assert "not that there were no announcements" in body


# ------------------------------------------------------------ pending orders


def test_a_resting_order_is_shown_with_the_distance_to_its_limit(journal):
    """A limit order that has not filled leaves no position and no explanation.

    The gap is what separates "waiting patiently" from "never going to fill".
    """
    from bot.broker import MockBroker
    from bot.models import OrderStatus, WorkingOrder

    broker = MockBroker(starting_equity=100_000.0)
    broker.connect()
    broker.set_price("SPY", bid=648.00, ask=648.04)
    broker.set_open_orders(
        [
            WorkingOrder(
                order_id="o-1",
                symbol="SPY",
                direction=Direction.BUY,
                qty=12,
                limit_price=641.20,
                status=OrderStatus.NEW,
                submitted_at=ENTRY,
            )
        ]
    )

    import bot.main as main_mod

    app = build_app(journal=journal, rules=load_rules(), env=_env(), force_mock=True)
    def _fixed(env: object, force_mock: bool = False) -> MockBroker:
        return broker

    original = main_mod.build_broker
    main_mod.build_broker = _fixed
    try:
        body = TestClient(app).get("/").text
    finally:
        main_mod.build_broker = original

    assert "Pending orders" in body
    assert "641.2000" in body          # the limit
    assert "648.0200" in body          # the market
    assert "% away" in body            # and how far it has to travel


def test_no_resting_orders_says_so(client):
    assert "Nothing resting at the broker" in client.get("/").text


def test_the_stylesheet_carries_no_control_characters():
    """STYLES is an ordinary Python string, so CSS escapes are read by Python first.

    A CSS hex escape such as backslash-2-5-B-8 is a valid OCTAL escape to
    Python, which silently turns it into a control character and leaves the
    remaining digits as text. The browser then draws a tofu box beside them.
    Nothing warns, ruff does not care, and it is invisible unless somebody looks
    at the rendered page. This caught exactly that, twice.
    """
    from bot.web.render import STYLES

    offenders = [
        (i, line)
        for i, line in enumerate(STYLES.splitlines(), 1)
        if any(ord(c) < 32 and c != "\t" for c in line)
    ]
    assert not offenders, f"control characters in STYLES: {offenders}"


def test_posts_the_model_read_are_shown_with_the_decision(audited):
    """Posts move a price before the wire story exists, so which ones were in
    front of the model is part of reading the decision back."""
    log, client = audited
    log.record(
        _decision(
            inputs=MarketInputs(
                social_posts=["[@realDonaldTrump 14:31] Announcing tariffs on steel imports"]
            )
        )
    )

    body = client.get("/decisions").text

    assert "tariffs on steel imports" in body.lower()
    assert "realDonaldTrump" in body


def test_a_degraded_social_feed_is_not_shown_as_a_quiet_morning(audited):
    log, client = audited
    log.record(_decision(inputs=MarketInputs(social_posts=[], social_degraded=True)))

    body = client.get("/decisions").text

    assert "social feed was DEGRADED" in body
    assert "not that nothing was posted" in body
