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
from bot.dreaming import DreamStore, DreamSummary
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
from bot.souls import Soul
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
def dreams(tmp_path):
    """Never the real store. Same rule as the journal: a test that wrote to
    data/ would leave a file on the developer's machine that the next run
    then reads."""
    return DreamStore(tmp_path / "dreams.db")


@pytest.fixture
def client(journal, dreams):
    app = build_app(
        journal=journal, rules=load_rules(), env=_env(),
        dreams=dreams, force_mock=True
    )
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


PAGES = ["/", "/decisions", "/trades", "/analytics", "/dreaming", "/settings", "/chat"]


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


def test_untracked_position_warning(client, journal, tmp_path, dreams):
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

    app = build_app(
        journal=journal, rules=load_rules(), env=_env(),
        dreams=dreams, force_mock=True
    )
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


def test_chat_rejects_a_wrong_token(tmp_path, journal, dreams):
    from bot.web.app import build_app

    env = Env(_env_file=None)  # type: ignore[call-arg]
    env.dashboard_chat_token = "correct-horse"
    app = build_app(
        journal=journal, rules=load_rules(), env=env,
        dreams=dreams, force_mock=True
    )

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


def test_settings_never_renders_a_credential(tmp_path, journal, dreams):
    """Loopback-bound is not the same as private. A screenshot travels."""
    env = Env(_env_file=None)  # type: ignore[call-arg]
    env.alpaca_api_key = "PK-SUPER-SECRET-KEY"
    env.finnhub_api_key = "fh-secret"
    app = build_app(
        journal=journal, rules=load_rules(), env=env,
        dreams=dreams, force_mock=True
    )

    body = TestClient(app).get("/settings").text

    assert "PK-SUPER-SECRET-KEY" not in body
    assert "fh-secret" not in body
    assert "configured" in body


# --------------------------------------------------------------- decisions
# The decision trail is the only surface on which a REJECTED proposal is
# visible. It never becomes a trade, so it reaches neither the journal nor the
# broker: if it does not render here, the reasoning is gone.


@pytest.fixture
def audited(tmp_path, journal, dreams):
    from bot.audit import AuditLog

    log = AuditLog(tmp_path / "audit")
    app = build_app(
        journal=journal, rules=load_rules(), env=_env(), audit_log=log,
        dreams=dreams, force_mock=True
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


def test_a_resting_order_is_shown_with_the_distance_to_its_limit(journal, dreams):
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

    app = build_app(
        journal=journal, rules=load_rules(), env=_env(),
        dreams=dreams, force_mock=True
    )
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


# ------------------------------------------------------------------ dreaming


def test_the_dreaming_page_leads_with_what_it_is_not(client):
    """The most important fact about the page, ahead of any of its contents.

    Everything on it is speculation produced by a model that is good at
    sounding certain, on a dashboard that otherwise reports measured facts about
    real money. A reader landing mid-page has to be told which of the two they
    are looking at, in the same way the public site labels its invented figures.
    """
    body = client.get("/dreaming").text

    assert "Nothing here is a proposal" in body
    assert "no quantity, no entry, no stop and no side" in body
    assert "risk.py" in body


def test_the_page_does_not_claim_an_isolation_it_does_not_have():
    """The banner used to say the dreamer had "no route to the broker".

    True of the dream RECORDS, which carry no order fields. Not true of the chat
    panel on the same page: without a separate instance it talks to the same
    Hermes as Chat and can reach the same order tools. The risk gate still runs
    on every one of them, so the operator's limits hold — but "it has no broker
    tool" and "it has one and was asked nicely" are different claims, and the
    page must make the one that is actually true today.
    """
    from bot.web.render import dreaming_page

    shared = dreaming_page(
        [], DreamSummary.of([]), enabled=True, token="t",
        hermes_available=True, soul_found=True, isolated=False,
    )
    assert "Sharing the account agent" in shared
    assert "including the order tools" in shared
    assert "souls/grogu.md" in shared
    assert "runs on its own agent" not in shared

    isolated = dreaming_page(
        [], DreamSummary.of([]), enabled=True, token="t",
        hermes_available=True, soul_found=True, isolated=True,
    )
    assert "runs on its own agent" in isolated
    assert "no broker tool to reach for" in isolated
    assert "Sharing the account agent" not in isolated


def test_the_dreamer_uses_its_own_instance_when_one_is_installed(journal, dreams, monkeypatch):
    """A speculative agent should have no broker tool, not one it was told not
    to use. When the second Hermes is absent the panel still works, and the page
    above it says so rather than implying the stronger arrangement."""
    from bot.web import chat as chat_mod

    env = Env(_env_file=None)  # type: ignore[call-arg]
    env.dashboard_chat_token = "tok"

    asked: list[str] = []

    def _ask(self, message, history=None, soul=None):
        asked.append(str(self.binary))
        return chat_mod.ChatReply(text="ok")

    monkeypatch.setattr(chat_mod.HermesBridge, "ask", _ask)
    # Both instances present: the dreamer gets its own.
    monkeypatch.setattr(chat_mod.HermesBridge, "available", property(lambda self: True))

    app = build_app(
        journal=journal, rules=load_rules(), env=env,
        dreams=dreams, force_mock=True
    )
    client = TestClient(app)
    client.post("/chat", json={"token": "tok", "message": "hi", "soul": "grogu"})
    client.post("/chat", json={"token": "tok", "message": "hi", "soul": "yoda"})

    assert asked[0].endswith("run-dream.sh")
    assert asked[1].endswith("run-chat.sh")


def test_the_dreamer_falls_back_rather_than_refusing(journal, dreams, monkeypatch):
    """A box without the second instance still gets a working panel."""
    from bot.web import chat as chat_mod

    env = Env(_env_file=None)  # type: ignore[call-arg]
    env.dashboard_chat_token = "tok"

    asked: list[str] = []

    def _ask(self, message, history=None, soul=None):
        asked.append(str(self.binary))
        return chat_mod.ChatReply(text="ok")

    monkeypatch.setattr(chat_mod.HermesBridge, "ask", _ask)
    # Only the account instance exists.
    monkeypatch.setattr(
        chat_mod.HermesBridge,
        "available",
        property(lambda self: self.binary == chat_mod.DEFAULT_BINARY),
    )

    app = build_app(
        journal=journal, rules=load_rules(), env=env,
        dreams=dreams, force_mock=True
    )
    TestClient(app).post("/chat", json={"token": "tok", "message": "hi", "soul": "grogu"})

    assert asked == [str(chat_mod.DEFAULT_BINARY)]


def test_an_empty_deck_shows_a_worked_example_marked_as_one(client):
    """A fresh box has no dreams, and a blank page teaches nothing.

    The illustration must not be mistakeable for something the agent produced,
    which is the same rule the public site follows about invented figures.
    """
    body = client.get("/dreaming").text

    assert "An illustration, not a recorded dream" in body
    assert "sesame" in body.lower()


def test_a_dream_renders_its_unchecked_hops_as_unchecked(client, dreams):
    """Missing stays missing. A chain is only as good as its weakest link and a
    hop nobody verified must not read like one somebody did."""
    from bot.dreaming import Dream, Hop

    dreams.save(
        Dream(
            title="Brood overlap",
            seed="Two of three producers inside overlapping ranges.",
            chain=[
                Hop("broods run on fixed cycles", checked=True, source="brood map"),
                Hop("the overlap lands this season"),
            ],
            weakest_hop="whether the overlap and the concentration coincide",
        )
    )

    body = client.get("/dreaming").text

    assert "Brood overlap" in body
    assert "Not checked. Nobody has verified this hop." in body
    assert "brood map" in body
    assert "Weakest hop" in body
    assert "partial" in body


def test_a_chain_with_no_stated_weakest_hop_is_called_out(client, dreams):
    """Same rule the Decisions page applies to a watch with no trigger.

    A chain nobody has attacked is not a strong chain, and rendering it without
    comment would let the dreamer look like it has a view when it does not.
    """
    from bot.dreaming import Dream, Hop

    dreams.save(Dream(title="Unattacked", seed="s", chain=[Hop("a claim")]))

    body = client.get("/dreaming").text

    assert "has not been attacked yet" in body


def test_a_kept_dream_with_no_trigger_is_called_out_as_a_note(client, dreams):
    from bot.dreaming import Dream, DreamStage, DreamVerdict, Hop

    dreams.save(
        Dream(
            title="Kept",
            seed="s",
            stage=DreamStage.VERDICT,
            verdict=DreamVerdict.KEEP,
            chain=[Hop("a claim", checked=True, source="somewhere")],
        )
    )

    body = client.get("/dreaming").text

    assert "a note, not a watch" in body


def test_the_dreaming_page_survives_a_store_it_cannot_read(journal, dreams, tmp_path):
    """A broken speculative-notes table is not a reason to show an error page.

    The dreams store is deliberately a different file from the journal, and the
    whole point of that separation is that losing it costs notes and nothing
    else. It must cost the page its contents, not the page.
    """
    from bot.dreaming import DreamStore

    broken = DreamStore(tmp_path / "broken.db")
    (tmp_path / "broken.db").write_text("this is not a database")

    app = build_app(
        journal=journal, rules=load_rules(), env=_env(),
        dreams=broken, force_mock=True
    )

    r = TestClient(app).get("/dreaming")

    assert r.status_code == 200
    assert "Dreaming" in r.text


@pytest.mark.parametrize(
    "requested,expected",
    [
        ("yoda", "yoda"),
        ("grogu", "grogu"),
        # Anything else is the account agent, including the shapes that would
        # matter if this string reached a path join.
        ("../../../../etc/passwd", "yoda"),
        ("souls/../../.env", "yoda"),
        ("", "yoda"),
        ("GROGU", "grogu"),  # case is normalised, not rejected
    ],
)
def test_the_soul_name_from_a_request_body_cannot_name_an_arbitrary_file(
    journal, dreams, monkeypatch, requested, expected
):
    """`load_soul` builds a filesystem path out of this string.

    An unvalidated value from a request body reaching a path join is a traversal
    waiting to happen, so the route checks it against a fixed set first. An
    unknown name falls back to the account agent rather than erroring: the worst
    case of getting it wrong is the wrong voice, and refusing to answer would be
    a larger failure than answering plainly.
    """
    from bot.web import app as app_module

    seen: list[str] = []

    def _spy(name, **kw):
        seen.append(name)
        return Soul.absent(name)

    monkeypatch.setattr(app_module, "load_soul", _spy)

    env = Env(_env_file=None)  # type: ignore[call-arg]
    env.dashboard_chat_token = "tok"
    app = build_app(
        journal=journal, rules=load_rules(), env=env,
        dreams=dreams, force_mock=True
    )

    TestClient(app).post(
        "/chat", json={"token": "tok", "message": "hi", "soul": requested}
    )

    assert seen, "the route did not consult the soul loader at all"
    assert seen[-1] == expected


def test_no_stylesheet_rule_collides_with_a_state_badge():
    """A `.pill` modifier must not double as a layout class.

    The stage badges are named after the states they show, so a rule written as
    `.dream .seed { padding: ... }` for the spark paragraph ALSO matched
    `<span class="pill seed">` and rendered the badge as a full-width block.
    Nothing warned: it is valid CSS that silently styles the wrong element, and
    it is only visible if somebody looks at the page.

    This flags the shape rather than the instance. A rule whose final compound
    selector is a bare `.name` that is also a pill modifier, and which sets a
    LAYOUT property, is the collision. Colour-only overlaps are deliberate and
    harmless: `.loss` means the same red wherever it lands.
    """
    import re

    from bot.web.render import STYLES

    # Strip comments first, so prose in them cannot look like a selector.
    css = re.sub(r"/\*.*?\*/", "", STYLES, flags=re.S)

    modifiers = set(re.findall(r"\.pill\.([a-z-]+)", css))
    assert modifiers, "no pill modifiers found; has the badge markup changed?"

    layout = (
        "display", "padding", "margin", "position", "width", "height",
        "font-size", "font-family", "border-bottom", "border-left", "grid",
        "flex",
    )

    offenders = []
    for selectors, block in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if not any(f"{prop}:" in block for prop in layout):
            continue
        for selector in selectors.split(","):
            final = selector.strip().split()[-1] if selector.strip() else ""
            # A bare `.name` — no element, no second class to narrow it.
            if re.fullmatch(r"\.([a-z-]+)", final) and final[1:] in modifiers:
                offenders.append((selector.strip(), final))

    assert not offenders, (
        "these rules restyle a state badge as a side effect: "
        f"{offenders}. Scope by element role, not by a word that is also a state."
    )


def test_settings_shows_the_dream_schedule_without_claiming_it_is_running(client):
    """The cadence is read from the timer unit rather than quoted from a
    constant, and the page is careful about what a file on disk proves.

    A unit existing is not a timer running, and this process cannot tell the
    difference. Saying "daily at 07:00" on a box where nobody enabled it would
    be the confident-partial-answer failure in a new place.
    """
    body = client.get("/settings").text

    # The section heading, not the bare word: "Dreaming" is in the nav on every
    # page, so asserting it alone would pass with the card entirely absent.
    assert "<h2>Dreaming</h2>" in body
    assert "Pacific/Auckland" in body
    assert "not visible from this process" in body
    assert "systemctl list-timers" in body
    # The tier and the cache decision are both surfaced, since both are
    # counter-intuitive enough to be worth stating.
    assert "Prompt cache" in body
    assert "runs daily, so it would miss every time" in body


def test_settings_still_offers_no_way_to_change_anything(client):
    """The dreaming card is display only, like every other card here."""
    body = client.get("/settings").text.lower()

    for control in ("<form", "<input", "<select", "contenteditable", "<button"):
        assert control not in body, f"settings page carries a {control}"


def test_the_dreamer_is_not_credited_with_the_account_agent_tools(client):
    """The two panels share an implementation but not a reach.

    The account agent has the news recording and the searchable history index;
    the dreamer has neither, and ideally runs on a separate Hermes with no
    broker tools at all. The shared `chat_panel` therefore takes the notes about
    those tools as a per-caller argument rather than baking them in — describing
    them on the Dreaming page would credit the dreamer with tools it cannot
    reach, which is the same class of overclaim as the "no route to the broker"
    banner that had to be corrected.
    """
    from bot.web.render import chat_page, dreaming_page

    # Rendered directly with chat enabled: the default client fixture has no
    # DASHBOARD_CHAT_TOKEN, so both routes show their "chat is off" branch and
    # neither panel exists to compare.
    chat = chat_page(enabled=True, token="t", hermes_available=True)
    dreaming = dreaming_page(
        [], DreamSummary.of([]), enabled=True, token="t",
        hermes_available=True, soul_found=True, isolated=False,
    )

    assert "News here is a recording" in chat
    assert "insight.db" in chat
    assert 'var SOUL = "yoda"' in chat

    assert "News here is a recording" not in dreaming
    assert "insight.db" not in dreaming
    assert 'var SOUL = "grogu"' in dreaming

def test_enter_sends_and_ctrl_enter_makes_a_new_line():
    """The composer's key handling, which is easy to regress into silence.

    A textarea sends nothing on its own, so every one of these is a deliberate
    branch rather than a default:

    - Enter sends, which is what anyone typing into a chat box expects.
    - Ctrl/Cmd+Enter inserts a newline BY HAND, because the browser does not
      insert one for that combination — taking the key over without doing this
      would make it silently do nothing at all.
    - Shift+Enter falls through to the browser, which does insert one.
    - An Enter that is confirming an IME candidate must not send, or the
      message goes off mid-word.

    And the page has to say which key does what, since none of it is guessable.
    """
    import re

    from bot.web.render import chat_page

    body = chat_page(enabled=True, token="tok", hermes_available=True)
    js = body[body.index("<script>") :]

    # Enter sends: no modifier check guarding the send path any more.
    assert "if (e.key !== 'Enter') return;" in js
    assert "e.preventDefault();\n    ask();" in js

    # Ctrl/Cmd+Enter inserts the newline itself.
    assert "if (e.ctrlKey || e.metaKey) { e.preventDefault(); newlineAtCaret(); return; }" in js
    assert "box.selectionStart" in js and "'\\n'" in js

    # Shift+Enter defers to the browser rather than being swallowed.
    assert "if (e.shiftKey) return;" in js

    # Composition must not be mistaken for a send.
    assert "e.isComposing" in js

    # And it is stated on the page, wired to the textarea for screen readers.
    assert 'aria-describedby="key-hint"' in body
    assert 'id="key-hint"' in body
    collapsed = re.sub(r"\s+", " ", body)
    assert "<b>Enter</b> sends." in collapsed
    assert "<b>Ctrl+Enter</b> or <b>Shift+Enter</b> for a new line." in collapsed
