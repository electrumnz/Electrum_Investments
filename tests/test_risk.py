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


def test_rejects_the_pre_market_the_utc_window_would_have_let_through(
    rules, account, spy_tick, buy_proposal
):
    """The operator's rule: no pre-market. After hours is fine.

    This is the case `sessions_utc` structurally cannot catch. It is fixed UTC
    hours; the US session is defined in New York time and moves an hour twice a
    year. `[[14, 21]]` is the winter window applied all year, so in JANUARY
    14:00 UTC is 09:00 New York — half an hour of pre-market, inside the
    configured window, every day, with nothing in the window able to notice.

    It used to cost nothing: an out-of-hours equity order was queued to the
    next open rather than filled. It costs something now, because Alpaca runs a
    pre-market session from 04:00 ET, so an order placed into it trades in a
    thinner book than anybody chose.
    """
    winter_premarket = datetime(2026, 1, 12, 14, 15, tzinfo=UTC)  # 09:15 EST
    gate = _gate(rules, now=winter_premarket)

    # The UTC window is satisfied — this is the trap.
    assert rules.instruments["us_equity"].is_in_session(winter_premarket)

    verdict = gate.evaluate(buy_proposal, account=account, tick=spy_tick)

    assert not verdict.approved
    assert _reasons_mention(verdict, "pre-market")
    assert not _reasons_mention(verdict, "outside the trading sessions")


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


# ------------------------------------------------------------------- config


def test_frequency_rules_reject_weekly_below_daily():
    from bot.config import FrequencyRules

    with pytest.raises(ValueError, match="max_trades_per_week"):
        FrequencyRules(
            max_trades_per_day=10,
            max_trades_per_week=5,
            min_seconds_between_trades_per_symbol=0,
        )
