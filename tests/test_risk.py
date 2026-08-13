"""Tests for the risk gate.

This is the load-bearing suite. The gate is the only thing standing between a
confidently-wrong model and the account, so each rule gets an explicit test that
proves it rejects, not merely that it exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.config import Rules, load_rules
from bot.models import (
    AccountSnapshot,
    AssetClass,
    Direction,
    OrderProposal,
    Position,
    Tick,
    TradingActivity,
)
from bot.risk import NewsWindow, RiskGate

from .conftest import INSIDE_SESSION, PAPER_EQUITY, RULES_PATH


def _gate(
    rules: Rules,
    *,
    equity: float = PAPER_EQUITY,
    now: datetime | None = None,
) -> RiskGate:
    return RiskGate(
        rules,
        equity_at_session_start=equity,
        now=now or INSIDE_SESSION,
    )


def _reasons_mention(verdict, fragment: str) -> bool:
    return any(fragment.lower() in r.lower() for r in verdict.reasons)


# --------------------------------------------------------------- happy path


def test_approves_well_formed_proposal(rules, account, spy_tick, buy_proposal):
    verdict = _gate(rules).evaluate(buy_proposal, account=account, tick=spy_tick)
    assert verdict.approved, verdict.reasons


def test_collects_every_failure_not_just_the_first(rules, account, spy_tick):
    """A bad proposal should report all its problems in one pass."""
    bad = OrderProposal(
        symbol="TSLA",  # not allowed
        direction=Direction.BUY,
        qty=1000,  # oversized
        limit_price=580.00,
        stop_loss_price=600.00,  # wrong side
        take_profit_price=560.00,  # wrong side
        rationale="Deliberately broken in several ways at once.",
    )
    verdict = _gate(rules).evaluate(bad, account=account, tick=spy_tick)
    assert not verdict.approved
    assert len(verdict.reasons) >= 3, verdict.reasons


# ------------------------------------------------------------ symbol/session


def test_rejects_disallowed_symbol(rules, account, spy_tick):
    proposal = OrderProposal(
        symbol="GME",
        direction=Direction.BUY,
        qty=1,
        limit_price=580.00,
        stop_loss_price=575.00,
        take_profit_price=590.00,
        rationale="Not on the allowlist, should be refused outright.",
    )
    verdict = _gate(rules).evaluate(proposal, account=account, tick=spy_tick)
    assert not verdict.approved
    assert _reasons_mention(verdict, "not in the allowed list")


def test_rejects_outside_session(rules, account, spy_tick, buy_proposal):
    night = datetime(2026, 5, 4, 3, 0, tzinfo=UTC)  # outside [14, 21)
    verdict = _gate(rules, now=night).evaluate(buy_proposal, account=account, tick=spy_tick)
    assert not verdict.approved
    assert _reasons_mention(verdict, "outside the trading sessions")


def test_a_class_limit_may_tighten_the_portfolio_limit(
    rules, account, spy_tick, buy_proposal
):
    """Each instrument class carries its own limits, and the gate takes the
    tighter of the two."""
    # The proposal risks $15 against $100k of equity, so the class limit has to
    # be below 0.015% to bite. That it takes a figure this small is the point:
    # the portfolio limit of 1% is nowhere near binding here, so a rejection can
    # only have come from the class.
    rules.instruments["us_equity"].max_risk_per_trade_pct = 0.01

    verdict = _gate(rules).evaluate(buy_proposal, account=account, tick=spy_tick)

    assert not verdict.approved
    assert _reasons_mention(verdict, "per-trade cap")
    # The message quotes the limit actually applied, not the portfolio one, or
    # an operator would go looking in the wrong block for the number.
    assert _reasons_mention(verdict, "0.01%")
    assert not _reasons_mention(verdict, "1.00%")


def test_a_class_limit_overrides_the_portfolio_limit_in_either_direction():
    """`account:` is the default, not a ceiling.

    An earlier version refused a class limit looser than the portfolio one at
    config load. That was the wrong mechanism: pushing back on a limit getting
    looser belongs to the settings agent, which argues the case and slows the
    operator down without denying the change. A validator that refuses to start
    is a denial, and it denies at the least useful moment — boot, with no
    explanation of the trade-off and no way to say "yes, I mean it".

    What matters instead is that the file and the gate agree. A config saying
    3% while the gate quietly applied 1% would be a limit nobody could read off
    the config, which is worse than either number on its own.
    """
    rules = load_rules()
    rules.instruments["us_equity"].max_risk_per_trade_pct = 3.0

    account = AccountSnapshot(
        equity_usd=100_000.0, cash_usd=100_000.0, buying_power_usd=100_000.0
    )
    tick = Tick(symbol="SPY", bid=579.98, ask=580.02, timestamp=INSIDE_SESSION)
    # Risks $2,500 — over the portfolio's 1% but inside the class's 3%.
    proposal = OrderProposal(
        symbol="SPY",
        direction=Direction.BUY,
        qty=100,
        limit_price=580.00,
        stop_loss_price=555.00,
        take_profit_price=620.00,
        rationale="Sized to the class limit rather than the portfolio default.",
    )

    verdict = _gate(rules).evaluate(proposal, account=account, tick=tick)

    # The per-trade gate does not object: the class said 3% and meant it.
    assert not _reasons_mention(verdict, "per-trade cap")


def test_a_looser_class_limit_still_meets_the_portfolio_total_risk_cap():
    """Worth knowing rather than guarded against.

    `max_total_risk_pct` is portfolio-wide and stays that way, so a per-trade
    override above it can never actually fill — the total-risk gate refuses the
    trade that would breach it. Raising a class limit past the total therefore
    does nothing on its own, which is a fact about the interaction rather than
    a rule stopping anybody.
    """
    rules = load_rules()
    rules.instruments["us_equity"].max_risk_per_trade_pct = 3.0

    account = AccountSnapshot(
        equity_usd=100_000.0, cash_usd=100_000.0, buying_power_usd=100_000.0
    )
    tick = Tick(symbol="SPY", bid=579.98, ask=580.02, timestamp=INSIDE_SESSION)
    proposal = OrderProposal(
        symbol="SPY",
        direction=Direction.BUY,
        qty=100,
        limit_price=580.00,
        stop_loss_price=555.00,
        take_profit_price=620.00,
        rationale="Risks 2.5%, inside the class limit and past the 2% total.",
    )

    verdict = _gate(rules).evaluate(proposal, account=account, tick=tick)

    assert not verdict.approved
    assert _reasons_mention(verdict, "total risk")


def test_a_class_position_cap_counts_only_that_class(rules, account, spy_tick, buy_proposal):
    """Both caps apply, and they measure different things. A class that gets
    loud must not be able to fill every slot the portfolio has."""
    rules.instruments["us_equity"].max_concurrent_positions = 1
    account.open_positions = [
        Position(
            symbol="SPY",
            direction=Direction.BUY,
            qty=1,
            entry_price=580.0,
            opened_at=INSIDE_SESSION,
            current_price=580.0,
        )
    ]

    verdict = _gate(rules).evaluate(buy_proposal, account=account, tick=spy_tick)

    assert not verdict.approved
    assert _reasons_mention(verdict, "for this instrument class")
    # The portfolio cap is 3 and only one position is held, so this rejection
    # can only have come from the class cap.
    assert not _reasons_mention(verdict, "already holding 1 positions")


def test_the_premarket_rule_still_rejects_when_a_class_switches_it_on(
    account, spy_tick, buy_proposal
):
    """The operator has switched this OFF for us_equity — pre-market and after
    hours are both acceptable now, and if the agent wants to trade them it may.

    The mechanism is still tested, with a class that enables it, because
    switching it back on must not be the moment anybody discovers it stopped
    working. It is also the one session rule `sessions_utc` structurally cannot
    express: those are fixed UTC hours against a session defined in New York
    time, so a fixed window is wrong at one end in each half of the year.
    """
    rules = load_rules()
    rules.instruments["us_equity"].refuse_premarket = True

    winter_premarket = datetime(2026, 1, 12, 14, 15, tzinfo=UTC)  # 09:15 EST
    gate = _gate(rules, now=winter_premarket)

    # The UTC window is satisfied — this is the trap the rule exists for.
    assert rules.instruments["us_equity"].is_in_session(winter_premarket)

    verdict = gate.evaluate(buy_proposal, account=account, tick=spy_tick)

    assert not verdict.approved
    assert _reasons_mention(verdict, "pre-market")


def test_the_shipped_config_now_permits_pre_market_and_after_hours(
    rules, account, spy_tick, buy_proposal
):
    """Operator's decision, pinned so it is a choice rather than a drift.

    What widening the window buys is the ability to PLACE at any hour. It does
    not buy an out-of-hours FILL: `place_order` sends a bracket or an OTO and
    Alpaca accepts neither with `extended_hours`, so the order rests and
    becomes eligible at the next open. That is the trade-off that keeps the
    stop at the broker.
    """
    assert rules.instruments["us_equity"].refuse_premarket is False

    premarket = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)   # 08:00 New York
    after_hours = datetime(2026, 8, 10, 21, 0, tzinfo=UTC)  # 17:00 New York

    for moment in (premarket, after_hours):
        verdict = _gate(rules, now=moment).evaluate(
            buy_proposal, account=account, tick=spy_tick
        )
        assert verdict.approved, (moment, verdict.reasons)


@pytest.mark.parametrize(
    ("label", "moment"),
    [
        # 10:00 New York, EST. The regular session.
        ("regular session", datetime(2026, 1, 12, 15, 0, tzinfo=UTC)),
        # 16:30 New York, EDT. After hours, which the operator allows.
        ("after hours", datetime(2026, 8, 10, 20, 30, tzinfo=UTC)),
    ],
)
def test_the_premarket_rule_refuses_nothing_else(
    rules, account, spy_tick, buy_proposal, label, moment
):
    """Only 04:00-09:30 New York. The rule must not quietly become
    regular-session-only — after hours was explicitly kept."""
    verdict = _gate(rules, now=moment).evaluate(
        buy_proposal, account=account, tick=spy_tick
    )

    assert not _reasons_mention(verdict, "pre-market"), f"{label} was refused"


def test_crypto_never_gets_a_premarket_rule(rules):
    """A 24/7 market has no pre-market, and the phases this reads are the US
    equity ones. Switched on for crypto it would refuse every hour of the day."""
    assert rules.instruments["crypto"].refuse_premarket is False


@pytest.mark.parametrize(
    ("moment", "day"),
    [
        (datetime(2026, 5, 9, 15, 0, tzinfo=UTC), "Saturday"),
        (datetime(2026, 5, 10, 15, 0, tzinfo=UTC), "Sunday"),
    ],
)
def test_rejects_a_weekend_even_inside_the_session_hours(
    rules, account, spy_tick, buy_proposal, moment, day
):
    """The hours matched and the market was shut.

    `sessions_utc` is hours only, so 15:00 on a Saturday sat inside `[14, 21)`
    and the gate approved. That cost nothing while the loop placed no orders.
    It stopped being free the moment `--execute` went on, because Alpaca does
    not refuse an out-of-hours equity order — it queues it to the next session,
    so a Saturday proposal would have filled at Monday's open, inside the
    half-hour the window is set to skip.
    """
    verdict = _gate(rules, now=moment).evaluate(
        buy_proposal, account=account, tick=spy_tick
    )

    assert not verdict.approved
    assert _reasons_mention(verdict, f"{day} is not a trading day")


def test_a_weekday_inside_the_hours_is_still_approved(
    rules, account, spy_tick, buy_proposal
):
    """The day check must not have swallowed the ordinary case."""
    verdict = _gate(rules).evaluate(buy_proposal, account=account, tick=spy_tick)

    assert verdict.approved, verdict.reasons


def test_crypto_still_trades_at_the_weekend(rules):
    """The reason session_days_utc has no default.

    Monday-to-Friday looks like a sensible default and would silently shut the
    24/7 class for two days a week, which is the same failure the per-instrument
    split was introduced to fix, in a new costume.
    """
    enabled = _with_crypto(rules)
    saturday = datetime(2026, 5, 9, 3, 0, tzinfo=UTC)
    account = AccountSnapshot(
        equity_usd=PAPER_EQUITY,
        cash_usd=PAPER_EQUITY,
        buying_power_usd=PAPER_EQUITY,
        open_positions=[],
    )

    verdict = _gate(enabled, now=saturday).evaluate(
        _btc(0.02, "Crypto on a Saturday should be allowed; it trades 24/7."),
        account=account,
        tick=_btc_tick(saturday),
    )

    assert verdict.approved, verdict.reasons


def _with_globex(rules: Rules) -> Rules:
    """A CME-shaped class: Sunday evening only, a daily break, Saturday dark."""
    from bot.config import InstrumentRules

    enabled = rules.model_copy(deep=True)
    enabled.instruments["futures"] = InstrumentRules(
        enabled=True,
        strategy="trend_break",
        allowed_symbols=["CL"],
        sessions_utc={
            0: [(0, 21), (22, 24)],
            4: [(0, 21)],
            6: [(22, 24)],
        },
    )
    return enabled


@pytest.mark.parametrize(
    ("moment", "approved", "why"),
    [
        (datetime(2026, 5, 4, 10, 0, tzinfo=UTC), True, "Monday mid-session"),
        (datetime(2026, 5, 4, 21, 30, tzinfo=UTC), False, "the daily maintenance break"),
        (datetime(2026, 5, 10, 12, 0, tzinfo=UTC), False, "Sunday midday, market shut"),
        (datetime(2026, 5, 10, 22, 30, tzinfo=UTC), True, "Sunday evening open"),
        (datetime(2026, 5, 9, 12, 0, tzinfo=UTC), False, "Saturday, dark"),
    ],
)
def test_the_gate_honours_a_per_day_session_map(moment, approved, why):
    """The gate must read per-day windows, not just the day list.

    Sunday midday is the case that matters: under one flat window Sunday would
    inherit Monday's hours and the gate would approve into a shut market, for
    the broker to queue into the next open. That is the weekend bug again with
    an extra step.
    """
    enabled = _with_globex(Rules.load(RULES_PATH))
    now = moment
    proposal = OrderProposal(
        symbol="CL",
        direction=Direction.BUY,
        qty=1,
        limit_price=70.00,
        stop_loss_price=69.30,
        take_profit_price=71.50,
        rationale="Session-window probe for the per-day mapping.",
    )
    tick = Tick(symbol="CL", bid=69.99, ask=70.01, timestamp=now)
    account = AccountSnapshot(
        equity_usd=PAPER_EQUITY,
        cash_usd=PAPER_EQUITY,
        buying_power_usd=PAPER_EQUITY,
        open_positions=[],
    )

    verdict = _gate(enabled, now=now).evaluate(proposal, account=account, tick=tick)

    assert verdict.approved is approved, f"{why}: {verdict.reasons}"


def test_rejects_during_news_blackout(rules, account, spy_tick, buy_proposal):
    window = NewsWindow(
        timestamp=INSIDE_SESSION + timedelta(minutes=5),
        affected_symbols=frozenset({"SPY", "QQQ"}),
    )
    verdict = _gate(rules).evaluate(
        buy_proposal, account=account, tick=spy_tick, news_windows=[window]
    )
    assert not verdict.approved
    assert _reasons_mention(verdict, "news blackout")


def test_news_window_for_other_symbol_does_not_block(rules, account, spy_tick, buy_proposal):
    window = NewsWindow(
        timestamp=INSIDE_SESSION + timedelta(minutes=5),
        affected_symbols=frozenset({"AAPL"}),
    )
    verdict = _gate(rules).evaluate(
        buy_proposal, account=account, tick=spy_tick, news_windows=[window]
    )
    assert verdict.approved, verdict.reasons


# ------------------------------------------------------------------ sizing


def test_rejects_oversized_risk(rules, account, spy_tick):
    """Stop 50 points away on 500 shares = $25,000 risk on a $100k account."""
    huge = OrderProposal(
        symbol="SPY",
        direction=Direction.BUY,
        qty=500,
        limit_price=580.00,
        stop_loss_price=530.00,
        take_profit_price=600.00,
        rationale="Oversized, should breach the per-trade risk cap.",
    )
    verdict = _gate(rules).evaluate(huge, account=account, tick=spy_tick)
    assert not verdict.approved
    assert _reasons_mention(verdict, "per-trade cap")


def test_rejects_position_larger_than_max_position_pct(rules, account, spy_tick):
    """A tight stop keeps risk small, but position size is capped independently."""
    concentrated = OrderProposal(
        symbol="SPY",
        direction=Direction.BUY,
        qty=100,  # $58,000 = 58% of equity
        limit_price=580.00,
        stop_loss_price=579.50,  # only $50 of risk
        take_profit_price=590.00,
        rationale="Tiny stop but a huge position; the size gate should catch it.",
    )
    verdict = _gate(rules).evaluate(concentrated, account=account, tick=spy_tick)
    assert not verdict.approved
    assert _reasons_mention(verdict, "of equity")


# --------------------------------------------------------- 2% total-risk cap


def _account_with_open_risk(risk_usd: float) -> AccountSnapshot:
    """Equity account carrying `risk_usd` of planned risk already on the books."""
    return AccountSnapshot(
        equity_usd=PAPER_EQUITY,
        cash_usd=PAPER_EQUITY,
        buying_power_usd=PAPER_EQUITY,
        open_positions=[],
        open_risk_usd=risk_usd,
    )


def test_two_full_size_trades_fit_under_the_total_risk_cap(rules, spy_tick):
    """1% per trade against a 2% total means two full-size trades, not one."""
    full_size = OrderProposal(
        symbol="SPY",
        direction=Direction.BUY,
        qty=83,
        limit_price=580.00,
        stop_loss_price=568.00,  # $12 x 83 = $996, just under 1% of equity
        take_profit_price=600.00,
        rationale="Full-size trade at roughly the per-trade risk cap.",
    )
    # First trade: nothing open yet.
    assert _gate(rules).evaluate(
        full_size, account=_account_with_open_risk(0.0), tick=spy_tick
    ).approved

    # Second: 1% already committed, so this reaches exactly 2%.
    assert _gate(rules).evaluate(
        full_size, account=_account_with_open_risk(1_000.0), tick=spy_tick
    ).approved


def test_third_full_size_trade_breaches_the_total_risk_cap(rules, spy_tick):
    full_size = OrderProposal(
        symbol="SPY",
        direction=Direction.BUY,
        qty=83,
        limit_price=580.00,
        stop_loss_price=568.00,
        take_profit_price=600.00,
        rationale="Third full-size trade; total risk would reach 3%.",
    )
    verdict = _gate(rules).evaluate(
        full_size, account=_account_with_open_risk(2_000.0), tick=spy_tick
    )
    assert not verdict.approved
    assert _reasons_mention(verdict, "total risk")


def test_the_measured_over_size_is_still_rejected(rules):
    """**The live failure that `context.sizing_ceilings` was written for.**

    Recovered from the gate's own rejection text, 12 Aug 2026. Equity $99,383,
    $1,486.95 already at risk, a $5.50 stop distance on a $305 name:

        per-trade risk cap    $993.83    ->  180 shares
        remaining combined    $500.71    ->   91 shares  <- binding
        concentration      $49,691.50    ->  163 shares

    It proposed 185, which is 2.03x the permissible size. It had sized to
    roughly 180 — the per-trade cap done approximately — and never computed the
    one ceiling requiring a SUBTRACTION.

    The fix is a rendering change: the ceilings reach the model in dollars now,
    with the subtraction already done. **This test exists because the fix must
    not be mistaken for a relaxation.** The gate is unchanged and is still the
    thing that refuses; the block only saves the model from having to derive
    what the gate already knows. A rendering change that made this pass would
    be the failure it was written to prevent.
    """
    account = AccountSnapshot(
        equity_usd=99_383.00,
        cash_usd=116_239.81,
        buying_power_usd=198_766.00,
        open_positions=[],
        open_risk_usd=1_486.95,
    )
    tick = Tick(symbol="MSFT", bid=304.98, ask=305.02, timestamp=INSIDE_SESSION)

    def sized(qty: float) -> OrderProposal:
        return OrderProposal(
            symbol="MSFT",
            direction=Direction.BUY,
            qty=qty,
            limit_price=305.00,
            stop_loss_price=299.50,
            rationale="The size the model named, against the size the gate allows.",
        )

    over = _gate(rules, equity=99_383.00).evaluate(sized(185), account=account, tick=tick)
    assert not over.approved
    assert _reasons_mention(over, "total risk would reach")
    # Every reason at once, as ever: 185 breaches the per-trade cap and the
    # concentration limit too, and a proposal told only the first would be
    # re-sized to 180 and refused again.
    assert _reasons_mention(over, "per-trade cap")
    assert _reasons_mention(over, "of equity")

    # And 91 — the figure the block now renders the numerator for — is approved,
    # so the ceiling shown is a real ceiling rather than a conservative guess.
    permitted = _gate(rules, equity=99_383.00).evaluate(
        sized(91), account=account, tick=tick
    )
    assert permitted.approved, permitted.reasons


def test_four_half_size_trades_fit(rules, spy_tick):
    """The cap counts risk, not positions — half-size means twice as many."""
    half_size = OrderProposal(
        symbol="SPY",
        direction=Direction.BUY,
        qty=41,
        limit_price=580.00,
        stop_loss_price=568.00,  # $12 x 41 = $492, about 0.5% of equity
        take_profit_price=600.00,
        rationale="Half-size trade; four of these fit inside the 2% total.",
    )
    verdict = _gate(rules).evaluate(
        half_size, account=_account_with_open_risk(1_500.0), tick=spy_tick
    )
    assert verdict.approved, verdict.reasons


def test_total_risk_cap_is_leverage_neutral(rules, spy_tick):
    """Notional is irrelevant to the risk cap; stop distance is what counts.

    A large position with a tight stop carries less risk than a small position
    with a wide one — which is backwards from a notional cap, and is the point
    of measuring risk instead.
    """
    big_position_tight_stop = OrderProposal(
        symbol="SPY",
        direction=Direction.BUY,
        qty=80,                  # $46,400 notional
        limit_price=580.00,
        stop_loss_price=578.00,  # $2 x 80 = $160 risk, 0.16% of equity
        take_profit_price=590.00,
        rationale="Large notional, tight stop, small risk.",
    )
    verdict = _gate(rules).evaluate(
        big_position_tight_stop, account=_account_with_open_risk(0.0), tick=spy_tick
    )
    assert verdict.approved, verdict.reasons


# --------------------------------------------------------------- margin guards


def test_rejects_order_using_too_much_buying_power(rules, spy_tick):
    thin = AccountSnapshot(
        equity_usd=PAPER_EQUITY,
        cash_usd=1_000.0,
        buying_power_usd=1_000.0,  # a $1,740 order is 174% of this
        open_positions=[],
    )
    proposal = OrderProposal(
        symbol="SPY",
        direction=Direction.BUY,
        qty=3,
        limit_price=580.00,
        stop_loss_price=575.00,
        take_profit_price=590.00,
        rationale="Small risk, but it would consume most of the buying power.",
    )
    verdict = _gate(rules).evaluate(proposal, account=thin, tick=spy_tick)
    assert not verdict.approved
    assert _reasons_mention(verdict, "buying power")


def test_rejects_when_gross_notional_would_be_breached(rules, spy_tick, buy_proposal):
    leveraged = AccountSnapshot(
        equity_usd=PAPER_EQUITY,
        cash_usd=PAPER_EQUITY,
        buying_power_usd=PAPER_EQUITY * 4,
        open_positions=[
            Position(
                symbol="QQQ",
                direction=Direction.BUY,
                qty=300,
                entry_price=500.0,     # $150,000 = 150% of equity, at the cap
                opened_at=INSIDE_SESSION,
                current_price=500.0,
            )
        ],
    )
    verdict = _gate(rules).evaluate(buy_proposal, account=leveraged, tick=spy_tick)
    assert not verdict.approved
    assert _reasons_mention(verdict, "gross notional")


def test_rejects_limit_price_far_from_market(rules, account, spy_tick):
    """A limit 20% below market is almost always a model arithmetic slip."""
    astray = OrderProposal(
        symbol="SPY",
        direction=Direction.BUY,
        qty=10,
        limit_price=464.00,
        stop_loss_price=460.00,
        take_profit_price=470.00,
        rationale="Limit price nowhere near the current market.",
    )
    verdict = _gate(rules).evaluate(astray, account=account, tick=spy_tick)
    assert not verdict.approved
    assert _reasons_mention(verdict, "away from the market")


# ------------------------------------------------------------------- stops


def test_rejects_buy_with_inverted_stop(rules, account, spy_tick):
    bad = OrderProposal(
        symbol="SPY",
        direction=Direction.BUY,
        qty=10,
        limit_price=580.00,
        stop_loss_price=585.00,  # above entry on a buy
        take_profit_price=590.00,
        rationale="Stop is on the wrong side of entry for a long.",
    )
    verdict = _gate(rules).evaluate(bad, account=account, tick=spy_tick)
    assert not verdict.approved
    assert _reasons_mention(verdict, "stop-loss")


def test_rejects_sell_with_inverted_stop(rules, account, spy_tick):
    bad = OrderProposal(
        symbol="SPY",
        direction=Direction.SELL,
        qty=10,
        limit_price=580.00,
        stop_loss_price=575.00,  # below entry on a sell
        take_profit_price=570.00,
        rationale="Stop is on the wrong side of entry for a short.",
    )
    verdict = _gate(rules).evaluate(bad, account=account, tick=spy_tick)
    assert not verdict.approved
    assert _reasons_mention(verdict, "stop-loss")


def test_accepts_well_formed_sell(rules, account, spy_tick):
    good = OrderProposal(
        symbol="SPY",
        direction=Direction.SELL,
        qty=3,
        limit_price=580.00,
        stop_loss_price=585.00,
        take_profit_price=570.00,
        rationale="Rejected the prior day high; invalidated above 585.",
    )
    verdict = _gate(rules).evaluate(good, account=account, tick=spy_tick)
    assert verdict.approved, verdict.reasons


# -------------------------------------------------------------- account state


def test_rejects_at_max_concurrent_positions(rules, spy_tick, buy_proposal):
    full = AccountSnapshot(
        equity_usd=PAPER_EQUITY,
        cash_usd=PAPER_EQUITY,
        buying_power_usd=PAPER_EQUITY,
        open_positions=[
            Position(
                symbol=sym,
                direction=Direction.BUY,
                qty=1,
                entry_price=100.0,
                opened_at=INSIDE_SESSION,
                current_price=100.0,
            )
            for sym in ("AAPL", "MSFT", "JNJ")[: rules.account.max_concurrent_positions]
        ],
    )
    verdict = _gate(rules).evaluate(buy_proposal, account=full, tick=spy_tick)
    assert not verdict.approved
    assert _reasons_mention(verdict, "positions")


def test_rejects_below_equity_floor(rules, spy_tick, buy_proposal):
    broke = AccountSnapshot(
        equity_usd=rules.account.min_equity_floor_usd - 1,
        cash_usd=50_000.0,
        buying_power_usd=50_000.0,
        open_positions=[],
    )
    verdict = _gate(rules, equity=PAPER_EQUITY).evaluate(
        buy_proposal, account=broke, tick=spy_tick
    )
    assert not verdict.approved
    assert _reasons_mention(verdict, "below floor")


def test_daily_kill_switch_trips_and_is_sticky(rules, spy_tick, buy_proposal):
    losing = AccountSnapshot(
        equity_usd=PAPER_EQUITY * (1 - rules.account.daily_loss_kill_pct / 100) - 1,
        cash_usd=PAPER_EQUITY,
        buying_power_usd=PAPER_EQUITY,
        open_positions=[],
    )
    gate = _gate(rules, equity=PAPER_EQUITY)

    first = gate.evaluate(buy_proposal, account=losing, tick=spy_tick)
    assert not first.approved
    assert _reasons_mention(first, "daily loss")

    # Equity recovers within the same session — the switch must stay tripped.
    recovered = AccountSnapshot(
        equity_usd=PAPER_EQUITY,
        cash_usd=PAPER_EQUITY,
        buying_power_usd=PAPER_EQUITY,
        open_positions=[],
    )
    second = gate.evaluate(buy_proposal, account=recovered, tick=spy_tick)
    assert not second.approved
    assert _reasons_mention(second, "kill-switch")


def test_reset_daily_clears_kill_switch(rules, account, spy_tick, buy_proposal):
    gate = _gate(rules)
    gate.trip_kill_switch()
    assert not gate.evaluate(buy_proposal, account=account, tick=spy_tick).approved

    gate.reset_daily(equity_at_session_start=PAPER_EQUITY)
    assert gate.evaluate(buy_proposal, account=account, tick=spy_tick).approved


# ---------------------------------------------------------------- frequency


def test_rejects_when_daily_trade_limit_reached(rules, account, spy_tick, buy_proposal):
    busy = TradingActivity(trades_today=rules.frequency.max_trades_per_day)
    verdict = _gate(rules).evaluate(
        buy_proposal, account=account, tick=spy_tick, activity=busy
    )
    assert not verdict.approved
    assert _reasons_mention(verdict, "trades today")


def test_rejects_when_weekly_trade_limit_reached(rules, account, spy_tick, buy_proposal):
    busy = TradingActivity(
        trades_today=0, trades_this_week=rules.frequency.max_trades_per_week
    )
    verdict = _gate(rules).evaluate(
        buy_proposal, account=account, tick=spy_tick, activity=busy
    )
    assert not verdict.approved
    assert _reasons_mention(verdict, "this week")


def test_rejects_inside_symbol_cooldown(rules, account, spy_tick, buy_proposal):
    just_traded = TradingActivity(
        last_trade_at_by_symbol={"SPY": INSIDE_SESSION - timedelta(seconds=60)}
    )
    verdict = _gate(rules).evaluate(
        buy_proposal, account=account, tick=spy_tick, activity=just_traded
    )
    assert not verdict.approved
    assert _reasons_mention(verdict, "cooldown")


def test_allows_after_cooldown_expires(rules, account, spy_tick, buy_proposal):
    cooldown = rules.frequency.min_seconds_between_trades_per_symbol
    long_ago = TradingActivity(
        last_trade_at_by_symbol={"SPY": INSIDE_SESSION - timedelta(seconds=cooldown + 60)}
    )
    verdict = _gate(rules).evaluate(
        buy_proposal, account=account, tick=spy_tick, activity=long_ago
    )
    assert verdict.approved, verdict.reasons


def test_cooldown_on_other_symbol_does_not_block(rules, account, spy_tick, buy_proposal):
    other = TradingActivity(
        last_trade_at_by_symbol={"QQQ": INSIDE_SESSION - timedelta(seconds=10)}
    )
    verdict = _gate(rules).evaluate(
        buy_proposal, account=account, tick=spy_tick, activity=other
    )
    assert verdict.approved, verdict.reasons


# ------------------------------------------------------- per-instrument rules


def _with_crypto(rules: Rules, cap: float = 15.0) -> Rules:
    enabled = rules.model_copy(deep=True)
    crypto = enabled.instruments["crypto"]
    crypto.enabled = True
    crypto.allowed_symbols = ["BTC/USD"]
    crypto.capital_cap_pct = cap
    return enabled


def _btc_tick(when: datetime) -> Tick:
    return Tick(symbol="BTC/USD", bid=64_990.0, ask=65_010.0, timestamp=when)


def _btc(qty: float, rationale: str) -> OrderProposal:
    return OrderProposal(
        symbol="BTC/USD",
        asset_class=AssetClass.CRYPTO,
        direction=Direction.BUY,
        qty=qty,
        limit_price=65_000.0,
        stop_loss_price=63_000.0,
        take_profit_price=70_000.0,
        rationale=rationale,
    )


def test_disabled_instrument_class_blocks_its_symbols(rules, account):
    """Crypto ships disabled, so its symbols are simply not tradeable."""
    verdict = _gate(rules).evaluate(
        _btc(0.01, "Crypto class is disabled in the shipped rules."),
        account=account,
        tick=_btc_tick(INSIDE_SESSION),
    )
    assert not verdict.approved
    assert _reasons_mention(verdict, "not in the allowed list")


def test_crypto_trades_outside_the_equity_session(rules):
    """The bug this restructure fixes.

    Under a single global session window, enabling crypto silently forbade
    trading it for three quarters of the day. Each class now carries its own.
    """
    enabled = _with_crypto(rules)
    night = datetime(2026, 5, 4, 3, 0, tzinfo=UTC)
    account = AccountSnapshot(
        equity_usd=PAPER_EQUITY,
        cash_usd=PAPER_EQUITY,
        buying_power_usd=PAPER_EQUITY,
        open_positions=[],
    )
    verdict = _gate(enabled, now=night).evaluate(
        _btc(0.02, "Crypto at 03:00 UTC should be allowed; it trades 24/7."),
        account=account,
        tick=_btc_tick(night),
    )
    assert verdict.approved, verdict.reasons


def test_equities_are_still_bound_by_their_session(rules, account, spy_tick, buy_proposal):
    """The equity window must keep applying, or the fix went too far."""
    night = datetime(2026, 5, 4, 3, 0, tzinfo=UTC)
    verdict = _gate(rules, now=night).evaluate(
        buy_proposal, account=account, tick=spy_tick
    )
    assert not verdict.approved
    assert _reasons_mention(verdict, "outside the trading sessions")


def test_instrument_capital_cap_is_enforced(rules):
    enabled = _with_crypto(rules, cap=5.0)
    account = AccountSnapshot(
        equity_usd=PAPER_EQUITY,
        cash_usd=PAPER_EQUITY,
        buying_power_usd=PAPER_EQUITY,
        open_positions=[
            Position(
                symbol="BTC/USD",
                asset_class=AssetClass.CRYPTO,
                direction=Direction.BUY,
                qty=0.07,
                entry_price=65_000.0,   # $4,550 = 4.55% of equity
                opened_at=INSIDE_SESSION,
                current_price=65_000.0,
            )
        ],
    )
    verdict = _gate(enabled).evaluate(
        _btc(0.05, "Second crypto position breaching the class capital cap."),
        account=account,
        tick=_btc_tick(INSIDE_SESSION),
    )
    assert not verdict.approved
    assert _reasons_mention(verdict, "allocation would reach")


def test_a_class_without_a_cap_is_not_capped(rules, account, spy_tick, buy_proposal):
    """Equities configure no capital_cap_pct, so only portfolio limits apply."""
    assert rules.instruments["us_equity"].capital_cap_pct is None
    verdict = _gate(rules).evaluate(buy_proposal, account=account, tick=spy_tick)
    assert verdict.approved, verdict.reasons


def test_strategy_label_resolves_per_class(rules):
    enabled = _with_crypto(rules)
    assert enabled.strategy_for("SPY") == "mean_reversion"
    assert enabled.strategy_for("BTC/USD") == "momentum"


# ------------------------------------------------- per-class TOTAL risk cap
#
# The operator's rule: "if crypto is enabled, crypto shall not consume more
# than 0.5% of equity in risk. Shall not hold more than 0.5% of risk in
# positions. Current positions in profit do not count towards offsetting this.
# Position must be closed if another wants to be taken if outside of risk
# profile."


def _crypto_class_total(rules: Rules) -> Rules:
    """Crypto enabled with only the class total-risk cap left in the way.

    The concurrent-position and capital caps are widened deliberately. Crypto
    ships with one position and a 15% capital cap, and both would fire on a
    second position long before the total-risk cap was reached — so a rejection
    would prove nothing about the rule under test. The cap itself is left at
    the shipped 0.5%, because that figure is the operator's rule and a test
    that set its own would stop guarding the config.
    """
    enabled = _with_crypto(rules, cap=100.0)
    enabled.instruments["crypto"].max_concurrent_positions = 5
    return enabled


def _btc_position(
    *, qty: float = 0.2, current_price: float = 65_000.0, pnl_usd: float = 0.0
) -> Position:
    return Position(
        symbol="BTC/USD",
        asset_class=AssetClass.CRYPTO,
        direction=Direction.BUY,
        qty=qty,
        entry_price=65_000.0,
        opened_at=INSIDE_SESSION,
        current_price=current_price,
        unrealised_pnl_usd=pnl_usd,
    )


def _account_with_class_risk(
    *,
    positions: list[Position] | None = None,
    open_risk_by_symbol: dict[str, float] | None = None,
    symbols_with_unknown_risk: list[str] | None = None,
) -> AccountSnapshot:
    by_symbol = open_risk_by_symbol or {}
    return AccountSnapshot(
        equity_usd=PAPER_EQUITY,
        cash_usd=PAPER_EQUITY,
        buying_power_usd=PAPER_EQUITY,
        open_positions=positions or [],
        open_risk_usd=sum(by_symbol.values()),
        open_risk_by_symbol=by_symbol,
        symbols_with_unknown_risk=symbols_with_unknown_risk or [],
    )


def test_the_shipped_config_caps_crypto_total_risk_at_half_a_percent(rules):
    """The rule is only as good as the file. Guard the number, not just the gate."""
    assert rules.instruments["crypto"].max_class_total_risk_pct == 0.5
    # Per-trade and class total at the same figure is what makes crypto one
    # full-size position at a time, which is the intent rather than a
    # coincidence of two numbers.
    assert rules.instruments["crypto"].max_risk_per_trade_pct == 0.5


def test_rejects_a_crypto_trade_that_breaches_the_class_total(rules):
    """$400 already at risk in the class, plus $400 more, against a $500 cap."""
    enabled = _crypto_class_total(rules)
    account = _account_with_class_risk(
        positions=[_btc_position()],
        open_risk_by_symbol={"BTC/USD": 400.0},
    )

    verdict = _gate(enabled).evaluate(
        # 0.2 BTC with the stop $2,000 away is $400 of risk.
        _btc(0.2, "Second crypto position; the class would hold 0.8% of risk."),
        account=account,
        tick=_btc_tick(INSIDE_SESSION),
    )

    assert not verdict.approved
    assert _reasons_mention(verdict, "open risk would reach")
    assert _reasons_mention(verdict, "800.00")
    assert _reasons_mention(verdict, "0.50% of equity")


def test_unrealised_profit_does_not_offset_open_crypto_risk(rules):
    """The clause most likely to be "simplified" away later, so it is pinned.

    Risk is `|entry - stop| x qty` — what the position loses if the stop fills.
    A position being up $8,000 has not reduced that by a cent, and netting the
    mark against it would make the cap loosest exactly when the class had run
    furthest. Two accounts identical but for the profit must produce identical
    verdicts.
    """
    enabled = _crypto_class_total(rules)
    proposal = _btc(0.2, "Second crypto position while the first is well ahead.")

    flat = _account_with_class_risk(
        positions=[_btc_position()],
        open_risk_by_symbol={"BTC/USD": 400.0},
    )
    in_profit = _account_with_class_risk(
        # Up $8,000: 0.2 BTC bought at 65,000 and marked at 105,000.
        positions=[_btc_position(current_price=105_000.0, pnl_usd=8_000.0)],
        open_risk_by_symbol={"BTC/USD": 400.0},
    )

    flat_verdict = _gate(enabled).evaluate(
        proposal, account=flat, tick=_btc_tick(INSIDE_SESSION)
    )
    profit_verdict = _gate(enabled).evaluate(
        proposal, account=in_profit, tick=_btc_tick(INSIDE_SESSION)
    )

    assert not profit_verdict.approved
    assert _reasons_mention(profit_verdict, "unrealised profit does not offset")
    # The figures are the same, not merely the outcome: the profit changed
    # nothing about what the class has at risk.
    assert [r for r in profit_verdict.reasons if "open risk would reach" in r] == [
        r for r in flat_verdict.reasons if "open risk would reach" in r
    ]


def test_a_held_position_with_unknown_risk_refuses_the_class(rules):
    """Fails CLOSED. An unknown is missing, never zero.

    Without this the held position contributes 0.0 to the class total and the
    trade sails through — a cap counting a position it cannot see as risking
    nothing, which is how a limit silently stops binding. `reconcile` already
    calls this `risk_is_understated` and warns; here it is a refusal, because
    this gate is what the figure exists for.
    """
    enabled = _crypto_class_total(rules)
    account = _account_with_class_risk(
        positions=[_btc_position()],
        open_risk_by_symbol={},            # nothing known about it
        symbols_with_unknown_risk=["BTC/USD"],
    )
    assert account.risk_is_understated

    verdict = _gate(enabled).evaluate(
        _btc(0.05, "Small crypto add while an untracked position is held."),
        account=account,
        tick=_btc_tick(INSIDE_SESSION),
    )

    assert not verdict.approved
    assert _reasons_mention(verdict, "cannot be established")
    assert _reasons_mention(verdict, "no journal row")
    assert _reasons_mention(verdict, "BTC/USD")


def test_a_crypto_trade_inside_the_class_total_is_approved(rules):
    """$400 already at risk plus $100 is $500, exactly the cap — not over it."""
    enabled = _crypto_class_total(rules)
    account = _account_with_class_risk(
        positions=[_btc_position()],
        open_risk_by_symbol={"BTC/USD": 400.0},
    )

    verdict = _gate(enabled).evaluate(
        _btc(0.05, "Adds $100 of risk, bringing the class to exactly 0.5%."),
        account=account,
        tick=_btc_tick(INSIDE_SESSION),
    )

    assert verdict.approved, verdict.reasons


def test_the_class_total_counts_only_that_class(rules):
    """Equity risk is not crypto risk. Membership is by `allowed_symbols`.

    $1,900 of equity risk on the books and a crypto trade proposed: the class
    total sees none of it, because the class is defined by the symbols in its
    own block rather than by anything on the position.
    """
    enabled = _crypto_class_total(rules)
    account = _account_with_class_risk(
        positions=[
            Position(
                symbol="SPY",
                direction=Direction.BUY,
                qty=3,
                entry_price=580.0,
                opened_at=INSIDE_SESSION,
                current_price=580.0,
            )
        ],
        open_risk_by_symbol={"SPY": 1_900.0},
    )

    verdict = _gate(enabled).evaluate(
        _btc(0.02, "Crypto trade beside an equity book that is nearly full."),
        account=account,
        tick=_btc_tick(INSIDE_SESSION),
    )

    # The portfolio-wide 2% cap still binds — $1,900 plus $40 is under $2,000 —
    # and the class cap sees only the $40 this trade brings.
    assert verdict.approved, verdict.reasons


def test_a_class_with_no_total_risk_cap_is_unaffected(rules, spy_tick, buy_proposal):
    """us_equity configures none, so it must behave exactly as it did before."""
    assert rules.instruments["us_equity"].max_class_total_risk_pct is None

    account = _account_with_class_risk(open_risk_by_symbol={"SPY": 900.0})
    account.open_positions = [
        Position(
            symbol="SPY",
            direction=Direction.BUY,
            qty=3,
            entry_price=580.0,
            opened_at=INSIDE_SESSION,
            current_price=580.0,
        )
    ]

    verdict = _gate(rules).evaluate(buy_proposal, account=account, tick=spy_tick)

    # $900 of SPY risk is nine times what a 0.5% class cap would allow, and
    # nothing refuses it: the equity class has no opinion, so only the
    # portfolio limits apply.
    assert verdict.approved, verdict.reasons


def test_the_class_total_rejection_says_to_close_a_position(rules):
    """The operator asked for the CONSEQUENCE, not a number.

    "Position must be closed if another wants to be taken if outside of risk
    profile" is the instruction. A bare figure would leave a reader looking for
    a limit to widen, which is the one response this repository refuses.
    """
    enabled = _crypto_class_total(rules)
    account = _account_with_class_risk(
        positions=[_btc_position()],
        open_risk_by_symbol={"BTC/USD": 400.0},
    )

    verdict = _gate(enabled).evaluate(
        _btc(0.2, "Breaches the class total; the message must say what to do."),
        account=account,
        tick=_btc_tick(INSIDE_SESSION),
    )

    assert not verdict.approved
    assert _reasons_mention(verdict, "close an existing")
    assert _reasons_mention(verdict, "before opening another")


def test_the_class_total_still_collects_other_failures(rules):
    """The gate reports everything wrong at once; this rule must not short-circuit."""
    enabled = _with_crypto(rules, cap=1.0)   # a 1% capital cap, also breached
    account = _account_with_class_risk(
        positions=[_btc_position()],
        open_risk_by_symbol={"BTC/USD": 400.0},
    )

    verdict = _gate(enabled).evaluate(
        _btc(0.2, "Breaches the class total, the capital cap and the slot count."),
        account=account,
        tick=_btc_tick(INSIDE_SESSION),
    )

    assert not verdict.approved
    assert _reasons_mention(verdict, "open risk would reach")     # this rule
    assert _reasons_mention(verdict, "allocation would reach")    # capital cap
    assert _reasons_mention(verdict, "for this instrument class")  # slot count


# ------------------------------------------------------------------- config


def test_frequency_rules_reject_weekly_below_daily():
    from bot.config import FrequencyRules

    with pytest.raises(ValueError, match="max_trades_per_week"):
        FrequencyRules(
            max_trades_per_day=10,
            max_trades_per_week=5,
            min_seconds_between_trades_per_symbol=0,
        )


def test_a_proposal_with_no_take_profit_is_approved(rules, account, spy_tick):
    """The stop is required; the exit never was. A trade with no target must
    pass the gate on its merits rather than being refused for a field the
    operator's rules do not mention."""
    proposal = OrderProposal(
        symbol="SPY",
        direction=Direction.BUY,
        qty=3,
        limit_price=580.00,
        stop_loss_price=575.00,
        take_profit_price=None,
        rationale="No target; the exit is managed rather than pre-committed.",
    )

    verdict = _gate(rules).evaluate(proposal, account=account, tick=spy_tick)

    assert verdict.approved, verdict.reasons


def test_a_short_with_no_take_profit_is_approved(rules, account, spy_tick):
    """The exact shape of the operator's first manual trade: SHORT, hard stop,
    no target.

    Split from the buy case rather than parametrised because the two directions
    are separate branches in `_stops_on_correct_side`, and a mutation to the
    sell branch survived the buy-only test. The configuration about to be
    traded had no coverage at all.
    """
    proposal = OrderProposal(
        symbol="SPY",
        direction=Direction.SELL,
        qty=3,
        limit_price=580.00,
        stop_loss_price=585.00,      # above entry: correct side for a short
        take_profit_price=None,
        rationale="Short with a hard stop and no target; the exit is managed.",
    )

    verdict = _gate(rules).evaluate(proposal, account=account, tick=spy_tick)

    assert verdict.approved, verdict.reasons


def test_a_stop_on_the_wrong_side_is_still_refused_without_a_target(
    rules, account, spy_tick
):
    """Making the target optional must not weaken the check that remains. A
    stop below entry on a short is not a stop, it is a target."""
    proposal = OrderProposal(
        symbol="SPY",
        direction=Direction.SELL,
        qty=3,
        limit_price=580.00,
        stop_loss_price=575.00,     # below entry on a SHORT — wrong side
        take_profit_price=None,
        rationale="Deliberately inverted stop, with no target to hide behind.",
    )

    verdict = _gate(rules).evaluate(proposal, account=account, tick=spy_tick)

    assert not verdict.approved
    assert _reasons_mention(verdict, "not above entry")


# ---------------------------------------------------- symbols granted by a dream
#
# The most safety-sensitive rule in this file, because it is the only one that
# WIDENS what may be traded. The grant is resolved outside the gate — see
# `grants.py` — and arrives as a mapping of symbol to instrument-class key, in
# the same shape as `news_windows` and for the same reason: this gate reads no
# database and cannot be allowed to fail open.
#
# What adoption buys is entry to the allowlist. Every test below is about
# something it does NOT buy.


def _tsla_tick(when: datetime) -> Tick:
    return Tick(symbol="TSLA", bid=419.98, ask=420.02, timestamp=when)


def _tsla(qty: float, rationale: str, *, stop: float = 415.0) -> OrderProposal:
    return OrderProposal(
        symbol="TSLA",
        direction=Direction.BUY,
        qty=qty,
        limit_price=420.00,
        stop_loss_price=stop,
        take_profit_price=430.00,
        rationale=rationale,
    )


def test_an_unlisted_symbol_is_refused_with_no_grant(rules, account):
    """The control for everything below: TSLA is in no instrument block."""
    verdict = _gate(rules).evaluate(
        _tsla(3, "Nothing grants this, so it is an ordinary unlisted symbol."),
        account=account,
        tick=_tsla_tick(INSIDE_SESSION),
    )

    assert not verdict.approved
    assert _reasons_mention(verdict, "not in the allowed list")
    assert verdict.granted_by_dream_class is None


def test_a_granted_symbol_is_approved(rules, account):
    verdict = _gate(rules).evaluate(
        _tsla(3, "An adopted dream grants this symbol under the equity block."),
        account=account,
        tick=_tsla_tick(INSIDE_SESSION),
        granted_symbols={"TSLA": "us_equity"},
    )

    assert verdict.approved, verdict.reasons


def test_the_verdict_records_that_a_dream_was_what_permitted_this(rules, account):
    """A permission that leaves no trace is not auditable.

    The verdict names the CLASS rather than the dream, and the field is named
    for that: the gate is handed a symbol-to-class mapping and a database read
    to fetch a dream id is exactly what it must not do. Which dream is resolved
    outside and lands on `Trade.dream_id`.
    """
    verdict = _gate(rules).evaluate(
        _tsla(3, "An adopted dream grants this symbol under the equity block."),
        account=account,
        tick=_tsla_tick(INSIDE_SESSION),
        granted_symbols={"TSLA": "us_equity"},
    )

    assert verdict.granted_by_dream_class == "us_equity"
    assert verdict.permitted_by_dream_grant


def test_a_listed_symbol_is_never_marked_as_granted(rules, account, spy_tick, buy_proposal):
    """A stale grant naming SPY must not make an ordinary trade look like a
    dream's. The allowlist answer wins, so the audit trail cannot claim a dream
    was load-bearing on a trade that would have happened anyway."""
    verdict = _gate(rules).evaluate(
        buy_proposal,
        account=account,
        tick=spy_tick,
        granted_symbols={"SPY": "crypto"},
    )

    assert verdict.approved, verdict.reasons
    assert verdict.granted_by_dream_class is None
    assert not verdict.permitted_by_dream_grant


def test_a_grant_under_a_disabled_class_is_refused(rules, account):
    """The class hard block, enforced in the gate as well as in the resolver.

    `grants.py` drops it first and this is the lock that does not depend on that
    having been got right — the same arrangement as `mode=ro` plus the statement
    guard in `insight.py`. A gate that trusted its input to have been filtered
    would be a gate whose safety lived in another file.
    """
    assert rules.instruments["crypto"].enabled is False

    verdict = _gate(rules).evaluate(
        _btc(0.01, "A dream grants this, but the whole class is switched off."),
        account=account,
        tick=_btc_tick(INSIDE_SESSION),
        granted_symbols={"BTC/USD": "crypto"},
    )

    assert not verdict.approved
    assert _reasons_mention(verdict, "not in the allowed list")
    # Named, so an operator is not sent looking for the wrong problem.
    assert _reasons_mention(verdict, "can never enable a class")
    # And no class was applied, so nothing may claim a dream permitted this.
    assert verdict.granted_by_dream_class is None


def test_a_grant_naming_a_class_nobody_configured_is_refused(rules, account):
    verdict = _gate(rules).evaluate(
        _tsla(3, "The grant names an instrument block that does not exist."),
        account=account,
        tick=_tsla_tick(INSIDE_SESSION),
        granted_symbols={"TSLA": "futures"},
    )

    assert not verdict.approved
    assert _reasons_mention(verdict, "not in the allowed list")


def test_an_empty_or_absent_mapping_behaves_exactly_as_before(
    rules, account, spy_tick, buy_proposal
):
    """The feature must be invisible when nothing is granted."""
    gate = _gate(rules)
    without = gate.evaluate(buy_proposal, account=account, tick=spy_tick)
    empty = gate.evaluate(
        buy_proposal, account=account, tick=spy_tick, granted_symbols={}
    )
    explicit_none = gate.evaluate(
        buy_proposal, account=account, tick=spy_tick, granted_symbols=None
    )

    assert without == empty == explicit_none
    assert without.approved, without.reasons


# ------------------------- a grant buys the allowlist and not one thing more


def test_a_granted_symbol_still_faces_the_per_trade_cap_of_its_class(rules, account):
    """The whole tolerability argument: a permission is not an order.

    Sized to breach the equity class's 1% per-trade cap, and refused for it.
    """
    verdict = _gate(rules).evaluate(
        # 5 points of stop distance x 250 shares is $1,250 against a $1,000 cap.
        _tsla(250, "Granted, and deliberately too large for the class limit."),
        account=account,
        tick=_tsla_tick(INSIDE_SESSION),
        granted_symbols={"TSLA": "us_equity"},
    )

    assert not verdict.approved
    assert _reasons_mention(verdict, "per-trade cap")
    # The rejection is about size, not about the symbol — the grant worked.
    assert not _reasons_mention(verdict, "not in the allowed list")
    # And the verdict still records that the symbol was only considerable
    # because of a dream. A refused trade in a granted symbol is a fact about
    # the grant, and the Decisions page is the only surface it exists on.
    assert verdict.granted_by_dream_class == "us_equity"


def test_a_granted_symbol_faces_its_class_session_window(rules, account):
    """Out of hours for the class it was granted under, not for no class at all."""
    saturday = datetime(2026, 5, 2, 15, 0, tzinfo=UTC)

    verdict = _gate(rules, now=saturday).evaluate(
        _tsla(3, "Granted, but the equity session does not run on a Saturday."),
        account=account,
        tick=_tsla_tick(saturday),
        granted_symbols={"TSLA": "us_equity"},
    )

    assert not verdict.approved
    assert _reasons_mention(verdict, "not a trading day")


def test_a_granted_symbol_counts_toward_its_class_concurrency_cap(rules):
    """The bypass that is not `_symbol_allowed`, and the reason `_class_symbols`
    exists.

    `_concurrent_positions` counts a class's positions by SYMBOL MEMBERSHIP of
    `allowed_symbols`, and a granted symbol is in no such list. Without the
    grants folded into that membership, a position held under a grant would be
    invisible to the very cap it is supposed to be subject to: the grant would
    have bought entry to the allowlist AND a quiet exemption.
    """
    tightened = rules.model_copy(deep=True)
    tightened.instruments["us_equity"].max_concurrent_positions = 1

    held = Position(
        symbol="TSLA",
        direction=Direction.BUY,
        qty=3,
        entry_price=420.0,
        opened_at=INSIDE_SESSION,
        current_price=420.0,
    )
    account = AccountSnapshot(
        equity_usd=PAPER_EQUITY,
        cash_usd=PAPER_EQUITY,
        buying_power_usd=PAPER_EQUITY,
        open_positions=[held],
        open_risk_by_symbol={"TSLA": 15.0},
        open_risk_usd=15.0,
    )

    verdict = _gate(tightened).evaluate(
        OrderProposal(
            symbol="SPY",
            direction=Direction.BUY,
            qty=3,
            limit_price=580.00,
            stop_loss_price=575.00,
            take_profit_price=590.00,
            rationale="A listed symbol, with the class's one slot already used.",
        ),
        account=account,
        tick=Tick(symbol="SPY", bid=579.98, ask=580.02, timestamp=INSIDE_SESSION),
        granted_symbols={"TSLA": "us_equity"},
    )

    assert not verdict.approved
    assert _reasons_mention(verdict, "for this instrument class")


def test_a_granted_symbol_counts_toward_its_class_total_risk_cap(rules):
    """Same bypass, on the cap the operator wrote in their own words.

    A granted crypto symbol holding 0.4% of the class's 0.5% must leave room for
    only 0.1% more, exactly as a listed one would.
    """
    enabled = _crypto_class_total(rules)

    held = Position(
        symbol="DOGE/USD",
        asset_class=AssetClass.CRYPTO,
        direction=Direction.BUY,
        qty=1000,
        entry_price=0.4,
        opened_at=INSIDE_SESSION,
        current_price=0.4,
    )
    account = _account_with_class_risk(
        positions=[held], open_risk_by_symbol={"DOGE/USD": 400.0}
    )

    verdict = _gate(enabled).evaluate(
        # 0.2 BTC with the stop $2,000 away is $400 of risk: $800 against $500.
        _btc(0.2, "The granted position already holds most of the class budget."),
        account=account,
        tick=_btc_tick(INSIDE_SESSION),
        granted_symbols={"DOGE/USD": "crypto"},
    )

    assert not verdict.approved
    assert _reasons_mention(verdict, "open risk would reach")
    assert _reasons_mention(verdict, "800.00")


def test_a_granted_position_with_no_journal_row_refuses_the_class(rules):
    """Fails closed, exactly as an untracked listed position does.

    A held position the journal has never seen has an unknowable planned stop,
    so the class total cannot be established — and a grant must not be a way to
    make a position invisible to that check.
    """
    enabled = _crypto_class_total(rules)

    held = Position(
        symbol="DOGE/USD",
        asset_class=AssetClass.CRYPTO,
        direction=Direction.BUY,
        qty=1000,
        entry_price=0.4,
        opened_at=INSIDE_SESSION,
        current_price=0.4,
    )
    account = _account_with_class_risk(
        positions=[held], symbols_with_unknown_risk=["DOGE/USD"]
    )

    verdict = _gate(enabled).evaluate(
        _btc(0.01, "The class total cannot be established while that is unknown."),
        account=account,
        tick=_btc_tick(INSIDE_SESSION),
        granted_symbols={"DOGE/USD": "crypto"},
    )

    assert not verdict.approved
    assert _reasons_mention(verdict, "cannot be established")


def test_a_granted_symbol_counts_toward_its_class_capital_cap(rules):
    """The third membership-counted gate. Crypto ships a 15% capital cap."""
    enabled = _with_crypto(rules, cap=15.0)
    enabled.instruments["crypto"].max_concurrent_positions = 5

    held = Position(
        symbol="DOGE/USD",
        asset_class=AssetClass.CRYPTO,
        direction=Direction.BUY,
        qty=1000,
        entry_price=14.0,          # $14,000 of the $15,000 cap
        opened_at=INSIDE_SESSION,
        current_price=14.0,
    )
    account = _account_with_class_risk(
        positions=[held], open_risk_by_symbol={"DOGE/USD": 100.0}
    )

    verdict = _gate(enabled).evaluate(
        _btc(0.05, "Small, but the granted position already fills the sleeve."),
        account=account,
        tick=_btc_tick(INSIDE_SESSION),
        granted_symbols={"DOGE/USD": "crypto"},
    )

    assert not verdict.approved
    assert _reasons_mention(verdict, "allocation would reach")


def test_a_granted_symbol_still_faces_the_portfolio_total_risk_cap(rules):
    """Nothing about a grant touches the account-wide rules."""
    account = AccountSnapshot(
        equity_usd=PAPER_EQUITY,
        cash_usd=PAPER_EQUITY,
        buying_power_usd=PAPER_EQUITY,
        open_risk_usd=1_900.0,     # 1.9% of a 2% portfolio budget already used
    )

    verdict = _gate(rules).evaluate(
        _tsla(40, "Granted, and it would take the portfolio through 2%."),
        account=account,
        tick=_tsla_tick(INSIDE_SESSION),
        granted_symbols={"TSLA": "us_equity"},
    )

    assert not verdict.approved
    assert _reasons_mention(verdict, "total risk would reach")


def test_a_granted_symbol_is_still_refused_during_a_live_stand_down(rules, account):
    """A grant is a permission to consider a symbol, never a way past a rule
    that has nothing to do with symbols."""
    from bot.models import ExecutionMode, StandDownState

    gate = RiskGate(
        rules,
        equity_at_session_start=PAPER_EQUITY,
        execution_mode=ExecutionMode.LIVE,
        now=INSIDE_SESSION,
    )
    state = StandDownState(
        stage=1,
        started_at=INSIDE_SESSION,
        ends_at=INSIDE_SESSION + timedelta(days=3),
        consecutive_losses=3,
    )

    verdict = gate.evaluate(
        _tsla(3, "Granted, and the account is standing down after three losses."),
        account=account,
        tick=_tsla_tick(INSIDE_SESSION),
        stand_down=state,
        granted_symbols={"TSLA": "us_equity"},
    )

    assert not verdict.approved
    assert _reasons_mention(verdict, "stand-down")


def test_the_gate_reads_nothing_but_its_arguments(rules, account):
    """Determinism, stated as a test rather than as a comment.

    The same gate, the same inputs, the same answer — and no store, no file and
    no clock beyond the injected one is involved in producing it. A gate that
    read the dream store itself would be a gate that could fail, and this one
    must not fail open.
    """
    import bot.risk as risk_mod

    assert "dreaming" not in risk_mod.__dict__
    assert "grants" not in risk_mod.__dict__

    gate = _gate(rules)
    first = gate.evaluate(
        _tsla(3, "Granted, and asked for twice with identical inputs."),
        account=account,
        tick=_tsla_tick(INSIDE_SESSION),
        granted_symbols={"TSLA": "us_equity"},
    )
    second = gate.evaluate(
        _tsla(3, "Granted, and asked for twice with identical inputs."),
        account=account,
        tick=_tsla_tick(INSIDE_SESSION),
        granted_symbols={"TSLA": "us_equity"},
    )

    assert first == second


# ------------------------------- the class block, checked here as the guarantee


def test_a_grant_whose_class_disagrees_with_the_symbol_is_refused(rules, account):
    """`grants.py` is the useful message; this is the promise.

    The gate is handed a mapping and must not assume it was filtered. A grant of
    `BTC/USD` under `us_equity` names an ENABLED class, so every check that
    existed said yes — and the equity book's limits then applied to an order
    `AlpacaBroker` routes to Alpaca as crypto: unbracketed, no broker-side stop.
    Same arrangement as `mode=ro` plus the statement guard in `insight.py`.
    """
    verdict = _gate(rules).evaluate(
        _btc(0.01, "A crypto symbol wearing the equity book's class key."),
        account=account,
        tick=_btc_tick(INSIDE_SESSION),
        granted_symbols={"BTC/USD": "us_equity"},
    )

    assert not verdict.approved
    assert verdict.granted_by_dream_class is None
    assert _reasons_mention(verdict, "belongs to 'crypto'")


def test_the_refusal_names_the_class_the_symbol_really_belongs_to(rules, account):
    """"Not in the allowed list" on a symbol a dream visibly granted sends an
    operator to the wrong problem, and so does "that class is switched off" when
    the class named is enabled."""
    verdict = _gate(rules).evaluate(
        _btc(0.01, "The message has to distinguish the two ways this fails."),
        account=account,
        tick=_btc_tick(INSIDE_SESSION),
        granted_symbols={"BTC/USD": "us_equity"},
    )

    assert not _reasons_mention(verdict, "not an enabled instrument class")
    assert _reasons_mention(verdict, "routes on the symbol")


def test_a_grant_for_a_symbol_whose_class_cannot_be_established_is_refused(
    rules, account, spy_tick
):
    """Two blocks claiming one symbol leaves no single answer, and an unknown is
    never treated as a permission."""
    muddled = rules.model_copy(deep=True)
    muddled.instruments["crypto"].allowed_symbols = ["WIDGET"]
    muddled.instruments["shelved"] = muddled.instruments["crypto"].model_copy(
        update={"allowed_symbols": ["WIDGET"], "enabled": False}
    )

    verdict = _gate(muddled).evaluate(
        OrderProposal(
            symbol="WIDGET",
            direction=Direction.BUY,
            qty=3,
            limit_price=580.00,
            stop_loss_price=575.00,
            take_profit_price=590.00,
            rationale="Two instrument blocks both list this symbol.",
        ),
        account=account,
        tick=Tick(symbol="WIDGET", bid=579.98, ask=580.02, timestamp=INSIDE_SESSION),
        granted_symbols={"WIDGET": "us_equity"},
    )

    assert not verdict.approved
    assert _reasons_mention(verdict, "cannot be established")


# --------------------------- an open position keeps the class it was opened in


def test_a_position_still_open_counts_against_its_class_after_the_grant_ends(rules):
    """Handing the dream back must not move live risk out of a class cap.

    `_class_symbols` computed membership from the grants in force NOW, so a
    position opened under a grant left its class's counts the instant the grant
    did — and `return_to_vault` is one of the two things the trading agent is
    allowed to do, so the agent chose the moment. Measured: a class total-risk
    rejection flipped to an approval with the position still open and still at
    risk, and nothing about the account had changed.
    """
    enabled = _crypto_class_total(rules)
    held = Position(
        symbol="DOGE/USD",
        asset_class=AssetClass.CRYPTO,
        direction=Direction.BUY,
        qty=1000,
        entry_price=0.4,
        opened_at=INSIDE_SESSION,
        current_price=0.4,
    )
    account = _account_with_class_risk(
        positions=[held], open_risk_by_symbol={"DOGE/USD": 400.0}
    )
    proposal = _btc(0.2, "The lapsed grant's position is still $400 at risk.")

    # The grant is gone: handed back, or expired. The position is not.
    verdict = _gate(enabled).evaluate(
        proposal,
        account=account,
        tick=_btc_tick(INSIDE_SESSION),
        granted_symbols={},
    )

    assert not verdict.approved
    assert _reasons_mention(verdict, "open risk would reach")
    assert _reasons_mention(verdict, "800.00")


def test_a_lapsed_grants_position_still_fills_its_class_concurrency_slot(rules):
    tightened = rules.model_copy(deep=True)
    tightened.instruments["us_equity"].max_concurrent_positions = 1
    held = Position(
        symbol="TSLA",
        direction=Direction.BUY,
        qty=3,
        entry_price=420.0,
        opened_at=INSIDE_SESSION,
        current_price=420.0,
    )
    account = AccountSnapshot(
        equity_usd=PAPER_EQUITY,
        cash_usd=PAPER_EQUITY,
        buying_power_usd=PAPER_EQUITY,
        open_positions=[held],
        open_risk_by_symbol={"TSLA": 15.0},
        open_risk_usd=15.0,
    )

    verdict = _gate(tightened).evaluate(
        OrderProposal(
            symbol="SPY",
            direction=Direction.BUY,
            qty=3,
            limit_price=580.00,
            stop_loss_price=575.00,
            take_profit_price=590.00,
            rationale="The class's one slot is held by a position under a dead grant.",
        ),
        account=account,
        tick=Tick(symbol="SPY", bid=579.98, ask=580.02, timestamp=INSIDE_SESSION),
        granted_symbols={},
    )

    assert not verdict.approved
    assert _reasons_mention(verdict, "for this instrument class")


def test_a_lapsed_grants_position_still_counts_toward_the_class_capital_cap(rules):
    enabled = _with_crypto(rules, cap=15.0)
    enabled.instruments["crypto"].max_concurrent_positions = 5
    held = Position(
        symbol="DOGE/USD",
        asset_class=AssetClass.CRYPTO,
        direction=Direction.BUY,
        qty=30_000,
        entry_price=0.4,
        opened_at=INSIDE_SESSION,
        current_price=0.4,
    )
    account = _account_with_class_risk(
        positions=[held], open_risk_by_symbol={"DOGE/USD": 100.0}
    )

    verdict = _gate(enabled).evaluate(
        _btc(0.05, "12% of equity is already held in this class under a dead grant."),
        account=account,
        tick=_btc_tick(INSIDE_SESSION),
        granted_symbols={},
    )

    assert not verdict.approved
    assert _reasons_mention(verdict, "allocation would reach")


def test_a_lapsed_grants_untracked_position_still_fails_the_class_closed(rules):
    """The fail-closed branch has to survive the grant ending too, or handing a
    dream back is a way to make an unknowable position stop being counted."""
    enabled = _crypto_class_total(rules)
    held = Position(
        symbol="DOGE/USD",
        asset_class=AssetClass.CRYPTO,
        direction=Direction.BUY,
        qty=1000,
        entry_price=0.4,
        opened_at=INSIDE_SESSION,
        current_price=0.4,
    )
    account = _account_with_class_risk(
        positions=[held], symbols_with_unknown_risk=["DOGE/USD"]
    )

    verdict = _gate(enabled).evaluate(
        _btc(0.01, "An unknowable position does not become knowable by expiring."),
        account=account,
        tick=_btc_tick(INSIDE_SESSION),
        granted_symbols={},
    )

    assert not verdict.approved
    assert _reasons_mention(verdict, "cannot be established")


def test_a_held_symbol_counts_in_its_own_class_and_not_another(rules):
    """The membership is derived, so it has to put the position in ONE class.

    An equity held under a lapsed grant must not start counting against
    crypto's budget, which would be the mirror failure: a cap binding on
    something that has nothing to do with it.
    """
    enabled = _crypto_class_total(rules)
    held = Position(
        symbol="MP",
        direction=Direction.BUY,
        qty=200,
        entry_price=100.0,
        opened_at=INSIDE_SESSION,
        current_price=100.0,
    )
    account = _account_with_class_risk(
        positions=[held], open_risk_by_symbol={"MP": 400.0}
    )

    verdict = _gate(enabled).evaluate(
        _btc(0.2, "An equity position says nothing about the crypto class."),
        account=account,
        tick=_btc_tick(INSIDE_SESSION),
        granted_symbols={},
    )

    assert verdict.approved, verdict.reasons
