"""Tests for the metrics engine.

Every headline figure is asserted against a hand-computed answer, because a
wrong metric is worse than no metric: it gets believed and then acted on. The
edge cases each get their own test, since that is where naive metric code goes
quietly wrong rather than loudly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.metrics import (
    THIN_SAMPLE_THRESHOLD,
    ExcursionAnalysis,
    analyse_excursions,
    breakdown_by,
    build_report,
    closed_only,
    render_excursions,
    render_summary,
    summarise,
)
from bot.models import AssetClass, Direction, Trade

ENTRY = datetime(2026, 5, 4, 15, 0, tzinfo=UTC)


def _trade(
    pnl: float | None,
    *,
    symbol: str = "SPY",
    qty: float = 10,
    entry_price: float = 580.0,
    stop: float = 570.0,   # 10 x 10 = $100 planned risk
    fees: float = 0.0,
    mae: float = 0.0,
    mfe: float = 0.0,
    strategy: str = "mean_reversion",
    asset_class: AssetClass = AssetClass.EQUITY,
    minutes: int = 0,
    closed: bool = True,
) -> Trade:
    return Trade(
        symbol=symbol,
        asset_class=asset_class,
        strategy=strategy,
        direction=Direction.BUY,
        qty=qty,
        entry_time=ENTRY + timedelta(minutes=minutes),
        entry_price=entry_price,
        planned_stop=stop,
        planned_target=600.0,
        exit_time=(ENTRY + timedelta(minutes=minutes + 30)) if closed else None,
        exit_price=590.0 if closed else None,
        realised_pnl_usd=pnl,
        fees_usd=fees,
        mae_usd=mae,
        mfe_usd=mfe,
        rationale="Fixture trade.",
    )


@pytest.fixture
def known_set() -> list[Trade]:
    """Four trades, each risking $100, with deliberately simple arithmetic.

    P&L sequence: +200, -100, +300, -100
      gross profit 500, gross loss 200  -> profit factor 2.5
      win rate 0.5, avg win 250, avg loss 100
      expectancy  = 0.5*250 - 0.5*100   = +75
      R values [+2, -1, +3, -1]         -> total +3R, average +0.75R
      cumulative 200, 100, 400, 300     -> max drawdown 100
    """
    return [
        _trade(200.0, minutes=0),
        _trade(-100.0, minutes=60),
        _trade(300.0, minutes=120),
        _trade(-100.0, minutes=180),
    ]


# ------------------------------------------------------- hand-computed values


def test_headline_figures_match_by_hand(known_set):
    s = summarise(known_set)
    assert s.trade_count == 4
    assert s.wins == 2 and s.losses == 2
    assert s.win_rate == pytest.approx(0.5)
    assert s.gross_profit_usd == pytest.approx(500.0)
    assert s.gross_loss_usd == pytest.approx(200.0)
    assert s.profit_factor == pytest.approx(2.5)
    assert s.avg_win_usd == pytest.approx(250.0)
    assert s.avg_loss_usd == pytest.approx(100.0)
    assert s.expectancy_usd == pytest.approx(75.0)
    assert s.total_pnl_usd == pytest.approx(300.0)


def test_r_multiples_match_by_hand(known_set):
    s = summarise(known_set)
    assert s.total_r == pytest.approx(3.0)
    assert s.avg_r == pytest.approx(0.75)
    assert s.expectancy_r == pytest.approx(0.75)
    assert s.r_sample_count == 4


def test_max_drawdown_matches_by_hand(known_set):
    # Peak 400 after trade 3, trough 300 after trade 4.
    assert summarise(known_set).max_drawdown_usd == pytest.approx(100.0)


def test_best_and_worst(known_set):
    s = summarise(known_set)
    assert s.best_trade_usd == pytest.approx(300.0)
    assert s.worst_trade_usd == pytest.approx(-100.0)


def test_fees_reduce_pnl_and_r():
    s = summarise([_trade(200.0, fees=50.0)])
    assert s.total_pnl_usd == pytest.approx(150.0)
    assert s.avg_r == pytest.approx(1.5)  # 150 / 100 planned risk


# ------------------------------------------------------------------ edge cases


def test_no_losing_trades_gives_none_not_infinity():
    """Dividing by zero gross loss is the classic profit-factor bug."""
    s = summarise([_trade(200.0), _trade(300.0)])
    assert s.profit_factor is None
    assert "no losing trades" in s.health
    assert "n/a (no losses)" in "\n".join(render_summary(s))


def test_zero_trades_returns_a_zeroed_summary():
    s = summarise([])
    assert s.trade_count == 0
    assert s.profit_factor is None
    assert s.total_pnl_usd == 0.0
    assert s.health == "no closed trades yet"
    assert render_summary(s) == ["No closed trades yet."]


def test_trade_without_planned_risk_is_excluded_from_r_not_counted_as_zero():
    """Counting it as 0R would drag the average down and flatter a bad system."""
    zero_risk = _trade(200.0, entry_price=580.0, stop=580.0)  # risk = 0
    assert zero_risk.planned_risk_usd == 0.0
    assert zero_risk.r_multiple is None

    s = summarise([_trade(200.0), zero_risk])
    assert s.trade_count == 2          # still counts for P&L
    assert s.r_sample_count == 1       # but not for R
    assert s.avg_r == pytest.approx(2.0)
    assert "excluded from R statistics" in "\n".join(render_summary(s))


def test_open_trades_never_count():
    trades = [_trade(200.0), _trade(None, closed=False)]
    assert len(closed_only(trades)) == 1
    assert summarise(trades).trade_count == 1


def test_thin_sample_is_flagged(known_set):
    s = summarise(known_set)
    assert s.sample_is_thin
    assert "thin sample" in "\n".join(render_summary(s))


def test_large_sample_is_not_flagged():
    trades = [_trade(100.0, minutes=i * 10) for i in range(THIN_SAMPLE_THRESHOLD + 1)]
    assert not summarise(trades).sample_is_thin


@pytest.mark.parametrize(
    "pf, expected",
    [(2.5, "strong"), (1.6, "solid"), (1.1, "marginally profitable"), (0.7, "losing money")],
)
def test_health_reads_profit_factor(pf, expected):
    """Enough trades to clear the thin-sample hedge, with a controlled ratio."""
    wins = [_trade(pf * 100.0, minutes=i * 10) for i in range(THIN_SAMPLE_THRESHOLD)]
    losses = [_trade(-100.0, minutes=(THIN_SAMPLE_THRESHOLD + i) * 10) for i in range(THIN_SAMPLE_THRESHOLD)]
    s = summarise(wins + losses)
    assert s.health == expected


# ------------------------------------------------------------------ excursions


def test_capture_ratio_matches_by_hand():
    """Winner made 200 but ran to 500 unrealised, so 60% was left behind."""
    a = analyse_excursions([_trade(200.0, mfe=500.0, mae=-50.0)])
    assert a.capture_ratio == pytest.approx(0.4)
    assert "too close" in a.target_verdict


def test_capture_ratio_boundary_at_half_is_not_condemned():
    """Capturing exactly half is a judgement call, so it reads as neutral.

    A mean-reversion strategy targeting a specific level may capture half the
    excursion by design. Only below half does the verdict harden.
    """
    a = analyse_excursions([_trade(200.0, mfe=400.0, mae=-50.0)])
    assert a.capture_ratio == pytest.approx(0.5)
    assert "some room to run" in a.target_verdict


def test_mae_to_risk_ratio_matches_by_hand():
    # Average worst drawdown 90 against 100 of planned risk.
    a = analyse_excursions([_trade(200.0, mfe=300.0, mae=-90.0)])
    assert a.mae_to_risk_ratio == pytest.approx(0.9)
    assert "too tight" in a.stop_verdict


def test_comfortable_stops_read_as_comfortable():
    a = analyse_excursions([_trade(200.0, mfe=250.0, mae=-20.0)])
    assert "comfortable room" in a.stop_verdict


def test_well_placed_targets_read_as_such():
    a = analyse_excursions([_trade(200.0, mfe=220.0, mae=-20.0)])
    assert "well placed" in a.target_verdict


def test_unsampled_trades_are_excluded_from_excursions():
    """A trade opened and closed inside one cycle has no excursion data.

    Counting its zeros would invent a verdict from nothing.
    """
    a = analyse_excursions([_trade(200.0, mae=0.0, mfe=0.0)])
    assert a.sampled_trades == 0
    assert a.capture_ratio is None
    assert "No excursion data" in render_excursions(a)[0]


def test_excursion_render_always_states_the_sampling_caveat():
    lines = render_excursions(analyse_excursions([_trade(200.0, mfe=400.0, mae=-50.0)]))
    joined = "\n".join(lines)
    assert "sampled once per decision cycle" in joined
    assert "floor, not a measurement" in joined


def test_empty_excursion_analysis_is_safe():
    a = ExcursionAnalysis()
    assert a.capture_ratio is None
    assert "not enough" in a.target_verdict
    assert "not enough" in a.stop_verdict


# ------------------------------------------------------------------ breakdowns


def test_breakdown_by_strategy_separates_groups():
    trades = [
        _trade(200.0, strategy="mean_reversion", minutes=0),
        _trade(-100.0, strategy="momentum", minutes=60),
        _trade(300.0, strategy="momentum", minutes=120),
    ]
    groups = breakdown_by(trades, lambda t: t.strategy)
    assert set(groups) == {"mean_reversion", "momentum"}
    assert groups["mean_reversion"].trade_count == 1
    assert groups["momentum"].total_pnl_usd == pytest.approx(200.0)


def test_report_covers_every_breakdown(known_set):
    report = build_report(known_set)
    assert report.overall.trade_count == 4
    assert "mean_reversion" in report.by_strategy
    assert AssetClass.EQUITY.value in report.by_asset_class
    assert report.by_weekday  # 2026-05-04 is a Monday
    assert "15:00 UTC" in report.by_hour or "16:00 UTC" in report.by_hour


def test_report_on_empty_journal_is_safe():
    report = build_report([])
    assert report.overall.trade_count == 0
    assert report.excursions.sampled_trades == 0
    assert report.by_strategy == {}
