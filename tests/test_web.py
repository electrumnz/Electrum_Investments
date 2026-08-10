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

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from bot.audit import DecisionEntry
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
    WorkingOrder,
)
from bot.souls import Soul
from bot.web import live, render
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
def poller(journal):
    """A poller primed with one synchronous read.

    The Board no longer talks to the broker during a render — the poller owns
    that conversation — so a test that wants figures on the page has to give it
    a reading first. `poll_once()` is synchronous precisely so this needs no
    event loop.
    """
    p = live.build_poller(journal=journal, env=_env(), force_mock=True)
    p.poll_once()
    return p


@pytest.fixture
def client(journal, dreams, poller):
    app = build_app(
        journal=journal, rules=load_rules(), env=_env(),
        dreams=dreams, poller=poller, force_mock=True
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
        # Built and polled INSIDE the swap: the poller reaches the broker
        # through the same `build_broker`, so a poller created outside it would
        # read the wrong account.
        primed = live.build_poller(journal=journal, env=_env(), force_mock=True)
        primed.poll_once()
        app = build_app(
            journal=journal, rules=load_rules(), env=_env(),
            dreams=dreams, poller=primed, force_mock=True
        )
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

    def _fixed(env: object, force_mock: bool = False) -> MockBroker:
        return broker

    original = main_mod.build_broker
    main_mod.build_broker = _fixed
    try:
        # Built and polled INSIDE the swap: the poller reaches the broker
        # through the same `build_broker`, so one created outside it would read
        # a different account and find no resting orders.
        primed = live.build_poller(journal=journal, env=_env(), force_mock=True)
        primed.poll_once()
        app = build_app(
            journal=journal, rules=load_rules(), env=_env(),
            dreams=dreams, poller=primed, force_mock=True
        )
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

    def _ask(self, message, history=None, soul=None, operator=""):
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

    def _ask(self, message, history=None, soul=None, operator=""):
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
    """A modifier class must not double as a layout class.

    Caught twice now, in the same shape both times.

    First: the stage badges are named after the states they show, so
    `.dream .seed { padding: ... }` written for the spark paragraph ALSO matched
    `<span class="pill seed">` and rendered the badge as a full-width block.

    Then, with this test written to cover only `.pill` modifiers, it happened
    again one selector across: the sign-in screen's full-height centring wrapper
    was `.gate`, and the Decisions page marks its risk-gate verdict row
    `<div class="rung gate no">`. So every verdict inherited
    `min-height: calc(100svh - 8rem); place-items: center` and stretched to most
    of a viewport with the rejection reason floating in the middle of the void.
    Valid CSS, silently styling the wrong element, and invisible on any page
    without a decision on it — which every empty-journal render is.

    So the guard now takes EVERY class used as a modifier anywhere, rather than
    only the badges. That is the general form: a word that names a state is a
    bad name for a box.

    Colour-only overlaps stay deliberate and harmless: `.loss` means the same
    red wherever it lands.
    """
    import re

    from bot.web.render import STYLES

    # Strip comments first, so prose in them cannot look like a selector.
    css = re.sub(r"/\*.*?\*/", "", STYLES, flags=re.S)

    # Any class appearing in second position of a compound selector is being
    # used as a modifier: `.pill.seed`, `.rung.gate`, `.live.link-live`.
    modifiers = set(re.findall(r"\.[a-z-]+\.([a-z-]+)", css))
    assert "seed" in modifiers and "gate" in modifiers, (
        "the two known collisions are no longer detectable as modifiers; "
        "has the markup changed?"
    )

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


# ------------------------------------------------------ since you were here


def test_the_board_stamps_the_visit_marker(client):
    """The cookie is what makes "since you were last here" possible at all."""
    from bot.web.seen import COOKIE_NAME as SEEN_COOKIE

    r = client.get("/")

    assert SEEN_COOKIE in r.cookies


def test_a_first_visit_is_not_told_that_nothing_happened(client):
    """A strip saying "we have no record of you" is noise on the one visit
    where there is genuinely nothing to report."""
    body = client.get("/").text

    assert "Since you were last here" not in body


def test_a_returning_visit_is_told_what_changed(client, audited=None):
    """The second visit, after a gap, reports the window between them."""
    from datetime import timedelta

    from bot.web import seen as seen_mod

    first = seen_mod.observe(None, now=ENTRY)
    later = seen_mod.observe(first.cookie_value, now=ENTRY + timedelta(hours=4))

    client.cookies.set(seen_mod.COOKIE_NAME, later.cookie_value)
    body = client.get("/").text

    assert "Since you were last here" in body


def test_the_marker_is_not_stamped_by_the_stream_or_the_health_probe(client):
    """An EventSource reconnects by itself and a monitor probes every thirty
    seconds. Either stamping the cookie would hold a sitting open forever,
    which is the bug the six-hour ceiling exists to bound."""
    from bot.web.seen import COOKIE_NAME as SEEN_COOKIE

    assert SEEN_COOKIE not in client.get("/healthz").cookies


def test_only_a_page_with_live_figures_opens_the_stream():
    """The sign-in page inherits SCRIPT, and used to open `/live` from it.

    `/live` sits behind the same password as the pages that render an account,
    so an unauthenticated page opening it got a 401 and a console error on every
    view of the login form. Confirmed in a browser before this guard, and gone
    after it.

    It also keeps Decisions, Trades, Settings and Chat off the stream: they have
    no `data-live` targets to paint. Since the poller starts on the first
    subscription and idle-stops after the last, a session that never opens the
    Board never talks to the broker at all.
    """
    from bot.web.render import SCRIPT

    assert "if (!document.querySelector('[data-live]')) return;" in SCRIPT


def _aged_client(journal, dreams, hours: float):
    """A Board whose reading is `hours` old, without waiting `hours`.

    The poller's clock is injectable, so the reading is taken at one moment and
    the page rendered at a later one. Reaching this path by sleeping would mean
    a test that takes minutes, which is how it went untested in the first place.
    """
    moment = [datetime(2026, 8, 10, 0, 0, tzinfo=UTC)]
    p = live.build_poller(
        journal=journal, env=_env(), force_mock=True, clock=lambda: moment[0]
    )
    p.poll_once()
    moment[0] += timedelta(hours=hours)
    app = build_app(
        journal=journal, rules=load_rules(), env=_env(),
        dreams=dreams, poller=p, force_mock=True
    )
    return TestClient(app)


def test_an_old_reading_is_never_stamped_with_the_current_time(journal, dreams):
    """The Board used to print `as at <now>` over figures read hours earlier.

    The poller idle-stops once nobody is watching and keeps its last reading, so
    the first load of a morning is served an overnight snapshot. Stamping it
    with the render clock made every figure on the page a present-tense claim
    about an account nobody had read since the night before — and the stamp is
    the one element a reader checks to find out. Confident, plausible, wrong,
    and delivered by the furniture rather than the model.

    The stamp names the READING now. The whole suite was green while it did not.
    """
    client = _aged_client(journal, dreams, hours=8)
    body = client.get("/").text

    assert '<p class="asof" data-live-read="">last read 10 Aug 2026, 00:00 UTC' in body
    # The regression itself: the render clock must not appear as a stamp. Named
    # rather than grepped for "as at", which also matches "canvas at all" in
    # the inlined script and passes for the wrong reason.
    assert render._when(datetime.now(UTC)) not in body


def test_an_old_reading_says_so_ahead_of_everything_it_qualifies(journal, dreams):
    """The banner leads because every other banner is a claim about a reading.

    An expiry alert or an untracked-position warning derived from an eight-hour
    -old snapshot is still worth showing — erring towards warning is the safe
    direction — but a reader has to be told which account state they describe.
    """
    client = _aged_client(journal, dreams, hours=8)
    body = client.get("/").text

    assert "These figures are not current" in body
    assert "not refreshed since" in body
    # Ahead of the page head, so it is read before any figure it qualifies.
    assert body.index("These figures are not current") < body.index("<h1>Board</h1>")


def test_a_fresh_reading_carries_no_staleness_warning(journal, dreams, client):
    """The other half. A warning that showed on every load would be furniture
    rather than a signal, and the next real one would be ignored."""
    body = client.get("/").text

    assert "These figures are not current" not in body
    assert "read " in body


def test_the_stamp_is_marked_for_the_stream_to_correct(journal, dreams, client):
    """A server-rendered timestamp is right for exactly one instant.

    The figures beneath it are repainted every few seconds, so a stamp left
    alone becomes a time attached to a reading it no longer describes — and it
    stays wrong for as long as the tab is open, which is the failure this whole
    change is about arriving by a slower route.
    """
    assert "data-live-read" in client.get("/").text
    assert "data.status === 'slow'" in render.SCRIPT


def test_the_four_live_states_do_not_collapse_into_two():
    """`slow` used to fall through to the `else` and paint the link green.

    That said "live" while a read was outstanding and the figures on screen were
    the previous ones — precisely the distinction the state exists to draw. Four
    states that render as two are two states with extra names.
    """
    for state in ("'failing'", "'slow'", "'starting'"):
        assert f"data.status === {state}" in render.SCRIPT


def _quote(
    symbol: str,
    last: float | None = None,
    prev: float | None = None,
    tradeable: bool = False,
) -> live.TickerQuote:
    return live.TickerQuote(
        symbol=symbol, last=last, previous_close=prev, tradeable=tradeable
    )


def test_a_quote_that_could_not_be_read_never_renders_as_flat():
    """On a strip of sixteen this is the least conspicuous place in the whole
    interface to put a plausible wrong figure, which is exactly why it must not
    happen here. No quote says so; a quote with no prior close says that."""
    from bot.market_clock import market_state

    state = market_state(datetime(2026, 8, 10, 15, 0, tzinfo=UTC))
    body = render.ticker_tape(
        state,
        [_quote("GLD"), _quote("TLT", last=91.2), _quote("SPY", last=580.0, prev=574.0)],
    )

    assert "no quote" in body            # nothing came back at all
    assert "no prior close" in body      # a price, but no yesterday to compare
    assert "0.00%" not in body           # neither may borrow a flat day
    assert "▲1.05%" in body


def test_the_tape_marks_which_symbols_the_gate_would_actually_allow():
    """`watchlist.symbols` is a view; `allowed_symbols` is a permission. The
    tape is the one surface where they sit side by side, so the difference is
    rendered rather than left to be assumed from a name scrolling past."""
    from bot.market_clock import market_state

    body = render.ticker_tape(
        market_state(datetime(2026, 8, 10, 15, 0, tzinfo=UTC)),
        [_quote("SPY", last=580.0, prev=574.0, tradeable=True), _quote("NVDA", last=9.0)],
    )

    # Membership rather than an exact string. The class list gained the venue
    # state, and an assertion pinned to the exact spelling of the whole
    # attribute fails on any addition to it — which says nothing about whether
    # the tradeable marker still works.
    def classes_of(symbol: str) -> set[str]:
        cell = body.split(f'data-tick="{symbol}"')[0].rsplit('<span class="', 1)[-1]
        return set(cell.split('"')[0].split())

    # SPY: on the watchlist AND in an enabled instrument class.
    assert {"cell", "can", "up"} <= classes_of("SPY")
    # NVDA: on the tape, not tradeable, and not marked as if it were.
    assert 'data-tick="NVDA"' in body
    assert "can" not in classes_of("NVDA")


def test_a_shut_market_greys_out_but_crypto_never_does():
    """A Saturday SPY price is last Friday's close, and must not read as live.

    Crypto is the half that matters. It trades continuously, so a Sunday BTC
    price IS current — dimming it alongside the equities would be the single
    global session all over again, which is the bug `config/rules.yaml` grew an
    `instruments:` block to fix, arriving through the interface instead.
    """
    from bot.market_clock import market_state

    quotes = [
        _quote("SPY", last=580.0, prev=574.0, tradeable=True),
        _quote("BTC/USD", last=61000.0, prev=60000.0),
    ]

    def greyed(body: str, symbol: str) -> bool:
        head = body.split(f'data-tick="{symbol}"')[0]
        return "shut" in head.rsplit("<span class=", 1)[-1]

    weekend = render.ticker_tape(
        market_state(datetime(2026, 8, 15, 12, 0, tzinfo=UTC)), quotes
    )
    assert greyed(weekend, "SPY")
    assert not greyed(weekend, "BTC/USD")
    assert "market shut" in weekend

    session = render.ticker_tape(
        market_state(datetime(2026, 8, 10, 15, 0, tzinfo=UTC)), quotes
    )
    assert not greyed(session, "SPY")
    assert not greyed(session, "BTC/USD")


def test_the_grey_override_outranks_what_the_live_painter_puts_back():
    """The painter re-adds `up`/`down` on every frame and removes only those
    two, so `shut` survives a repaint — and therefore has to keep winning. At
    equal specificity that is source order, so the rule must stay below."""
    styles = render.STYLES
    assert styles.index(".tape .cell.up .mv") < styles.index(".tape .cell.shut .sym")


def test_the_run_is_emitted_twice_so_the_marquee_does_not_snap():
    """The track scrolls to -50%, where the second copy sits exactly where the
    first began. One copy would jump back to the start every cycle."""
    from bot.market_clock import market_state

    body = render.ticker_tape(
        market_state(datetime(2026, 8, 10, 15, 0, tzinfo=UTC)),
        [_quote("SPY", last=580.0, prev=574.0)],
    )

    assert body.count('data-tick="SPY"') == 2
    assert "translate3d(-50%,0,0)" in render.STYLES


def test_the_tape_sits_above_the_projection_planes():
    """The grid, vignette and scanline are fixed full-screen at z 1-3. A strip
    left at `auto` renders beneath all three and its cells come out dimmed to
    near invisibility — while the header beside them, at z 20, looks fine.

    Nothing warns: the elements are present, opaque, and `elementFromPoint`
    returns them. It took a screenshot to see."""
    assert "z-index:19}" in render.STYLES


def test_settings_shows_a_disabled_class_rather_than_omitting_it(client):
    """Crypto is off, and the page has to say so.

    The moment to enable it is a moment when nothing else is open — a weekend
    or the small hours — which is the worst possible moment to be deciding what
    a crypto position should be allowed to risk. So the limits are configured
    while it is off, and visible, and enabling is a one-word edit rather than a
    design exercise under time pressure.
    """
    body = client.get("/settings").text

    assert "crypto" in body
    assert "0.50% (this class)" in body      # its own per-trade limit
    assert "Disabled." in body

    # A 24/7 market has no pre-market, so "permitted" would answer a question
    # that does not apply and read as a gap in the rules.
    assert "not applicable (24/7 market)" in body


def test_settings_names_which_limit_is_binding_per_class(client):
    """A class with no opinion shows the portfolio figure rather than a blank.
    An empty cell reads as "no limit", which is the opposite of what an absent
    override means."""
    body = client.get("/settings").text

    assert "1.00% (this class)" in body      # us_equity states its own


def test_settings_says_when_a_class_limit_is_looser_than_the_default():
    """`account:` is a default, not a ceiling — a class may set a looser limit
    and nothing refuses it. So a looser value is said out loud rather than
    rendered identically to a tighter one.

    Information, not a warning. The operator chose it; the settings agent is
    what argues the case at the moment one is being changed.
    """
    assert render._limit_row(3.0, 1.0, "{:.2f}%") == (
        "3.00% (this class) — looser than the 1.00% default"
    )
    assert render._limit_row(0.5, 1.0, "{:.2f}%") == "0.50% (this class)"
    assert render._limit_row(None, 1.0, "{:.2f}%") == "1.00% (portfolio default)"


def test_the_watchlist_is_not_a_trading_permission():
    """Growing the tape by extending `allowed_symbols` would quietly grant the
    bot nine new instruments to open positions in — a change to what may be
    traded, wearing the costume of a display tweak."""
    from bot.config import load_rules

    rules = load_rules()
    watch_only = set(rules.watchlist.symbols) - set(rules.allowed_symbols)

    assert watch_only, "the two lists have converged; the distinction is gone"
    assert "NVDA" in watch_only

    # And the reverse, which is the half that actually matters: the gate's list
    # is exactly the union of the enabled instrument classes, so nothing can
    # reach it by being added to the tape.
    from_instruments = {
        symbol
        for instrument in rules.enabled_instruments.values()
        for symbol in instrument.allowed_symbols
    }
    assert set(rules.allowed_symbols) == from_instruments


def test_the_command_console_survives_the_reduced_motion_bail_out():
    """Cmd+K is navigation, and navigation is not decoration.

    SCRIPT answers `prefers-reduced-motion` by returning on line one and doing
    none of the work — right for a starfield, wrong for the only keyboard route
    to every page. Somebody asking for less motion is asking for fewer moving
    pixels, not for a way around the site to be withdrawn.

    So the palette lives in a second closure, after the projection layer's, and
    this pins that arrangement: the bail-out must be shut before `openConsole`
    is defined. Found in a browser rather than here — the whole suite was green
    while reduced motion had no console at all, because a closure boundary is
    exactly the kind of thing a unit test does not see.
    """
    from bot.web.render import SCRIPT

    assert SCRIPT.count("if (reduced && reduced.matches) return;") == 1
    bail = SCRIPT.index("if (reduced && reduced.matches) return;")
    close = SCRIPT.index("})();", bail)
    # Anchored on the projection closure's own last statement rather than on a
    # count of closures, so adding a fourth does not make this pass or fail for
    # a reason that has nothing to do with what it guards.
    assert "api.settle = settleAll;" in SCRIPT[bail:close]
    assert close < SCRIPT.index("function openConsole")


def test_the_clocks_tick_regardless_of_the_motion_preference():
    """Same rule as the console, one layer out: a clock is information.

    Somebody asking for less motion still needs to know what time it is in New
    York. The spin-to-blur on a session boundary IS decoration, so it lives in
    the stylesheet where the reduced-motion block switches it off while the
    digits carry on."""
    from bot.web.render import SCRIPT, STYLES

    bail = SCRIPT.index("if (reduced && reduced.matches) return;")
    assert SCRIPT.index("})();", bail) < SCRIPT.index("var bar = document.querySelector")
    # Membership rather than the exact rule text: the selector list grew
    # when the spin gained directions, and pinning the whole rule fails on
    # any addition to it without saying anything about the preference.
    reduced_block = STYLES[STYLES.index("prefers-reduced-motion") :]
    assert ".tape .clk.turning .t" in reduced_block
    assert ".tape.turning" in reduced_block


def test_a_clock_that_is_not_ticking_says_so(client):
    """A frozen clock is the one plausible wrong figure a clock can be.

    Every other value here is a reading — true of a moment, labelled with that
    moment. A clock showing 08:51 at 09:30 is not an old reading, it is a wrong
    one, and it looks exactly like a right one. So the server renders the label
    and SCRIPT removes it before its first tick: present means stopped.
    """
    assert "not ticking" in client.get("/").text
    assert "if (stale && stale.parentNode) stale.parentNode.removeChild(stale);" in (
        render.SCRIPT
    )


def test_the_clock_states_the_venue_and_the_gate_separately(client):
    """"The market is open" and "this bot will trade" are different claims.

    They were confused once already, at 04:49 New York time on a Monday:
    Alpaca's pre-market genuinely was running, four and a half hours before the
    window `config/rules.yaml` permits. Merging them into one green light is
    how that happens again.
    """
    from datetime import datetime

    from bot.config import load_rules
    from bot.market_clock import MarketPhase, market_state

    windows = load_rules().instruments["us_equity"].windows_by_day

    # 20:30 UTC in August: the regular session shut at 20:00, the configured
    # window runs to 21:00. Both facts are true and the display must not pick.
    state = market_state(
        datetime(2026, 8, 10, 20, 30, tzinfo=UTC), windows_by_day=windows
    )
    assert state.phase is MarketPhase.POST
    assert state.bot_window_open is True
    assert state.is_tradeable_by_bot is False

    body = render.ticker_tape(state, [])
    assert "armed · orders rest until open" in body


def test_the_console_returns_focus_somewhere_reachable():
    """A palette that dismisses and leaves focus on the body strands a keyboard
    user at the top of the document with the whole page to tab back through.

    The shortcut is global, so it is usually pressed with nothing focused and
    `activeElement` is the body — and `body.focus()` silently does nothing,
    raising no error while the strand happens anyway. The restore is therefore
    checked rather than assumed, with the main region as the fallback."""
    from bot.web.render import SCRIPT

    assert "lastFocus !== document.body" in SCRIPT
    assert "document.activeElement === lastFocus" in SCRIPT


def test_the_sign_in_page_carries_no_live_targets(client):
    """The other half of the guard: if the gate ever grew a `data-live`
    attribute it would start opening an authenticated stream unauthenticated."""
    from bot.config import Env
    from bot.web.render import login_page

    env = Env(_env_file=None)  # type: ignore[call-arg]
    # The ATTRIBUTE, not the bare word: SCRIPT is inlined into this page and
    # contains the `[data-live]` selector it guards on, so a looser check
    # passes for the wrong reason.
    assert 'data-live="' not in login_page(env=env)


# ------------------------------------------------ the trading calendar card


def _settings_with(
    sessions_ahead: Sequence[live.SessionDayView] = (),
    *,
    loaded: bool = False,
    degraded: bool = False,
    poller_has_read: bool = False,
) -> str:
    return render.settings_page(
        load_rules(),
        _env(),
        chat_enabled=False,
        sessions_ahead=sessions_ahead,
        calendar_loaded=loaded,
        calendar_degraded=degraded,
        poller_has_read=poller_has_read,
    )


def test_an_unloaded_calendar_renders_as_unknown_not_as_an_empty_run():
    """The cold-start Board rule in a second place. A calendar nobody fetched
    and a quarter with no trading days are opposite findings, and a card that
    drew both as blank space would present the dangerous one silently."""
    html = _settings_with()

    assert "Trading calendar" in html
    assert "Not loaded" in html
    assert "assume an ordinary session" in html


def test_a_loaded_calendar_lists_the_sessions_and_marks_the_half_day():
    from bot.web.live import SessionDayView

    html = _settings_with(
        sessions_ahead=[
            SessionDayView(
                date="2026-11-25",
                label="Wed 25 Nov 09:30-16:00 New York",
                early_close=False,
            ),
            SessionDayView(
                date="2026-11-27",
                label="Fri 27 Nov 09:30-13:00 New York — EARLY CLOSE",
                early_close=True,
            ),
        ],
        loaded=True,
    )

    assert "Not loaded" not in html
    assert "2026-11-27" in html
    assert "EARLY CLOSE" in html
    assert "Shorter session than the usual" in html
    # The sentence that says why a half-day is worth a card at all.
    assert "bites quietly" in html


def test_a_stale_calendar_says_the_dates_are_unconfirmed():
    """It still renders the dates — they were published months ago and a failed
    refresh does not make them wrong — but it must not present them as this
    morning's reading."""
    html = _settings_with(loaded=True, degraded=True)

    assert "last refresh failed" in html
    assert "not confirmed current" in html


def test_the_calendar_card_is_not_keyed_by_symbol():
    """Every US equity on Alpaca shares one session. A per-symbol table would be
    N identical rows with N chances to drift apart, so the hours are keyed to
    the instrument class and the calendar sits beside them once."""
    from bot.config import load_rules
    from bot.web.live import SessionDayView

    html = _settings_with(
        sessions_ahead=[
            SessionDayView(date="2026-11-25", label="Wed 25 Nov", early_close=False)
        ],
        loaded=True,
    )
    # Bounded to the card itself. A fixed-width slice ran on into the
    # instrument cards below, which DO list symbols — and would have failed
    # this test for the wrong reason.
    end = "Alpaca trading calendar, cached."
    card = html[html.index("Trading calendar") : html.index(end)]

    for symbol in load_rules().instruments["us_equity"].allowed_symbols:
        assert symbol not in card


def test_the_calendar_card_does_not_guess_why_it_is_unloaded():
    """Two different facts, and the card said the wrong one.

    "The Board has not been opened" is a plausible cause and is wrong whenever
    the poller HAS read and the broker had no calendar to give — which is every
    mock deployment. Caught by loading the page, with the suite green.
    """
    never_read = _settings_with(poller_has_read=False)
    read_but_empty = _settings_with(poller_has_read=True)

    assert "has not read yet" in never_read
    assert "returned no calendar" not in never_read

    assert "returned no calendar" in read_but_empty
    assert "has not read yet" not in read_but_empty


# ------------------------------------------- the tape's three venue states


def test_a_pre_market_cell_is_neither_live_nor_greyed():
    """The state the tape did not have, and the reason it needed one.

    A pre-market quote is REAL — greying it like a Sunday close says the figure
    is stale, which is false. But an order against it rests until the open, so
    rendering it identically to a regular-session cell is false the other way.
    Three states because there are three claims.
    """
    from bot.market_clock import market_state

    pre = render.ticker_tape(
        market_state(datetime(2026, 8, 10, 12, 0, tzinfo=UTC)),   # 08:00 ET
        [_quote("SPY", last=580.0, prev=574.0, tradeable=True)],
    )
    session = render.ticker_tape(
        market_state(datetime(2026, 8, 10, 15, 0, tzinfo=UTC)),   # 11:00 ET
        [_quote("SPY", last=580.0, prev=574.0, tradeable=True)],
    )

    def spy_cell(markup: str) -> str:
        # Scoped to the cell. "shut" is now legitimately in the ASX and NZX
        # clock tooltips on the same strip, so searching the whole tape
        # tested something other than what this is about.
        return markup.split('data-tick="SPY"')[0].rsplit("<span", 1)[-1]

    assert "v-ooh" in spy_cell(pre)
    assert "shut" not in spy_cell(pre)   # the price is current, not last week's
    assert "v-live" in spy_cell(session)
    assert "v-ooh" not in spy_cell(session)
    # And the tooltip makes the distinction in words, not only in styling.
    assert "an order placed now rests" in pre


def test_the_venue_states_are_styled_apart_rather_than_by_degree():
    """`.v-ooh` must not just be a dimmer `.shut`. They make opposite claims
    about whether the FIGURE is current."""
    from bot.web.render import STYLES

    assert ".tape .cell.v-ooh::before" in STYLES
    assert ".tape .cell.shut .sym,.tape .cell.shut .px" in STYLES


def test_the_clock_transition_carries_a_direction():
    """The operator's idea, and it earns its keep: a blur alone says something
    changed and makes the reader hunt for what. Up into the regular session,
    down into a shut market, sideways into out of hours."""
    from bot.web.render import SCRIPT, STYLES

    for name in ("tape-spin-up", "tape-spin-down", "tape-spin-side"):
        assert f"@keyframes {name}" in STYLES
    for cls in ("turn-up", "turn-down", "turn-side"):
        assert f".tape .clk.{cls} .t" in STYLES
        assert cls in SCRIPT

    # An unknown phase must fall back to sideways rather than pick a direction.
    # A wrong direction is worse than a neutral one — it reads as information.
    assert "'up'" in SCRIPT and "'down'" in SCRIPT and "'side'" in SCRIPT


def test_reduced_motion_switches_off_the_directional_spins_too():
    """A preference honoured for the old animation and silently ignored for its
    replacements is not honoured."""
    from bot.web.render import STYLES

    block = STYLES[STYLES.index("prefers-reduced-motion") :]
    for cls in ("turn-up", "turn-down", "turn-side"):
        assert f".tape .clk.{cls} .t" in block


# ------------------------------------------------------- the watchlist mix


def test_the_watchlist_is_interleaved_so_neighbours_differ_in_kind():
    """Sixteen large caps scroll past as sixteen of the same thing. Rendering
    the kinds in declaration order would put the equities in a block and then
    the metals, which is the same monotony one scroll later."""
    w = load_rules().watchlist
    kinds = [w.kind_of(s) for s in w.symbols]

    assert len(w.symbols) == 16
    assert len(set(kinds)) >= 8
    # No two neighbours share a kind anywhere in the first pass.
    first_pass = kinds[: len(w.kinds)]
    assert len(set(first_pass)) == len(first_pass)


def test_a_symbol_under_no_kind_is_unclassified_rather_than_guessed():
    """Never inferred from the ticker. Same rule as an indicator with too few
    bars reporting unavailable instead of a shorter average mislabelled."""
    w = load_rules().watchlist

    assert w.kind_of("ZZZZ") == "unclassified"


def test_every_kind_has_its_own_colour_and_none_are_the_direction_colours():
    """Direction owns green and red on the price, the move and the rail. A kind
    hue that collided with either would make a bond look like a loss."""
    from bot.web.render import KIND_HUES

    w = load_rules().watchlist
    for kind in w.kinds:
        assert kind in KIND_HUES, f"{kind} would render in the fallback grey"
    assert len(set(KIND_HUES.values())) == len(KIND_HUES)
    for hue in KIND_HUES.values():
        assert "--gain" not in hue and "--loss" not in hue


def test_the_kind_rules_are_generated_from_the_palette():
    """A hand-maintained second list is how a kind gets added to the config and
    silently renders grey."""
    from bot.web.render import KIND_HUES, STYLES

    for kind in KIND_HUES:
        assert f'.tape .cell[data-kind="{kind}"] .sym' in STYLES


def test_each_clock_reports_its_own_exchange_not_new_yorks():
    """The single-global-session bug arriving through the interface.

    One "PRE-MARKET" pinned over the strip was a claim about every cell under
    it, and false for crypto trading right beside it. Each clock now answers
    for the exchange in ITS zone, and a zone with no exchange makes no claim at
    all rather than borrowing New York's.
    """
    from bot.market_clock import CLOCKS

    by_label = {f.label: f for f in CLOCKS}

    assert by_label["New York"].exchange == "NYSE"
    assert by_label["Tokyo"].exchange == "TSE"
    assert by_label["Sydney"].exchange == "ASX"
    assert by_label["Auckland"].exchange == "NZX"


def test_tokyo_is_shut_over_its_lunch_break():
    """The reason a face holds a TUPLE of sessions rather than one pair.

    The TSE breaks 11:30-12:30 JST. A single 09:00-15:30 window would paint it
    open through an hour it is shut — a plausible wrong figure on a strip whose
    only job is orientation, which is the failure this repository is built to
    refuse.
    """
    from bot.market_clock import CLOCKS

    tokyo = next(f for f in CLOCKS if f.code == "TYO")

    def at_jst(hour: int, minute: int = 0) -> datetime:
        # 2026-08-11 is a Tuesday. JST is UTC+9, no daylight saving.
        return datetime(2026, 8, 11, hour - 9, minute, tzinfo=UTC)

    assert tokyo.is_open(at_jst(11, 0)) is True     # morning session
    assert tokyo.is_open(at_jst(12, 0)) is False    # lunch
    assert tokyo.is_open(at_jst(13, 0)) is True     # afternoon session
    assert tokyo.is_open(at_jst(15, 45)) is False   # after the 15:30 close


def test_a_face_with_no_exchange_makes_no_claim_about_a_market():
    """Nothing on the strip uses this today — every clock trades somewhere —
    but the shape keeps it, because the alternative when a zone has no exchange
    is to borrow a neighbour's state, which is how a strip starts asserting a
    market is open when it is not."""
    from bot.market_clock import ClockFace

    nowhere = ClockFace("Nowhere", "UTC", code="NWH")

    assert nowhere.is_open(datetime(2026, 8, 10, 15, 0, tzinfo=UTC)) is None
    assert nowhere.state(datetime(2026, 8, 10, 15, 0, tzinfo=UTC)) is None


def test_the_exchanges_open_at_their_own_local_hours():
    """Sydney at 10:00 Sydney time, not at 10:00 New York time. The whole point
    of hanging the state off the clock is that the zones differ."""
    from bot.market_clock import CLOCKS

    by_label = {f.label: f for f in CLOCKS}
    # 2026-08-11 01:00 UTC = 11:00 Sydney (ASX open), 21:00 Mon New York (shut).
    moment = datetime(2026, 8, 11, 1, 0, tzinfo=UTC)

    assert by_label["Sydney"].is_open(moment) is True
    assert by_label["New York"].is_open(moment) is False


def test_a_weekend_shuts_every_exchange():
    from bot.market_clock import CLOCKS

    saturday = datetime(2026, 8, 15, 3, 0, tzinfo=UTC)   # Sat 13:00 Sydney
    for face in CLOCKS:
        assert face.is_open(saturday) in (False, None)


def test_the_global_session_banner_is_gone_from_the_tape():
    """It said nothing the strip did not already say better, and what it did
    say was wrong for crypto."""
    from bot.market_clock import market_state

    # With the real windows, so the gate genuinely IS open here — without them
    # `market_state` reports the cautious reading and the verdict would be
    # "bot idle" for a reason that has nothing to do with what is being tested.
    windows = load_rules().instruments["us_equity"].windows_by_day
    body = render.ticker_tape(
        market_state(
            datetime(2026, 8, 10, 12, 0, tzinfo=UTC), windows_by_day=windows
        ),
        [_quote("SPY", last=580.0, prev=574.0, tradeable=True)],
    )
    fixed = body.split('<div class="view">')[0]

    assert "Pre-market" not in fixed
    # The gate verdict stays: RiskGate IS account-wide, so it is true of the
    # bot whichever instrument you are looking at. In plain words now — the
    # middle state is exactly the session work's point, so it says it.
    assert "armed · orders rest until open" in fixed


def test_the_tape_is_a_band_rather_than_the_same_colour_as_the_page():
    """Three surfaces at the same value is one surface. The tape was `--ink`,
    the body is `--ink`, and the header is within a hair of it — so the strip
    dissolved into the dark field and took the clocks with it."""
    from bot.web.render import STYLES

    tape_rule = STYLES[STYLES.index(".tape{") : STYLES.index(".tape .fixed{")]

    assert "background:var(--graphite)" in tape_rule
    assert "background:var(--ink)" not in tape_rule
    assert "border-top" in tape_rule and "border-bottom" in tape_rule


# ------------------------------------------- browser-audited UI defects
#
# Every test below pins the SHAPE of a fix that was found by driving the deck
# in a real browser, because none of them was visible from here. A unit test
# cannot see a scrollbar, cannot watch a banner outlive its cause and cannot
# tell that an overlay crossfaded across a password field — so what is asserted
# is the rule or the markup that produced the behaviour, and the behaviour
# itself was re-measured in Chromium after the change.


def _css_blocks(selector_fragment: str) -> list[str]:
    """Every declaration block whose selector list names this fragment.

    Crude on purpose: `STYLES` is one string, and the alternative is a CSS
    parser as a test dependency. Good enough for a flat rule, and every rule it
    is pointed at below is flat.
    """
    out: list[str] = []
    for chunk in render.STYLES.split("}"):
        if "{" not in chunk:
            continue
        selector, _, decls = chunk.partition("{")
        if selector_fragment in selector:
            out.append(decls)
    return out


def test_a_scroll_container_has_no_bracket_hanging_past_its_own_edge():
    """The operator's "weird scrolling stuff", in one declaration.

    `.scroll` is `overflow-x:auto`, which in CSS makes the computed `overflow-y`
    `auto` as well — the box is a scroll container on BOTH axes. End-direction
    overflow from an absolutely positioned descendant is scrollable overflow
    rather than clipped decoration, so the HUD bracket at `bottom:-1px;
    right:-1px` gave every table on the deck exactly 1px of scroll range each
    way: two full-length scrollbars per table, six on Analytics, the first notch
    of any wheel gesture over a table eaten moving it one pixel, and a keyboard
    tab stop with no `tabindex` — Chrome makes a scroll container focusable when
    nothing inside it is.

    Measured before: `scrollWidth 1239 / clientWidth 1238`. After: equal.
    """
    for fragment in (".scroll::after", ".chat .log::after"):
        blocks = _css_blocks(fragment)
        assert blocks, fragment
        for decls in blocks:
            assert "bottom:-1px" not in decls, fragment
            assert "right:-1px" not in decls, fragment
        assert any("bottom:0" in d and "right:0" in d for d in blocks), fragment

    # The start-direction pair moves with it. `top:-1px` adds no scrollable
    # overflow — start overflow is clipped — but it IS clipped, so leaving it
    # would draw one corner of the same box a pixel short of the other.
    for fragment in (".scroll::before", ".chat .log::before"):
        for decls in _css_blocks(fragment):
            assert "top:-1px" not in decls, fragment

    # The other members of the shared rule are NOT scroll containers and keep
    # the bracket overhanging the border, which is what it is drawn for.
    assert any("bottom:-1px" in d for d in _css_blocks(".card::after"))


def test_the_fix_is_not_to_stop_a_wide_table_scrolling_sideways():
    """A table wider than the deck genuinely needs `overflow-x`. Removing it
    would answer the phantom scrollbars by making a real one impossible."""
    assert any("overflow-x:auto" in d for d in _css_blocks(".scroll"))


def test_a_table_header_does_not_claim_a_stickiness_it_cannot_have():
    """`th{position:sticky;top:0}` could never once have fired.

    Sticky resolves against the nearest SCROLLPORT, which is `.scroll` rather
    than the viewport, and `.scroll` is sized by its content — so there is no
    vertical range to stick within. Measured in Chromium: the header's offset
    from its wrapper stayed at exactly 1px through a full page scroll.

    Making it work needs a `max-height` on `.scroll`, which puts an inner scroll
    region back on every table. A property that cannot work reads like a feature
    to the next person, so it goes rather than staying as decoration.
    """
    styles = render.STYLES
    start = styles.index("\nth{text-align:left")
    rule = styles[start : styles.index("}", start)]

    assert "position:sticky" not in rule
    assert "top:0" not in rule
    # The header ground stays: it is what separates the labels from the rows.
    assert "background:var(--graphite)" in rule


def test_the_tapes_second_run_is_addressable_and_hidden_from_a_screen_reader():
    """The marquee emits the run twice so the loop is seamless. Two bugs.

    Under `prefers-reduced-motion` the strip becomes a manual scroller and the
    duplicate is simply exposed — 32 cells for 16 instruments, 8 clocks for 4 —
    and nothing could hide it because nothing could address it. And in EVERY
    mode a screen reader has been reading the whole watchlist twice, for a
    duplicate that exists to make a translation loop seamless.
    """
    from bot.market_clock import market_state

    body = render.ticker_tape(
        market_state(datetime(2026, 8, 10, 15, 0, tzinfo=UTC)),
        [_quote("SPY", last=580.0, prev=574.0)],
    )

    assert body.count('<div class="marquee-run">') == 1
    assert body.count('<div class="marquee-run dup" aria-hidden="true">') == 1
    # Still twice, or the marquee snaps back at every loop.
    assert body.count('data-tick="SPY"') == 2

    # -50% still lands the second copy where the first began, which needs the
    # two wrappers to be equal-width flex rows rather than shrinking items.
    # Named `marquee-run`, not `run`: `.fx-sweep.run` already uses that word as
    # a state, so a bare `.run` layout rule would restyle the sweep element too.
    assert any(
        "flex:none" in d for d in _css_blocks(".tape .track > .marquee-run")
    )

    opener = "@media (prefers-reduced-motion:reduce){"
    block = render.STYLES[render.STYLES.index(opener) :]
    block = block[: block.index("\n}")]
    assert ".tape .track > .marquee-run.dup{display:none}" in block


def _order(
    *,
    direction: Direction = Direction.BUY,
    qty: float = 21,
    limit_price: float | None = None,
    stop_price: float | None = None,
    order_type: str = "",
    filled_qty: float = 0.0,
) -> WorkingOrder:
    from bot.models import OrderStatus

    return WorkingOrder(
        order_id="o-1",
        symbol="SPY",
        direction=direction,
        qty=qty,
        limit_price=limit_price,
        stop_price=stop_price,
        order_type=order_type,
        status=OrderStatus.NEW,
        submitted_at=ENTRY,
        filled_qty=filled_qty,
    )


def _rows(body: str) -> str:
    return body.split("<tbody>")[1]


def test_a_resting_stop_leg_shows_its_trigger_and_is_never_called_market():
    """The operator's third rule, made visible.

    Every entry is a GTC bracket now, so the stop leg protecting a live position
    rests at the broker for as long as the position is open. It has no
    `limit_price`, and the price cell branched on `limit_price` alone — so the
    live SPY stop at 820 rendered as **market**, and 820 appeared nowhere on the
    deck. A stop is not a market order; it becomes one only if it triggers.
    """
    body = render._working_orders(
        [_order(stop_price=820.0, order_type="stop")], {"SPY": 773.10}
    )
    rows = _rows(body)

    assert "820.0000" in rows
    assert "stop" in rows
    # "Market" survives as a column label; the lowercase word was the lie.
    assert "market" not in rows


def test_a_readable_stop_and_an_unreadable_one_do_not_render_identically():
    """`WorkingOrder` carries `order_type` beside `stop_price` precisely so
    "this is a limit order and correctly has no stop" can be told from "this is
    the leg rule 3 depends on and nobody can read its level". Both used to print
    the same word."""
    prices = {"SPY": 773.10}
    known = render._working_orders(
        [_order(stop_price=820.0, order_type="stop")], prices
    )
    unknown = render._working_orders([_order(order_type="stop")], prices)

    assert known != unknown
    # Loud, not muted: this is a value that should exist and does not.
    assert '<span class="alert">unknown</span>' in _rows(unknown)
    assert '<span class="alert">' not in _rows(known)
    assert "market" not in _rows(unknown)


def test_a_stop_with_no_type_reported_is_still_not_called_market():
    """An order with no limit and no stated type is a gap in what was read
    back. Printing "market" would be a confident answer to a question nobody
    asked the broker."""
    rows = _rows(render._working_orders([_order()], {"SPY": 773.10}))

    assert "market" not in rows
    assert "unknown" in rows


def test_market_is_still_said_when_the_broker_actually_says_market():
    rows = _rows(
        render._working_orders([_order(order_type="market")], {"SPY": 773.10})
    )

    assert "market" in rows


def test_the_distance_to_a_stop_is_the_mirror_of_the_distance_to_a_limit():
    """A buy limit rests BELOW the market and a buy stop triggers ABOVE it.

    Reusing `distance_to_fill` for a stop leg would report the right magnitude
    with the wrong sign, and a stop 6% away from firing would read as one that
    should already have gone. "Needs" said `n/a` for every stop leg before this.
    """
    buy_stop = render._order_gap(_order(order_type="stop", stop_price=820.0), 773.10)
    sell_stop = render._order_gap(
        _order(direction=Direction.SELL, order_type="stop", stop_price=700.0),
        773.10,
    )
    limit = render._order_gap(_order(limit_price=641.20), 648.02)

    assert buy_stop is not None and buy_stop > 0
    assert sell_stop is not None and sell_stop > 0
    assert limit is not None and limit > 0

    # Through the trigger is the `stop_watch` case and must not be muted away.
    through = render._working_orders(
        [_order(order_type="stop", stop_price=770.0)], {"SPY": 773.10}
    )
    assert "through the trigger" in through
    assert "alert" in _rows(through)


def test_the_pending_orders_caption_no_longer_claims_limit_orders_only():
    """It was true when nothing sent a stop to the broker. Every entry is a
    bracket now, so a stop leg always rests there."""
    body = render._working_orders([_order(limit_price=641.20)], {"SPY": 648.02})
    # `<caption` rather than `<caption>`: it carries an id now, because it is
    # what names the scroll region around it via `aria-labelledby`.
    caption = body[body.index("<caption") : body.index("</caption>")]

    assert "stop leg" in caption
    assert "limit orders only" not in (render._working_orders.__doc__ or "")


def test_a_value_and_its_qualifier_travel_inside_one_element():
    """Under 760px each `td` is a `space-between` flex row with the label
    injected as `::before`, so a bare "21" and a bare "(2 filled)" are two flex
    items and land at opposite ends of the card with the label between them —
    one figure rendered as two fields."""
    body = render._working_orders(
        [_order(limit_price=641.20, filled_qty=2)], {"SPY": 648.02}
    )

    assert '<td data-l="Qty" class="r num"><span>21' in body
    assert "(2 filled)</span></span></td>" in body
    # The rule that makes it matter, so this test fails if the layout changes
    # underneath it rather than passing for the wrong reason.
    assert "justify-content:space-between" in render.STYLES


def test_a_position_row_keeps_its_risk_and_its_percentage_together():
    from bot.models import AccountSnapshot, Position

    account = AccountSnapshot(
        equity_usd=100_000.0,
        cash_usd=100_000.0,
        buying_power_usd=200_000.0,
        open_positions=[
            Position(
                symbol="SPY",
                direction=Direction.SELL,
                qty=21,
                entry_price=773.324285,
                opened_at=ENTRY,
                current_price=773.10,
            )
        ],
    )

    body = render._positions(account, [], 100_000.0)

    assert '<td data-l="At risk" class="r num"><span>' in body
    assert "</span></span></td>" in body


def test_the_not_current_banner_can_be_taken_away_by_the_stream(journal, dreams):
    """It never cleared. Measured: forty-five seconds and eleven stream messages
    later the page still said its figures were not current, while the four tiles
    above the sentence repainted every five seconds.

    A warning that outlives its cause teaches an operator to ignore the next
    one — the same reasoning that put `RECHECK_COMMAND` on the tailnet banner.
    """
    body = _aged_client(journal, dreams, hours=8).get("/").text

    assert f'id="{render.STALE_BANNER_ID}"' in body
    assert "These figures are not current" in body
    assert f"getElementById('{render.STALE_BANNER_ID}')" in render.SCRIPT
    assert "staleBanner.remove()" in render.SCRIPT
    # Only on a reading that is provably fresh. The stream must not retract a
    # warning it has not disproved.
    assert "if (!data.stale) {" in render.SCRIPT


def test_the_banner_ids_the_script_uses_are_the_ids_the_server_writes():
    """`SCRIPT` is a plain string and interpolating into it is how the
    `{field: "close"}` trap got into `SYSTEM_PROMPT_TEMPLATE`. So the ids are
    repeated as literals, and this is what keeps the two copies together."""
    for banner_id in (render.STALE_BANNER_ID, render.COLD_START_BANNER_ID):
        assert f"getElementById('{banner_id}')" in render.SCRIPT


def test_the_cold_start_board_stamp_is_live_and_the_page_fetches_the_rest():
    """One screen said three separate times that it had no figures, directly
    above four of them.

    `_board_waiting` passed no `asof_live`, so the stamp carried no
    `data-live-read`, `paintStamp` returned on its first line, and "not read
    yet" stood there while the tiles repainted with real equity and real open
    risk. And a cold-start Board renders NO sections — the stream can only
    repaint what the server already rendered, so the positions, the resting
    orders and the risk meters could never arrive at all.
    """
    body = render.board(None, load_rules(), [], [], StandDownState(), 0)

    assert "not read yet" in body
    assert "data-live-read" in body
    assert f'id="{render.COLD_START_BANNER_ID}"' in body
    # The sections are genuinely absent, which is why a reload is the fix
    # rather than a nicety.
    assert "Open positions" not in body
    assert "window.location.reload()" in render.SCRIPT
    # One-shot, or every message inside the delay queues another reload.
    assert "coldReloading" in render.SCRIPT


def test_a_cold_start_still_renders_unknown_rather_than_zero():
    """The half that was already right, pinned so the reload cannot take it
    away. `0.00` equity would be a plausible wrong figure."""
    body = render.board(None, load_rules(), [], [], StandDownState(), 0)

    assert 'class="pending"' in body
    assert "it is unknown" in body


def test_the_palette_hands_focus_back_without_scrolling_the_page():
    """Focusing an element scrolls it into view, and the element focus came
    from is frequently not the one the reader is looking at. Measured: Escape
    from the palette jumped the Board 108px, pushing the header and the tape off
    screen — 872px with a table focused first. Closing a palette is not a
    request to go anywhere."""
    assert "lastFocus.focus({ preventScroll: true })" in render.SCRIPT
    assert "main.focus({ preventScroll: true })" in render.SCRIPT


def test_the_sign_in_panel_is_part_of_the_projection_layer():
    """It was the only content on any page the boot overlay crossfaded ON TOP
    of rather than into: an opaque z-60 overlay printing four status lines and a
    second wordmark straight across "OPERATOR SIGN-IN", the password label and
    the input. It already carries the same bracket rules as every other member
    of the set, so it was plainly meant to be one."""
    assert ".signin .panel" in render.SCRIPT


def test_hiding_the_sign_in_panel_still_needs_the_script_to_have_said_so():
    """Fail-to-visible, unchanged. Hiding takes BOTH `html.fx-ready` and the
    per-element class, added together in one synchronous block, so a throw or a
    blocked file leaves a fully usable form. The obvious arrangement — hide in
    CSS, reveal in JS — fails to a blank sign-in page."""
    blocks = _css_blocks(".fx-panel")
    assert blocks
    for chunk in render.STYLES.split("}"):
        selector, _, decls = chunk.partition("{")
        if ".fx-panel" in selector and "opacity:0" in decls:
            assert "fx-ready" in selector


def test_a_count_agrees_with_its_noun():
    """"1 qualifying loss(es) in a row", "only 1 trades so treat as noise",
    "Median chain 1 — hops". `(s)` is the same shrug written down. This deck's
    whole argument is that its figures are careful, and a reader who catches it
    being sloppy about a word cannot know it is not sloppy about a number."""
    assert render._count(1, "qualifying loss", "qualifying losses") == (
        "1 qualifying loss"
    )
    assert render._count(3, "qualifying loss", "qualifying losses") == (
        "3 qualifying losses"
    )
    assert render._count(0, "position") == "0 positions"
    assert render._count(1, "position") == "1 position"
    assert render._word(1, "hop") == "hop"
    assert render._word(2.5, "hop") == "hops"


def test_no_rendered_string_still_carries_the_bracketed_plural():
    """The helper exists so this can be asserted once rather than string by
    string."""
    from bot.metrics import PerformanceSummary

    body = render.board(None, load_rules(), [], [], StandDownState(), 1)
    assert "(s)" not in body
    assert "(es)" not in body

    assert "1 trade so treat as noise" in PerformanceSummary(
        trade_count=1, profit_factor=1.2
    ).health
    assert "across 1 trade" in PerformanceSummary(
        trade_count=1, profit_factor=None
    ).health


def test_no_block_comment_terminator_stands_outside_a_comment():
    """`SCRIPT` and `STYLES` are Python strings, so nothing checks their syntax.

    This caught a real one, and only a browser had: a paragraph appended to an
    existing comment landed AFTER its `*/`, so seven lines of English prose
    became statements and the whole script failed to parse with `Unexpected
    identifier 'on'`. Every page still rendered — the projection layer is built
    to fail to visible — so the symptom was the stream, the Cmd+K palette and
    the starfield all silently not existing, with 1,050 tests, `ruff` and `mypy`
    green throughout.

    Same family as the `render.STYLES` backslash trap and the unescaped brace in
    `SYSTEM_PROMPT_TEMPLATE`: a Python string that is really another language,
    with nothing on the Python side that can tell.
    """
    for name, src in (("SCRIPT", render.SCRIPT), ("STYLES", render.STYLES)):
        inside = False
        strays = []
        i = 0
        while i < len(src) - 1:
            pair = src[i : i + 2]
            if inside:
                if pair == "*/":
                    inside = False
                    i += 2
                    continue
            elif pair == "/*":
                inside = True
                i += 2
                continue
            elif pair == "*/":
                strays.append((src.count("\n", 0, i) + 1, src[i - 70 : i + 40]))
                i += 2
                continue
            i += 1

        assert not strays, f"{name}: comment terminator outside a comment: {strays}"
        assert not inside, f"{name}: unterminated block comment"


def test_the_browser_script_parses_as_javascript():
    """The stronger half of the test above, when a JS engine happens to be on
    the box. Skipped rather than made a dependency: the balance check is what
    always runs, and this is what proves it is enough."""
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if node is None:  # pragma: no cover - depends on the machine
        pytest.skip("no node on this box; the comment-balance check still ran")

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(render.SCRIPT)
        path = fh.name
    done = subprocess.run([node, "--check", path], capture_output=True, text=True)

    assert done.returncode == 0, done.stderr


# --------------------------------------- accessibility and platform integration
#
# The section above pins fixes found by driving the deck in a browser. These pin
# fixes found by MEASURING it — contrast ratios computed rather than eyeballed,
# and markup checked against what a screen reader or a keyboard actually gets.
# Same reason for existing: none of it is visible from a rendered page, and a
# palette edit or one forgotten attribute puts it back in silence.


def _relative_luminance(hex_colour: str) -> float:
    """WCAG 2.1 relative luminance. Six-digit hex only, which is all `:root` has."""
    raw = hex_colour.lstrip("#")
    channels = [int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    la, lb = _relative_luminance(a), _relative_luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def _token(name: str) -> str:
    """The value of a `--custom-property` as `:root` actually declares it.

    Read out of `STYLES` rather than restated here, so this measures the
    stylesheet that ships instead of a copy of it that can drift.
    """
    marker = f"--{name}:"
    start = render.STYLES.index(marker) + len(marker)
    return render.STYLES[start : render.STYLES.index(";", start)].strip()


def test_the_contrast_ratios_that_were_measured_stay_measured():
    """Three failures found by computing the ratios, not by looking.

    `--rust` at 3.48:1 on graphite was the label colour of the CRITICAL banner —
    11px uppercase mono at .14em tracking, which is small text needing 4.5:1. The
    most severe state on the deck had the least readable heading, which is a
    warning that did not happen. The border keeps `--rust`, because a 3px rail is
    a non-text boundary and 3:1 is the bar it has to clear.

    An inline link was `--bone` — the body colour — underlined in `--slate` at
    1.47:1 on ink. Neither channel distinguished it from the prose around it.

    `--pewter` cleared 4.5:1 at 4.81:1, on a formula that does not model reverse
    polarity and is known to overstate contrast on a dark ground, at 10px. Lifted
    once rather than auditing the ~40 rules that read it.

    WCAG 2 numbers, deliberately: they are the ones that can be computed
    deterministically today. APCA is a WCAG 3 draft and is the argument for
    headroom, not the acceptance test.
    """
    ink, graphite = _token("ink"), _token("graphite")

    # Text on the panel ground, so the small-text bar applies: 4.5:1.
    assert _contrast(_token("rust-text"), graphite) >= 4.5
    assert _contrast(_token("pewter"), graphite) >= 4.5
    # The link underline is the only channel saying "link", so it has to clear
    # the 3:1 that applies to a meaningful non-text mark, and it clears 4.5.
    assert _contrast(_token("pewter"), ink) >= 4.5
    # The border rust is a rail, not text. 3:1, and it is allowed to stay where
    # it is — splitting the token is what let the text half move.
    assert _contrast(_token("rust"), graphite) >= 3.0

    # The two are different colours, or the split has been undone by a tidy-up.
    assert _token("rust") != _token("rust-text")


def test_the_critical_banner_label_uses_the_text_weight_rust():
    """The one place the split actually has to land. `--rust` on this element is
    3.48:1 at 11px; it is the most severe state the deck can show."""
    assert ".banner.crit b{color:var(--rust-text)}" in render.STYLES
    # And the border it is paired with keeps the darker one, which is correct
    # for a 3px rail and is what makes the two tokens worth having.
    assert ".banner.crit{border-left-color:var(--rust)}" in render.STYLES


def test_an_inline_link_is_distinguishable_from_the_prose_around_it():
    """Body-coloured text with a 1.47:1 underline is not a link by any channel.

    WCAG 1.4.1 failing in the direction where there is no colour difference
    either, so there was nothing left to fall back on.
    """
    # The bare `a` rule specifically, not `a:hover` and not `nav a` — the
    # selector has to END in a standalone `a` or this is measuring a different
    # declaration and would keep passing after the one that matters regressed.
    rules = [
        decls
        for chunk in render.STYLES.split("}")
        if "{" in chunk
        for selector, _, decls in [chunk.partition("{")]
        if selector.rsplit("*/", 1)[-1].strip() == "a"
    ]
    assert len(rules) == 1, rules
    rule = rules[0]

    assert "text-decoration-color:var(--pewter)" in rule
    assert "text-underline-offset" in rule
    # 1.47:1 on ink. An underline nobody can see is not a second channel.
    assert "var(--slate)" not in rule


def test_the_document_tells_the_platform_it_is_dark():
    """`color-scheme` is the only thing that reaches the chrome the platform
    draws itself: the chat `<textarea>`, the sign-in password field, every
    scrollbar, `::selection`, and the paint that happens BEFORE this stylesheet
    applies. Without it a phone on a slow link flashes white and a white input
    sits inside a graphite panel, with the stylesheet entirely correct."""
    assert "color-scheme:dark" in render.STYLES

    for markup in (
        render.shell("Board", "/", "", env=_env()),
        render.login_page(env=_env()),
    ):
        # Paints the browser's own chrome to match the deck.
        assert '<meta name="theme-color" content="#0B0E12">' in markup
        # Not a layout preference: this is the precondition for
        # `env(safe-area-inset-*)` resolving to anything but zero, and without it
        # the safe-area padding below is dead code on the hardware that needs it.
        assert "viewport-fit=cover" in markup

    assert "env(safe-area-inset-bottom" in render.STYLES
    assert "env(safe-area-inset-left" in render.STYLES
    # iOS inflates text in a rotated viewport, and what it inflates hardest is a
    # wide table — which is every table here, in columns aligned on purpose.
    assert "text-size-adjust:100%" in render.STYLES


def test_a_horizontal_table_scroll_does_not_drag_the_page_with_it():
    """The second cause of the operator's "weird scrolling stuff", and a
    different one from the 1px bracket overflow already fixed.

    A touch drag that reaches the end of a scrolled box CHAINS into the nearest
    scrollable ancestor, which is the document — so swiping a wide table sideways
    on a phone walks the whole deck, and pulling inside the chat log bounces the
    page.
    """
    assert any(
        "overscroll-behavior:contain" in d for d in _css_blocks(".scroll,.chat .log")
    )


# ------------------------------------------------------------ table semantics


def _table_markup(journal: Journal) -> list[tuple[str, str]]:
    """Every piece of markup in the repository that emits a `<th>` or a
    `.scroll`, named so a failure says which one.

    Built from the render functions directly rather than from a page, because
    the Board's tables need a broker holding a position and the point here is
    the markup, not the plumbing.
    """
    from bot.metrics import build_report
    from bot.models import AccountSnapshot, OrderStatus, Position

    account = AccountSnapshot(
        equity_usd=100_000.0,
        cash_usd=50_000.0,
        buying_power_usd=100_000.0,
        open_positions=[
            Position(
                symbol="SPY",
                direction=Direction.SELL,
                qty=21,
                entry_price=773.324285,
                opened_at=ENTRY,
                current_price=770.0,
                unrealised_pnl_usd=69.81,
            )
        ],
    )
    order = WorkingOrder(
        order_id="o-1",
        symbol="SPY",
        direction=Direction.BUY,
        qty=21,
        limit_price=641.20,
        stop_price=None,
        order_type="limit",
        status=OrderStatus.NEW,
        submitted_at=ENTRY,
        filled_qty=0.0,
    )

    _closed_trade(journal, 200.0, minutes=0, mae=-40.0, mfe=300.0)
    _closed_trade(journal, -100.0, minutes=120)
    trades = journal.closed_trades()
    report = build_report(trades)

    inputs = MarketInputs(indicators={"SPY": "close 580.12, sma20 574.30, atr 6.41"})
    entry = _decision(rationale="Nothing met the conditions.", inputs=inputs)

    return [
        ("pending orders", render._working_orders([order], {"SPY": 648.02})),
        ("pending orders, empty", render._working_orders([], {})),
        ("positions", render._positions(account, trades, 100_000.0)),
        ("positions, empty", render._positions(
            account.model_copy(update={"open_positions": []}), [], 100_000.0
        )),
        ("trades", render.trades_page(trades, report)),
        ("analytics", render.analytics_page(report)),
        ("what it read", render._read(_entry_for(entry))),
    ]


def _entry_for(decision: Decision) -> DecisionEntry:
    return DecisionEntry(timestamp=decision.timestamp, decision=decision)


def test_every_column_header_names_its_scope(journal):
    """Twenty column headers, zero `scope`, across four `<thead>` blocks.

    On a multi-column financial table that is the difference between hearing
    "773.32" and hearing "SPY, Entry, 773.32". A `<th>` in a single header row is
    usually inferred correctly, but inference is not the guarantee, and one of
    these tables emits a two-column body with no header row at all — so the ones
    that DO have headers had better say what they head.
    """
    import re

    for name, markup in _table_markup(journal):
        for tag in re.findall(r"<th\b[^>]*>", markup):
            assert "scope=" in tag, f"{name}: {tag}"


def test_every_scroll_region_is_named_and_reachable(journal):
    """`.scroll` is `overflow-x:auto`, so it is a scroll container — and a
    keyboard user on Safari or Firefox could not reach one, because neither
    makes such a container focusable on its own.

    The tab stop is DELIBERATE, and it is not the junk one that came from 1px of
    phantom bracket overflow: that one had no name and no reason. Whether any
    given wrapper actually scrolls depends on the viewport, which the server
    cannot see — the same table scrolls on a phone and does not on the deck — so
    the stop is unconditional, and every one of them is named.
    """
    import re

    seen = 0
    for name, markup in _table_markup(journal):
        for tag in re.findall(r'<div class="scroll"[^>]*>', markup):
            seen += 1
            assert 'tabindex="0"' in tag, f"{name}: {tag}"
            assert 'role="region"' in tag, f"{name}: {tag}"

            label = re.search(r'aria-labelledby="([^"]+)"', tag)
            if label:
                # The name has to point at something that is actually there, or
                # the region is anonymous and the tab stop IS junk.
                assert f'id="{label.group(1)}"' in markup, f"{name}: {tag}"
            else:
                assert re.search(r'aria-label="[^"]+"', tag), f"{name}: {tag}"

    assert seen >= 7, "a scroll wrapper stopped being covered by this test"


def test_a_right_aligned_cell_gets_tabular_figures_without_being_asked():
    """The alignment a reader scans a column of money by was held by discipline:
    every `.r` cell also carries `.num`, which supplies the tabular figures. One
    forgotten `num` puts a column on proportional digits, the decimal points stop
    lining up, and nothing anywhere reports it."""
    assert any("font-variant-numeric:tabular-nums" in d for d in _css_blocks("td.r"))


def test_a_coloured_figure_always_carries_its_sign(journal):
    """Colour is never the only channel, and the sign is the other one.

    `_cls()` puts `pos` or `neg` on a cell and the stylesheet colours it. A cell
    added with `_cls(...)` and a plain `_money(...)` would be green or red and
    nothing else — invisible to a colour-blind reader, to a monochrome screenshot
    and to a screen reader alike.

    The sign is preferred to a CSS pseudo-element arrow deliberately: `content`
    is announced by several screen readers and lands in copy-paste in some
    browsers, so an arrow would add a stray glyph to a figure that already has an
    accessible channel.
    """
    import re

    checked = 0
    for name, markup in _table_markup(journal):
        for tag, inner in re.findall(
            r'(<(?:td|b)\b[^>]*class="[^"]*\b(?:pos|neg)\b[^"]*"[^>]*>)(.{0,120}?)'
            r"</(?:td|b)>",
            markup,
            re.S,
        ):
            checked += 1
            assert "+" in inner or "-" in inner, f"{name}: {tag}{inner}"

    assert checked, "no coloured figure rendered; the fixture stopped exercising one"


def test_the_marquee_is_named_reachable_and_read_once():
    """Three separate things, all about the same strip.

    The run is emitted twice so `translateX(-50%)` loops seamlessly, which is a
    statement about pixels and about nothing a non-visual reader is being told —
    so exactly one copy is in the accessibility tree.

    `role="marquee"` carries an implicit `aria-live="off"`, which is what these
    cells need: they repaint every few seconds and announcing that would never
    stop. The role REQUIRES an accessible name, so the label is load-bearing
    rather than decoration.

    `tabindex="0"` is the reduced-motion half. With the animation off the strip
    becomes an ordinary horizontal scroller, and neither Safari nor Firefox makes
    a scroll container focusable on its own — the whole watchlist would be
    mouse-only in the mode chosen by somebody asking for less movement.
    """
    import re

    from bot.market_clock import market_state

    body = render.ticker_tape(
        market_state(datetime(2026, 8, 10, 15, 0, tzinfo=UTC)),
        [_quote("SPY", last=580.0, prev=574.0)],
    )

    view = re.search(r'<div class="view"[^>]*>', body)
    assert view, body[:400]
    assert 'role="marquee"' in view.group(0)
    assert re.search(r'aria-label="[^"]+"', view.group(0)), view.group(0)
    assert 'tabindex="0"' in view.group(0)

    # One copy read, one copy hidden. Both halves, or the marquee either snaps
    # back visually or is read twice.
    assert body.count('<div class="marquee-run">') == 1
    assert body.count('aria-hidden="true"') == 1

    # `aria-hidden` must never wrap a focusable element — a keyboard user can
    # reach one and a screen reader cannot describe where they landed. The cells
    # are spans today and the tooltip is a `title` attribute, so this holds; it
    # would silently stop holding the day a cell becomes a button.
    hidden = body[body.index('<div class="marquee-run dup"') :]
    for focusable in ("<a ", "<a>", "<button", "tabindex", "<input", "<select"):
        assert focusable not in hidden, focusable

    # The outline is inset on this element alone. `:focus-visible` draws at +3px
    # everywhere else and `.tape` is `overflow:hidden`, so an outside ring here
    # is clipped away to nothing.
    assert any(
        "outline-offset:-3px" in d for d in _css_blocks(".tape .view:focus-visible")
    )


def test_a_section_the_stream_cannot_repaint_carries_its_own_reading(
    journal, dreams, client
):
    """One stamp was describing two different readings at once.

    Four elements on the Board carry `data-live`. The positions table, the
    resting orders, the risk meters and the equity curve are server-rendered and
    cannot be repainted — the client may only update a figure the server already
    put on the page. So after the first stream message the stamp under the title
    described the TILES while sitting above tables from an older reading, with
    nothing on screen saying which was which.

    The fix is not a vaguer stamp. Each section that cannot refresh itself says
    which reading it was built from, and says that it will not move.
    """
    body = client.get("/").text

    assert body.count("This section does not repaint") == 2
    assert "Reload for a newer one." in body
    # Fixed on purpose: the whole claim is that this one does NOT change, so it
    # must not be marked for the stream.
    for chunk in body.split("This section does not repaint"):
        assert not chunk.endswith("data-live-read")

    positions = body.index("<h2>Open positions</h2>")
    orders = body.index("<h2>Pending orders</h2>")
    for start in (positions, orders):
        assert "does not repaint" in body[start : start + 700]


def test_the_fixed_reading_note_says_unknown_rather_than_guessing():
    """A caller that cannot say when the broker was read must not be handed a
    time by default. Same rule as the stamp it sits under."""
    assert "time is unknown" in render._fixed_reading_note(None)
    assert "taken 04 May 2026" in render._fixed_reading_note(ENTRY)
