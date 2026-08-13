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
    ACCOUNT_HALTED,
    BUDGET_SPENT,
    HEADROOM_UNKNOWN,
    RISK_UNIT,
    TIGHT_STOP_ATR,
    VALUE_UNIT,
    Ceiling,
    build_market_context,
    crossover_stop_pct,
    fetch_indicators,
    fetch_market_ticks,
    measure_stop_widths,
    render_grants,
    render_sizing_ceilings,
    sizing_ceilings,
)
from bot.dreaming import Dream, Hop
from bot.grants import GrantBriefing
from bot.indicators import Indicators
from bot.model_client import build_system_prompt
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


def test_the_gate_still_rejects_the_size_this_block_was_written_for():
    """**The fix is a rendering change, and it must not be mistaken for a
    relaxation.**

    The standing rule for anything touching the risk path is a test that proves
    a REJECT. `tests/test_risk.py` pins the live shape against the gate directly;
    this one runs the other way round, through the block — it takes the figure
    the context ACTUALLY RENDERS, performs the single division the system prompt
    now asks for, and drives the real `RiskGate` with the result.

    That is the whole claim of the feature in one test: a size read off the block
    is approved, and the size the model reached by skipping the subtraction is
    refused exactly as it was on 12 Aug 2026. If the block ever renders a figure
    the gate will not accept, or the 185 that started this is quietly permitted,
    this fails.
    """
    rules = load_rules()
    account = AccountSnapshot(
        equity_usd=99_383.00,
        cash_usd=116_239.81,
        buying_power_usd=198_766.00,
        open_positions=[_held("SPY", qty=21, price=774.18)],
        open_risk_usd=1_486.95,
        open_risk_by_symbol={"SPY": 1_486.95},
        planned_stop_by_symbol={"SPY": 820.0},
    )

    # $5.50 of stop distance on a $305 name, as on the day.
    def sized(qty: float) -> OrderProposal:
        return OrderProposal(
            symbol="MSFT",
            direction=Direction.BUY,
            qty=qty,
            limit_price=305.00,
            stop_loss_price=299.50,
            rationale="Sized by dividing the rendered ceiling by the stop distance.",
        )

    tick = Tick(symbol="MSFT", bid=304.98, ask=305.02, timestamp=IN_SESSION)
    ceilings = sizing_ceilings(account=account, rules=rules)
    # The tightest RISK ceiling, taken from the rendering rather than restated:
    # a test that hardcoded 500.71 would pass while the block printed something
    # else entirely.
    tightest = min(
        c.headroom_usd
        for c in ceilings
        if c.unit == RISK_UNIT and c.headroom_usd is not None
    )
    # The one division the prompt describes, rounded DOWN as it says.
    qty = int(tightest / 5.50)
    assert qty == 91

    permitted = RiskGate(
        rules, equity_at_session_start=99_383.00, now=IN_SESSION
    ).evaluate(sized(qty), account=account, tick=tick)
    assert permitted.approved, permitted.reasons

    # And the size that skipping the subtraction produced — 2.03x over.
    over = RiskGate(
        rules, equity_at_session_start=99_383.00, now=IN_SESSION
    ).evaluate(sized(185), account=account, tick=tick)
    assert not over.approved
    assert any("total risk would reach" in r for r in over.reasons)
    assert any("per-trade cap" in r for r in over.reasons)


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


# ------------------------------------ the two units, and which one binds
#
# Item 21's fix moved the error rather than removing it. Five arithmetic steps
# became one division, the models do that division reliably, and every material
# over-size in a 160-call run on 13 Aug 2026 was a model that did it and never
# looked at a POSITION VALUE ceiling — `deepseek-v4-pro` at 7.06-7.27x on buying
# power, 2.06-2.13x on gross exposure, once 14.39x, and `nemotron-3-ultra-550b`
# once at 1.51x.
#
# The block already printed both units and already said "take the smaller".
# These pin the replacement: the stop distance at which the two CROSS, which is
# one number, needs no price and no stop, and turns "check a second figure" into
# one comparison the model makes against its own choice.


def _flat_account(
    *,
    open_positions: list[Position] | None = None,
    symbols_with_unknown_risk: list[str] | None = None,
) -> AccountSnapshot:
    """$100,000, nothing at risk. Round figures on purpose.

    On the shipped rules that makes every ceiling exact: $1,000 of risk (the
    per-trade cap, tighter than the $2,000 portfolio budget) and $50,000 of
    position value (concentration, tighter than $100,000 of buying power and
    $150,000 of gross exposure). So the crossover is 1,000/50,000 = 2.000% and
    every figure below can be checked by hand.
    """
    return AccountSnapshot(
        equity_usd=100_000.0,
        cash_usd=100_000.0,
        buying_power_usd=200_000.0,
        open_positions=open_positions or [],
        symbols_with_unknown_risk=symbols_with_unknown_risk or [],
    )


def test_the_rendered_value_ceiling_is_where_the_gate_flips():
    """The same guarantee the two RISK ceilings already have, in the other unit.

    Both existing flip tests drive `_total_risk` and `_class_total_risk`, which
    are RISK. Nothing held the four POSITION VALUE ceilings to the gate at all —
    and this is the unit the model was measured skipping, so a figure here that
    disagreed with `risk.py` would be wrong on exactly the line the fix now
    sends it to.
    """
    rules = load_rules()
    account = _flat_account()
    headroom = _headroom(
        sizing_ceilings(account=account, rules=rules), "Most ONE us_equity position"
    )
    assert headroom == 50_000.0

    gate = RiskGate(rules, equity_at_session_start=100_000.0, now=IN_SESSION)

    def sized(notional: float) -> OrderProposal:
        """$5 of stop distance, so the RISK ceilings keep slack either side.

        The value ceiling has to be the ONLY gate that moves across the cent, or
        the test would pass on a rejection the risk cap produced.
        """
        return OrderProposal(
            symbol="QQQ",
            direction=Direction.BUY,
            qty=notional / 500.0,
            limit_price=500.00,
            stop_loss_price=495.00,
            rationale="Sized against the position value ceiling as rendered.",
        )

    # $50,000 is exactly the rendered ceiling, and `_position_size` compares
    # with `>`, so the figure itself is approved.
    at_the_ceiling = gate.evaluate(
        sized(headroom), account=account, tick=_qqq_tick()
    )
    assert at_the_ceiling.approved, at_the_ceiling.reasons

    one_cent_over = gate.evaluate(
        sized(headroom + 0.01), account=account, tick=_qqq_tick()
    )
    assert not one_cent_over.approved
    assert any("of equity (max 50.0%)" in r for r in one_cent_over.reasons)


def test_the_two_units_cross_exactly_where_the_gate_stops_approving():
    """**The whole claim of the fix, driven through the real gate.**

    The rendered crossover says a stop that far from entry is the point at which
    RISK stops being the binding unit. So at exactly that stop the two divisions
    must give the SAME quantity and the gate must approve it; and one tick
    tighter, the RISK division alone — the arithmetic every model in the run
    performed correctly — must produce a size the gate REFUSES, while the
    POSITION VALUE division on the same stop is approved.

    That last pair is the standing REJECT test for this change. It is not a
    formatting assertion: if the crossover ever drifts from where `risk.py`
    flips, the approved half or the refused half fails.
    """
    rules = load_rules()
    account = _flat_account()
    ceilings = sizing_ceilings(account=account, rules=rules)

    crossover = crossover_stop_pct(ceilings, "us_equity")
    assert crossover is not None
    assert crossover == pytest.approx(2.0)

    entry = 500.00
    risk_ceiling = _headroom(ceilings, "Most ONE us_equity trade may risk")
    value_ceiling = _headroom(ceilings, "Most ONE us_equity position")
    gate = RiskGate(rules, equity_at_session_start=100_000.0, now=IN_SESSION)

    def sized(qty: float, stop: float) -> OrderProposal:
        return OrderProposal(
            symbol="QQQ",
            direction=Direction.BUY,
            qty=qty,
            limit_price=entry,
            stop_loss_price=stop,
            rationale="Sized through the branch the ceilings block selects.",
        )

    # --- At the crossover the two branches agree, to the share.
    at_stop = entry * crossover / 100          # $10.00 away
    by_risk = risk_ceiling / at_stop
    by_value = value_ceiling / entry
    assert by_risk == by_value == 100.0
    both = gate.evaluate(
        sized(by_risk, entry - at_stop), account=account, tick=_qqq_tick()
    )
    assert both.approved, both.reasons

    # --- Tighter than the crossover, the RISK division alone is refused. This
    # is the measured failure reproduced exactly: correct risk arithmetic,
    # 2.0x the permitted size, because the other unit was never consulted.
    tight_stop = entry * 1.0 / 100             # $5.00 away, 1% of entry
    over = gate.evaluate(
        sized(risk_ceiling / tight_stop, entry - tight_stop),
        account=account,
        tick=_qqq_tick(),
    )
    assert risk_ceiling / tight_stop == 200.0
    assert not over.approved
    assert any("of equity (max 50.0%)" in r for r in over.reasons)

    # --- And the branch the block now sends it to is approved on that same stop.
    correct = gate.evaluate(
        sized(value_ceiling / entry, entry - tight_stop),
        account=account,
        tick=_qqq_tick(),
    )
    assert correct.approved, correct.reasons


def test_the_block_names_the_binding_unit_rather_than_printing_two_figures():
    """The repair that was explicitly ruled out was a second worked figure.

    Both units were already printed and the instruction to check both was
    already there, so what is pinned here is the SHAPE of the replacement: a
    crossover, and two branches of one division, rather than a third number to
    take a minimum over.
    """
    blob = "\n".join(render_sizing_ceilings(account=_flat_account(), rules=load_rules()))

    assert "### Which ceiling binds — ONE comparison, then ONE division" in blob
    assert "us_equity: the two ceilings cross at a stop 2.000% of your entry price" in blob
    assert "RISK binds: qty = $1,000.00 / |limit_price - stop_loss_price|" in blob
    assert "POSITION VALUE binds: qty = $50,000.00 / limit_price" in blob
    # The instruction that was measured failing is gone rather than restated —
    # and it is named as unfollowable rather than merely dropped, or the next
    # reader adds it back as an obvious omission.
    assert "ceiling and take the smaller" not in blob
    assert "'take the smaller of the two' is not a rule that can be followed" in blob


def test_every_figure_carries_its_own_unit():
    """A line lifted out of its section must not be readable as the other unit.

    That is what the measured failure looks like from the inside: the model
    reaches for a dollar figure and divides into it. A heading four lines up is
    not attached to the number.
    """
    lines = render_sizing_ceilings(account=_flat_account(), rules=load_rules())
    figures = [x for x in lines if x.startswith("- ") and "$" in x and "ceiling" not in x]

    assert figures, "no ceiling lines rendered at all"
    for line in figures:
        # Against the constants, so a rename reaches the assertion rather than
        # leaving it passing on a string nothing produces any more.
        assert f" of {RISK_UNIT} (" in line or f" of {VALUE_UNIT} (" in line, line


def test_the_crossover_is_not_offered_as_a_minimum_stop_distance():
    """**The failure mode this fix could itself create.**

    `RiskGate` deliberately holds no opinion on where a stop goes, and neither
    does this file. A crossover phrased as "your stop must be at least X%" is
    that opinion arriving through the renderer — a minimum stop distance nobody
    agreed to, and one that would be obeyed by WIDENING the stop, which buys a
    larger loss at the same size.
    """
    blob = "\n".join(render_sizing_ceilings(account=_flat_account(), rules=load_rules()))

    assert "NOT a minimum stop distance" in blob
    assert "Nothing here has an opinion on placement" in blob
    assert "buys a larger loss at the same size" in blob
    # Stated as a property of the arithmetic, never as a requirement on the stop.
    assert "your stop must be at least" not in blob


def test_a_spent_value_ceiling_has_no_crossover_rather_than_a_number():
    """A crossover computed from a spent budget is a plausible wrong figure.

    "No crossover" also has to say WHY, in the words the units already use.
    Left bare it invites the reading that nothing constrains the class, which is
    the exact inversion of the state that produces it.
    """
    rules = load_rules()
    # Gross exposure already at 150% of equity, so the tightest value ceiling is
    # spent while every risk ceiling is untouched.
    account = _flat_account(open_positions=[_held("SPY", qty=1_000, price=150.0)])

    ceilings = sizing_ceilings(account=account, rules=rules)
    assert crossover_stop_pct(ceilings, "us_equity") is None

    blob = "\n".join(render_sizing_ceilings(account=account, rules=rules))
    assert "NO CROSSOVER TO STATE" in blob
    assert f"the tightest position value ceiling is {BUDGET_SPENT}" in blob
    assert "no stop distance makes a us_equity position fit" in blob


def test_an_unknown_risk_ceiling_leaves_no_crossover_either():
    """`HEADROOM UNKNOWN` on one unit is not a smaller number on the other.

    Same precedence the tightest line already uses: an unknown could be smaller
    than anything established, so dividing what happens to be known would answer
    a question nobody asked, and answer it optimistically.
    """
    rules = _with_class_total_cap(1.0)
    account = _flat_account(
        open_positions=[_held("SPY")], symbols_with_unknown_risk=["SPY"]
    )

    ceilings = sizing_ceilings(account=account, rules=rules)
    assert crossover_stop_pct(ceilings, "us_equity") is None

    blob = "\n".join(render_sizing_ceilings(account=account, rules=rules))
    assert f"the tightest risk ceiling is {HEADROOM_UNKNOWN}" in blob


def test_a_crossover_built_on_an_overstated_ceiling_says_it_is_an_upper_bound():
    """The overstatement travels into anything derived from the figure.

    It travels in the safe direction here — an overstated RISK ceiling makes the
    crossover too HIGH, which sends more stops into the POSITION VALUE branch
    and therefore to a smaller size — and it is still said out loud, because a
    figure that is an upper bound must not print as a measurement whichever way
    the error runs.
    """
    account = _flat_account(
        open_positions=[_held("SPY")], symbols_with_unknown_risk=["SPY"]
    )

    lines = render_sizing_ceilings(account=account, rules=load_rules())
    crossover = next(x for x in lines if "the two ceilings cross" in x)
    index = lines.index(crossover)

    assert "UPPER BOUND" in "\n".join(lines[index : index + 5])
    assert "the safe direction" in "\n".join(lines[index : index + 5])


def test_a_class_whose_value_ceiling_is_unreachable_says_which_branch_that_leaves():
    """A crossover at or above 100% of entry cannot be reached by a long.

    Left unsaid it reads as an unreachable requirement rather than as "POSITION
    VALUE binds on everything", which is what it actually means.
    """
    rules = load_rules()
    # Risk ceilings above the concentration one, in both places they are set:
    # 60% of equity of risk against 50% of equity of position value.
    rules.account = rules.account.model_copy(update={"max_total_risk_pct": 100.0})
    rules.instruments["us_equity"] = rules.instruments["us_equity"].model_copy(
        update={"max_risk_per_trade_pct": 60.0}
    )
    account = _flat_account()

    ceilings = sizing_ceilings(account=account, rules=rules)
    crossover = crossover_stop_pct(ceilings, "us_equity")
    assert crossover is not None and crossover >= 100

    blob = "\n".join(render_sizing_ceilings(account=account, rules=rules))
    assert "which a long's stop cannot reach" in blob
    assert "POSITION VALUE binds on every long here" in blob


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


def test_an_unknown_is_not_subtracted_as_nothing_already_at_risk():
    """The basis line said `less $0.00 already at risk`, which is a claim.

    `open_risk_usd` reads $0.00 on an account whose only position has no journal
    row — there is no row to add up — so the sentence asserted that nothing was
    at risk, one line above the caveat correcting it. Both cannot be read; the
    figure is the one that gets read. What the subtraction actually measured is
    what the journal could account for, so that is what it now says, and the
    caveat is left to carry the rest.
    """
    account = AccountSnapshot(
        equity_usd=100_000.0,
        cash_usd=100_000.0,
        buying_power_usd=200_000.0,
        open_positions=[_held("SPY")],
        symbols_with_unknown_risk=["SPY"],
    )

    blob = "\n".join(render_sizing_ceilings(account=account, rules=load_rules()))

    assert "$0.00 the journal can account for" in blob
    assert "$0.00 already at risk" not in blob

    # And the phrasing narrows only where something is unknown: on an account
    # the journal fully describes, the subtracted figure IS all of what is at
    # risk, and hedging it there would teach the reader to discount the words.
    known = AccountSnapshot(
        equity_usd=100_000.0,
        cash_usd=100_000.0,
        buying_power_usd=200_000.0,
        open_positions=[_held("SPY")],
        open_risk_usd=600.0,
        open_risk_by_symbol={"SPY": 600.0},
        planned_stop_by_symbol={"SPY": 520.0},
    )
    assert "$600.00 already at risk" in "\n".join(
        render_sizing_ceilings(account=known, rules=load_rules())
    )


def test_the_tightest_line_carries_the_overstatement_it_summarises():
    """**The summary line is the one a quantity gets divided out of.**

    The unknown reaches the portfolio ceiling as a caveat on that ceiling's own
    line. The tightest line is a claim about the whole SET — the smallest of
    them — so a member that is overstated can truly be smaller than the figure
    printed, whether or not it is the member that won.

    Here it is not: the per-trade cap wins at $1,000 while the contaminated
    combined budget prints $2,000 and could genuinely be anything below it. A
    reader of the summary alone would size to $1,000 with nothing on the line to
    say the set it was drawn from is not fully known — which is the
    confident-partial-answer failure arriving through the one line most likely
    to be acted on.
    """
    account = AccountSnapshot(
        equity_usd=100_000.0,
        cash_usd=100_000.0,
        buying_power_usd=200_000.0,
        open_positions=[_held("SPY")],
        symbols_with_unknown_risk=["SPY"],
    )

    lines = render_sizing_ceilings(account=account, rules=load_rules())
    tightest = next(x for x in lines if "Tightest risk ceiling" in x)

    # The figure is still the gate's own, so it is still stated.
    assert "$1,000.00" in tightest
    assert "UPPER BOUND, not a measurement" in tightest
    assert "OVERSTATED" in tightest
    # And it says which way the error runs, because the gate will not refuse a
    # trade sized to an overstated figure: this one is the reader's to be
    # careful about rather than a rejection waiting to happen.
    assert "smaller size or no trade, never a larger one" in tightest

    # The POSITION VALUE summary is untouched: an unjournalled stop makes RISK
    # unknowable and says nothing whatever about what a position is worth.
    value = next(x for x in lines if "Tightest position value ceiling" in x)
    assert "UPPER BOUND" not in value


def test_a_fully_journalled_account_gets_no_upper_bound_warning():
    """The warning must mean something, so it cannot appear unconditionally.

    Same rule as `DATA YOU DO NOT HAVE` in the strategy blocks: a caveat printed
    on every cycle is a caveat that stops being read, and this one has to survive
    for the cycle where a position really is unjournalled.
    """
    account = AccountSnapshot(
        equity_usd=100_000.0,
        cash_usd=100_000.0,
        buying_power_usd=200_000.0,
        open_positions=[_held("SPY")],
        open_risk_usd=600.0,
        open_risk_by_symbol={"SPY": 600.0},
        planned_stop_by_symbol={"SPY": 520.0},
    )

    blob = "\n".join(render_sizing_ceilings(account=account, rules=load_rules()))

    assert "UPPER BOUND" not in blob
    assert "OVERSTATED" not in blob


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
    # The two HALTING gates were missing from that list, and they are the two
    # most worth naming: the others refuse a trade, these refuse every trade.
    assert "equity floor" in blob
    assert "daily-loss kill switch" in blob


def test_an_account_below_the_equity_floor_says_so_above_every_figure():
    """**A budget quoted to an account that may open nothing.**

    `RiskGate._equity_floor` refuses EVERY proposal once equity drops below
    `min_equity_floor_usd`, and audit drove it: at $89,999 against the shipped
    $90,000 floor the block rendered a $899.99 risk budget and a $44,999.50
    value budget — every figure correct as a size limit — and the gate refused a
    proposal sized at exactly those. The floor is in neither the cached system
    prompt nor this block, so the agent had no way at all to know the account
    was halted.

    Both halves are pinned. The words lead, because every line under them is a
    statement about what may be opened and all of them are moot; and the
    ceilings still render, because they are still true and going silent would
    make a halted account indistinguishable from a broken renderer.
    """
    rules = load_rules()
    floor = rules.account.min_equity_floor_usd
    account = AccountSnapshot(
        equity_usd=floor - 1.0,
        cash_usd=floor - 1.0,
        buying_power_usd=floor * 2,
    )

    lines = render_sizing_ceilings(account=account, rules=rules)
    blob = "\n".join(lines)

    assert ACCOUNT_HALTED in blob
    assert "REFUSES every new position" in blob
    # Ahead of the first ceiling, not buried under it.
    assert lines.index(next(x for x in lines if ACCOUNT_HALTED in x)) < lines.index(
        next(x for x in lines if "Most ONE us_equity trade may risk" in x)
    )
    # And the figures are still there rather than withheld.
    assert "Most ONE us_equity trade may risk" in blob

    # The gate agrees this account is halted — which is the point of the words.
    proposal = _proposal(qty=1)
    verdict = RiskGate(
        rules, equity_at_session_start=account.equity_usd, now=IN_SESSION
    ).evaluate(proposal, account=account, tick=_qqq_tick())
    assert not verdict.approved
    assert any("below floor" in r for r in verdict.reasons)


def test_an_account_at_the_floor_is_not_described_as_halted():
    """`_equity_floor` compares with `<`, so equity exactly at the floor trades.

    The mirror of the test above, and the one that stops the warning being
    printed at an account that is fine — a halt notice on a working account is
    the same class of wrong figure in the other direction.
    """
    rules = load_rules()
    account = AccountSnapshot(
        equity_usd=rules.account.min_equity_floor_usd,
        cash_usd=rules.account.min_equity_floor_usd,
        buying_power_usd=rules.account.min_equity_floor_usd * 2,
    )

    assert ACCOUNT_HALTED not in "\n".join(
        render_sizing_ceilings(account=account, rules=rules)
    )
    verdict = RiskGate(
        rules, equity_at_session_start=account.equity_usd, now=IN_SESSION
    ).evaluate(_proposal(qty=1), account=account, tick=_qqq_tick())
    assert verdict.approved, verdict.reasons


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


# ------------------------------- how wide a stop is, reported and never refused
#
# Size is a ceiling divided by the stop distance, so a stop tightened towards
# nothing buys a position tending towards infinity. Measured 13 Aug 2026:
# `qwen3-coder-flash` proposed KO with a $0.05 stop against a $1.32 ATR — 0.04
# ATR, inside the spread — where its own median was 0.83 ATR and
# `nemotron-3-ultra-550b` and `deepseek-v4-pro` ran 1.65-1.66.
#
# **The standing rule for a new RULE is a test that proves it rejects. There is
# no new rule here, so the load-bearing test proves the opposite**: the gate
# still approves a 0.008-ATR stop, exactly as it did before, and what changed is
# that the fact is now stated. A minimum stop distance would be the
# opinion-on-placement `RiskGate` exists without, arriving through a renderer.


def _reading(symbol: str, *, atr: float | None) -> Indicators:
    return Indicators(
        symbol=symbol, bar_count=250, last_close=100.0, last_volume=1.0, atr_14=atr
    )


def _ko(stop: float) -> OrderProposal:
    """The proposal from the run, to the cent."""
    return OrderProposal(
        symbol="KO",
        direction=Direction.BUY,
        qty=100,
        limit_price=68.50,
        stop_loss_price=stop,
        rationale="The stop that was measured at four hundredths of an ATR.",
    )


def test_a_stop_is_measured_against_the_atr_the_loop_recorded():
    """The ATR is READ, never derived — the same rule as everywhere else here."""
    widths = measure_stop_widths(
        [_ko(68.45)], {"KO": _reading("KO", atr=1.32)}
    )

    assert len(widths) == 1
    assert widths[0].stop_distance_usd == pytest.approx(0.05)
    assert widths[0].atr_multiple == pytest.approx(0.05 / 1.32)
    assert widths[0].is_tight
    assert "0.04 ATR" in widths[0].render()


def test_a_symbol_with_no_atr_is_unmeasured_rather_than_acceptable():
    """`fetch_indicators` DROPS a symbol whose bars failed, so an absent ATR
    means "could not ask". Reading that as a stop of acceptable width is the
    `calendar_degraded` mistake with a position attached — and `is_tight` must
    stay False without that ever meaning fine."""
    absent = measure_stop_widths([_ko(68.45)], {})[0]
    flat = measure_stop_widths([_ko(68.45)], {"KO": _reading("KO", atr=0.0)})[0]

    for width in (absent, flat):
        assert width.atr_multiple is None
        assert width.is_measured is False
        assert width.is_tight is False, "an unknown must not read as a tight stop"
        assert "ATR UNMEASURED" in width.render()
        assert "not the same as fine" in width.render()


def test_a_wide_stop_is_measured_and_carries_no_warning():
    """The threshold has to mean something, so it cannot fire on every stop.

    1.65 ATR is what the two best models in the run actually proposed. A caveat
    printed on those would train a reader to stop seeing it before the 0.04 one
    arrived.
    """
    width = measure_stop_widths(
        [_ko(66.32)], {"KO": _reading("KO", atr=1.32)}
    )[0]

    assert width.atr_multiple == pytest.approx(1.65, abs=0.005)
    assert width.is_tight is False
    # Still stated, though: a zero-breach cycle must be a stated fact rather
    # than the absence of a warning, which is what an outage looks like too.
    assert "1.65 ATR" in width.render()
    assert "inside a single day" not in width.render()


def test_two_proposals_on_one_symbol_are_measured_separately():
    """A mapping keyed by symbol would silently report one of the two."""
    widths = measure_stop_widths(
        [_ko(68.45), _ko(66.32)], {"KO": _reading("KO", atr=1.32)}
    )

    assert [w.is_tight for w in widths] == [True, False]


def test_a_tight_stop_is_reported_and_the_gate_still_approves_it():
    """**The whole shape of this change, and the assertion that guards it.**

    `RiskGate` deliberately holds no opinion on where a stop goes:
    `_stops_on_correct_side` checks which SIDE of entry a level sits on and
    nothing else, because a wider stop already buys a smaller position and the
    cost is what the gate measures. A floor on stop distance would be a rule
    nobody agreed to, arriving through the back door.

    So this drives the real gate at a 0.008-ATR stop and asserts it is APPROVED.
    If somebody later closes item 25 by adding a minimum stop distance, this
    fails and says why.
    """
    rules = load_rules()
    account = _flat_account()
    proposal = OrderProposal(
        symbol="QQQ",
        direction=Direction.BUY,
        qty=100,
        limit_price=500.00,
        stop_loss_price=499.95,
        rationale="A stop far inside the noise, sized small enough to fit.",
    )

    verdict = RiskGate(
        rules, equity_at_session_start=100_000.0, now=IN_SESSION
    ).evaluate(proposal, account=account, tick=_qqq_tick())
    assert verdict.approved, verdict.reasons

    width = measure_stop_widths([proposal], {"QQQ": _reading("QQQ", atr=6.0)})[0]
    assert width.is_tight
    assert "Reported, not refused" in width.render()
    assert "where the stop goes is yours" in width.render()


def test_the_measurement_reaches_the_model_beside_the_gates_own_verdict(account):
    """It rides the feedback block that already exists, so no caller changes.

    Deterministic, outcome-free and true regardless of how the trade would have
    gone — the same test the gate's verdicts pass and a P&L history fails, which
    is why this may be fed back at all.
    """
    proposal = _ko(68.45)
    # The real verdict, from the real gate, on the real proposal: APPROVED at
    # four hundredths of an ATR. The feedback block therefore carries an
    # approval and a measurement side by side, which is the arrangement — the
    # gate said yes and the document still says how wide the stop was.
    verdict = RiskGate(
        load_rules(), equity_at_session_start=100_000.0, now=IN_SESSION
    ).evaluate(
        proposal,
        account=account,
        tick=Tick(symbol="KO", bid=68.49, ask=68.51, timestamp=IN_SESSION),
    )
    assert verdict.approved, verdict.reasons

    blob = build_market_context(
        account=account,
        ticks={},
        headlines=[],
        news_windows=[],
        indicators={"KO": _reading("KO", atr=1.32)},
        previous_verdicts=[(proposal, verdict)],
    )

    assert "0.04 ATR" in blob
    assert "inside a single day's average range" in blob
    # The vintage of the figure is stated. Cycles are skipped while the market
    # is shut, so "last cycle" on a Monday can be Friday afternoon.
    assert "not the reading you had when you proposed it" in blob
    assert "No gate refuses a stop for being tight" in blob


def test_the_prompt_says_a_stop_is_a_claim_rather_than_a_lever_on_size():
    """The other half of the seam, and the half item 25 named as missing.

    The block warns against MOVING a stop to reach a size, which is a different
    sentence: it does not say what a stop is FOR. Without that the arithmetic is
    the only guidance in the document, and the arithmetic rewards tightening.
    """
    prompt = " ".join(build_system_prompt(load_rules()).split())

    assert "statement of where the thesis is WRONG" in prompt
    assert "the tighter the stop the larger the position" in prompt
    assert "atr_14" in prompt
    # And it must not become a limit in prose: a threshold stated in the prompt
    # is a limit nobody can read off config/rules.yaml.
    assert str(TIGHT_STOP_ATR) not in prompt
    assert "no proposal has ever been refused for a tight stop" in prompt


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


# ------------------------------------------- the other half, in the other document
#
# The bug was never in one document. It was the SEAM: percentages in the cached
# system prompt, dollars in this one, and five steps of arithmetic between them
# that nobody had asked whether the model would actually do. Rendering the
# ceilings fixes one side of that seam and leaves the prompt still saying "here
# are three percentages, size against them".
#
# So these tests live here rather than in `tests/test_strategy.py` with the rest
# of the prompt assertions: the claim they pin spans both documents, and the
# thing they are guarding is that the two agree. Same shape as the out-of-hours
# mechanics being stated in `market_clock` AND in the prompt, deliberately both —
# the context block is conditional on a caller passing `rules`, and the prompt
# half is a permanent property of how a size is arrived at.


def test_the_prompt_sends_the_model_to_the_block_rather_than_the_percentages():
    """The prompt asked for the arithmetic and the model did it wrong, twice.

    Telling it harder was already tried — the combined cap is described there as
    "the binding constraint most of the time", explicitly, and the subtraction
    still did not happen. What was missing was somewhere else to look. This
    pins that the prompt now names the block, says which cap needs the
    subtraction, and gives the division that replaced the five steps.
    """
    prompt = " ".join(build_system_prompt(load_rules()).split())

    assert "Sizing ceilings, in dollars" in prompt
    assert "Do not re-derive a ceiling from the percentages above" in prompt
    assert "BUDGET SHARED with the positions already open" in prompt
    assert "qty = tightest RISK ceiling / |limit_price - stop_loss_price|" in prompt
    assert "Round DOWN" in prompt


def test_the_prompt_refuses_the_stop_as_a_way_to_reach_a_size():
    """The failure mode the fix itself creates.

    A ceiling in dollars makes the constraint concrete, and the cheapest way to
    make a wanted quantity fit a concrete constraint is to widen the stop —
    which buys a bigger loss at the same percentage, and which the gate
    APPROVES, because it checks the arithmetic and not the intent. The block
    says this and so must the prompt: the block is conditional on a caller
    passing `rules`, and this is not conditional on anything.
    """
    prompt = " ".join(build_system_prompt(load_rules()).split())

    assert "Do not move the stop to make a size fit" in prompt
    assert "a quantity with no stop behind it" in prompt
    assert "the trade is smaller, or it is not a trade" in prompt


def test_the_prompt_explains_the_words_the_block_prints_instead_of_a_figure():
    """**The words are the join between the two documents, so they are asserted
    against the constants rather than retyped.**

    `BUDGET_SPENT` and `HEADROOM_UNKNOWN` are module constants in `context.py`
    precisely so a renderer and a test cannot drift apart. The prompt is where
    they are given a MEANING — a model that meets `BUDGET SPENT` having never
    been told what it is reads an unfamiliar token beside a limit and guesses,
    and the available guess is "some small number". A rename that reached only
    one of the two documents fails here.
    """
    prompt = build_system_prompt(load_rules())

    assert BUDGET_SPENT in prompt
    assert HEADROOM_UNKNOWN in prompt
    # Both are explained as "nothing fits", never as a small budget — which is
    # the whole reason they are words and not `$0.00`.
    assert "Nothing fits at ANY size" in prompt
    assert "Missing is not zero and it is not room" in prompt


def test_the_prompt_treats_an_absent_block_as_missing_rather_than_unlimited():
    """A cycle rendered without `rules` carries no ceilings at all.

    Both `main.py` call sites pass it, so this is a code change away rather than
    a live state — which is exactly when the instruction is worth having, because
    the failure would otherwise be silent: no block, no figures, and a model
    falling back to the percentages and the arithmetic that started this. Same
    answer as a symbol reported with no price history: propose nothing on it.
    """
    prompt = " ".join(build_system_prompt(load_rules()).split())

    assert "no ceilings block at all" in prompt
    assert "Propose nothing rather than working them out from the percentages" in prompt
