"""The market context, and the bars and indicators that now feed it.

The load-bearing assertions here are the negative ones: a symbol whose history
could not be fetched must be NAMED in the prompt, not quietly dropped. A symbol
that disappears from the indicators block is indistinguishable from one nobody
asked about, and the model would then reason about its live quote with no
history and nothing to say so.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from bot.broker import MockBroker
from bot.config import Rules, load_rules
from bot.context import (
    BUDGET_SPENT,
    HEADROOM_UNKNOWN,
    Ceiling,
    build_market_context,
    fetch_indicators,
    fetch_market_ticks,
    render_grants,
    render_sizing_ceilings,
    sizing_ceilings,
)
from bot.dreaming import Dream, Hop
from bot.grants import GrantBriefing
from bot.models import (
    AccountSnapshot,
    Bar,
    Direction,
    OrderProposal,
    Position,
    Tick,
)
from bot.risk import RiskGate

START = datetime(2026, 1, 5, tzinfo=UTC)


def daily_bars(symbol: str, count: int, *, start_price: float = 100.0) -> list[Bar]:
    """A gently rising series, oldest first, as every Alpaca bars endpoint returns."""
    return [
        Bar(
            symbol=symbol,
            timestamp=START + timedelta(days=i),
            open=start_price + i * 0.1,
            high=start_price + i * 0.1 + 1.0,
            low=start_price + i * 0.1 - 1.0,
            close=start_price + i * 0.1,
            volume=1_000_000.0,
        )
        for i in range(count)
    ]


@pytest.fixture
def account() -> AccountSnapshot:
    return AccountSnapshot(
        equity_usd=100_000.0, cash_usd=100_000.0, buying_power_usd=100_000.0
    )


# ------------------------------------------------------------------- the mock


def test_mock_broker_refuses_to_invent_bars_for_an_unseeded_symbol():
    """An empty list would be indistinguishable from "this symbol has no history"."""
    with pytest.raises(KeyError):
        MockBroker().get_daily_bars("SPY")


def test_mock_broker_returns_the_most_recent_bars_within_the_lookback():
    broker = MockBroker()
    broker.set_bars("SPY", daily_bars("SPY", 300))

    bars = broker.get_daily_bars("SPY", 50)

    assert len(bars) == 50
    assert bars[-1].timestamp == START + timedelta(days=299)


# ------------------------------------------------------------------- fetching


class ExplodingBroker(MockBroker):
    """A broker that fails the way the Alpaca SDK actually fails.

    `APIError` and `httpx.ReadTimeout` are neither `KeyError` nor
    `RuntimeError`, which is the whole point: the narrow catch these two
    functions started with would have let them through.
    """

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    def get_daily_bars(self, symbol: str, lookback: int = 260) -> list[Bar]:
        raise self._error

    def get_tick(self, symbol: str) -> Tick:
        raise self._error


@pytest.mark.parametrize(
    "error",
    [
        Exception("APIError: rate limit exceeded"),
        TimeoutError("read timed out"),
        ValueError("Expecting value: line 1 column 1 (char 0)"),
    ],
    ids=["api_error", "timeout", "bad_json"],
)
def test_a_broker_failure_degrades_the_cycle_rather_than_ending_the_loop(error, account):
    """A trading loop that dies quietly is worse than one that trades badly.

    When the bars endpoint has a bad minute the journal stops being reconciled
    and open positions stop being watched, with nothing on screen to say so.
    So a failed fetch has to come back as "no history for this symbol", which
    is already the honest description of what the model has.
    """
    broker = ExplodingBroker(error)

    indicators, missing = fetch_indicators(broker, ["SPY", "QQQ"])
    ticks = fetch_market_ticks(broker, ["SPY", "QQQ"])

    assert indicators == {}
    assert missing == ["SPY", "QQQ"]
    assert ticks == {}

    context = build_market_context(
        account=account,
        ticks=ticks,
        headlines=[],
        news_windows=[],
        indicators=indicators,
        symbols_without_history=missing,
    )
    assert "NO PRICE HISTORY AVAILABLE for: QQQ, SPY" in context


def test_fetch_indicators_names_the_symbols_it_could_not_price():
    broker = MockBroker()
    broker.set_bars("SPY", daily_bars("SPY", 250))
    # QQQ is deliberately left unseeded.

    indicators, missing = fetch_indicators(broker, ["SPY", "QQQ"])

    assert set(indicators) == {"SPY"}
    assert missing == ["QQQ"]


def test_a_symbol_with_no_history_is_called_out_in_the_prompt(account):
    """The whole point of the second return value from fetch_indicators."""
    broker = MockBroker()
    broker.set_bars("SPY", daily_bars("SPY", 250))
    broker.set_price("QQQ", 500.0, 500.04)

    indicators, missing = fetch_indicators(broker, ["SPY", "QQQ"])
    context = build_market_context(
        account=account,
        ticks=fetch_market_ticks(broker, ["QQQ"]),
        headlines=[],
        news_windows=[],
        indicators=indicators,
        symbols_without_history=missing,
    )

    assert "NO PRICE HISTORY AVAILABLE for: QQQ" in context
    assert "propose nothing on them" in context


# -------------------------------------------------------------------- context


def test_context_carries_computed_indicators_rather_than_raw_bars(account):
    """The model is handed answers. Bars would invite it to do the arithmetic."""
    broker = MockBroker()
    broker.set_bars("SPY", daily_bars("SPY", 250))
    indicators, _ = fetch_indicators(broker, ["SPY"])

    context = build_market_context(
        account=account,
        ticks={},
        headlines=[],
        news_windows=[],
        indicators=indicators,
    )

    assert "## Indicators (computed from daily bars, not estimated)" in context
    assert "20-day average:" in context
    assert "200-day average:" in context
    assert "ATR(14):" in context
    assert "distance from the 20-day average:" in context
    # Nothing that looks like a bar series, which is what would prompt the model
    # to start averaging things itself.
    assert "open" not in context.lower().split("## indicators")[1].split("##")[0]


def test_context_still_renders_with_no_indicators_at_all(account):
    """The loop must survive a total bars outage, saying so rather than crashing."""
    context = build_market_context(
        account=account, ticks={}, headlines=[], news_windows=[]
    )

    assert "## Indicators (computed from daily bars, not estimated)" in context
    assert "- (none)" in context


def test_short_history_is_rendered_as_unavailable_not_omitted(account):
    broker = MockBroker()
    broker.set_bars("SPY", daily_bars("SPY", 30))
    indicators, missing = fetch_indicators(broker, ["SPY"])

    context = build_market_context(
        account=account,
        ticks={},
        headlines=[],
        news_windows=[],
        indicators=indicators,
        symbols_without_history=missing,
    )

    assert missing == []
    assert "200-day average: unavailable" in context
    assert "NOT AVAILABLE for this symbol" in context


def test_watched_posts_lead_the_headlines_in_the_prompt(account):
    """A post moves the price before the wire carries it, so it is read first."""
    context = build_market_context(
        account=account,
        ticks={},
        headlines=["Reuters: steel makers rally"],
        news_windows=[],
        social_posts=["[@realDonaldTrump 14:31] Tariffs on steel imports"],
    )

    posts_at = context.index("Posts from watched accounts")
    headlines_at = context.index("## Recent headlines")

    assert posts_at < headlines_at
    assert "Tariffs on steel imports" in context
    assert "gates nothing" in context


def test_a_degraded_social_feed_says_so_in_the_prompt(account):
    """An empty list from a dead token must not read as a quiet morning."""
    context = build_market_context(
        account=account,
        ticks={},
        headlines=[],
        news_windows=[],
        social_posts=[],
        social_degraded=True,
    )

    assert "FEED DEGRADED" in context
    assert "does NOT mean nothing was posted" in context


# ---------------------------------------------------------------- the session


def test_the_session_block_precedes_the_quotes_it_qualifies(account):
    """Ordering is the point, not decoration. Every figure below the session
    block is a reading; the block says what an order built on those readings
    would actually become. A model that reads the snapshot first has anchored on
    a fill price it will not get."""
    from bot.config import load_rules

    context = build_market_context(
        account=account,
        ticks={},
        headlines=[],
        news_windows=[],
        instruments=load_rules().instruments,
        now=datetime(2026, 8, 10, 8, 45, tzinfo=UTC),   # 04:45 ET, pre-market
    )

    assert "## Session" in context
    assert context.index("## Session") < context.index("## Market snapshot")
    assert "PRE-MARKET" in context
    assert "RESTS" in context


def test_a_disabled_class_is_not_described_as_shut(account):
    """Crypto ships disabled. Listing it here would invite a proposal for a
    class the gate refuses on membership, and `build_system_prompt` already
    omits disabled classes for exactly that reason."""
    from bot.config import load_rules

    rules = load_rules()
    assert rules.instruments["crypto"].enabled is False

    context = build_market_context(
        account=account,
        ticks={},
        headlines=[],
        news_windows=[],
        instruments=rules.instruments,
        now=datetime(2026, 8, 10, 8, 45, tzinfo=UTC),
    )

    assert "crypto" not in context


def test_no_instruments_means_no_session_block_rather_than_a_guessed_one(account):
    """A caller that supplied no rules described no market. Computing a session
    from nothing would be a confident statement about hours nobody configured —
    the same reason `market_state` reports the gate window shut without them."""
    context = build_market_context(
        account=account, ticks={}, headlines=[], news_windows=[]
    )

    assert "## Session" not in context


# --------------------------------------------- the position the agent manages
#
# `model_client` asks for a `position_plan` on every open position, with an
# action of hold, close or TIGHTEN_STOP. The context block used to render
# direction, quantity, entry, current price and P&L and no stop at all, so the
# model was asked whether to tighten a level it had never been shown, and
# whether a thesis still held without the figure that says what being wrong
# costs. These pin the fix.


def _short_spy() -> AccountSnapshot:
    """The live position's own shape: SHORT 21 SPY, stop 820, risk $980.19."""
    from bot.models import Direction, Position

    return AccountSnapshot(
        equity_usd=100_000.0,
        cash_usd=116_239.81,
        buying_power_usd=200_000.0,
        open_positions=[
            Position(
                symbol="SPY",
                direction=Direction.SELL,
                qty=21,
                entry_price=773.324285,
                opened_at=START,
                current_price=774.18,
                unrealised_pnl_usd=-18.39,
            )
        ],
    )


def test_a_position_shows_the_stop_the_agent_is_asked_to_manage():
    account = _short_spy()
    account.planned_stop_by_symbol = {"SPY": 820.0}
    account.open_risk_by_symbol = {"SPY": 980.19}

    text = build_market_context(account=account, ticks={}, headlines=[], news_windows=[])

    assert "stop 820.0000" in text
    assert "980.19" in text
    # And it is framed as the agent's own position, not a thing it comments on.
    assert "yours to manage" in text


def test_a_position_with_no_journal_row_says_UNKNOWN_rather_than_nothing():
    """The failure this guards is a blank that reads like "no stop needed".

    A held position whose journal row is missing has real exposure and unknown
    protection. Those are different facts from "flat" and from "unprotected",
    and the prompt has to say which one it is. Same rule as
    `symbols_with_unknown_risk` refusing to be counted as zero.
    """
    account = _short_spy()  # no planned_stop_by_symbol, no open_risk_by_symbol

    text = build_market_context(account=account, ticks={}, headlines=[], news_windows=[])

    assert "STOP UNKNOWN" in text
    # Never a plausible wrong figure in place of the missing one.
    assert "stop 0" not in text
    assert "risking $0.00" not in text


def test_the_journalled_stop_is_not_presented_as_the_brokers():
    """planned_stop and the resting leg's trigger are two different facts.

    `WorkingOrder.stop_price` is the other half of the pair, and the whole
    reason both exist is that they can disagree. The prompt must not let the
    model assume they agree.
    """
    account = _short_spy()
    account.planned_stop_by_symbol = {"SPY": 820.0}

    text = build_market_context(account=account, ticks={}, headlines=[], news_windows=[])

    assert "the JOURNAL planned" in text


# ---------------------------------------- symbols an adopted dream permits
#
# The block that made the permission usable. Until it existed the gate honoured
# a grant that the model was never told about, so a granted symbol had no quote,
# no indicators and no chance of being proposed — the feature was wired and
# inert.


def _dream(**kw) -> Dream:
    base = {
        "id": 4,
        "title": "Smelters and power",
        "seed": "Data centres bid up the same grid aluminium smelters run on.",
        "chain": [
            Hop("Smelters buy power on the same interconnect.", True, "grid operator filing"),
            Hop("Two of the three largest are inside that region.", False, ""),
        ],
        "weakest_hop": "that the smelters cannot re-contract elsewhere",
        "symbols": ["AA"],
        "asset_class_key": "us_equity",
    }
    base.update(kw)
    return Dream(**base)  # type: ignore[arg-type]


def _briefing(**kw) -> GrantBriefing:
    base = {
        "symbols": {"AA": "us_equity"},
        "dream_ids": {"AA": 4},
        "dreams": (_dream(),),
        "expires_at": {4: datetime(2026, 8, 1, tzinfo=UTC)},
    }
    base.update(kw)
    return GrantBriefing(**base)  # type: ignore[arg-type]


def test_no_grants_renders_no_section(account):
    """A deployment that does not use dreams pays nothing and reads nothing."""
    blob = build_market_context(
        account=account, ticks={}, headlines=[], news_windows=[]
    )

    assert "adopted dream permits" not in blob


def test_a_granted_symbol_reaches_the_prompt_with_its_class_and_its_expiry(account):
    """A permission rendered without an end reads as permanent, and it is not."""
    blob = build_market_context(
        account=account,
        ticks={},
        headlines=[],
        news_windows=[],
        grants=_briefing(),
        now=datetime(2026, 7, 1, tzinfo=UTC),
    )

    assert "AA (us_equity)" in blob
    assert "permission ends 2026-08-01" in blob
    assert "31.0 days" in blob


def test_the_prompt_says_a_dream_permits_a_symbol_and_does_not_propose_a_position(
    account,
):
    """The operator's rule, stated where the reasoner will read it.

    A dream is a reason the symbol is on the table, never a reason to be in it.
    Direction, entry, stop and size are still the agent's to justify on its own
    evidence.
    """
    blob = build_market_context(
        account=account, ticks={}, headlines=[], news_windows=[], grants=_briefing()
    )

    assert "does NOT propose a position" in blob
    assert "Direction, entry, stop and size are yours to justify" in blob


def test_the_chain_never_appears_without_its_badge_and_its_weakest_hop(account):
    """**The property that makes shipping the chain tolerable at all.**

    An unqualified causal chain in a prompt reads as established fact, and the
    whole reason `Hop.checked` exists is that some of those sentences were
    invented. So the verification badge and the weakest hop are rendered
    adjacent to the hops and must never be separated from them.
    """
    lines = render_grants(_briefing(), now=datetime(2026, 7, 1, tzinfo=UTC))

    heading = next(i for i, line in enumerate(lines) if line.startswith("### Dream 4"))
    first_hop = next(i for i, line in enumerate(lines) if "hop 1 " in line)
    weakest = next(i for i, line in enumerate(lines) if "WEAKEST HOP" in line)
    last_hop = max(i for i, line in enumerate(lines) if "hop " in line and "(" in line)

    assert "PARTIAL" in lines[heading]
    # Badge above the hops, weakest hop immediately under them. Nothing may be
    # inserted that separates a hop from either.
    assert heading < first_hop < weakest
    assert weakest == last_hop + 1
    assert "cannot re-contract" in lines[weakest]


def test_an_unchecked_hop_is_labelled_as_an_assertion(account):
    lines = render_grants(_briefing(), now=datetime(2026, 7, 1, tzinfo=UTC))
    blob = "\n".join(lines)

    assert "hop 2 (UNCHECKED)" in blob
    assert "1 of 2 hop(s) below are UNCHECKED" in blob
    assert "assertions, not facts" in blob


def test_a_dream_that_named_no_weakest_hop_says_so_rather_than_going_quiet(account):
    """A silent omission would read as "nothing here could break", which is the
    opposite of what an unnamed weakest hop means."""
    lines = render_grants(
        _briefing(dreams=(_dream(weakest_hop=""),)),
        now=datetime(2026, 7, 1, tzinfo=UTC),
    )

    assert any("not named by the dreamer" in line for line in lines)


def test_an_unknown_expiry_is_rendered_as_a_reason_for_more_caution(account):
    """Never as an absent limit. A permission whose end cannot be read is the
    state to be more careful about, not less."""
    lines = render_grants(
        _briefing(dream_ids={}, expires_at={}), now=datetime(2026, 7, 1, tzinfo=UTC)
    )

    assert any("expiry unknown" in line for line in lines)


def test_a_grant_whose_reasoning_could_not_be_read_says_so(account):
    """Missing record, not an absence of speculation.

    The symbol still stands — the gate has it — and the model is told to judge
    it on the figures alone rather than left to assume there was no chain.
    """
    lines = render_grants(
        GrantBriefing(symbols={"AA": "us_equity"}),
        now=datetime(2026, 7, 1, tzinfo=UTC),
    )

    assert any("could not be read this cycle" in line for line in lines)
    assert any("NOT an absence of speculation" in line for line in lines)


def test_the_grant_block_is_rendered_last(account):
    """Everything above it is a measurement; this is the one speculative block.

    A model that reads a story before it has seen a figure anchors on the story.
    """
    blob = build_market_context(
        account=account,
        ticks={},
        headlines=[],
        news_windows=[],
        grants=_briefing(),
    )

    assert blob.index("adopted dream permits") > blob.index("## Indicators")
    assert blob.index("adopted dream permits") > blob.index("## Recent headlines")


# --------------------------------------------------------- sizing ceilings
#
# The model sized over the cap twice, and both times the gate caught it. Every
# limit reached it as a PERCENTAGE in the cached system prompt and every input
# in DOLLARS here, so naming a quantity meant multiplying three caps by equity,
# SUBTRACTING the open risk from the combined budget, dividing by the stop
# distance and taking the minimum — across two documents. It skipped the
# subtraction, which is the only one of those steps the percentages cannot be
# read off directly.
#
# These pin the fix. The load-bearing ones are the two that drive the REAL
# `RiskGate` at the rendered figure and one cent past it: a ceiling that
# disagrees with `risk.py` is worse than the arithmetic it replaced, because it
# looks measured.

# A Monday, 15:00 UTC — inside the us_equity window in the shipped rules.
IN_SESSION = datetime(2026, 5, 4, 15, 0, tzinfo=UTC)


def _held(symbol: str, *, qty: float = 10, price: float = 580.0) -> Position:
    return Position(
        symbol=symbol,
        direction=Direction.BUY,
        qty=qty,
        entry_price=price,
        opened_at=START,
        current_price=price,
    )


def _with_class_total_cap(pct: float) -> Rules:
    """The shipped rules with a us_equity class total-risk cap bolted on.

    `config/rules.yaml` sets one on crypto, which is DISABLED and therefore
    renders nothing, so the class branch has to be exercised on the class that
    is actually enabled.
    """
    rules = load_rules()
    rules.instruments["us_equity"] = rules.instruments["us_equity"].model_copy(
        update={"max_class_total_risk_pct": pct}
    )
    return rules


def _headroom(ceilings: list[Ceiling], label_fragment: str) -> float:
    match = next(c for c in ceilings if label_fragment in c.label)
    assert match.headroom_usd is not None
    return match.headroom_usd


def _proposal(*, qty: float, symbol: str = "QQQ") -> OrderProposal:
    """20 dollars of stop distance, so risk is exactly `20 x qty`."""
    return OrderProposal(
        symbol=symbol,
        direction=Direction.BUY,
        qty=qty,
        limit_price=500.00,
        stop_loss_price=480.00,
        rationale="Sized against the ceiling the context block rendered.",
    )


def _qqq_tick() -> Tick:
    return Tick(symbol="QQQ", bid=499.98, ask=500.02, timestamp=IN_SESSION)


# ---------------------------------------------------- the figures themselves


def test_the_caps_reach_the_model_in_dollars_with_the_subtraction_done():
    """The exact account that produced the 2.03x over-size, from the gate's own
    rejection text on 12 Aug 2026.

    Three ceilings, and the one requiring a subtraction is the binding one. All
    three are recovered here to the cent, which is what makes this a regression
    test rather than a formatting test.
    """
    account = AccountSnapshot(
        equity_usd=99_383.00,
        cash_usd=116_239.81,
        buying_power_usd=198_766.00,
        open_positions=[_held("SPY", qty=21, price=774.18)],
        open_risk_usd=1_486.95,
        open_risk_by_symbol={"SPY": 1_486.95},
        planned_stop_by_symbol={"SPY": 820.0},
    )

    blob = "\n".join(render_sizing_ceilings(account=account, rules=load_rules()))

    assert "$993.83" in blob        # per-trade risk cap, 1.00% of equity
    assert "$500.71" in blob        # remaining COMBINED risk budget — the binding one
    assert "$49,691.50" in blob     # concentration, 50% of equity
    # And the binding one is named as such rather than left to a comparison.
    assert "Tightest risk ceiling for a us_equity trade: $500.71" in blob


def test_the_combined_risk_headroom_is_exactly_where_the_gate_flips():
    """**The property that makes rendering these figures safe at all.**

    A ceiling that disagrees with `risk.py` is not a convenience — it is a new
    way to mislead the model, and a worse one than the arithmetic it replaced,
    because it looks measured. So this drives the real gate at the rendered
    figure and one cent past it.
    """
    rules = load_rules()
    account = AccountSnapshot(
        equity_usd=100_000.0,
        cash_usd=100_000.0,
        buying_power_usd=200_000.0,
        open_positions=[_held("SPY")],
        open_risk_usd=1_500.0,
        open_risk_by_symbol={"SPY": 1_500.0},
        planned_stop_by_symbol={"SPY": 575.0},
    )
    headroom = _headroom(
        sizing_ceilings(account=account, rules=rules), "Combined risk left across"
    )
    assert headroom == 500.0

    gate = RiskGate(rules, equity_at_session_start=100_000.0, now=IN_SESSION)

    at_the_ceiling = gate.evaluate(
        _proposal(qty=headroom / 20.0), account=account, tick=_qqq_tick()
    )
    assert at_the_ceiling.approved, at_the_ceiling.reasons

    one_cent_over = gate.evaluate(
        _proposal(qty=(headroom + 0.01) / 20.0), account=account, tick=_qqq_tick()
    )
    assert not one_cent_over.approved
    assert any("total risk would reach" in r for r in one_cent_over.reasons)


def test_the_rendered_class_headroom_is_where_the_gate_flips():
    """The same guarantee for a per-class cap, and the reason `_symbols_counting_as`
    may exist beside `RiskGate._class_symbols` at all.

    The membership rule is written twice — the gate's copy is a private method
    taking a `ResolvedClass` built inside `evaluate`, and `risk.py` must not
    grow a public API for a renderer — so this is what holds the two in step. If
    it goes, the block is free to quote a cap nobody enforces.
    """
    rules = _with_class_total_cap(1.0)
    account = AccountSnapshot(
        equity_usd=100_000.0,
        cash_usd=100_000.0,
        buying_power_usd=200_000.0,
        open_positions=[_held("SPY")],
        open_risk_usd=600.0,
        open_risk_by_symbol={"SPY": 600.0},
        planned_stop_by_symbol={"SPY": 520.0},
    )
    ceilings = sizing_ceilings(account=account, rules=rules)

    # The class cap binds well before the portfolio one, which is the case the
    # per-class figure exists for: 1,400 left across the book, 400 in the class.
    assert _headroom(ceilings, "Combined risk left across") == 1_400.0
    headroom = _headroom(ceilings, "Combined risk left inside us_equity")
    assert headroom == 400.0

    gate = RiskGate(rules, equity_at_session_start=100_000.0, now=IN_SESSION)

    at_the_ceiling = gate.evaluate(
        _proposal(qty=headroom / 20.0), account=account, tick=_qqq_tick()
    )
    assert at_the_ceiling.approved, at_the_ceiling.reasons

    one_cent_over = gate.evaluate(
        _proposal(qty=(headroom + 0.01) / 20.0), account=account, tick=_qqq_tick()
    )
    assert not one_cent_over.approved
    assert any("class cap" in r for r in one_cent_over.reasons)


def test_a_position_held_under_a_grant_counts_against_its_class_ceiling():
    """A grant buys entry to the allowlist and must not buy an exemption.

    `AA` is in no `allowed_symbols` list, so a ceiling computed from that list
    alone would report the whole class cap as free while $700 of it was live —
    which is `RiskGate._class_symbols`'s bypass, arriving through the renderer
    instead of through the gate.
    """
    rules = _with_class_total_cap(1.0)
    account = AccountSnapshot(
        equity_usd=100_000.0,
        cash_usd=100_000.0,
        buying_power_usd=200_000.0,
        open_positions=[_held("AA", qty=10, price=40.0)],
        open_risk_usd=700.0,
        open_risk_by_symbol={"AA": 700.0},
        planned_stop_by_symbol={"AA": 30.0},
    )

    ceilings = sizing_ceilings(
        account=account, rules=rules, granted_symbols={"AA": "us_equity"}
    )

    assert _headroom(ceilings, "Combined risk left inside us_equity") == 300.0


# ------------------------------------------- zero and unknown, both in words


def test_a_spent_budget_renders_in_words_and_never_as_a_zero():
    """A zero reads as "cheap, size small". It means "nothing fits at any size".

    Same shape as the `STOP UNKNOWN` treatment beside an open position: the
    missing-versus-zero rule with money attached.
    """
    account = AccountSnapshot(
        equity_usd=100_000.0,
        cash_usd=100_000.0,
        buying_power_usd=200_000.0,
        open_positions=[_held("SPY")],
        open_risk_usd=2_000.0,
        open_risk_by_symbol={"SPY": 2_000.0},
        planned_stop_by_symbol={"SPY": 480.0},
    )

    lines = render_sizing_ceilings(account=account, rules=load_rules())
    spent = next(line for line in lines if "Combined risk left across" in line)

    assert BUDGET_SPENT in spent
    assert "$0.00" not in spent
    assert "No new position fits at ANY size" in spent
    # And the summary line does not quietly fall back to a number.
    assert any(
        "Tightest risk ceiling" in line and BUDGET_SPENT in line for line in lines
    )


def test_a_headroom_that_rounds_to_zero_is_spent_rather_than_printed():
    """Three tenths of a cent is a POSITIVE number that prints as `$0.00`.

    That is the exact string the operator ruled out, arriving through the
    formatter rather than through the arithmetic — so the words have to start
    above the rounding boundary, not at zero.
    """
    account = AccountSnapshot(
        equity_usd=100_000.0,
        cash_usd=100_000.0,
        buying_power_usd=200_000.0,
        open_risk_usd=1_999.997,
    )

    lines = render_sizing_ceilings(account=account, rules=load_rules())
    line = next(x for x in lines if "Combined risk left across" in x)

    assert BUDGET_SPENT in line
    assert "$0.00" not in line


def test_an_unestablishable_class_total_says_the_gate_will_refuse():
    """`_class_total_risk` is the one gate in the repository that fails CLOSED.

    A held position with no journal row has an unknowable planned stop, so the
    class total cannot be computed and the gate REJECTS rather than counting the
    unknown as zero. Rendering a figure there would invent the one input the
    gate declines to invent.
    """
    rules = _with_class_total_cap(1.0)
    account = AccountSnapshot(
        equity_usd=100_000.0,
        cash_usd=100_000.0,
        buying_power_usd=200_000.0,
        open_positions=[_held("SPY")],
        symbols_with_unknown_risk=["SPY"],
    )

    lines = render_sizing_ceilings(account=account, rules=rules)
    unknown = next(x for x in lines if "Combined risk left inside us_equity" in x)

    assert HEADROOM_UNKNOWN in unknown
    assert "REJECTS every new us_equity position" in unknown
    # An unknown could be smaller than anything established, so the summary must
    # not take a minimum over what happens to be known.
    assert any(
        "Tightest risk ceiling" in line and HEADROOM_UNKNOWN in line for line in lines
    )


def test_the_portfolio_figure_is_still_the_gates_own_but_flagged_as_overstated():
    """`_total_risk` does NOT refuse on an unknown — it computes with the
    understated total — so going `HEADROOM UNKNOWN` here would disagree with the
    gate, which is the one thing this block may not do. What is added is the
    direction of the error, in `reconcile`'s own words."""
    account = AccountSnapshot(
        equity_usd=100_000.0,
        cash_usd=100_000.0,
        buying_power_usd=200_000.0,
        open_positions=[_held("SPY")],
        symbols_with_unknown_risk=["SPY"],
    )

    blob = "\n".join(render_sizing_ceilings(account=account, rules=load_rules()))

    assert "Combined risk left across ALL open positions: $2,000.00" in blob
    assert "OVERSTATED: SPY held with no journal row" in blob


def test_zero_equity_cannot_be_stated_rather_than_being_a_small_budget():
    """A percentage of nothing is not a tight limit, it is no limit anybody can
    read. The two would be acted on completely differently."""
    account = AccountSnapshot(equity_usd=0.0, cash_usd=0.0, buying_power_usd=0.0)

    lines = render_sizing_ceilings(account=account, rules=load_rules())

    assert sizing_ceilings(account=account, rules=load_rules()) == []
    assert any(HEADROOM_UNKNOWN in line for line in lines)
    assert not any("$0.00" in line and "ceiling" in line.lower() for line in lines)


# ------------------------------------------------- what is deliberately absent


def test_no_worked_maximum_quantity_is_offered(account):
    """The operator's decision, and there are two reasons for it.

    It cannot be precomputed — it depends on the stop, which is the agent's own
    choice — and a worked example at the current price would read as a
    recommendation to trade at that size.
    """
    lines = render_sizing_ceilings(account=account, rules=load_rules())
    blob = "\n".join(lines)

    assert "No maximum quantity is given here" in blob
    # Nothing in the block resolves to a share count. Every figure is money, so
    # no number anywhere in it is followed by a unit of quantity. (The word
    # "shares" appears once, in the sentence telling the agent not to move the
    # stop to reach one — a warning, never a worked figure.)
    assert not re.search(r"\d[\d,.]*\s*(shares|units|contracts)", blob)
    assert "A figure below is a ceiling, not a target." in blob


def test_the_block_states_that_it_bounds_size_and_nothing_else(account):
    """Half the gates in `risk.py` are not about size. A block that implied
    completeness would turn "fits every ceiling" into "will be approved"."""
    blob = "\n".join(render_sizing_ceilings(account=account, rules=load_rules()))

    assert "These bound SIZE only" in blob
    assert "stand-down" in blob


def test_a_looser_class_limit_is_named_as_the_class_own_rather_than_a_mistake():
    """`account:` is the default, not a ceiling, and an override is deliberately
    not floored back with a `min`. A figure above the account default is a real
    setting, and saying which direction it moved stops it reading as an error."""
    rules = load_rules()
    rules.instruments["us_equity"] = rules.instruments["us_equity"].model_copy(
        update={"max_risk_per_trade_pct": 3.0}
    )
    account = AccountSnapshot(
        equity_usd=100_000.0, cash_usd=100_000.0, buying_power_usd=100_000.0
    )

    blob = "\n".join(render_sizing_ceilings(account=account, rules=rules))

    assert "Most ONE us_equity trade may risk: $3,000.00" in blob
    assert "looser than the 1.00% default" in blob


# --------------------------------------------------- placement in the document


def test_no_rules_means_no_ceilings_block_rather_than_a_guessed_one(account):
    """Same rule as the session block: a limit computed from nothing would be a
    confident statement about an account nobody described."""
    blob = build_market_context(
        account=account, ticks={}, headlines=[], news_windows=[]
    )

    assert "## Sizing ceilings" not in blob


def test_the_ceilings_sit_directly_under_the_account_figures(account):
    """Adjacency is the point. The line above says what is already at risk; this
    block is that figure already subtracted from the cap it eats into. A page of
    quotes between the two is what left the subtraction to the model."""
    blob = build_market_context(
        account=account,
        ticks={},
        headlines=[],
        news_windows=[],
        rules=load_rules(),
    )

    assert blob.index("## Account") < blob.index("## Sizing ceilings")
    assert blob.index("## Sizing ceilings") < blob.index("## Market snapshot")
