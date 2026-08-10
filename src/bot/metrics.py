"""Performance metrics computed from the trade journal.

Pure functions over `list[Trade]`, deliberately independent of SQLite, so every
figure can be asserted against a hand-computed answer with no database in the
way. That matters more here than elsewhere: a wrong metric is worse than no
metric, because it gets believed and then acted on.

The one measurement worth building this for is the excursion analysis. Win rate
and profit factor tell you whether the trades made money. **MAE and MFE tell you
whether the model's own stop and target placement was sane**, which is the thing
an LLM proposer is most likely to get wrong and the thing nothing else here can
see.

Honest about its own limits in two places, both surfaced in the rendered output
rather than buried in a docstring: excursions are sampled at the decision
interval rather than from ticks, and small samples are labelled as noise.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .models import Trade

# Below this many closed trades, expectancy and profit factor are noise. A
# profit factor of 3.0 over four trades will be believed unless it is labelled.
THIN_SAMPLE_THRESHOLD = 20

WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _count(n: int, singular: str, plural: str | None = None) -> str:
    """A count and its noun agreeing: `1 trade`, `3 trades`.

    Deliberately a local four-liner rather than an import. These strings are
    read by the Analytics page, but this module is pure functions over
    `list[Trade]` and must not learn about a rendering layer to get a plural
    right; `render.py` carries its own copy for the same reason `money` in the
    browser matches `_money` in Python. The rule is that both agree, not that
    one calls the other.
    """
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


@dataclass(frozen=True)
class PerformanceSummary:
    """Headline performance over a set of closed trades."""

    trade_count: int = 0
    wins: int = 0
    losses: int = 0

    win_rate: float = 0.0
    avg_win_usd: float = 0.0
    avg_loss_usd: float = 0.0
    gross_profit_usd: float = 0.0
    gross_loss_usd: float = 0.0

    # None when there are no losing trades. Deliberately not inf and not 0.0,
    # because both read as a real number and one of them reads as terrible.
    profit_factor: float | None = None

    expectancy_usd: float = 0.0
    expectancy_r: float | None = None

    total_pnl_usd: float = 0.0
    total_r: float | None = None
    avg_r: float | None = None
    r_sample_count: int = 0

    best_trade_usd: float = 0.0
    worst_trade_usd: float = 0.0
    max_drawdown_usd: float = 0.0

    @property
    def sample_is_thin(self) -> bool:
        return self.trade_count < THIN_SAMPLE_THRESHOLD

    @property
    def health(self) -> str:
        """Plain reading of profit factor, hedged when the sample is thin."""
        if self.trade_count == 0:
            return "no closed trades yet"
        if self.profit_factor is None:
            return f"no losing trades yet across {_count(self.trade_count, 'trade')}"
        if self.profit_factor >= 2.0:
            verdict = "strong"
        elif self.profit_factor >= 1.5:
            verdict = "solid"
        elif self.profit_factor > 1.0:
            verdict = "marginally profitable"
        else:
            verdict = "losing money"
        if self.sample_is_thin:
            return (
                f"{verdict}, but only {_count(self.trade_count, 'trade')} "
                "so treat as noise"
            )
        return verdict


@dataclass(frozen=True)
class ExcursionAnalysis:
    """How well stops and targets were placed, judged after the fact.

    Both ratios are computed from excursions sampled at the decision interval,
    so they understate the true high-water and low-water marks. Treat the
    capture ratio as a floor.
    """

    sampled_trades: int = 0

    avg_mfe_usd: float = 0.0
    avg_realised_win_usd: float = 0.0
    capture_ratio: float | None = None

    avg_mae_usd: float = 0.0
    avg_planned_risk_usd: float = 0.0
    mae_to_risk_ratio: float | None = None

    @property
    def target_verdict(self) -> str:
        if self.capture_ratio is None:
            return "not enough winning trades to judge target placement"
        if self.capture_ratio < 0.5:
            return (
                f"targets look too close: capturing {self.capture_ratio:.0%} of the "
                "favourable move on winners"
            )
        if self.capture_ratio < 0.75:
            return f"some room to run further: capturing {self.capture_ratio:.0%}"
        return f"targets look well placed: capturing {self.capture_ratio:.0%}"

    @property
    def stop_verdict(self) -> str:
        if self.mae_to_risk_ratio is None:
            return "not enough trades to judge stop placement"
        if self.mae_to_risk_ratio >= 0.9:
            return (
                f"stops look too tight: trades routinely draw down to "
                f"{self.mae_to_risk_ratio:.0%} of the planned risk, so noise is "
                "hitting them"
            )
        if self.mae_to_risk_ratio >= 0.6:
            return f"stops are being tested: average drawdown reaches {self.mae_to_risk_ratio:.0%}"
        return f"stops have comfortable room: average drawdown {self.mae_to_risk_ratio:.0%}"


@dataclass(frozen=True)
class JournalReport:
    """Everything the dashboard and the MCP tool need, in one object."""

    overall: PerformanceSummary
    excursions: ExcursionAnalysis
    by_strategy: dict[str, PerformanceSummary] = field(default_factory=dict)
    by_asset_class: dict[str, PerformanceSummary] = field(default_factory=dict)
    by_weekday: dict[str, PerformanceSummary] = field(default_factory=dict)
    by_hour: dict[str, PerformanceSummary] = field(default_factory=dict)
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# --------------------------------------------------------------------- helpers


def closed_only(trades: list[Trade]) -> list[Trade]:
    """Open trades have no result, so they never count toward performance."""
    return [t for t in trades if not t.is_open and t.net_pnl_usd is not None]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _max_drawdown(pnls: list[float]) -> float:
    """Largest peak-to-trough fall in cumulative P&L. Returned positive."""
    peak = 0.0
    equity = 0.0
    worst = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


# ------------------------------------------------------------------- summarise


def summarise(trades: list[Trade]) -> PerformanceSummary:
    """Headline metrics. Safe on an empty list."""
    closed = closed_only(trades)
    if not closed:
        return PerformanceSummary()

    pnls = [t.net_pnl_usd or 0.0 for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    # Only trades with a real planned risk have a meaningful R. Counting a
    # zero-risk trade as 0R would drag the average toward zero and flatter a
    # bad system, so they are excluded rather than defaulted.
    r_values = [t.r_multiple for t in closed if t.r_multiple is not None]

    win_rate = len(wins) / len(closed)
    avg_win = _mean(wins)
    avg_loss = abs(_mean(losses))

    return PerformanceSummary(
        trade_count=len(closed),
        wins=len(wins),
        losses=len(losses),
        win_rate=win_rate,
        avg_win_usd=avg_win,
        avg_loss_usd=avg_loss,
        gross_profit_usd=gross_profit,
        gross_loss_usd=gross_loss,
        # Guarded: no losses means the ratio is undefined, not infinite.
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else None,
        expectancy_usd=(win_rate * avg_win) - ((1 - win_rate) * avg_loss),
        expectancy_r=_mean(r_values) if r_values else None,
        total_pnl_usd=sum(pnls),
        total_r=sum(r_values) if r_values else None,
        avg_r=_mean(r_values) if r_values else None,
        r_sample_count=len(r_values),
        best_trade_usd=max(pnls),
        worst_trade_usd=min(pnls),
        max_drawdown_usd=_max_drawdown(pnls),
    )


def analyse_excursions(trades: list[Trade]) -> ExcursionAnalysis:
    """Judge stop and target placement from MAE and MFE.

    Only trades that actually recorded an excursion are included: a trade opened
    and closed inside a single decision interval has never been sampled, and
    counting its zeros would pull both ratios toward zero and invent a verdict.
    """
    closed = closed_only(trades)
    sampled = [t for t in closed if t.mae_usd != 0.0 or t.mfe_usd != 0.0]
    if not sampled:
        return ExcursionAnalysis()

    winners = [t for t in sampled if (t.net_pnl_usd or 0.0) > 0]
    avg_mfe = _mean([t.mfe_usd for t in winners])
    avg_realised_win = _mean([t.net_pnl_usd or 0.0 for t in winners])

    avg_mae = _mean([t.mae_usd for t in sampled])
    risks = [t.planned_risk_usd for t in sampled if t.planned_risk_usd > 0]
    avg_risk = _mean(risks)

    return ExcursionAnalysis(
        sampled_trades=len(sampled),
        avg_mfe_usd=avg_mfe,
        avg_realised_win_usd=avg_realised_win,
        capture_ratio=(avg_realised_win / avg_mfe) if avg_mfe > 0 else None,
        avg_mae_usd=avg_mae,
        avg_planned_risk_usd=avg_risk,
        mae_to_risk_ratio=(abs(avg_mae) / avg_risk) if avg_risk > 0 else None,
    )


def breakdown_by(
    trades: list[Trade], key: Callable[[Trade], str]
) -> dict[str, PerformanceSummary]:
    """Group closed trades and summarise each group."""
    groups: dict[str, list[Trade]] = {}
    for trade in closed_only(trades):
        groups.setdefault(key(trade), []).append(trade)
    return {name: summarise(group) for name, group in sorted(groups.items())}


def build_report(trades: list[Trade]) -> JournalReport:
    """Full report across every breakdown the dashboard shows."""
    return JournalReport(
        overall=summarise(trades),
        excursions=analyse_excursions(trades),
        by_strategy=breakdown_by(trades, lambda t: t.strategy),
        by_asset_class=breakdown_by(trades, lambda t: t.asset_class.value),
        by_weekday=breakdown_by(
            trades, lambda t: WEEKDAY_NAMES[(t.exit_time or t.entry_time).weekday()]
        ),
        by_hour=breakdown_by(
            trades, lambda t: f"{(t.exit_time or t.entry_time).hour:02d}:00 UTC"
        ),
    )


# --------------------------------------------------------------------- render


def render_summary(summary: PerformanceSummary) -> list[str]:
    """Human-readable lines for logs, the MCP tool and the dashboard."""
    if summary.trade_count == 0:
        return ["No closed trades yet."]

    pf = f"{summary.profit_factor:.2f}" if summary.profit_factor is not None else "n/a (no losses)"
    expectancy_r = (
        f"{summary.expectancy_r:+.2f}R" if summary.expectancy_r is not None else "n/a"
    )

    lines = [
        f"Trades: {summary.trade_count} ({summary.wins}W / {summary.losses}L), "
        f"win rate {summary.win_rate:.0%}",
        f"Profit factor: {pf}  ({summary.health})",
        f"Expectancy: ${summary.expectancy_usd:+,.2f} per trade, {expectancy_r}",
        f"Total P&L: ${summary.total_pnl_usd:+,.2f}   "
        f"Max drawdown: ${summary.max_drawdown_usd:,.2f}",
        f"Best ${summary.best_trade_usd:+,.2f}   Worst ${summary.worst_trade_usd:+,.2f}",
    ]
    if summary.r_sample_count < summary.trade_count:
        skipped = summary.trade_count - summary.r_sample_count
        lines.append(
            f"Note: {skipped} trade(s) had no planned risk recorded and are "
            "excluded from R statistics."
        )
    if summary.sample_is_thin:
        lines.append(
            f"Note: {summary.trade_count} trades is a thin sample. Expectancy and "
            f"profit factor are not meaningful below about {THIN_SAMPLE_THRESHOLD}."
        )
    return lines


def render_excursions(analysis: ExcursionAnalysis) -> list[str]:
    if analysis.sampled_trades == 0:
        return ["No excursion data yet (trades need to span at least one decision cycle)."]
    return [
        f"Stops and targets, from {analysis.sampled_trades} sampled trade(s):",
        f"  {analysis.target_verdict}",
        f"    average best unrealised on winners ${analysis.avg_mfe_usd:,.2f} "
        f"vs realised ${analysis.avg_realised_win_usd:,.2f}",
        f"  {analysis.stop_verdict}",
        f"    average worst unrealised ${analysis.avg_mae_usd:,.2f} "
        f"vs planned risk ${analysis.avg_planned_risk_usd:,.2f}",
        "  Caveat: excursions are sampled once per decision cycle, not from "
        "ticks, so both figures understate the true swing. The capture ratio is "
        "a floor, not a measurement.",
    ]
