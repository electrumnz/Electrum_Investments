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
from typing import Any

import pytest

from bot.audit import DecisionEntry
from bot.config import Env, load_rules
from bot.dreaming import (
    ConferenceDecision,
    ConferenceVerdict,
    Dream,
    DreamCondition,
    DreamStore,
    DreamSummary,
    Vault,
)
from bot.journal import Journal
from bot.models import (
    AccountSnapshot,
    Decision,
    Direction,
    MarketInputs,
    OrderProposal,
    OrderResult,
    Position,
    PositionAction,
    PositionPlan,
    RiskVerdict,
    Stance,
    StandDownState,
    SymbolAssessment,
    Trade,
    TriggerField,
    TriggerOp,
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
    body = client.get("/trades").text
    assert "Nothing has closed yet" in body
    # And says where the open ones are, rather than leaving an empty page to
    # read as "there is nothing anywhere".
    assert "on the Board" in body


def test_a_lede_answers_rather_than_apologises(client):
    """The copy fault the operator named, pinned as a shape rather than as a
    wording.

    *"Super sterile and logical, the agent is doing the work the user doesn't
    need to read an essay."* The cause was identifiable: every lesson about not
    overclaiming a FIGURE got applied to the page copy too, so a sentence
    introducing a panel was written with the caution owed to a measurement.

    The rule that actually applies is narrower — a figure may never be stated
    more confidently than it was measured, and a heading is not a figure. So
    what is asserted here is that the three ledes open by saying what the page
    is FOR, and that none of them opens by qualifying itself. Every caveat that
    qualifies a number is pinned by its own test elsewhere and none is touched.
    """
    for path, opening in (
        ("/decisions", "The loop wakes every fifteen minutes"),
        ("/trades", "What actually happened"),
        ("/analytics", "How it has actually gone"),
    ):
        body = client.get(path).text
        assert opening in body, path

    # The specific sentences that were cut, kept out. Each was true, and each
    # spent the page's opening line on the page rather than on the account.
    trail = client.get("/decisions").text
    assert "folded behind its head line" not in trail
    assert "A wrong metric is worse than no metric" not in client.get("/analytics").text


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


def test_the_write_routes_are_exactly_the_three_that_were_decided_on(client):
    """The dashboard was wholly read-only and this test enforced it.

    It has been widened three times now, each time deliberately and each time by
    editing this set rather than loosening the assertion: `POST /chat` when the
    agent panel landed, `POST /login` when the operator chose to expose the
    dashboard publicly, and `POST /settings/request` when the settings agent
    did. Anything else appearing here means a write route arrived without
    anyone deciding it should.

    None of the three writes to the journal or reaches an order path. `/login`
    mints a session; `/chat` is gated by its own separate token on top; and
    `/settings/request` records a change REQUEST in
    `data/settings_requests.db` and cannot touch `config/rules.yaml` — that
    file is root-owned on the box precisely so the service account cannot edit
    its own limits, and applying a request is `electrum-bot settings-apply`,
    run as root by a person.
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
        ("/settings/request", "POST"),
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
    during a losing run, which is exactly when the limit is doing its job.

    With the forge OFF — no `DASHBOARD_CHAT_TOKEN`, which is this fixture and is
    the default everywhere — the page is exactly what it always was: not one
    control of any kind. The settings agent is a decision somebody makes, not
    something a deploy switches on.
    """
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


def test_settings_reports_the_x_feed_as_off_rather_than_broken(client):
    """Off is the NORMAL state. Reading timelines needs a paid X tier — there
    has been no free read tier since 2026-02-06 — so a deployment without one
    is fully functional and must not be shown a fault."""
    body = client.get("/settings").text

    assert "X posts" in body
    assert "social.enabled is false" in body
    assert "fully functional" in body
    # Configuration is on file whether or not the feed runs, so the numbers a
    # subscription decision needs are visible before there is a subscription.
    assert "Cache TTL" in body
    assert "600s" in body


def test_settings_never_claims_the_x_feed_is_healthy_when_nothing_was_read():
    """No cycles on file is not "nothing failed". Same rule as `has_cycles`."""
    state = render.social_state(load_rules(), _env(), None)
    card = render._social_card(state, window_hours=24)

    assert state.degraded is None
    # Not "no failed fetch", which is the answer a `False` default would give.
    assert "not known" in card
    assert "no failed fetch" not in card
    assert any("only of how it is configured" in c for c in state.caveats())


def test_settings_separates_an_unconfigured_x_feed_from_a_failed_one():
    """Three states, and only the third is a fault."""
    from bot.news_history import NewsRecall

    rules = load_rules()
    off = render.social_state(rules, _env(), NewsRecall(cycles_read=1))
    assert off.status == "off"

    enabled = rules.model_copy(
        update={"social": rules.social.model_copy(update={
            "enabled": True, "accounts": ["realDonaldTrump"]
        })}
    )
    no_token = render.social_state(enabled, _env(), NewsRecall(cycles_read=1))
    assert no_token.status == "no token"
    assert "X_BEARER_TOKEN is unset" in no_token.headline()

    env = _env()
    env.x_bearer_token = "a-token"
    broken = render.social_state(
        enabled, env, NewsRecall(cycles_read=1, social_degraded=True)
    )
    assert broken.status == "degraded"
    assert "not evidence of a quiet morning" in broken.headline()


def test_settings_does_not_read_no_posts_as_a_failed_fetch():
    """A watched account with nothing to say and a token that expired produce
    the same absence in the record. Only one of them is a fault."""
    from bot.news_history import NewsRecall

    rules = load_rules()
    enabled = rules.model_copy(
        update={"social": rules.social.model_copy(update={
            "enabled": True, "accounts": ["realDonaldTrump"]
        })}
    )
    env = _env()
    env.x_bearer_token = "a-token"

    state = render.social_state(enabled, env, NewsRecall(cycles_read=4))

    assert state.status == "on"
    assert any("not evidence that the fetch failed" in c for c in state.caveats())


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
    assert "Nothing recorded yet" in body
    assert "audit/" in body
    assert "electrum-bot loop" in body


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

    # An entry on a symbol nothing is held in. "Pending" is a true claim about
    # exactly this row — it will become a position — and about nothing else on
    # the Board, which is why the word is reserved for this group.
    assert "Pending entries" in body
    assert "641.2000" in body          # the limit
    assert "648.0200" in body          # the market
    assert "% away" in body            # and how far it has to travel


def test_no_resting_orders_says_so(client):
    """Both groups state their own nought, and they say different things.

    "Nothing is protecting anything" and "no entry is waiting" are different
    facts, and one sentence covering both was what let a live stop leg read as
    an order stuck in a queue.
    """
    body = client.get("/").text

    assert "No entries pending" in body
    assert "not part-way into a new position" in body
    assert "nothing to protect" in body


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


# -------------------------------------------------- the feeds, inside a cycle
# The headlines and the posts belong to the cycle they informed rather than to
# a feed page of their own. This is the only surface a rejected proposal exists
# on, so the story and the decision it produced have to be readable together.


def _feed_cycle(minutes_ago: int, **inputs: object) -> Decision:
    """One cycle, with an explicit age so provenance can be asserted."""
    return Decision(
        timestamp=datetime(2026, 5, 4, 15, 0, tzinfo=UTC) - timedelta(minutes=minutes_ago),
        inputs=MarketInputs(**inputs),  # type: ignore[arg-type]
    )


def test_posts_render_ahead_of_headlines_as_they_do_in_the_prompt(audited):
    """By the time a headline carries the story the gap has already opened, so
    reading them in the other order inverts what makes the feed worth having."""
    log, client = audited
    log.record(
        _decision(
            inputs=MarketInputs(
                headlines=["Wire story about steel"],
                social_posts=["[@realDonaldTrump 14:31] Tariffs on steel"],
            )
        )
    )

    body = client.get("/decisions").text

    assert body.index("Tariffs on steel") < body.index("Wire story about steel")


def test_a_cycle_that_saw_no_headlines_says_so_rather_than_rendering_nothing(audited):
    """An absent block and an empty one read identically. Only one is a reading."""
    log, client = audited
    log.record(_decision(inputs=MarketInputs()))

    body = client.get("/decisions").text

    assert "no headlines this cycle" in body
    assert "no posts this cycle" in body


def test_an_empty_post_list_does_not_claim_the_feed_was_even_running(audited):
    """`MarketInputs` records no posts and no feed identically, so the page
    must not read the first as evidence of the second."""
    log, client = audited
    log.record(_decision(inputs=MarketInputs()))

    body = client.get("/decisions").text

    assert "An unconfigured feed and a quiet one look identical" in body


def test_a_headline_already_on_file_is_not_presented_as_new(audited):
    """The 30-minute cache puts one story in front of the model three cycles
    running. Three fresh sightings would be one story called news three times."""
    log, client = audited
    # Three cycles so the oldest is the edge and the newest can claim nothing.
    log.record(_feed_cycle(90, headlines=["Fed holds rates steady"]))
    log.record(_feed_cycle(45, headlines=["Fed holds rates steady"]))
    log.record(_feed_cycle(15, headlines=["Fed holds rates steady", "Chip orders soften"]))

    body = client.get("/decisions").text

    # The newest cycle renders first and carries both markers: the repeated
    # story is on file from 75 minutes earlier, the second one is genuinely new.
    assert "on file since 13:30 UTC" in body
    assert "1h 15m before this cycle" in body
    assert "new this cycle" in body
    # The legend is on the page ONCE rather than on every cycle. It carries what
    # the terse marker cannot: what the count is over, and why an item repeats.
    assert body.count("already on file was in front of the model") == 1


def test_the_oldest_cycle_read_never_claims_an_item_is_new(audited):
    """It cannot know. The index is built from what was read off disk, so an
    item first appearing in the oldest cycle may be far older than that —
    saying "new" there would be an artefact of where the read stopped."""
    log, client = audited
    log.record(_feed_cycle(15, headlines=["Only ever seen once"]))

    body = client.get("/decisions").text

    assert "oldest cycle read &mdash; may be older" in body
    assert "new this cycle" not in body


def test_a_cycle_with_no_inputs_is_a_gap_in_the_record_not_a_quiet_cycle(audited):
    """The audit log is append-only and never migrated, so a line written
    before `MarketInputs` existed has no feeds at all. Rendering nothing made
    that indistinguishable from a cycle that saw nothing."""
    log, client = audited
    log.record(_decision())  # no inputs whatever

    body = client.get("/decisions").text

    assert "predates input recording" in body
    assert "gap in the record rather than a cycle that saw nothing" in body


def test_a_partial_list_beside_a_failed_fetch_is_labelled_partial(audited):
    """Degraded with items is not degraded with none: the list is readable and
    it is still incomplete, and only saying one of those would mislead."""
    log, client = audited
    log.record(
        _decision(
            inputs=MarketInputs(
                social_posts=["[@federalreserve 14:02] Statement"], social_degraded=True
            )
        )
    )

    body = client.get("/decisions").text

    assert "Statement" in body
    assert "This list is INCOMPLETE" in body


def test_a_warning_colour_is_not_cancelled_by_the_class_beside_it():
    """`class="note alert"` renders PEWTER, not amber, and nothing says so.

    Both are single-class rules and `.note` is declared after `.alert`, so at
    equal specificity the later one wins and the warning colour is silently
    lost. Measured in Chromium at rgb(139,150,164) where amber was intended:
    valid CSS, no error, the text still reads — it just stops looking like a
    warning, which is the only job it has. Same family as the `.pill.seed`
    collision and the `.rung.gate` one.

    The fix is `.alert` on an inner element, and this is what keeps it there.
    """
    import re

    css = re.sub(r"/\*.*?\*/", "", render.STYLES, flags=re.S)
    # The collision is real: assert the ordering that causes it, so that a
    # future reorder of the stylesheet retires this test honestly rather than
    # leaving it passing for a reason that has gone away.
    assert css.index(".alert{") < css.index(".note{")

    for name, markup in (
        ("settings", render.settings_page(load_rules(), _env(), chat_enabled=False)),
        (
            # Degraded WITH items, which is the branch that carries the
            # "this list is incomplete" warning rather than replacing the list.
            "decisions",
            render._read(
                _entry_for(
                    _decision(
                        inputs=MarketInputs(social_posts=["p"], social_degraded=True)
                    )
                )
            ),
        ),
    ):
        assert "INCOMPLETE" in markup or "X posts" in markup, name
        assert 'class="note alert"' not in markup, name
        assert 'class="alert note"' not in markup, name


def test_the_feeds_stay_behind_the_cycles_own_disclosure(audited):
    """The Decisions page measured 269 KB and 57,574px tall for forty cycles.
    Everything added here sits inside `What it read`, which is a `<details>`
    inside the cycle's own `<details>`, so it costs no layout until opened."""
    log, client = audited
    log.record(_decision(inputs=MarketInputs(headlines=["Fed holds rates steady"])))

    body = client.get("/decisions").text
    step = body[body.index("What it read") :]

    # The headline is inside the step, and the step opens with a `<summary>`.
    assert "Fed holds rates steady" in step
    opener = body.rindex("<details", 0, body.index("What it read"))
    assert " open" not in body[opener : body.index("What it read")]


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

    def _ask(self, message, history=None, soul=None, operator="", briefing=""):
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

    def _ask(self, message, history=None, soul=None, operator="", briefing=""):
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
    """The dreaming card is display only, like every other card here.

    Asserted with the forge off, which is what this fixture is. The controls the
    forge does add when it is on are pinned separately and by name in
    `tests/test_settings_agent.py`, so a control appearing here is still a
    control nobody decided on.
    """
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


def test_a_cell_carries_one_vertical_mark_and_it_is_the_one_that_means_something():
    """Every cell used to draw two, doing unrelated jobs.

    The rail on the left is coloured by direction and scaled by `--mag`, so it
    carries information. The `border-right` on the other edge was a flat grey
    line meaning nothing, and adjacent cells therefore rendered a grey mark and
    a coloured one a few pixels apart with no way to tell which was which.

    Padding divides a list perfectly well and cannot be mistaken for a gauge.
    """
    from bot.web.render import STYLES

    cell_rule = STYLES[STYLES.index(".tape .cell{") : STYLES.index(".tape .cell::before")]

    assert "border-right" not in cell_rule
    assert "padding:0 1.1rem" in cell_rule


def test_a_cell_with_nothing_to_measure_draws_no_gauge():
    """`--mag:0` left a 28% pewter stub, which reads as a tiny move rather than
    as an absent one — a plausible wrong figure drawn in two pixels, in the
    least conspicuous place on the deck to put one.

    Both no-reading cases: nothing came back at all, and a real price with no
    yesterday to compare it against."""
    from bot.market_clock import market_state
    from bot.web.render import STYLES

    body = render.ticker_tape(
        market_state(datetime(2026, 8, 10, 15, 0, tzinfo=UTC)),
        [
            _quote("GLD"),                                   # no quote
            _quote("TLT", last=91.2),                        # no prior close
            _quote("SPY", last=580.0, prev=574.0),           # a real move
        ],
    )

    def classes_of(symbol: str) -> set[str]:
        cell = body.split(f'data-tick="{symbol}"')[0].rsplit('<span class="', 1)[-1]
        return set(cell.split('"')[0].split())

    assert "norail" in classes_of("GLD")
    assert "norail" in classes_of("TLT")
    assert "norail" not in classes_of("SPY")
    assert ".tape .cell.norail::before{display:none}" in STYLES
    # The painter has to take it off again when a percentage arrives, or a cell
    # the server rendered without a move keeps its gauge suppressed for the life
    # of the tab — the mirror of the stub.
    assert "cell.classList.remove('norail')" in render.SCRIPT


def test_a_clock_is_not_framed_in_the_same_line_a_cell_is_divided_by():
    """Contrast alone could never fix this while the frame was shared.

    `.clk` was bounded left and right by `1px solid var(--slate)` — the exact
    line the cells used between one another — so a clock read as one more cell
    with a darker fill. It is a module set INTO the band now: inset on all four
    sides, recessed, and carrying the deck's own bracket corners, none of which
    any cell has.
    """
    from bot.web.render import STYLES

    clk = STYLES[STYLES.index(".tape .clk{") : STYLES.index(".tape .clk::before")]

    assert "border-left" not in clk and "border-right" not in clk
    assert "margin:" in clk                       # inset from the strip
    assert ".tape .clk::before{top:0;left:0" in STYLES     # bracket corners
    assert ".tape .clk::after{bottom:0;right:0" in STYLES


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
    # Not a named symbol. This asserted "NVDA" and went red the day
    # `allowed_symbols` was widened from six names to twenty — which is a
    # change to what may be traded, made deliberately in its own commit, and
    # exactly the change this test exists to let through. A membership check on
    # one ticker tests where that ticker happens to be filed; the property is
    # that the two lists are different things.
    #
    # An EQUITY has to be watch-only, because the crypto pair on the tape is
    # watch-only for a second reason entirely — its class is disabled — and a
    # test satisfied by that alone would go quiet the moment crypto was enabled
    # while still reading as though it had checked something.
    assert any("/" not in symbol for symbol in watch_only), (
        "only the disabled crypto class keeps these lists apart"
    )

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


def test_the_deck_states_the_venue_and_the_gate_separately(client):
    """"The market is open" and "this bot will trade" are different claims.

    They were confused once already, at 04:49 New York time on a Monday:
    Alpaca's pre-market genuinely was running, four and a half hours before the
    window `config/rules.yaml` permits. Merging them into one green light is
    how that happens again.

    The claim now lives on the Board rather than pinned to the tape — the
    operator flagged the badge twice, three words could never carry the middle
    state, and a card has room for what it costs. What must not change is that
    the middle state exists at all and is neither of its neighbours.
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

    card = render.gate_stance(state)
    assert "It will place. The order will wait." in card
    # And it says WHY, which is the half the badge never had room for.
    assert "rests at the broker" in card

    # The two neighbours are different answers, not degrees of this one.
    trading = render.gate_stance(
        market_state(datetime(2026, 8, 10, 15, 0, tzinfo=UTC), windows_by_day=windows)
    )
    assert "It will trade." in trading
    shut = render.gate_stance(
        market_state(datetime(2026, 8, 15, 15, 0, tzinfo=UTC), windows_by_day=windows)
    )
    assert "It will not trade." in shut


def test_a_caller_with_no_clock_reading_gets_no_verdict():
    """Same rule as the reading stamp: a caller that did not ask is not a
    market that could not be read, and neither is a licence to state one."""
    assert render.gate_stance(None) == ""


def test_the_gate_verdict_reaches_the_board_and_has_left_the_tape(client):
    """The route has to supply it, or the card is a function nobody calls.

    That is the failure mode this project has hit twice — `Dream.is_offerable`
    defined and never invoked, and the `.until` countdown the script still
    looks for. A rendering helper with no caller is invisible to every test
    that exercises the helper.
    """
    body = client.get("/").text
    # The strip sits between the header and `<main>`. Sliced in that order, or
    # a reversed slice comes back empty and the assertion below passes for
    # having tested nothing.
    tape = body[body.index('<div class="tape"') : body.index("<main")]
    assert 'class="until"' in tape, "sliced the wrong region"

    assert "What it will do right now" in body
    # And the strip it came off does not still carry it.
    assert "verdict" not in tape


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


def test_whatever_is_pinned_to_the_tape_names_the_exchange_it_speaks_for():
    """Two things have been thrown off that slot for the same reason.

    "PRE-MARKET" was a claim about every cell beneath it and false for the
    crypto trading right beside it. The gate verdict — `trading` / `armed` /
    `idle` — was account-wide and therefore true, and read as a status light
    with nothing in it to act on; it is on the Board now, where the middle
    state has room to say what it costs.

    What is pinned there has to be something no cell can contradict, so it
    names its exchange.
    """
    from bot.market_clock import market_state

    # With the real windows, so the gate genuinely IS open here — without them
    # `market_state` reports the cautious reading, which is a different case.
    windows = load_rules().instruments["us_equity"].windows_by_day
    body = render.ticker_tape(
        market_state(
            datetime(2026, 8, 10, 12, 0, tzinfo=UTC), windows_by_day=windows
        ),
        [_quote("SPY", last=580.0, prev=574.0, tradeable=True)],
    )
    fixed = body.split('<div class="view"')[0]

    assert "Pre-market" not in fixed
    assert "armed" not in fixed and "idle" not in fixed
    # 12:00 UTC is 08:00 New York, and the regular session opens at 13:30 UTC.
    assert "NYSE open in 1h 30m" in fixed


def test_the_pinned_countdown_is_wired_to_the_script_that_updates_it():
    """The countdown element was deleted with the phase block it sat in, and
    the script that paints it was left behind.

    `bar.querySelector('.until')` returned null on every load from that day, so
    `tick()` bailed one line later — and the session-boundary spin, the whole
    `turn-up`/`turn-down`/`turn-side` apparatus with its own tests, has not
    fired since. Nothing warned: the clocks kept ticking, because painting them
    happens above the bail-out.

    So the two halves are pinned together. A countdown with no script is a
    frozen figure that looks live; a script with no countdown is dead code
    holding a feature hostage.
    """
    from bot.market_clock import market_state

    body = render.ticker_tape(
        market_state(datetime(2026, 8, 10, 15, 0, tzinfo=UTC)), []
    )

    assert 'class="until"' in body
    assert "bar.querySelector('.until')" in render.SCRIPT
    # And it must render the same WORDS the server did. `data-next-phase`
    # carries the enum value, so formatting it raw would rewrite "NYSE after
    # hours in 3h 12m" as "post in 3h 11m" one second after load.
    assert "post: 'after hours'" in render.SCRIPT
    assert "'NYSE ' + phaseWord(nextPhase)" in render.SCRIPT


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
    trail_percent: float | None = None,
    trail_price: float | None = None,
    high_water_mark: float | None = None,
    symbol: str = "SPY",
) -> WorkingOrder:
    from bot.models import OrderStatus

    return WorkingOrder(
        order_id="o-1",
        symbol=symbol,
        direction=direction,
        qty=qty,
        limit_price=limit_price,
        stop_price=stop_price,
        order_type=order_type,
        trail_percent=trail_percent,
        trail_price=trail_price,
        high_water_mark=high_water_mark,
        status=OrderStatus.NEW,
        submitted_at=ENTRY,
        filled_qty=filled_qty,
    )


def _position(
    *, direction: Direction = Direction.SELL, symbol: str = "SPY"
) -> Position:
    """One held position, so a resting order can be classified against it.

    A protective leg is one that would REDUCE something held — the buy stop
    over a short, the sell target under a long. Without a position on file the
    same order is an entry, which is exactly the distinction the two groups on
    the Board exist to make.
    """
    return Position(
        symbol=symbol,
        direction=direction,
        qty=21,
        entry_price=773.32,
        opened_at=ENTRY,
        current_price=773.10,
    )


def _rows(body: str) -> str:
    """The first table body on the page.

    With the orders split into two groups this is the PROTECTIVE table whenever
    it has rows, and the pending one when it does not — an empty group renders a
    sentence rather than a `<tbody>`. Every caller below passes exactly one
    order, so there is only ever one table with rows in it.
    """
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
    # Held short, so both legs are PROTECTIVE and the difference under test is
    # the trigger price rather than which group they land in.
    held = [_position(direction=Direction.SELL)]
    known = render._working_orders(
        [_order(stop_price=820.0, order_type="stop")], prices, positions=held
    )
    unknown = render._working_orders(
        [_order(order_type="stop")], prices, positions=held
    )

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


def test_a_trailing_stop_does_not_render_as_a_fixed_one():
    """A real trailing leg read `815.0000 stop` — correct, and a lie about who
    chose 815. The broker trailed to it and will be somewhere else next
    reading, so the word and the trail size both have to be on the cell."""
    held = [_position(direction=Direction.SELL)]
    prices = {"SPY": 790.0}
    trailing = _rows(
        render._working_orders(
            [
                _order(
                    order_type="trailing_stop",
                    stop_price=815.0,
                    trail_percent=1.5,
                    high_water_mark=803.0,
                )
            ],
            prices,
            positions=held,
        )
    )
    fixed = _rows(
        render._working_orders(
            [_order(order_type="stop", stop_price=815.0)], prices, positions=held
        )
    )

    assert trailing != fixed
    assert "815.0000" in trailing
    assert "trailing stop" in trailing
    assert "1.5% behind" in trailing
    # The mark the trail is measured FROM. Without it the size is a distance
    # from a point that appears nowhere on the page.
    assert "803.0000" in trailing
    assert "trailing" not in fixed


def test_a_trailing_leg_with_no_trail_size_gets_the_alert_treatment():
    """`trail_is_unreadable` is `trigger_price_unknown` one level up: the
    current trigger reads fine and where it goes next cannot be stated, so the
    level on screen is a number quietly moving for reasons nobody can give."""
    held = [_position(direction=Direction.SELL)]
    prices = {"SPY": 790.0}
    unreadable = _rows(
        render._working_orders(
            [_order(order_type="trailing_stop", stop_price=815.0)],
            prices,
            positions=held,
        )
    )
    readable = _rows(
        render._working_orders(
            [
                _order(
                    order_type="trailing_stop", stop_price=815.0, trail_percent=1.5
                )
            ],
            prices,
            positions=held,
        )
    )

    assert '<span class="alert">' in unreadable
    assert "trail size unknown" in unreadable
    # The working case must not borrow the alert colour, or it stops meaning
    # anything on the row where it is warranted.
    assert '<span class="alert">' not in readable


def test_a_trail_stated_as_an_amount_is_rendered_as_one():
    """Alpaca supplies `trail_percent` OR `trail_price`, never both."""
    row = _rows(
        render._working_orders(
            [
                _order(
                    order_type="trailing_stop", stop_price=815.0, trail_price=12.0
                )
            ],
            {"SPY": 790.0},
            positions=[_position(direction=Direction.SELL)],
        )
    )

    assert "12.0000 behind" in row
    assert "%" not in row.split("Market")[0]


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


def test_the_protective_caption_says_the_leg_is_doing_its_job():
    """It was true when nothing sent a stop to the broker. Every entry is a
    bracket now, so a stop leg always rests there — and the operator has twice
    read that leg as clutter. The caption over the group it sits in has to say
    what it is FOR, not merely what it is."""
    body = render._working_orders(
        [_order(stop_price=820.0, order_type="stop")],
        {"SPY": 773.10},
        positions=[_position(direction=Direction.SELL)],
    )
    # `<caption` rather than `<caption>`: it carries an id now, because it is
    # what names the scroll region around it via `aria-labelledby`.
    caption = body[body.index("<caption") : body.index("</caption>")]

    assert "hard stop doing its job" in caption
    assert "BECAUSE a position does" in caption
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


def test_a_board_with_no_read_time_says_unknown_rather_than_now():
    """The stamp describes the READING, never the render.

    This page printed `as at <now>` above figures the poller had read overnight
    — every one a present-tense claim about an account nobody had looked at
    since the previous evening, with the one element a reader checks to find
    out saying it was current. A caller that cannot say when the broker was
    read must not be handed a timestamp by default.

    Pinned because it is prose, and a copy pass over prose is exactly how a
    sentence like this gets tidied into a default.
    """
    from datetime import datetime

    body = render.board(
        _held(), load_rules(), [], [], StandDownState(), 0, read_at=None
    )

    assert "read time unknown" in body
    assert f"{datetime.now(UTC):%d %b %Y}" not in body
    # And the stream says the same words when the payload carries no `as_of`,
    # rather than leaving the server's honest answer to be overwritten.
    assert "'read time unknown'" in render.SCRIPT


def test_a_cold_start_can_still_say_what_the_bot_will_do():
    """The one figure on this page that owes nothing to the broker.

    "Nothing has been read yet" is a statement about the account, and it was
    being applied to a question that is pure clock arithmetic against
    `config/rules.yaml`. Withholding the answer there treats "not read yet" as
    though it meant "nothing can be said".
    """
    from bot.market_clock import market_state

    windows = load_rules().instruments["us_equity"].windows_by_day
    body = render.board(
        None, load_rules(), [], [], StandDownState(), 0,
        market=market_state(
            datetime(2026, 8, 10, 15, 0, tzinfo=UTC), windows_by_day=windows
        ),
    )

    assert "not read yet" in body          # still honest about the figures
    assert "What it will do right now" in body
    assert "It will trade." in body


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
    #
    # Counted on the RUN WRAPPERS rather than on every `aria-hidden` in the
    # strip. A bare count of the attribute was measuring the wrong thing and
    # went off the moment anything inside a cell was hidden from the
    # accessibility tree for its own reasons — which is what the clock badges
    # now do, marking a decorative pip beside the state word that carries the
    # same fact in text. The claim being made here is about the duplicate run,
    # so that is what is counted.
    assert body.count('<div class="marquee-run">') == 1
    assert body.count('<div class="marquee-run dup" aria-hidden="true">') == 1
    assert len(re.findall(r'<div class="marquee-run[^"]*"', body)) == 2

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

    # Three now: the positions table and BOTH order groups. Neither order
    # table repaints, so one note across the pair would leave the second
    # looking live.
    assert body.count("This section does not repaint") == 3
    assert "Reload for a newer one." in body
    # Fixed on purpose: the whole claim is that this one does NOT change, so it
    # must not be marked for the stream.
    for chunk in body.split("This section does not repaint"):
        assert not chunk.endswith("data-live-read")

    positions = body.index("<h2>Open positions</h2>")
    protective = body.index("<h2>Stop losses and targets in force</h2>")
    pending = body.index("<h2>Pending entries</h2>")
    for start in (positions, protective, pending):
        assert "does not repaint" in body[start : start + 700]


def test_the_fixed_reading_note_says_unknown_rather_than_guessing():
    """A caller that cannot say when the broker was read must not be handed a
    time by default. Same rule as the stamp it sits under."""
    assert "time is unknown" in render._fixed_reading_note(None)
    assert "taken 04 May 2026" in render._fixed_reading_note(ENTRY)


# ------------------------------------- an empty sample must not look like a result


def test_analytics_never_prints_a_win_rate_with_no_closed_trades(client):
    """`Win rate 0%` on an empty journal reads as "everything lost".

    Measured on the live deck with one open position and nothing closed:

        WIN RATE      0%      0W / 0L
        PROFIT FACTOR n/a     no closed trades yet
        EXPECTANCY    $0.00   per trade, n/a
        NET           $0.00   max drawdown $0.00

    Profit factor was the only one of the four that was right. The other three
    are `PerformanceSummary` defaults formatted as though they were
    measurements, on the page whose own strapline is that a wrong metric is
    worse than no metric because it gets believed and then acted on.
    """
    body = client.get("/analytics").text
    card = body[body.index("Win rate") : body.index("Profit factor")]

    assert "0%" not in card, "an empty sample rendered a percentage"
    assert "n/a" in card
    assert "no closed trades yet" in card


def test_analytics_never_prints_a_dollar_result_with_no_closed_trades(client):
    """Expectancy and Net are the same defect one step milder.

    `$0.00` is not "unmeasured", it is "broke even" — a figure an operator can
    read as a flat month rather than as an empty journal.
    """
    body = client.get("/analytics").text
    # Sliced on the structure rather than on a heading's wording. It used to cut
    # at the word "Headline" and went red when that heading was reworded, which
    # says nothing whatever about whether an empty sample printed a dollar.
    tiles = body[body.index("Expectancy") : body.index('<section class="block"')]

    assert "$0.00" not in tiles, "an empty sample rendered a dollar result"
    assert "+$0.00" not in tiles
    assert tiles.count("n/a") >= 2
    assert "max drawdown" not in tiles, "a drawdown of nothing is not a drawdown"


def test_analytics_prints_the_real_figures_once_something_has_closed(client, journal):
    """The gate is emptiness, not a general reluctance to state a number.

    One closed winner is a genuine, terrible-sample, honestly-reported 100%,
    and withholding it would swap one wrong reading for another.
    """
    _closed_trade(journal, 200.0)
    body = client.get("/analytics").text
    card = body[body.index("Win rate") : body.index("Profit factor")]

    assert "100%" in card
    assert "1W / 0L" in card
    assert "no closed trades yet" not in card


# ------------------------------------------- the curve's labels are not scaled type


def test_equity_curve_labels_are_html_not_svg_text(journal):
    """SVG `text` inside a stretched viewBox is sized in USER units.

    The chart is `viewBox="0 0 1000 240"` drawn at whatever the card is wide,
    so `font-size:10px` on an SVG label renders at 10 x (container / 1000):
    14px on a 1440 deck and a measured **4.00px** at 390 wide, where the
    container is 358. Unreadable, and unreadable in the direction nobody
    notices, because the deck it is designed on is the wide one.

    A media-query bump is a workaround that has to be re-tuned at every width.
    Taking the labels out of the scaled coordinate system fixes it once.
    """
    curve = render._curve([("2026-08-09", 100_000.0), ("2026-08-10", 99_998.62)])

    svg = curve[curve.index("<svg") : curve.index("</svg>")]
    assert "<text" not in svg, "an axis label is back inside the scaled viewBox"

    # And the labels are still on the page, in CSS pixels.
    assert "$100,000.00" in curve
    assert "2026-08-09" in curve
    assert any(
        "font-size:.625rem" in d for d in _css_blocks(".curve .lab")
    ), "the value labels are not sized in CSS px"
    assert any(
        "font-size:.625rem" in d for d in _css_blocks(".curve .axis")
    ), "the date labels are not sized in CSS px"


def test_equity_curve_labels_track_the_data_they_name(journal):
    """Percentages of the plot box, which is what a user-space coordinate IS.

    The point of moving them out of the SVG was the type size, not the
    placement, so a label must still sit against the reading it names — the
    high label above the high, the low label below the low.
    """
    curve = render._curve(
        [("2026-08-09", 100.0), ("2026-08-10", 200.0), ("2026-08-11", 150.0)]
    )
    import re

    hi = re.search(r'class="lab" style="bottom:([\d.]+)%"', curve)
    lo = re.search(r'class="lab" style="top:([\d.]+)%"', curve)
    assert hi and lo, curve

    # The high label is anchored from the bottom and the low from the top, so
    # each is pushed AWAY from its reading and neither can be clipped by the
    # plot's own edge at a narrow width.
    assert 50.0 < float(hi.group(1)) < 100.0
    assert 50.0 < float(lo.group(1)) < 100.0


# --------------------------------------- the trail says what it is not showing


def _cycles(n: int) -> list[DecisionEntry]:
    return [
        DecisionEntry(
            timestamp=datetime(2026, 5, 4, 15, 0, tzinfo=UTC) + timedelta(minutes=i),
            decision=_decision(notes=f"cycle {i}"),
        )
        for i in range(n)
    ]


def test_the_trail_states_what_it_is_withholding():
    """It printed the total in the view above a slice of it.

    `len(view.decisions)` over `view.decisions[:shown]`. Those coincided at 40
    of 40 on the day it was measured, so the header said "40 cycles" and showed
    forty. The loop wakes 96 times a day: on a full session that header says 96
    with 56 missing, no "showing 40 of 96" and no route to the rest — on the
    page whose whole reason to exist is that a rejected proposal is visible
    nowhere else.
    """
    from bot.audit import AuditView

    body = render.decisions(AuditView(decisions=_cycles(96)))

    assert "96 cycles" not in body, "the header claims cycles it is not showing"
    assert "showing 1-40 of the newest 96" in body
    assert "1-40 of 96" in body


def test_the_trail_offers_a_route_to_the_older_cycles():
    from bot.audit import AuditView

    view = AuditView(decisions=_cycles(96))

    first = render.decisions(view)
    assert '/decisions?page=2' in first
    assert '/decisions?page=0' not in first

    second = render.decisions(view, page=2)
    assert "showing 41-80 of the newest 96" in second
    assert '/decisions?page=1' in second
    assert '/decisions?page=3' in second

    # The last page offers nothing older, rather than a link to an empty one.
    last = render.decisions(view, page=3)
    assert "showing 81-96 of the newest 96" in last
    assert '/decisions?page=4' not in last


def test_a_page_past_the_end_walks_back_rather_than_rendering_nothing():
    """`?page=99` is a typo, not a request to be told the trail is empty."""
    from bot.audit import AuditView

    body = render.decisions(AuditView(decisions=_cycles(96)), page=99)

    assert "showing 81-96 of the newest 96" in body


def test_every_cycle_but_the_newest_is_folded_away():
    """40 cycles rendered open measured 57,574px tall at 390 wide.

    Roughly 68 phone screens for one day. `<details>` rather than a script,
    because the projection layer's rule is that nothing may be hidden unless
    the script said so — and this is hidden by the browser with no script
    involved, so it survives JavaScript being off exactly as the plain page
    does.
    """
    from bot.audit import AuditView

    body = render.decisions(AuditView(decisions=_cycles(5)))

    assert body.count("<details class=\"cycle\"") == 5
    assert body.count("<details class=\"cycle\" open>") == 1
    assert body.count("<summary class=\"head\">") == 5
    # And it stays reachable without JavaScript: no click handler, no hidden
    # attribute, no inline display:none.
    assert "<article class=\"cycle\"" not in body


def test_a_proposal_with_a_target_still_renders_its_risk_and_closes_its_box(audited):
    """A ternary trailing a multi-part f-string ate half the row.

    Adjacent string literals concatenate BEFORE the conditional binds, so
    `a b if cond else c d e` is `(a b)` against `(c d e)`. A proposal WITH a
    target rendered no "risks" figure and never closed its `<div>`; one without
    lost its `<b>` heading and opened a `</span>` that was never opened. The
    same trap is called out by name in `_working_orders`, which is where the
    shape was first recognised.
    """
    log, client = audited
    log.record(_decision(proposals=[_proposal()], verdicts=[RiskVerdict.approve()]))

    body = client.get("/decisions").text
    step = body[body.rindex('<div class="step">') :]
    step = step[: step.index("</details>")]

    assert "target 590.0000" in step
    assert "risks" in step, "the risk figure was eaten by the ternary"
    assert step.count("<div") == step.count("</div>"), step


# ------------------------------------- the exchange badge is not colour alone


def test_the_exchange_badge_carries_a_word_and_a_shape_not_only_a_colour():
    """A glow is not a second channel; it is luminance around a hue.

    Measured live with NYSE genuinely open: `class="mkt mkt-live"`, a teal
    `text-shadow`, and `title: null`. Greyscale, a low-contrast screen and a
    screen reader all got the same nothing out of it that the colour gave
    them — while the instrument cells one element across already carried a
    `title`.
    """
    from bot.market_clock import market_state

    body = render.ticker_tape(
        market_state(datetime(2026, 8, 10, 15, 0, tzinfo=UTC)),
        [_quote("SPY", last=580.0, prev=574.0)],
    )

    # The state in text, on the badge itself.
    assert '<span class="st">open</span>' in body
    assert '<span class="st">shut</span>' in body
    # And a tooltip in the same voice as the instrument cells.
    assert 'title="New York — regular session, trading now"' in body
    # The pip is a SHAPE: filled, half filled, or an empty ring.
    assert body.count('<i class="pip" aria-hidden="true"></i>') == 8
    assert any("background:currentColor" in d for d in _css_blocks(".mkt-live .pip"))
    assert any("background:transparent" in d for d in _css_blocks(".mkt .pip"))


def test_a_zone_with_no_exchange_makes_no_state_claim():
    """A badge saying "shut" for a zone with no exchange asserts a session
    that does not exist there.

    All four configured clocks carry an exchange today, so this is driven
    against `_tape_clock` directly rather than through the strip: the state is
    reachable — `ClockFace.state` returns `None` whenever `sessions` is empty —
    and it should keep behaving when the operator asks for a fifth zone.
    """
    from bot.market_clock import ClockFace

    face = ClockFace("Los Angeles", "America/Los_Angeles", code="LAX")
    now = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)
    badge = render._tape_clock(face, face.at(now), now, None)

    assert "mkt-bare" in badge
    assert "pip" not in badge
    assert 'class="st"' not in badge
    assert "no exchange in this zone" in badge


# ---------------------------------------- a resting order's status is the broker's


def test_a_resting_order_shows_the_brokers_own_status_word(client, poller, journal):
    """`OTHER` is a truthful bucket and an unusable readout.

    The live GTC stop leg holding the operator's third rule up rendered as
    `OTHER` — indistinguishable from a status this build has never seen.
    Alpaca calls it `held`, which is a thing an operator can act on.
    """
    from bot.models import OrderStatus

    order = WorkingOrder(
        order_id="abc",
        symbol="SPY",
        direction=Direction.BUY,
        qty=21,
        stop_price=820.0,
        order_type="stop",
        status=OrderStatus.OTHER,
        broker_status="held",
    )
    body = render._working_orders([order], {"SPY": 774.42})

    assert ">held<" in body
    assert ">other<" not in body


def test_an_order_with_no_broker_word_falls_back_to_the_bucket():
    """`MockBroker` and anything constructed by hand say nothing, and a blank
    status cell would be worse than a coarse one."""
    order = WorkingOrder(
        order_id="abc", symbol="SPY", direction=Direction.BUY, qty=3, limit_price=570.0
    )
    body = render._working_orders([order], {"SPY": 574.0})

    assert ">new<" in body


# ------------------------------- a lapsed session is not a network outage


def test_the_stream_client_tells_a_401_from_a_dropped_connection():
    """`EventSource` exposes no HTTP status, so the two look identical.

    A refused-with-401 stream is a PERMANENT failure per the spec — one error
    event, `readyState` CLOSED, no further attempt — while a transport failure
    leaves readyState at CONNECTING and the browser retries. The Board settled
    on amber RECONNECTING forever with the last-good figures on screen and
    nothing saying the operator was signed out.
    """
    script = render.SCRIPT

    assert "es.readyState !== 2" in script, "a permanent failure is not distinguished"
    assert "'/session'" in script, "nothing asks whether the session is the reason"
    assert "signed out - reload to sign in" in script
    # And the weaker claim is what a failed probe falls back to. Telling
    # somebody whose Wi-Fi dropped that they are signed out sends them to
    # re-enter a password they never lost.
    assert "stream closed" in script
    assert any("background:var(--rust)" in d for d in _css_blocks(".live.link-out i"))


# ------------------------- the stop in force, and the moves nobody explained
# Two stop figures live on a `Trade` and they answer different questions. The
# Board is "what is at risk right now", so it shows `effective_stop`; the
# Trades page sits beside R, so it keeps `planned_stop`. The tests below pin
# each to the right one, and pin the three states the broker/journal
# comparison can be in — found, could-not-read, and never ran.


def _held(symbol: str = "SPY", qty: float = 21.0, price: float = 810.0) -> AccountSnapshot:
    from bot.models import Position

    return AccountSnapshot(
        equity_usd=100_000.0,
        cash_usd=100_000.0,
        buying_power_usd=100_000.0,
        open_positions=[
            Position(
                symbol=symbol,
                direction=Direction.SELL,
                qty=qty,
                entry_price=773.32,
                opened_at=ENTRY,
                current_price=price,
                unrealised_pnl_usd=-770.0,
            )
        ],
    )


def _short_spy(current_stop: float | None = None) -> Trade:
    trade = Trade(
        symbol="SPY",
        direction=Direction.SELL,
        qty=21,
        entry_time=ENTRY,
        entry_price=773.32,
        planned_stop=820.0,
        rationale="Operator test short.",
    )
    trade.current_stop = current_stop
    return trade


def test_the_board_shows_the_stop_in_force_rather_than_the_sizing_one():
    """A stop tightened from 820 to 800 means the position stands to lose less,
    and the Board is the surface that has to say so.

    `planned_stop` is what the position was SIZED against and stays the
    denominator of R. Showing it here would report a loss this position can no
    longer take: 980.19 of risk against a real 565.32.
    """
    trade = _short_spy(current_stop=800.0)
    account = _held()

    body = render.board(
        account, load_rules(), [], [trade], StandDownState(), 0, env=_env()
    )

    assert "800.0000" in body, "the stop in force is not on the page"
    assert render._money(trade.current_risk_usd) in body      # the real figure
    assert render._money(trade.planned_risk_usd) not in body  # what it was sized to
    # And the row says WHY the two differ, so a reader holding a journal row
    # that still reads 820 is not left thinking the page is wrong. Asserted on
    # the badge itself: the caption explains the tag, so the words alone would
    # pass with no tag on any row.
    assert '<span class="pill moved">' in body
    assert "was sized against 820.0000" in body


def test_an_unmoved_stop_renders_exactly_as_it_always_did():
    """`effective_stop` falls back to `planned_stop`, which is every trade this
    bot has placed. The switch above must change none of them."""
    trade = _short_spy()
    body = render.board(
        _held(), load_rules(), [], [trade], StandDownState(), 0, env=_env()
    )

    assert "820.0000" in body
    assert render._money(trade.planned_risk_usd) in body
    assert '<span class="pill moved">' not in body


def test_the_trades_page_keeps_the_stop_the_trade_was_sized_with():
    """The mirror of the test above, and the reason it is a separate rule.

    A closed row's Stop column sits beside R, and R is the result as a multiple
    of what the trade was DESIGNED to lose. Swapping in a tightened level would
    silently redefine every historical R on the page.
    """
    from bot.metrics import build_report

    trade = _short_spy(current_stop=800.0)
    trade.exit_time = ENTRY + timedelta(hours=3)
    trade.exit_price = 760.0
    trade.realised_pnl_usd = 280.0

    body = render.trades_page([trade], build_report([trade]))

    assert "820.0000" in body, "the sizing stop must stay on a closed row"
    assert "800.0000" not in body
    assert render._money(trade.planned_risk_usd) in body


# ------------------------------------------------------- why an exit ended
#
# `exit_review.py` classified every close since it shipped and rendered
# nowhere, which is the "wired and inert" failure this repository has now hit
# twice. These pin the distinctions that make the classification worth having.


def _exit(reason: Any, **kw: Any) -> Any:
    from bot.exit_review import ExitReview

    fields: dict[str, Any] = {
        "symbol": "SPY",
        "trade_id": 1,
        "reason": reason,
        "detail": "closed by hand at 124.5000, with neither level reached.",
    }
    fields.update(kw)
    return ExitReview(**fields)


def test_the_exit_reasons_reach_a_page_at_all():
    """The gap this closes: six classifications, none of them on any surface."""
    from bot.exit_review import ExitReport
    from bot.models import ExitReason

    report = ExitReport(
        reviews=[
            _exit(ExitReason.STOP_HIT, symbol="AAPL", detail="at or through the stop."),
            _exit(ExitReason.CLOSED_EARLY, symbol="NVDA"),
        ]
    )
    body = render.exit_reviews(report)

    assert "stop hit" in body
    assert "plan abandoned" in body
    assert "NVDA" in body and "AAPL" in body


def test_closed_early_is_kept_apart_from_closed_at_a_level():
    """The bucket that matters against the one that looks like it.

    Both are somebody pressing the button. Only one is the plan being
    abandoned; the other is the plan being followed by hand.
    """
    from bot.exit_review import ExitReport
    from bot.models import ExitReason

    body = render.exit_reviews(
        ExitReport(
            reviews=[
                _exit(ExitReason.CLOSED_EARLY, symbol="NVDA", closed_by_hand=True),
                _exit(ExitReason.CLOSED_AT_LEVEL, symbol="AMD", closed_by_hand=True),
            ]
        )
    )

    assert 'data-exit="closed_early"' in body
    assert 'data-exit="closed_at_level"' in body
    # And the abandoned one is called out above the list by name, because it is
    # the only verdict here that says something about the operator or the agent.
    assert "NVDA closed by hand with the price at neither level" in body
    assert "AMD closed by hand" not in body


def test_the_exit_section_carries_no_realised_figure():
    """`exit_review.py` reads no P&L — a test parses its AST and fails if it
    does. Putting a result beside a reason HERE would undo that at the one
    layer left that can, so the money stays in the table below."""
    import re

    from bot.exit_review import ExitReport
    from bot.models import ExitReason

    body = render.exit_reviews(
        ExitReport(reviews=[_exit(ExitReason.CLOSED_EARLY, symbol="NVDA")])
    )
    section = body[body.index("Why each position ended") :]

    assert "$" not in section, "a money figure reached the exit verdicts"
    assert not re.search(r"[-+]?\d+(\.\d+)?R\b", section), "an R multiple did"


def test_no_report_is_not_an_empty_report():
    """`None` means nothing was classified for this render, so the absence of
    verdicts says nothing at all. Same shape as `unexplained=None`."""
    from bot.exit_review import ExitReport

    absent = render.exit_reviews(None)
    empty = render.exit_reviews(ExitReport())

    assert absent != empty
    assert "were not classified for this render" in absent
    assert "not the same as every trade having ended as designed" in empty


def test_everything_unestablishable_is_not_a_clean_sheet():
    """`can_grade_anything` is deliberately a different question from "is the
    list empty". Trades closed and not one could be graded is a finding about
    the record; nothing having closed is not."""
    from bot.exit_review import ExitReport
    from bot.models import ExitReason

    body = render.exit_reviews(
        ExitReport(reviews=[_exit(ExitReason.UNKNOWN, symbol="TSLA")])
    )

    assert "not one exit could be established" in body
    assert '<span class="alert">' in body


def test_never_classified_is_not_the_same_as_unknown():
    """One was never asked, the other was asked and had no answer. Folding them
    together would report a row that predates the feature as evidence."""
    from bot.exit_review import ExitReport
    from bot.models import ExitReason

    body = render.exit_reviews(
        ExitReport(reviews=[_exit(ExitReason.STOP_HIT)]), never_classified=3
    )

    assert "3 closed row(s) carry no reason recorded" in body
    assert "Never classified is NOT the same as" in body


def test_every_exit_bucket_is_listed_including_the_zeros():
    """`ExitReport.counts` includes the zeros on purpose. A bucket rendered as
    absent rather than as `0` would undo that at the last step — and an absent
    line is what an outage looks like."""
    from bot.exit_review import ExitReport
    from bot.models import ExitReason

    body = render.exit_reviews(
        ExitReport(reviews=[_exit(ExitReason.STOP_HIT)])
    )

    for word in render.EXIT_WORDS.values():
        assert word in body
    assert body.count("<li data-exit=") >= len(ExitReason)


def test_the_trades_page_renders_the_exit_verdicts(client, journal):
    """End to end: the route builds the report and the page carries it."""
    _closed_trade(journal, pnl=120.0)
    body = client.get("/trades").text

    assert "Why each position ended" in body
    assert "This grades the PLAN" in body


def test_a_move_with_no_reason_on_file_is_tagged_on_the_board():
    """The inverse tag: the broker's figure differs from the journal's and
    nothing recorded says why.

    The whole point of storing a reason with every move is that its ABSENCE is
    visible. A feature that only displayed the moves it managed to capture
    would hide its own failures.
    """
    from bot.position_actions import UnexplainedMove, UnexplainedMoveReport

    report = UnexplainedMoveReport(
        moves=[
            UnexplainedMove(
                symbol="SPY", kind="stop", journal_value=820.0, broker_value=805.0
            )
        ]
    )

    body = render.board(
        _held(), load_rules(), [], [_short_spy()], StandDownState(), 0,
        env=_env(), unexplained=report,
    )

    assert "unexplained-move" in body
    assert "805.0000" in body and "820.0000" in body
    assert "no recorded action" in body


def test_the_two_caveats_render_as_their_own_states_rather_than_as_silence():
    """"We could not read it" and "there is no leg there" are different facts
    from "nothing has moved", and neither may render as a clean row.

    Same rule as `stops_unchecked` beside `stops_breached`: an absence of
    evidence is its own state and must not share a shape with the good outcome.
    """
    from bot.position_actions import UnexplainedMoveReport

    body = render.board(
        _held(), load_rules(), [], [_short_spy()], StandDownState(), 0,
        env=_env(),
        unexplained=UnexplainedMoveReport(
            unreadable_stops=["SPY"], positions_without_a_resting_stop=["SPY"]
        ),
    )

    assert "stop unreadable" in body
    assert "UNKNOWN rather than" in body
    assert "no resting stop" in body
    assert "third rule" in body


def test_an_unreadable_order_book_is_not_a_clean_comparison():
    """`get_open_orders` returns `[]` on failure, so an outage looks exactly
    like an account with nothing resting. An empty `moves` list then means
    nothing at all, and the page has to say so above the table."""
    from bot.position_actions import UnexplainedMoveReport

    body = render.board(
        _held(), load_rules(), [], [_short_spy()], StandDownState(), 0,
        env=_env(), unexplained=UnexplainedMoveReport(can_check=False),
    )

    assert "resting orders could not be read" in body
    assert "means UNKNOWN here, not clean" in body
    assert "unexplained-move</span>" not in body


def test_a_board_given_no_report_does_not_imply_a_clean_one():
    """The third state. A render with nothing to compare must not let the
    absence of a tag read as a finding — that is `has_cycles` and
    `can_grade_anything` arriving on the Board."""
    body = render.board(
        _held(), load_rules(), [], [_short_spy()], StandDownState(), 0, env=_env()
    )

    assert "were not compared against the journal" in body


def test_the_board_route_compares_the_broker_against_the_journal(journal, dreams):
    """End to end against `MockBroker`, because the wiring is the thing that
    was missing rather than the renderer.

    A short SPY position, a stop leg resting at 805, a journal row that says
    820 and no recorded action anywhere: the Board must tag it.
    """
    from bot.broker import MockBroker
    from bot.models import OrderStatus, Position

    broker = MockBroker(starting_equity=100_000.0)
    broker.connect()
    broker.set_price("SPY", bid=809.98, ask=810.02)
    broker._positions["SPY"] = Position(
        symbol="SPY",
        direction=Direction.SELL,
        qty=21,
        entry_price=773.32,
        opened_at=ENTRY,
        current_price=810.0,
    )
    broker.set_open_orders(
        [
            WorkingOrder(
                order_id="stop-1",
                symbol="SPY",
                direction=Direction.BUY,
                qty=21,
                # A plain string, as the broker reports it. `WorkingOrder.is_stop`
                # reads it, so this is what makes the leg a stop leg.
                order_type="stop",
                stop_price=805.0,
                status=OrderStatus.NEW,
                submitted_at=ENTRY,
            )
        ]
    )
    journal.record_entry(_short_spy())

    import bot.main as main_mod

    def _fixed(env: object, force_mock: bool = False) -> MockBroker:
        return broker

    original = main_mod.build_broker
    main_mod.build_broker = _fixed
    try:
        primed = live.build_poller(journal=journal, env=_env(), force_mock=True)
        primed.poll_once()
        app = build_app(
            journal=journal, rules=load_rules(), env=_env(),
            dreams=dreams, poller=primed, force_mock=True
        )
        body = TestClient(app).get("/").text
    finally:
        main_mod.build_broker = original

    assert "unexplained-move" in body
    assert "805.0000" in body
    assert "no recorded action" in body


def test_a_symbol_and_its_badges_travel_inside_one_element():
    """The same 760px trap as the "at risk" cell, one column across.

    A `td` is a `space-between` flex row down there with the label injected as
    generated content, so `<b>SPY</b>` beside two bare pills is four flex items
    flung across the card as though the symbol and its tags were four separate
    fields.
    """
    from bot.position_actions import UnexplainedMove, UnexplainedMoveReport

    body = render._positions(
        _held(),
        [_short_spy(current_stop=800.0)],
        100_000.0,
        UnexplainedMoveReport(
            moves=[
                UnexplainedMove(
                    symbol="SPY", kind="stop", journal_value=800.0, broker_value=795.0
                )
            ]
        ),
    )

    assert '<td data-l="Symbol"><span><b>SPY</b>' in body
    assert '</span></span></td>' in body
    # Adjacent pills are bordered inline-blocks, so with no whitespace between
    # them the two borders meet and read as one badge.
    assert "</span> <span class=\"pill unexplained\">" in body
    assert "justify-content:space-between" in render.STYLES


def test_a_recorded_tighten_is_not_dressed_as_a_warning():
    """The alert colour has to keep meaning something.

    A stop that moved WITH a reason on file is the feature working; a stop that
    moved without one is the finding. Colouring both amber spends the warning
    colour on the ordinary case, after which it says nothing on the row where
    it matters.
    """
    from bot.position_actions import UnexplainedMove, UnexplainedMoveReport

    body = render._positions(
        _held(),
        [_short_spy(current_stop=800.0)],
        100_000.0,
        UnexplainedMoveReport(
            moves=[
                UnexplainedMove(
                    symbol="SPY", kind="stop", journal_value=800.0, broker_value=795.0
                )
            ]
        ),
    )

    assert '<p class="note">SPY was sized against 820.0000' in body
    assert '<p class="note"><span class="alert">SPY: stop is 795.0000' in body
    # And never both classes on one element: `.note` is declared after `.alert`,
    # so the two together resolve to pewter and the warning loses its colour.
    assert 'class="note alert"' not in body


# ============================================================== resting orders
#
# The operator has now twice read the same live stop leg as debris. The first
# time an SDK enum rendered it as status `other`; the second time the HEADING
# over it said "Pending orders", which claims the agent is part-way into a new
# trade. A heading is a claim, and a wrong one is not fixed by the row
# underneath being correct.


def test_a_protective_leg_is_never_filed_under_pending():
    """"Pending" infers the agent is about to put another trade down.

    A resting stop is the opposite claim — it is the guarantee that a loss will
    not run. Filing it under a heading that says something is about to happen is
    what made a live stop leg read as clutter.
    """
    body = render._working_orders(
        [_order(stop_price=820.0, order_type="stop")],
        {"SPY": 773.10},
        positions=[_position(direction=Direction.SELL)],
    )

    protective = body.index("<h2>Stop losses and targets in force</h2>")
    pending = body.index("<h2>Pending entries</h2>")
    assert protective < pending
    # The stop row is above the pending heading, so it is in the other group.
    assert body.index("820.0000") < pending
    assert "No entries pending" in body[pending:]
    assert "Pending orders" not in body


def test_a_take_profit_leg_is_protective_even_though_it_is_a_limit_order():
    """The bracket's other child is a plain limit order and is every bit as
    much a consequence of the position as the stop is. Classifying on `is_stop`
    would leave it in the pending list, where "waiting to become a position" is
    exactly the wrong reading of an order that closes one."""
    body = render._working_orders(
        [_order(limit_price=740.0, order_type="limit")],
        {"SPY": 773.10},
        positions=[_position(direction=Direction.SELL)],
    )

    assert body.index("740.0000") < body.index("<h2>Pending entries</h2>")


def test_an_entry_on_a_symbol_nothing_is_held_in_stays_pending():
    """The other half of the rule. Without a position there is nothing for an
    order to be the other side of, so it is an entry and "pending" is true."""
    body = render._working_orders(
        [_order(limit_price=641.20, order_type="limit")], {"SPY": 648.02}
    )

    assert body.index("641.2000") > body.index("<h2>Pending entries</h2>")


def test_an_order_adding_to_a_position_is_not_called_protective():
    """A buy resting under a long adds to it. Only an order that would REDUCE
    what is held is protection, which is why the classification reads the
    direction rather than assuming any order on a held symbol is a leg."""
    body = render._working_orders(
        [_order(direction=Direction.BUY, limit_price=500.0, order_type="limit")],
        {"SPY": 773.10},
        positions=[_position(direction=Direction.BUY)],
    )

    assert body.index("500.0000") > body.index("<h2>Pending entries</h2>")


def test_a_protective_leg_states_the_level_it_triggers_at():
    """`WorkingOrder.stop_price` and `order_type` exist for exactly this. A leg
    whose trigger cannot be read is most of the way to no stop at all."""
    body = render._working_orders(
        [_order(stop_price=820.0, order_type="stop")],
        {"SPY": 773.10},
        positions=[_position(direction=Direction.SELL)],
    )

    assert "820.0000" in body
    assert "% away" in body


def test_an_unreadable_trigger_in_the_protective_group_is_its_own_state():
    """On a plain limit order a missing stop price is correct and dull. On the
    leg holding rule 3 up it means nobody can say where the stop is, and the two
    must not print the same blank."""
    body = render._working_orders(
        [_order(order_type="stop")],
        {"SPY": 773.10},
        positions=[_position(direction=Direction.SELL)],
    )

    assert '<span class="alert">unknown</span>' in body
    assert "nobody can say where the stop is" in body


def test_a_position_with_no_protective_leg_is_loud():
    """Grouped, the absence is legible for the first time. In one list an open
    position with nothing resting behind it was invisible."""
    from bot.position_actions import UnexplainedMoveReport

    body = render._working_orders(
        [],
        {"SPY": 773.10},
        positions=[_position(direction=Direction.SELL)],
        unexplained=UnexplainedMoveReport(positions_without_a_resting_stop=["SPY"]),
    )

    assert '<span class="alert">SPY is open with no stop order' in body


def test_an_unreadable_order_book_is_not_an_empty_one():
    """`AlpacaBroker.get_open_orders` catches its own errors and returns `[]`,
    so an outage arrives here indistinguishable from a clean book. Reporting
    "nothing resting" would be an all-clear nobody established — the
    `FinnhubCalendar.is_degraded` rule at the order book."""
    from bot.position_actions import UnexplainedMoveReport

    body = render._working_orders(
        [],
        {"SPY": 773.10},
        positions=[_position(direction=Direction.SELL)],
        unexplained=UnexplainedMoveReport(can_check=False),
    )

    assert "could not be read" in body
    assert "Nothing is resting behind the open positions." not in body
    assert "No entries pending." not in body


def test_a_stop_with_no_position_behind_it_is_named_rather_than_assumed():
    """The bot submits no stop entries, so a stop with nothing held is a leg
    whose position has gone — closed by hand, or filled and not read back. It is
    filed under neither heading silently."""
    body = render._working_orders(
        [_order(direction=Direction.SELL, stop_price=201.0, order_type="stop")],
        {"SPY": 205.0},
    )

    assert "a stop with no position on this reading" in body


def test_the_board_offers_no_way_to_cancel_a_resting_stop():
    """There is no `cancel_order` on the `Broker` protocol, and a button that
    took a stop off a dashboard would be the shortest route in this repository
    to an unprotected live position."""
    body = render._working_orders(
        [_order(stop_price=820.0, order_type="stop")],
        {"SPY": 773.10},
        positions=[_position(direction=Direction.SELL)],
    ).lower()

    for control in ("<form", "<button", "cancel_order", 'href="/cancel'):
        assert control not in body


# ------------------------------------------------------- was the loop running
#
# `jobs.py` recorded every pass and only the CLI could read it, so the one
# surface an operator opens could not answer the question the module was
# written for.


def _history(
    *passes: tuple[float, Any, dict[str, Any]],
    now: datetime | None = None,
    **kw: Any,
) -> Any:
    """A `JobHistory` built the way `jobs.history` builds one, from records."""
    from bot import jobs

    moment = now or datetime(2026, 5, 4, 18, 0, tzinfo=UTC)
    built = []
    for minutes, outcome, extra in passes:
        started = moment - timedelta(minutes=minutes)
        built.append(
            jobs.Job(
                outcome=outcome,
                started_at=started,
                recorded_at=started + timedelta(seconds=3),
                interval_seconds=900.0,
                **extra,
            )
        )
    built.sort(key=lambda j: j.started_at, reverse=True)
    return jobs.JobHistory(
        jobs=built,
        window_from=moment - timedelta(hours=24),
        window_to=moment,
        **kw,
    )


def test_the_board_says_whether_the_loop_is_running_at_all():
    from bot import jobs

    history = _history(
        (10, jobs.Outcome.RAN, {"proposals": 0, "approved": 0, "executed": 0}),
        (25, jobs.Outcome.SKIPPED, {"detail": "no class in session"}),
        (40, jobs.Outcome.FAILED, {"detail": "read timed out"}),
    )
    body = render.loop_activity(history)

    assert "The decision loop" in body
    assert 'data-loop="ran"' in body
    # The three outcomes counted apart, and the quiet cycle counted apart from
    # the skip: one looked and stood pat, the other never looked.
    assert "market shut, never looked" in body
    assert "no decision obtained" in body
    assert "1 proposed nothing" in body


def test_no_recorded_pass_is_never_reported_as_a_quiet_loop():
    """The fourth state, and the one that is not an outcome. An empty window
    means the loop was stopped, restarting, or its record was lost."""
    body = render.loop_activity(_history())

    assert "No pass of the loop is on file" in body
    assert "NOT a report that it ran and found nothing to do" in body
    assert '<span class="alert">' in body


def test_a_moment_nothing_covers_is_not_a_moment_out_of_range():
    """`Coverage.OUT_OF_RANGE` says the read could not speak for the moment;
    `NOT_RECORDED` says nothing covers it. Opposite claims about one silence,
    and they must not share a colour or a sentence."""
    from bot import jobs

    now = datetime(2026, 5, 4, 18, 0, tzinfo=UTC)
    # One pass three hours ago: `now` is far past its cadence horizon.
    uncovered = render.loop_activity(
        _history((180, jobs.Outcome.RAN, {"proposals": 0}), now=now), now=now
    )
    # The same history, asked about a moment before the window began.
    outside = render.loop_activity(
        _history((180, jobs.Outcome.RAN, {"proposals": 0}), now=now),
        now=now - timedelta(hours=48),
    )

    assert 'data-loop="not_recorded"' in uncovered
    assert "recorded no pass covering" in uncovered
    assert 'data-loop="out_of_range"' in outside
    assert "outside the span that was read" in outside


def test_the_loop_card_asks_about_the_moment_the_log_was_read():
    """Found on the rendered page, not by the suite.

    `JobHistory.at` refuses anything past `window_to`, and `window_to` is
    stamped when the log is READ. A fresh `datetime.now()` at render time is
    always a hair later, so every single load answered "outside the span that
    was read" over a history holding fifteen passes. A test passing a fixed
    `now` cannot see it: the bug is that the two clocks are read twice.
    """
    from bot import jobs

    audit_now = datetime.now(UTC)
    history = _history(
        (1, jobs.Outcome.RAN, {"proposals": 0, "approved": 0, "executed": 0}),
        now=audit_now,
    )

    body = render.loop_activity(history)

    assert "out of range" not in body.lower()
    assert 'data-loop="ran"' in body


def test_an_incomplete_reading_of_the_loops_day_says_so():
    """A short read must not render as a quiet loop. Same trap as
    `seen.reaches_past_marker`."""
    from bot import jobs

    body = render.loop_activity(
        _history(
            (10, jobs.Outcome.RAN, {"proposals": 0}),
            truncated=True,
            unreadable_records=2,
        )
    )

    assert "known to be incomplete" in body
    assert "hit its limit" in body
    assert "could not be read" in body


def test_no_history_at_all_is_its_own_answer():
    """`None` is "not read for this render", which is not "the loop is down"."""
    body = render.loop_activity(None)

    assert "was not read for this render" in body
    assert "No pass of the loop is on file" not in body


def test_the_board_carries_the_loop_card_on_a_cold_start_too(client):
    """It comes off the audit log rather than the broker, so it is answerable
    before the first reading lands — which is exactly when somebody wants to
    know whether the thing that trades is running."""
    from bot import jobs

    cold = render.board(
        None, load_rules(), [], [], StandDownState(), 0, env=_env(),
        loop=_history((10, jobs.Outcome.RAN, {"proposals": 0})),
    )

    assert "The decision loop" in cold
    assert client.get("/").status_code == 200
    assert "The decision loop" in client.get("/").text


# =================================================================== symbiosis
#
# A fusion is a STATE the store reached in the background, and the animation is
# a notification about it. The card is rendered already fused; the client may
# only ever add the transient `joining` class.


def _fusion_pair(store: DreamStore) -> tuple[int, list[int]]:
    """Two dreams sharing a claim, fused. Returns (child_id, parent_ids)."""
    from bot.dreaming import DREAMER, Dream, Hop, VaultCaps

    shared = "Transits are rationed by draught, and draught is rainfall."
    a = store.save(
        Dream(
            title="Rainfall and the landed cost of everything",
            seed="Nobody reads a lake.",
            chain=[
                Hop(shared, checked=True, source="ACP advisories"),
                Hop("Marginal cargo moves to the long route."),
            ],
        )
    )
    b = store.save(
        Dream(
            title="Separation capacity is the bottleneck",
            seed="Everyone counts deposits.",
            chain=[
                Hop(shared, checked=True, source="ACP advisories"),
                Hop("Capacity outside one country is a short list."),
            ],
        )
    )
    result = store.fuse(
        [a, b], by=DREAMER, caps=VaultCaps(workbench=50, prophecy=50, vault=50)
    )
    assert result.ok, result.detail
    assert result.dream_id is not None
    return result.dream_id, [a, b]


def test_a_fusion_card_is_fused_in_the_markup_the_server_sends(client, dreams):
    """The fail-to-visible rule where it is easiest to get wrong.

    It is very natural to draw two cards and have JavaScript merge them, and
    that fails to two cards and a lie. Script off, script threw, reduced motion:
    all show a fused card with both parent titles and the shared hop as text.
    """
    child, parents = _fusion_pair(dreams)
    body = client.get("/dreaming").text

    assert f'<article class="dream fused" id="dream-{child}"' in body
    assert "The hop they both stand on" in body
    assert "Transits are rationed by draught" in body
    for parent in parents:
        assert f'href="#dream-{parent}"' in body


def test_a_parent_renders_alive_and_linked(client, dreams):
    """`fuse` consumes nothing and the parents keep everything they had. A crest
    greyed out or struck through would describe a store that does not work that
    way."""
    _child, parents = _fusion_pair(dreams)
    body = client.get("/dreaming").text

    crest = body[body.index('<a class="crest"') :][:400]
    assert "text-decoration:line-through" not in render.STYLES
    assert "Both parents are unchanged and still on their own shelves" in body
    assert f"#{parents[0]}" in crest


def test_a_fusion_badge_is_read_and_never_recomputed(dreams):
    """A card that counted `checked` flags for itself would show a better badge
    than the dream claims. The union of two chains can read PARTIAL when a
    parent was UNVERIFIED, and combining two arguments is not evidence for
    either."""
    from bot.dreaming import Verification

    child, _ = _fusion_pair(dreams)
    dream = dreams.get(child)
    dream.verification_ceiling = Verification.UNVERIFIED

    body = render._dream(dream)

    assert '<span class="pill unverified">' in body
    assert "Capped at the weakest parent" in body
    # The chain itself has a checked hop, so a card doing its own arithmetic
    # would have printed `partial` here.
    assert dream._counted_verification() is Verification.PARTIAL


def test_a_fusion_is_not_dressed_as_an_endorsement(dreams):
    """It has no weakest hop and no verdict BY DESIGN — `fuse` refuses to
    inherit either, because claiming the combined chain had been examined would
    back-date an attack nobody made. That is a state, not a defect, and it is
    refused promotion on the verdict clause long before the weak-link one."""
    child, _ = _fusion_pair(dreams)
    body = render._dream(dreams.get(child))

    assert "Nobody has attacked this yet" in body
    # Not the stuck-keep explanation: a fusion is not a failing keep.
    assert "Not a prophecy yet" not in body
    assert "stronger" not in body.lower()


def test_a_fusion_inherits_every_condition_and_the_page_says_so(dreams):
    """A fusion carries both parents' conditions, so it is HARDER to promote
    than either of them. A long unmet list must not read as a failing."""
    from bot.dreaming import DreamCondition
    from bot.models import TriggerField, TriggerOp

    child, _ = _fusion_pair(dreams)
    dream = dreams.get(child)
    dream.conditions = [
        DreamCondition(
            text="one",
            symbol="SPY",
            field=TriggerField.CLOSE,
            op=TriggerOp.ABOVE,
            value=1.0,
        ),
        DreamCondition(
            text="two",
            symbol="SPY",
            field=TriggerField.CLOSE,
            op=TriggerOp.ABOVE,
            value=2.0,
            fulfilled=True,
        ),
    ]

    body = render._dream(dream)

    assert "1 of 2 conditions met" in body
    assert "every condition both parents wrote" in body


def test_a_first_visit_does_not_animate_the_vault(client, dreams):
    """`Marker.is_new` answers `None` with no marker rather than True. A vault
    of dreams all animating at once on a first load is a fireworks display, not
    a notification."""
    _fusion_pair(dreams)
    body = client.get("/dreaming").text

    # The attribute the client looks for is absent from the markup entirely.
    # `data-fuse-new` appears inside the inlined script as a selector, so the
    # assertion is on the rendered attribute rather than on the bare word.
    assert 'data-fuse-new="1"' not in body


def test_a_fusion_newer_than_the_marker_is_marked_for_the_reveal(dreams):
    """The other half. The server decides; the client plays it once."""
    from bot.web.seen import Marker, MarkerState

    child, _ = _fusion_pair(dreams)
    dream = dreams.get(child)
    marker = Marker(MarkerState.MARKED, dream.created_at - timedelta(hours=2))

    body = render.dreaming_page(
        [dream],
        DreamSummary.of([dream]),
        enabled=False,
        token="",
        hermes_available=False,
        soul_found=True,
        marker=marker,
    )

    assert 'data-fuse-new="1"' in body
    assert f'data-fuse-id="{child}"' in body


def test_the_fusion_client_is_emitted_and_its_styling_is_in_the_sheet():
    """`dream_fx` is wired up rather than sitting unreferenced. Its CSS has to
    reach `STYLES` so the no-control-character guard sees it too."""
    from bot.web import dream_fx

    page = render.dreaming_page(
        [], DreamSummary.of([]), enabled=False, token="",
        hermes_available=False, soul_found=True,
    )

    assert dream_fx.SCRIPT in page
    assert ".wisped .wisps b" in render.STYLES
    assert "fuse-flare" in render.STYLES


# ===================================================================== the vaults


def test_every_shelf_gets_a_section_even_when_it_is_empty(client, dreams):
    """`DreamSummary.by_vault` keys every vault for this reason: a missing row
    reads as "no data" and a nought reads as a stated fact. On the adopted
    shelf, where a row is a live symbol permission, those are very different
    claims."""
    from bot.dreaming import Dream, Vault

    dreams.save(Dream(title="On the bench", seed="s"))
    body = client.get("/dreaming").text

    for vault in Vault:
        assert f'id="shelf-{vault}"' in body
    assert "No positions open" not in body  # not this page's sentence
    assert "Nothing adopted. No dream is permitting a symbol right now." in body


def test_the_dream_vault_says_what_being_on_it_buys(client, dreams):
    """It is the only shelf the trading agent can see, so putting something
    there is an act rather than a filing decision — and what it buys is small."""
    from bot.dreaming import Dream

    # An empty store returns the worked example rather than the shelves, so a
    # dream has to exist for there to be shelves at all.
    dreams.save(Dream(title="On the bench", seed="s"))
    body = client.get("/dreaming").text

    assert "the only shelf the trading agent can see" in body.lower()
    assert "Not a position, not a size, not a direction." in body


def test_an_adopted_dream_shows_the_wisp_and_the_grant(client, dreams):
    """Two different facts side by side: what Grogu kept, and what the account
    may trade because of it. Only the second is a permission."""
    from bot.dreaming import DREAMER, Dream, DreamCondition, Vault, VaultCaps
    from bot.models import TriggerField, TriggerOp

    caps = VaultCaps(workbench=50, prophecy=50, vault=50)
    dream_id = dreams.save(
        Dream(
            title="Rail-car backlog",
            seed="A hopper out of service is a bushel that cannot move.",
            symbols=["SPY"],
            asset_class_key="us_equity",
            conditions=[
                DreamCondition(
                    text="c",
                    symbol="SPY",
                    field=TriggerField.CLOSE,
                    op=TriggerOp.ABOVE,
                    value=1.0,
                )
            ],
        )
    )
    dreams.move(dream_id, Vault.PROPHECY, by=DREAMER, caps=caps)
    dreams.move(dream_id, Vault.VAULT, by=DREAMER, caps=caps)
    assert dreams.adopt(dream_id, ttl_days=30, caps=caps).ok

    body = client.get("/dreaming").text

    assert "What Grogu kept" in body
    assert '<span class="lbl">Granted</span>' in body
    assert "SPY" in body
    assert "us_equity" in body
    # The motes go on the wisp, which is the one thing on the card describing
    # something that has been let go of.
    assert '<div class="wisptrace wisped">' in body


def test_the_transcript_is_rendered_so_a_person_can_read_it(client, dreams):
    """Stored either way, including exchanges that ended in nothing: a dream the
    trader kept declining is a fact about the dreamer worth having."""
    from bot.dreaming import DREAMER, OPERATOR, TRADER, Dream

    dream_id = dreams.save(Dream(title="Offered", seed="s"))
    dreams.add_message(dream_id, speaker=DREAMER, kind="offer", text="Here is one.")
    dreams.add_message(dream_id, speaker=TRADER, kind="question", text="What kills it?")
    dreams.add_message(dream_id, speaker=OPERATOR, kind="note", text="Leave it a week.")

    body = client.get("/dreaming").text

    assert "What was said about it, 3 turns" in body
    assert '<li class="said" data-who="dreamer">' in body
    assert "The trading agent" in body
    assert "What kills it?" in body


def test_the_fusion_note_in_a_parent_transcript_gets_a_name(dreams):
    """`speaker` is an open string and `fusion` is a new value the renderer
    meets. An unfamiliar one costs a badge rather than a turn."""
    _, parents = _fusion_pair(dreams)
    messages = dreams.messages(parents[0])

    body = render._transcript(messages)

    assert 'data-who="fusion"' in body
    assert "The store" in body
    assert "Fused into dream" in body


def test_a_trade_from_a_dream_carries_its_provenance(journal, dreams):
    """The motes say THAT it came from one; only the line says which, and a
    treatment that cannot be traced back to a record is decoration pretending to
    be provenance."""
    trade = Trade(
        symbol="QQQ",
        direction=Direction.BUY,
        qty=10,
        entry_price=500.0,
        planned_stop=490.0,
        entry_time=ENTRY,
        exit_price=510.0,
        exit_time=ENTRY + timedelta(days=1),
        strategy="trend_break",
        dream_id=7,
    )
    from bot.metrics import build_report

    body = render.trades_page([trade], build_report([trade]))

    assert '<span class="wisped">' in body
    assert 'href="/dreaming#dream-7"' in body
    assert "from dream #7" in body


def test_a_trade_with_no_dream_carries_no_motes(journal):
    from bot.metrics import build_report

    trade = Trade(
        symbol="SPY",
        direction=Direction.BUY,
        qty=10,
        entry_price=500.0,
        planned_stop=490.0,
        entry_time=ENTRY,
        exit_price=510.0,
        exit_time=ENTRY + timedelta(days=1),
        strategy="trend_break",
    )
    body = render.trades_page([trade], build_report([trade]))

    assert "wisped" not in body
    assert "from-dream" not in body


# ============================================================ the weakest link


def test_the_weakest_hop_is_marked_in_the_chain_and_never_recomputed(dreams):
    """Read the field. A page that worked out its own answer would disagree
    with `promotion_for`, and the rule is the one that decides which shelf a
    dream sits on."""
    from bot.dreaming import Dream, Hop

    dream = Dream(
        title="Pinned",
        seed="s",
        chain=[Hop("first", checked=True, source="x"), Hop("second")],
        weakest_hop="second",
        weakest_hop_index=2,
    )

    body = render._dream(dream)

    assert 'data-weakest=""' in body
    assert "the weakest link" in body
    assert '<circle class="node open" data-weakest=""' in body


def test_a_weakest_hop_naming_no_hop_says_it_was_not_established(dreams):
    """`resolved_weakest_hop` is deliberately not clamped: a model answering 9
    on a three-hop chain has named no hop. That is "which one was not
    established", never "there is no weak link"."""
    from bot.dreaming import Dream, Hop

    dream = Dream(
        title="Unplaced",
        seed="s",
        chain=[Hop("first", checked=True, source="x"), Hop("second")],
        weakest_hop="something that matches nothing in the chain",
        weakest_hop_index=9,
    )

    assert dream.resolved_weakest_hop is None
    body = render._dream(dream)

    assert "has not been established" in body
    assert 'data-weakest=""' not in body


def test_a_condition_says_which_hop_it_settles_or_that_it_settles_none(dreams):
    """"Pinned to hop 3" and "settles no hop yet" are different states, and the
    second is the actionable one."""
    from bot.dreaming import Dream, DreamCondition, Hop
    from bot.models import TriggerField, TriggerOp

    dream = Dream(
        title="Two conditions",
        seed="s",
        chain=[Hop("first", checked=True, source="x"), Hop("second")],
        weakest_hop="second",
        weakest_hop_index=2,
        conditions=[
            DreamCondition(
                text="pinned",
                symbol="SPY",
                field=TriggerField.CLOSE,
                op=TriggerOp.ABOVE,
                value=1.0,
                settles_hops=(2,),
            ),
            DreamCondition(
                text="floating",
                symbol="SPY",
                field=TriggerField.CLOSE,
                op=TriggerOp.ABOVE,
                value=2.0,
            ),
        ],
    )

    body = render._dream(dream)

    assert "Pinned to hop 2 — the weakest link." in body
    assert "Settles no hop yet." in body


def test_a_stuck_keep_explains_itself_in_the_rules_own_words(dreams):
    """A dream that has been attacked, kept, and will not promote reads as
    broken rather than incomplete unless the page says which clause holds it.
    The sentence is `promotion_for`'s, not a shorter paraphrase beside it."""
    from bot.dreaming import (
        Dream,
        DreamCondition,
        DreamStage,
        DreamVerdict,
        Hop,
        promotion_for,
    )
    from bot.models import TriggerField, TriggerOp

    dream = Dream(
        title="Stuck",
        seed="s",
        stage=DreamStage.VERDICT,
        verdict=DreamVerdict.KEEP,
        chain=[Hop("first", checked=True, source="x"), Hop("second")],
        weakest_hop="second",
        weakest_hop_index=2,
        conditions=[
            DreamCondition(
                text="on a link nobody doubted",
                symbol="SPY",
                field=TriggerField.CLOSE,
                op=TriggerOp.ABOVE,
                value=1.0,
            )
        ],
    )
    promotion = promotion_for(dream)
    assert not promotion.moves

    body = render._dream(dream)

    assert "Not a prophecy yet" in body
    assert render._e(promotion.reason) in body


def test_a_dream_still_being_worked_is_not_told_off(dreams):
    """Only a KEEP that will not move gets the explanation. "Stays on the
    workbench: the verdict is no verdict yet" is what a dream in progress is,
    and the stage pill already says it."""
    from bot.dreaming import Dream, Hop

    body = render._dream(
        Dream(title="In progress", seed="s", chain=[Hop("a claim")])
    )

    assert "Not a prophecy yet" not in body


# ========================================== what the two agents settled on ==
#
# The storing half refuses an incoherent row and skips one it cannot parse, so
# most of what follows is about the states a RENDERER can still get wrong: the
# three that are absences rather than values, and a badge that states an outcome
# the row does not claim.


def _verdict_row(
    verdict: ConferenceVerdict, **kw: object
) -> ConferenceDecision:
    kw.setdefault("decided_by", "trader")
    kw.setdefault("at", ENTRY)
    return ConferenceDecision(dream_id=1, verdict=verdict, **kw)  # type: ignore[arg-type]


def _wake_cond(
    text: str = "SPY closing below 641.20, roughly 1 ATR under the 20-day.",
) -> DreamCondition:
    return DreamCondition(
        text=text,
        symbol="SPY",
        field=TriggerField.CLOSE,
        op=TriggerOp.BELOW,
        value=641.2,
    )


def _offered_dream(vault: Vault | None = None) -> Dream:
    return Dream(title="Sesame after the brood", seed="s", vault=vault or Vault.VAULT)


def test_a_dream_nobody_has_conferred_about_is_not_a_no_decision():
    """`None` is a THIRD state, and the mildest verdict is not where it goes.

    Rendering it as `NO_DECISION` would report a conversation that never
    happened as one that happened and settled nothing — the same rule as
    `seen.py`'s first visit and `news_history.has_cycles`, arriving at the
    conference.
    """
    body = render._conference(_offered_dream(), None)

    assert "Never conferred" in body
    assert "Nobody has met about it" in body
    assert "Nobody decided" not in body
    assert "no decision" not in body
    assert "data-verdict" not in body


def test_why_a_dream_has_no_verdict_is_answered_per_shelf():
    """A workbench dream has never been offered because the trading agent
    cannot SEE that shelf, which is the machine working. A dream that reached
    ADOPTED with no exchange got there some other way, which is worth knowing.
    One sentence for both would make the first read like the second."""
    from bot.dreaming import Vault

    bench = render._conference(_offered_dream(Vault.WORKBENCH), None)
    vault = render._conference(_offered_dream(Vault.VAULT), None)
    adopted = render._conference(_offered_dream(Vault.ADOPTED), None)

    assert "only sees the dream vault" in bench
    assert "no exchange about it has been recorded yet" in vault
    assert "without a recorded exchange" in adopted
    for vault_enum in Vault:
        assert vault_enum in render.UNCONFERRED


def test_a_conference_record_that_could_not_be_read_says_so():
    """A fourth state, and the one that would otherwise be the worst.

    An unreadable store yields an empty mapping, and an empty mapping renders
    as "never conferred" on every card at once — a confident wrong claim about
    the one thing on the card nothing else can establish.
    """
    body = render._conference(_offered_dream(), None, readable=False)

    assert "could not be read" in body
    assert "not the same as their never having met" in body
    assert "Never conferred" not in body


def test_each_way_of_deciding_nothing_is_named_and_only_one_is_a_fault():
    """A bare "no decision" hides which, and the distinction is the value.

    A turn cap and an unconditioned deferral are facts about the two agents; a
    failed call is a fact about the machinery and says nothing about either of
    them. Only the last is anybody's fault, and the styling says so with BOTH
    attributes rather than by winning a declaration-order tie.
    """
    from bot.dreaming import ConferenceVerdict, NoDecision

    rendered = {
        cause: render._conference(
            _offered_dream(),
            _verdict_row(
                ConferenceVerdict.NO_DECISION, undecided=cause, decided_by="conference"
            ),
        )
        for cause in NoDecision
    }

    assert "did not converge" in rendered[NoDecision.TURNS_EXHAUSTED]
    assert "machinery broke" in rendered[NoDecision.CALL_FAILED]
    assert "could not name what would end the wait" in rendered[
        NoDecision.DEFER_WITHOUT_WAKE
    ]
    # Exactly one of the three claims a fault.
    faults = [c for c, body in rendered.items() if "is a fault" in body]
    assert faults == [NoDecision.CALL_FAILED]

    for cause, body in rendered.items():
        assert f'data-cause="{cause}"' in body
        assert 'data-verdict="no_decision"' in body
        assert "Nobody decided" in body

    css = render.STYLES
    assert (
        ".conclave[data-verdict=no_decision][data-cause=call_failed]" in css
    ), "the fault colour must outrank the no_decision rule on specificity"


def test_nobody_deciding_never_reads_as_one_of_the_four_decisions():
    """`decided` is read BEFORE `verdict`, which is what keeps this true.

    A sixth verdict added later is classified once, in `DECIDED_VERDICTS`, and
    an undecided one must not fall through into a decision's wording.
    """
    from bot.dreaming import ConferenceVerdict, NoDecision

    body = render._conference(
        _offered_dream(),
        _verdict_row(
            ConferenceVerdict.NO_DECISION,
            undecided=NoDecision.TURNS_EXHAUSTED,
            decided_by="conference",
            turns=6,
        ),
    )

    assert "The verdict" not in body
    for _badge, _lede in render.CONFERENCE_WORDS.values():
        assert _badge not in body
    # And who is named as recording it, never as having decided it.
    assert "recorded by The conference" in body


def test_a_call_that_failed_before_the_first_turn_says_so_in_words():
    """`0 turns` reads like a counter nobody wired up. A call that failed
    before the opening offer produced no turn at all, and that is a fact."""
    from bot.dreaming import ConferenceVerdict, NoDecision

    body = render._conference(
        _offered_dream(),
        _verdict_row(
            ConferenceVerdict.NO_DECISION,
            undecided=NoDecision.CALL_FAILED,
            decided_by="conference",
            turns=0,
        ),
    )

    assert "no turn taken" in body
    assert "0 turns" not in body


def test_a_deferral_renders_the_condition_that_would_wake_it():
    """The difference between a real deferral and running out of things to say,
    and the threshold is a NUMBER: a wake condition naming another figure would
    test a level nobody ever saw."""
    from bot.dreaming import ConferenceVerdict

    body = render._conference(
        _offered_dream(), _verdict_row(ConferenceVerdict.DEFER, wake=_wake_cond(), turns=6)
    )

    assert "What would wake it" in body
    assert "roughly 1 ATR under the 20-day" in body
    assert "SPY close below 641.2" in body
    assert "incomplete record" not in body


def test_a_deferral_with_no_wake_is_called_out_rather_than_drawn_empty():
    """A stored `DEFER` always carries a gradeable one — `is_coherent` refuses
    the row on write and the reader drops one it cannot parse. So a deferral
    reaching this renderer without a wake means the row was SKIPPED rather than
    shown, and saying so loudly is the only honest option: an empty deferral is
    exactly the shape the wake condition exists to make impossible."""
    from bot.dreaming import ConferenceVerdict

    body = render._conference(_offered_dream(), _verdict_row(ConferenceVerdict.DEFER))

    assert "incomplete record" in body
    assert 'class="alert"' in body
    # The alert colour on an INNER span, never beside `.note` on the `<p>`:
    # `.note` is declared after `.alert`, so the two together resolve to pewter.
    assert 'class="note alert"' not in body
    assert 'class="alert note"' not in body


def test_a_wake_nothing_can_look_up_is_not_presented_as_checkable():
    """`is_gradeable` is strictly narrower than `is_checkable`: a threshold with
    no symbol is a comparison with no subject."""
    from bot.dreaming import ConferenceVerdict, DreamCondition
    from bot.models import TriggerField, TriggerOp

    body = render._conference(
        _offered_dream(),
        _verdict_row(
            ConferenceVerdict.DEFER,
            wake=DreamCondition(
                text="a level, somewhere",
                field=TriggerField.CLOSE,
                op=TriggerOp.BELOW,
                value=641.2,
            ),
        ),
    )

    assert "Nothing can settle this" in body
    assert "no symbol" in body


def test_an_effect_that_was_never_attempted_is_not_a_failure():
    """`effected` is three-valued and `None` is the one that gets misread.

    A decline, a deferral and a no-decision all leave every shelf where it was,
    so there was nothing to attempt — which is a different answer from
    something being attempted and refused.
    """
    from bot.dreaming import ConferenceVerdict

    body = render._conference(
        _offered_dream(), _verdict_row(ConferenceVerdict.DECLINE, reason="Wrong instrument.")
    )

    assert "A decline moves nothing" in body
    assert "Nothing was attempted" in body
    assert "refused to carry it out" not in body
    assert "Carried out" not in body


def test_an_unrecorded_effect_on_a_verdict_that_asks_for_one_is_unknown():
    """`None` means two things, and only one of them is "nothing".

    On a deferral, a decline or a no-decision it is a stated fact: those leave
    every shelf where they found it. A promote and an archive both ASK something
    of a shelf, so "this verdict asks nothing of any shelf" over one of those is
    a wrong claim about the verdict itself — which is what the page said until
    somebody looked at it.
    """
    from bot.dreaming import ConferenceVerdict

    for verdict in (ConferenceVerdict.PROMOTE, ConferenceVerdict.ARCHIVE):
        body = render._conference(_offered_dream(), _verdict_row(verdict))
        assert "was not recorded" in body, verdict
        assert "unknown rather than a nothing" in body, verdict
        assert "asks nothing of any shelf" not in body, verdict
        assert 'class="alert"' in body, verdict


def test_a_deferral_does_not_promise_a_condition_it_may_not_have():
    """The lede used to end "…written below with a number in it", which on an
    incomplete row was contradicted four lines under it by the card's own
    alert. The condition's own heading makes the promise now, so the thing that
    keeps it is the thing that makes it."""
    from bot.dreaming import ConferenceVerdict

    lede = render.CONFERENCE_WORDS[ConferenceVerdict.DEFER][1]
    assert "written below" not in lede

    empty = render._conference(_offered_dream(), _verdict_row(ConferenceVerdict.DEFER))
    assert "What would wake it" not in empty
    assert "incomplete record" in empty


def test_an_agreement_the_store_refused_keeps_the_agreement():
    """They agreed and the shelf had no room, and those are two facts.

    The badge used to read `adopted` over a row that said two lines lower that
    nothing had moved — a heading stating an outcome the record does not claim.
    It names the DECISION now, which is true either way, and `effected` says
    which happened. Found by looking at the page.
    """
    from bot.dreaming import ConferenceVerdict

    refused = render._conference(
        _offered_dream(),
        _verdict_row(
            ConferenceVerdict.PROMOTE,
            reason="Yes. I want this one.",
            effected=False,
            effect_detail="The adopted vault is full: 3 of 3 slots are taken.",
        ),
    )
    landed = render._conference(
        _offered_dream(),
        _verdict_row(
            ConferenceVerdict.PROMOTE,
            effected=True,
            effect_detail="Adopted; the grant expires in 30 days.",
        ),
    )

    assert "agreed to adopt" in refused
    assert ">adopted<" not in refused
    assert "the store refused to carry it out" in refused.lower()
    assert "3 of 3 slots" in refused
    assert "The decision stands" in refused

    assert "Carried out" in landed
    assert "the grant expires in 30 days" in landed
    assert "refused" not in landed


def test_the_archive_verdict_is_the_dreamer_withdrawing_its_own_dream():
    """No verdict here is the trading agent's route to an archive.

    It may adopt out of the vault and hand back with a reason, and nothing
    else — and it cannot so much as recommend one, because a recommendation to
    destroy a chain from the party with a route to the broker would be pressure
    rather than an opinion. So this must never read as something done TO the
    dream by the conference.
    """
    from bot.dreaming import ConferenceVerdict

    body = render._conference(
        _offered_dream(),
        _verdict_row(
            ConferenceVerdict.ARCHIVE,
            decided_by="dreamer",
            reason="I checked the second hop and it is not true.",
            effected=True,
            effect_detail="Moved to the archive.",
        ),
    )

    assert "the dreamer withdrew it" in body
    assert "no archive verb" in body
    assert "cannot so much as recommend one" in body
    assert "Grogu" in body


def test_the_letting_go_treatment_says_nothing_the_words_do_not():
    """The motes are `dream_fx`'s mark for something let go of, and an archive
    verdict is the one that is. Decoration: `prefers-reduced-motion` takes them
    away, and everything they say is said in the sentence beside them."""
    from bot.dreaming import ConferenceVerdict
    from bot.web import dream_fx

    body = render._conference(
        _offered_dream(), _verdict_row(ConferenceVerdict.ARCHIVE, decided_by="dreamer")
    )

    assert 'class="conclave wisped"' in body
    assert '<span class="wisps">' in body
    reduced = dream_fx.CSS[dream_fx.CSS.index("prefers-reduced-motion") :]
    assert ".wisped .wisps{display:none}" in reduced
    # And no other verdict borrows it: a decline is not a letting-go.
    assert "wisps" not in render._conference(
        _offered_dream(), _verdict_row(ConferenceVerdict.DECLINE)
    )


def test_every_verdict_and_cause_carries_its_own_sentence():
    """The tables must cover the enums, and a gap must not take the page down.

    `_to_conference_decision` skips a value it cannot parse, so nothing unknown
    reaches the renderer from the store today — but a value added to
    `ConferenceVerdict` or `NoDecision` later would parse, and a `KeyError` on a
    page an operator opened to browse is the wrong failure. The first half of
    this keeps the copy honest; the second keeps the page up while somebody
    writes it.
    """
    from bot.dreaming import DECIDED_VERDICTS, ConferenceVerdict, NoDecision

    assert set(render.CONFERENCE_WORDS) == set(DECIDED_VERDICTS)
    assert ConferenceVerdict.NO_DECISION not in render.CONFERENCE_WORDS
    assert set(render.NO_DECISION_CAUSES) == set(NoDecision)

    unworded = render._unworded("some_future_verdict")
    assert "some_future_verdict" in unworded
    assert 'class="alert"' in unworded
    assert "has not been written" in unworded


def test_a_verdict_identity_never_depends_on_winning_a_tie():
    """`promote`, `defer`, `decline` and `archive` are words that name states.

    A `.conclave.archive` rule would put four more of them into the modifier
    vocabulary, where the next bare `.archive{padding:...}` written for
    something else silently restyles them. The shelves already answered this
    with `data-vault`; verdicts use `data-verdict` for the same reason, and an
    attribute selector cannot collide with a class at all.
    """
    import re

    from bot.dreaming import ConferenceVerdict

    css = re.sub(r"/\*.*?\*/", "", render.STYLES, flags=re.S)

    for verdict in ConferenceVerdict:
        assert f"[data-verdict={verdict}]" in css, verdict
        assert f".{verdict}{{" not in css, verdict
        assert f".{verdict} " not in css, verdict


def test_the_cross_dream_feed_is_newest_first_and_reaches_the_card():
    """The cards answer "what did they decide about this one"; the feed answers
    "what have those two been doing", which should not need eleven cards opened.

    Newest first, unlike the transcript inside a card — a feed is asked what
    happened most recently and a negotiation read backwards is a negotiation
    read backwards.
    """
    from datetime import timedelta

    from bot.dreaming import ConferenceDecision, ConferenceVerdict

    older = ConferenceDecision(
        dream_id=1,
        verdict=ConferenceVerdict.DECLINE,
        decided_by="trader",
        at=ENTRY,
        reason="Wrong instrument.",
    )
    newer = ConferenceDecision(
        dream_id=2,
        verdict=ConferenceVerdict.DEFER,
        decided_by="trader",
        at=ENTRY + timedelta(hours=3),
        reason="Park it.",
        wake=_wake_cond(),
    )

    body = render._conference_feed([newer, older], {1: "Sesame", 2: "Aluminium"})

    assert body.index("Aluminium") < body.index("Sesame")
    assert '<a href="#dream-1">Sesame</a>' in body
    assert "Would wake on" in body
    # A dream outside the window below is named, not linked to nothing.
    orphan = render._conference_feed([older], {})
    assert "Dream #1" in orphan
    assert 'href="#dream-1"' not in orphan


def test_an_empty_conference_feed_is_not_read_as_having_settled_nothing():
    """An exchange skipped because nothing changed writes no row at all, so an
    empty list is most often the change gate doing its job. Same rule as
    `news_history.has_cycles`: no cycles recorded is not "no news"."""
    body = render._conference_feed([], {})

    assert "No exchange has been recorded" in body
    assert "writes no row at all" in body
    assert "not the same as their having met and settled nothing" in body


def test_a_feed_that_could_not_be_read_says_unknown_rather_than_empty():
    body = render._conference_feed([], {}, readable=False)

    assert "could not be read" in body
    assert "not empty" in body
    assert "No exchange has been recorded" not in body


def test_everything_an_agent_wrote_reaches_the_page_escaped():
    """The reason is the deciding agent's own words and the title is the
    dreamer's. Server-side escaping, never `innerHTML`."""
    from bot.dreaming import ConferenceDecision, ConferenceVerdict

    hostile = ConferenceDecision(
        dream_id=1,
        verdict=ConferenceVerdict.DECLINE,
        decided_by="trader",
        at=ENTRY,
        reason="<script>alert(1)</script>",
    )

    card = render._conference(_offered_dream(), hostile)
    feed = render._conference_feed([hostile], {1: "<img onerror=x>"})

    for body in (card, feed):
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body
    assert "<img onerror=x>" not in feed
    assert "&lt;img onerror=x&gt;" in feed


def test_the_dreaming_route_carries_the_stored_verdict_onto_the_card(client, dreams):
    """End to end, through the store and the route rather than the renderer."""
    from bot.dreaming import (
        DREAMER,
        ConferenceDecision,
        ConferenceVerdict,
        Dream,
        Vault,
        VaultCaps,
    )

    caps = VaultCaps(workbench=50, prophecy=50, vault=50)
    offered = dreams.save(Dream(title="Offered", seed="s"))
    dreams.move(offered, Vault.PROPHECY, by=DREAMER, caps=caps)
    dreams.move(offered, Vault.VAULT, by=DREAMER, caps=caps)
    dreams.save(Dream(title="Untouched", seed="s"))
    dreams.record_conference_decision(
        ConferenceDecision(
            dream_id=offered,
            verdict=ConferenceVerdict.DEFER,
            decided_by="trader",
            at=ENTRY,
            reason="Park it against the level.",
            wake=_wake_cond(),
            turns=6,
        )
    )

    body = client.get("/dreaming").text

    assert "What the two agents have been deciding" in body
    assert "Park it against the level." in body
    assert "What would wake it" in body
    # And the one nobody has met about is a stated third state, on the same page.
    assert "Never conferred" in body


def test_the_verdict_sits_immediately_above_the_conversation_behind_it(dreams):
    """The conclusion is always visible; the six turns that produced it are one
    click away. A verdict buried in the last turn of a collapsed transcript is
    the state this record was stored to replace."""
    from bot.dreaming import ConferenceVerdict, DreamMessage

    body = render._dream(
        _offered_dream(),
        transcript=[
            DreamMessage(
                dream_id=1, speaker="trader", kind="verdict", text="No.", at=ENTRY
            )
        ],
        decision=_verdict_row(ConferenceVerdict.DECLINE, reason="Wrong instrument."),
    )

    assert body.index('class="conclave') < body.index('class="talk"')


# ================================================================ touch targets


def test_the_nav_links_clear_the_touch_target_minimum_on_a_phone():
    """MEASURED at a 390px viewport: eight links rendering 32px tall against a
    44px minimum, and they are the only route around the deck on a phone.

    Inside the 760px block on purpose, so the desktop bar keeps the compact
    links it lays out in one row beside the wordmark.
    """
    import re

    css = re.sub(r"/\*.*?\*/", "", render.STYLES, flags=re.S)
    start = css.index("@media (max-width:760px)")
    rest = css[start + 24 :]
    block = rest[: rest.index("@media")] if "@media" in rest else rest

    assert "nav a{min-height:44px" in block
    # And NOT outside it: the desktop bar lays the links out in one row beside
    # the wordmark and a 44px link there would grow the header for nothing.
    assert "nav a{min-height:44px" not in css[:start]
