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
from bot.dreaming import DREAMER, Dream, Vault
from bot.journal import Journal
from bot.risk import NewsWindow, RiskGate

from .conftest import INSIDE_SESSION, PAPER_EQUITY


class _FixedCalendar:
    """An earnings calendar that answers with exactly these windows.

    `StaticCalendar` filters against the wall clock, and the gate's clock here
    is pinned to a fixed session moment — so a real feed stub would answer
    "nothing upcoming" and the blackout tests would pass for the wrong reason.
    """

    def __init__(self, windows: list[NewsWindow] | None = None) -> None:
        self._windows = list(windows or [])

    def upcoming_windows(self, *, lookahead_minutes: int) -> list[NewsWindow]:
        del lookahead_minutes
        return list(self._windows)


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
    # The query index is derived from that audit directory, so it follows it
    # into tmp_path rather than building data/insight.db in the repo.
    session._insight_path = tmp_path / "insight.db"
    # The dream store follows the same rule as the journal and the index: its
    # path comes from the session, so the suite never opens `data/dreams.db`.
    session._dreams_path = tmp_path / "dreams.db"
    # The earnings calendar the news blackout reads. Injected rather than built,
    # for the reason every other store here is: `build_calendar_feed` would read
    # the environment and, on a box with a Finnhub key, reach the network — and
    # no test may do that. Empty by default; a blackout test replaces it.
    session._calendar = _FixedCalendar()
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


def test_place_order_journals_the_fill_so_the_cap_can_count_it(wired_session):
    """Placing without journalling is `14b88c8` arriving through another door.

    Alpaca holds a stop-loss as a separate order, so the broker cannot report
    what a position was designed to lose. The journal is the only place that
    knows. A position placed here with no entry therefore has an unknowable
    stop — and `AccountSnapshot.open_risk_usd` is what the 2% total-risk cap
    counts against, so the exposure is invisible to the cap and the next
    proposal is measured against a total that is missing this one.

    This tool wrote an audit event and no journal entry. The Board would have
    rendered the very first hand-placed trade as untracked, under an "open risk
    is understated" banner.
    """
    before = wired_session.journal.open_risk_usd()
    assert before == 0.0

    result = mcp_server.place_order(**_good_args())

    assert result["placed"]
    assert result["trade_id"] is not None

    open_trades = wired_session.journal.open_trades()
    assert len(open_trades) == 1
    trade = open_trades[0]
    assert trade.symbol == "SPY"
    # The planned stop is the field that carries across, and the reason the
    # entry exists at all.
    assert trade.planned_stop == _good_args()["stop_loss_price"]
    # Which is what makes the risk countable.
    assert wired_session.journal.open_risk_usd() > 0.0

    # Not folded into a strategy's record. Metrics group by strategy, and an
    # operator-directed trade in `mean_reversion`'s bucket would corrupt the
    # track record of a strategy that never proposed it.
    assert trade.strategy == "manual"


def test_a_refused_order_journals_nothing(wired_session):
    """A rejection must leave no trace in the journal. An entry with no
    position is the mirror of the bug above: risk counted against exposure that
    does not exist, which tightens the cap for no reason."""
    result = mcp_server.place_order(**_good_args(symbol="GME"))

    assert not result["placed"]
    assert wired_session.journal.open_trades() == []
    assert wired_session.journal.open_risk_usd() == 0.0


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
            "SPY", "buy", 3, 580.0, 575.0, "Opened outside the journal.",
            take_profit_price=590.0,
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


# ------------------------------------------------------------ query history


def test_query_history_answers_across_the_whole_log(wired_session):
    """The window tools answer 'lately'; this has to answer 'ever'."""
    from datetime import UTC, datetime, timedelta

    from bot.models import Decision, Direction, OrderProposal, RiskVerdict

    # Written straight into a dated file from three months ago.
    stamp = datetime.now(UTC) - timedelta(days=90)
    old = Decision(
        timestamp=stamp,
        proposals=[
            OrderProposal(
                symbol="AAPL",
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(old.model_dump_json() + "\n", encoding="utf-8")

    result = mcp_server.query_history(
        "SELECT symbol, reason FROM rejections ORDER BY ts DESC"
    )

    assert result["ok"], result
    assert result["rows"][0]["symbol"] == "AAPL"
    assert "per-trade cap" in result["rows"][0]["reason"]


def test_query_history_refuses_to_write(wired_session):
    from datetime import UTC, datetime

    from bot.models import Decision

    wired_session.audit.record(Decision(timestamp=datetime.now(UTC)))
    mcp_server.describe_history()

    result = mcp_server.query_history("DELETE FROM cycles")
    assert result["ok"] is False
    assert "allowed" in result["error"]

    # Still there.
    after = mcp_server.query_history("SELECT COUNT(*) c FROM cycles")
    assert after["rows"][0]["c"] == 1


def test_query_history_hints_at_the_schema_when_it_fails(wired_session):
    result = mcp_server.query_history("SELECT * FROM nonexistent")
    assert result["ok"] is False
    assert "describe_history" in result["hint"]


def test_describe_history_reports_the_live_schema(wired_session):
    from datetime import UTC, datetime

    from bot.models import Decision, MarketInputs

    wired_session.audit.record(
        Decision(timestamp=datetime.now(UTC), inputs=MarketInputs(headlines=["a story"]))
    )

    described = mcp_server.describe_history()

    assert "cycles" in described["tables"]
    assert "waiting_for" in described["tables"]["assessments"]
    assert described["row_counts"]["cycles"] == 1
    assert described["freshly_indexed"]["decisions"] == 1


def test_search_news_finds_an_item_outside_any_recent_window(wired_session):
    """get_recent_news covers a window; this has to reach further back."""
    from datetime import UTC, datetime, timedelta

    from bot.models import Decision, MarketInputs

    stamp = datetime.now(UTC) - timedelta(days=45)
    decision = Decision(
        timestamp=stamp,
        inputs=MarketInputs(headlines=["[MSFT] Something notable happened (2026-06-25)"]),
    )
    path = wired_session.audit._base / f"{stamp.date().isoformat()}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(decision.model_dump_json() + "\n", encoding="utf-8")

    # The window tool cannot see it...
    assert mcp_server.get_recent_news(hours=24)["headlines"] == []

    # ...and the search tool can, with the age attached.
    found = mcp_server.search_news(text="MSFT")
    assert found["ok"] is True
    assert found["item_count"] == 1
    assert found["items"][0]["age_hours"] > 24 * 40
    assert "not a live news search" in found["source"].lower()


def test_search_news_filters_by_kind(wired_session):
    from datetime import UTC, datetime

    from bot.models import Decision, MarketInputs

    wired_session.audit.record(
        Decision(
            timestamp=datetime.now(UTC),
            inputs=MarketInputs(
                headlines=["a headline"], social_posts=["[@someone 14:20] a post"]
            ),
        )
    )

    posts = mcp_server.search_news(kind="social")
    assert [i["text"] for i in posts["items"]] == ["[@someone 14:20] a post"]


def test_search_news_empty_result_does_not_claim_nothing_happened(wired_session):
    result = mcp_server.search_news(text="NOTHINGMATCHESTHIS")
    assert result["item_count"] == 0
    assert "not that nothing was published" in result["source"]


# --------------------------------------------------------------- the dreams
#
# The point of these is the same as the point of the order tests above: this
# surface must not become a way around the risk gate. Adoption grants a symbol
# permission and nothing else — no broker call, no position, no order — and a
# refusal has to come back as an explanation rather than as an exception or as
# silent success.


def _vaulted_dream(session: mcp_server._Session, **overrides: Any) -> int:
    """A dream sitting in the vault, ready to be adopted. Returns the id."""
    from bot.dreaming import DREAMER, Dream, Hop, Vault

    # Annotated, or mypy infers the narrowest common value type from the
    # literal and then rejects every splatted field against Dream's real
    # signature. The dict is heterogeneous by construction.
    fields: dict[str, Any] = {
        "title": "Cicada broods and the marginal sesame supplier",
        "seed": "Two of the three largest producers fall inside overlapping broods",
        "chain": [
            Hop(claim="Broods emerge on fixed multi-year cycles", checked=True, source="USDA"),
            Hop(claim="Indonesia has no periodical cicadas"),
        ],
        "weakest_hop": "That the harvest overlap is large enough to move a price",
        "symbols": ["SESM"],
        "asset_class_key": "us_equity",
    }
    fields.update(overrides)
    dream = Dream(**fields)
    dream_id: int = session.dreams.save(dream)
    assert session.dreams.move(dream_id, Vault.VAULT, by=DREAMER).ok
    return dream_id


def test_list_dreams_states_an_age_on_every_row(wired_session):
    """A dream offered three months ago and one offered this morning are
    different facts, and a readout that cannot tell them apart is the
    confident-partial-answer failure arriving through the dream surface."""
    _vaulted_dream(wired_session)

    result = mcp_server.list_dreams(vault="vault")

    assert result["store_readable"] is True
    row = result["shelves"]["vault"]["dreams"][0]
    assert row["age_days"] >= 0.0
    assert row["days_on_this_shelf"] >= 0.0
    assert row["expires_in_days"] is not None
    assert row["verification"] == "partial"
    assert row["unchecked_hops"] == 1


def test_list_dreams_reports_every_shelf_including_the_empty_ones(wired_session):
    """A missing key reads as 'no data' and a zero reads as a stated fact.

    Same reason `stop_watch` puts a breach count of zero on every cycle line:
    an empty shelf is an ordinary state, and it has to be visibly stated rather
    than inferred from an absence.
    """
    result = mcp_server.list_dreams()

    assert set(result["counts_by_vault"]) == {
        "workbench",
        "prophecy",
        "vault",
        "adopted",
        "archive",
    }
    assert result["counts_by_vault"]["adopted"] == 0
    assert "store_readable" in result["note"]


def test_an_unreadable_store_is_never_an_empty_vault(wired_session, monkeypatch):
    """The `FinnhubCalendar.is_degraded` lesson on the dream surface.

    A store that will not open produces exactly the same empty lists as a store
    with nothing in it, and only one of those says anything about the dreamer.
    It must also never reach the agent as a traceback: an agent handed a crash
    reports "I cannot see the dreams", which reads in a transcript as "there
    are no dreams".
    """

    def _boom(_path):
        raise RuntimeError("disk is on fire")

    wired_session._dreams = None
    monkeypatch.setattr(mcp_server, "DreamStore", _boom)

    for result in (
        mcp_server.list_dreams(),
        mcp_server.get_dream(1),
        mcp_server.dream_vault_status(),
        mcp_server.adopt_dream(1),
        mcp_server.return_dream(1, reason="no"),
        mcp_server.post_dream_message(1, speaker="trader", text="hello"),
    ):
        assert result["store_readable"] is False
        assert "disk is on fire" in result["error"]
        assert "NOT an empty vault" in result["note"]


def test_list_dreams_refuses_an_unknown_shelf_without_raising(wired_session):
    result = mcp_server.list_dreams(vault="cupboard")
    assert "not a shelf" in result["error"]
    assert "vault" in result["valid_vaults"]


def test_get_dream_returns_the_chain_the_conditions_and_the_transcript(wired_session):
    from bot.dreaming import TRADER

    dream_id = _vaulted_dream(wired_session)
    wired_session.dreams.add_message(
        dream_id, speaker=TRADER, kind="question", text="What would kill it?"
    )

    result = mcp_server.get_dream(dream_id)

    assert result["found"] is True
    assert [h["checked"] for h in result["chain"]] == [True, False]
    assert result["weakest_hop"]
    assert result["messages"][0]["speaker"] == TRADER
    # The age travels with the turn: a message from March is not part of a
    # conversation happening now.
    assert result["messages"][0]["age_days"] >= 0.0


def test_get_dream_missing_is_a_stated_fact(wired_session):
    result = mcp_server.get_dream(4242)
    assert result["found"] is False
    assert result["store_readable"] is True


def test_adopt_dream_grants_the_symbols_and_places_nothing(wired_session):
    """The whole safety argument, asserted rather than assumed.

    Adoption widens what may be CONSIDERED. It must not open a position, must
    not reach the broker, and must not leave anything resting there.
    """
    dream_id = _vaulted_dream(wired_session)

    result = mcp_server.adopt_dream(dream_id)

    assert result["ok"] is True
    assert result["grant"]["symbols_granted"] == ["SESM"]
    # A permission with no stated end is one nobody revisits.
    assert result["grant"]["expires_at"]
    assert result["grant"]["expires_in_days"] > 0
    assert wired_session.broker.get_account().open_positions == []
    assert wired_session.broker.get_open_orders() == []
    assert wired_session.journal.open_trades() == []


def test_adopt_refusal_explains_itself_rather_than_raising(wired_session):
    """A dream that is not in the vault cannot be adopted, and the answer is an
    explanation. `MoveResult` collects every reason at once, exactly as
    `RiskGate` does, so an agent is not told one thing at a time."""
    from bot.dreaming import Dream

    # On the workbench, with no symbols and no class: three refusals at once.
    dream_id = wired_session.dreams.save(Dream(title="half an idea", seed="..."))

    result = mcp_server.adopt_dream(dream_id)

    assert result["ok"] is False
    assert set(result["refusals"]) == {
        "wrong_vault",
        "needs_symbols",
        "needs_asset_class",
    }
    assert "will not change if it is asked again" in result["note"]


def test_return_dream_refuses_a_blank_reason(wired_session):
    """A return with no record is an argument reversed silently."""
    dream_id = _vaulted_dream(wired_session)
    assert mcp_server.adopt_dream(dream_id)["ok"]

    result = mcp_server.return_dream(dream_id, reason="   ")

    assert result["ok"] is False
    assert "needs_reason" in result["refusals"]
    # Still adopted, still granted.
    assert mcp_server.dream_vault_status()["symbols_in_force"] == {"SESM": "us_equity"}


def test_return_dream_withdraws_the_grant(wired_session):
    dream_id = _vaulted_dream(wired_session)
    mcp_server.adopt_dream(dream_id)

    result = mcp_server.return_dream(dream_id, reason="No edge I can size.")

    assert result["ok"] is True
    assert result["moved_to"] == "vault"
    assert mcp_server.dream_vault_status()["symbols_in_force"] == {}
    # The reason is in the transcript, where the dreamer can read it.
    assert "No edge I can size." in [m["text"] for m in mcp_server.get_dream(dream_id)["messages"]]


def test_vault_status_separates_what_is_claimed_from_what_is_in_force(wired_session):
    """A grant naming a disabled class grants nothing, and the gap between the
    two figures is the rules doing their job rather than an error."""
    dream_id = _vaulted_dream(wired_session, symbols=["DOGE/USD"], asset_class_key="crypto")
    assert mcp_server.adopt_dream(dream_id)["ok"]

    status = mcp_server.dream_vault_status()

    assert status["symbols_claimed_by_adoptions"] == {"DOGE/USD": "crypto"}
    # Crypto is configured while disabled in config/rules.yaml, so the class
    # hard block drops it. "Off" means off, whatever a dream says.
    assert status["symbols_in_force"] == {}
    assert status["shelves"]["adopted"]["held"] == 1
    assert status["shelves"]["adopted"]["cap"] == 3


def test_vault_status_names_a_grant_that_is_about_to_lapse(wired_session):
    dream_id = _vaulted_dream(wired_session)
    # Adopted with two days left rather than ninety.
    wired_session.dreams.adopt(dream_id, ttl_days=2)

    status = mcp_server.dream_vault_status()

    assert len(status["grants_expiring_soon"]) == 1
    assert status["grants_expiring_soon"][0]["expires_in_days"] <= 2
    assert status["expiring_within_days"] == mcp_server.GRANT_WARNING_DAYS


def test_post_dream_message_appends_a_turn(wired_session):
    dream_id = _vaulted_dream(wired_session)

    result = mcp_server.post_dream_message(
        dream_id, speaker="trader", text="What is the invalidation?", kind="question"
    )

    assert result["posted"] is True
    assert result["truncated"] is False
    assert [m["text"] for m in mcp_server.get_dream(dream_id)["messages"]] == [
        "What is the invalidation?"
    ]


def test_post_dream_message_refuses_an_id_that_does_not_exist(wired_session):
    """`dream_messages` has no foreign key, so the store would happily write a
    turn hanging off an id nobody can read back — a conversation that never
    happened, on a dream that does not exist."""
    result = mcp_server.post_dream_message(999, speaker="trader", text="hello?")

    assert result["posted"] is False
    assert "No dream with id 999" in result["error"]
    assert wired_session.dreams.messages(999) == []


def test_post_dream_message_refuses_an_empty_turn(wired_session):
    dream_id = _vaulted_dream(wired_session)
    result = mcp_server.post_dream_message(dream_id, speaker="trader", text="  \n ")
    assert result["posted"] is False
    assert wired_session.dreams.messages(dream_id) == []


def test_the_dream_tools_are_not_an_order_path():
    """The structural claim, checked structurally.

    Two verbs, and no more: there is no `move_dream` and no `delete_dream` on
    this surface, because the trading agent is the one with a route to the
    broker and therefore gets the smaller set. `DreamStore` refuses it both
    anyway; the absence of the tool is the readable half of that guarantee.

    Checked over the parsed syntax rather than the raw text, because the raw
    text includes the docstrings — which say the words "place_order" and
    "broker" on purpose, to tell the agent where those live. A grep-shaped test
    would either fail on its own explanation or force the explanation out.
    """
    import ast
    import inspect
    import textwrap

    names = {
        name
        for name, obj in vars(mcp_server).items()
        if inspect.isfunction(obj) and "dream" in name
    }
    assert "move_dream" not in names
    assert "delete_dream" not in names

    forbidden = {"broker", "place_order", "close_position", "OrderProposal", "_build_proposal"}
    for tool in (
        "list_dreams",
        "get_dream",
        "dream_vault_status",
        "adopt_dream",
        "return_dream",
        "post_dream_message",
    ):
        tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(mcp_server, tool))))
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert not (used & forbidden), f"{tool} reaches {sorted(used & forbidden)}"


# ---------------------------------- the blackout gate applies on THIS path too


def test_the_earnings_blackout_refuses_an_order_placed_here(wired_session):
    """It never had, and nothing said so.

    `check_order` ran the gate with no `news_windows` at all, so
    `RiskGate._news_blackout` could not fire on an order placed by the operator
    or through chat — while firing on every order the loop proposed. One rule in
    `config/rules.yaml`, enforced on one path, with the Settings page saying it
    applied to the account.
    """
    wired_session._calendar = _FixedCalendar(
        [NewsWindow(timestamp=INSIDE_SESSION, affected_symbols=frozenset({"SPY"}))]
    )

    result = mcp_server.check_order(**_good_args())

    assert not result["approved"]
    assert any("news blackout" in r for r in result["reasons"])


def test_an_order_inside_a_blackout_is_not_placed(wired_session):
    """The refusal has to survive the whole `place_order` path, not only the
    check that reports on it."""
    wired_session._calendar = _FixedCalendar(
        [NewsWindow(timestamp=INSIDE_SESSION, affected_symbols=frozenset({"SPY"}))]
    )

    result = mcp_server.place_order(**_good_args())

    assert result["placed"] is False
    assert wired_session.broker.get_account().open_positions == []
    assert wired_session.journal.open_trades() == []


def test_a_window_on_another_symbol_does_not_hold_this_order(wired_session):
    """The other half, so the test is about the blackout rather than about the
    calendar refusing everything."""
    wired_session._calendar = _FixedCalendar(
        [NewsWindow(timestamp=INSIDE_SESSION, affected_symbols=frozenset({"AAPL"}))]
    )

    assert mcp_server.check_order(**_good_args())["approved"]


# ------------------------------------- adopting cannot grant an unclaimed symbol


def test_adopt_dream_cannot_invent_the_symbols_it_grants(wired_session):
    """The tool hands `symbols` and `asset_class` to a model, and the store took
    both as given — so any dream in the vault was a token for granting yourself
    anything, one tool call away.

    The lock is in `DreamStore.adopt` rather than here, which is what makes it
    hold for the conference, the CLI and whatever calls it next.
    """
    store = wired_session.dreams
    dream_id = store.save(
        Dream(title="a perfectly ordinary equity dream", seed="s")
    )
    store.move(dream_id, Vault.VAULT, by=DREAMER)

    result = mcp_server.adopt_dream(
        dream_id=dream_id, symbols=["BTC/USD"], asset_class="us_equity"
    )

    assert result["ok"] is False
    assert "symbols_not_offered" in result["refusals"]
    assert mcp_server.dream_vault_status()["symbols_in_force"] == {}


def test_adopt_dream_still_takes_the_dreams_own_claim(wired_session):
    """The narrowing rule must not break the ordinary case."""
    store = wired_session.dreams
    dream_id = store.save(
        Dream(
            title="rare earth refiners",
            seed="s",
            symbols=["MP"],
            asset_class_key="us_equity",
        )
    )
    store.move(dream_id, Vault.VAULT, by=DREAMER)

    result = mcp_server.adopt_dream(dream_id=dream_id)

    assert result["ok"] is True, result
    assert result["symbols_in_force"] == {"MP": "us_equity"}


# ------------------------------------- the agent's control of its own position


def _seed_the_live_position(session: Any) -> None:
    """Journal row 1 plus its resting stop leg, on the live position's numbers.

    SHORT 21 SPY at 773.324285 with a stop at 820, and the pending BUY 21 that
    IS that stop leg. See TODO.md CURRENT STATE — and item 5, which is the
    record of what happens when a safety mechanism renders badly enough to look
    like debris.
    """
    from bot.models import Direction, Position, Trade, WorkingOrder

    session.journal.record_entry(
        Trade(
            symbol="SPY",
            direction=Direction.SELL,
            strategy="manual",
            qty=21,
            entry_time=INSIDE_SESSION,
            entry_price=773.324285,
            planned_stop=820.0,
            planned_target=None,
            rationale="Placed by hand as an operator test of the order path.",
        )
    )
    session.broker.set_price("SPY", bid=774.08, ask=774.10)
    session.broker._positions["SPY"] = Position(
        symbol="SPY",
        direction=Direction.SELL,
        qty=21,
        entry_price=773.324285,
        opened_at=INSIDE_SESSION,
        current_price=774.09,
    )
    session.broker.set_open_orders(
        [
            WorkingOrder(
                order_id="952237ac-d7ec-426e-bb5f-5c6ce7294260",
                symbol="SPY",
                direction=Direction.BUY,
                qty=21,
                stop_price=820.0,
                order_type="stop",
                broker_status="held",
            )
        ]
    )


def test_tighten_stop_moves_the_leg_and_stores_the_reason(wired_session):
    """The attended path the operator asked for: the agent's own position, its
    own decision, and the reason on file afterwards."""
    _seed_the_live_position(wired_session)

    result = mcp_server.tighten_stop(
        symbol="spy",
        new_stop_price=800.0,
        reason="Two sessions without a lower high; taking the room back.",
    )

    assert result["ok"] is True, result
    assert result["reached_broker"] is True
    assert result["risk_before_usd"] == 980.19
    assert result["risk_after_usd"] == 560.19
    assert result["risk_reduced_usd"] == 420.0

    # The leg at the broker actually moved, under a NEW id — Alpaca replaces a
    # leg rather than editing one.
    legs = [o for o in wired_session.broker.get_open_orders() if o.is_stop]
    assert [o.stop_price for o in legs] == [800.0]
    assert legs[0].order_id == result["broker_order_id"]

    # And the record reads back through the tool that exists to read it.
    readback = mcp_server.get_position_actions(symbol="SPY")
    assert readback["recorded_moves"] == 1
    assert "lower high" in readback["actions"][0]["reason"]
    assert readback["unexplained_moves"] == []


def test_tighten_stop_refuses_a_widen_and_leaves_the_position_alone(wired_session):
    """**The refusal this whole surface exists for.**

    `RiskGate.evaluate` never sees a position move, so nothing else in the
    system would catch this. Checked on three surfaces: the refusal, the leg
    still at its original trigger under its original id, and the journal
    untouched.
    """
    _seed_the_live_position(wired_session)

    result = mcp_server.tighten_stop(
        symbol="SPY",
        new_stop_price=850.0,
        reason="Give it room through the CPI print.",
    )

    assert result["ok"] is False
    assert any("REFUSED" in r for r in result["reasons"])
    assert "will not change the answer" in result["note"]

    legs = wired_session.broker.get_open_orders()
    assert [(o.order_id, o.stop_price) for o in legs] == [
        ("952237ac-d7ec-426e-bb5f-5c6ce7294260", 820.0)
    ]
    assert wired_session.journal.position_actions() == []
    assert wired_session.journal.open_trade_for("SPY").effective_stop == 820.0


@pytest.mark.parametrize("blank", ["", "   "])
def test_both_reasoned_tools_refuse_a_blank_reason(wired_session, blank):
    """The reason is the point. A blank one would be an explained move that
    explains nothing — and it would silence the unexplained-move report."""
    _seed_the_live_position(wired_session)

    tightened = mcp_server.tighten_stop(
        symbol="SPY", new_stop_price=800.0, reason=blank
    )
    assert tightened["ok"] is False
    assert any("reason is required" in r for r in tightened["reasons"])

    closed = mcp_server.close_position_with_reason(symbol="SPY", reason=blank)
    assert closed["ok"] is False

    assert wired_session.journal.position_actions() == []
    assert wired_session.broker.get_open_orders()[0].stop_price == 820.0
    assert wired_session.broker.get_account().open_positions != []


def test_close_position_with_reason_records_why(wired_session):
    """`record_exit` stores a price, a time and a realised figure, so afterwards
    a stop-hit and a deliberate close look identical. This is the part nothing
    else can reconstruct."""
    _seed_the_live_position(wired_session)

    result = mcp_server.close_position_with_reason(
        symbol="SPY", reason="Thesis is done; the gap filled this morning."
    )

    assert result["ok"] is True, result
    assert wired_session.broker.get_account().open_positions == []

    actions = wired_session.journal.position_actions()
    assert len(actions) == 1
    assert actions[0].action.value == "close"
    assert "gap filled" in actions[0].reason


def test_the_unreasoned_close_still_works_and_says_it_recorded_nothing(wired_session):
    """Closing must never be harder than opening — a tool that made getting out
    conditional on writing a sentence would be the stand-down mistake again. So
    it stays, and it is honest about the gap it leaves."""
    _seed_the_live_position(wired_session)

    result = mcp_server.close_position("SPY")

    assert result["closed"] is True
    assert result["reason_recorded"] is False
    assert "close_position_with_reason" in result["note"]
    assert wired_session.journal.position_actions() == []


def test_get_position_actions_reports_a_move_nobody_recorded(wired_session):
    """A stop pulled in through Alpaca's web UI shows nowhere else at all. A
    record that only listed the moves it captured would hide its own
    failures."""
    from bot.models import Direction as _Direction
    from bot.models import WorkingOrder

    _seed_the_live_position(wired_session)
    wired_session.broker.set_open_orders(
        [
            WorkingOrder(
                order_id="moved-by-hand",
                symbol="SPY",
                direction=_Direction.BUY,
                qty=21,
                stop_price=805.0,
                order_type="stop",
            )
        ]
    )

    readback = mcp_server.get_position_actions()

    assert readback["recorded_moves"] == 0
    assert len(readback["unexplained_moves"]) == 1
    assert "no recorded action" in readback["unexplained_moves"][0]
    assert readback["broker_orders_readable"] is True


def test_get_position_actions_says_the_loop_is_not_armed(wired_session):
    """So an agent describing its own powers describes them correctly: it has
    the two tools, and the unattended loop does not."""
    readback = mcp_server.get_position_actions()

    assert readback["loop_may_act_unattended"] is False
    assert "acts on none of them" in readback["loop_execution_note"]
