"""Format current market state into a single text blob for Claude."""

from __future__ import annotations

from datetime import UTC, datetime

from .broker import Broker
from .indicators import Indicators, compute
from .indicators import render as render_indicators
from .models import AccountSnapshot, Tick, TradingActivity
from .options import ExpiryAlert, render_alerts
from .risk import NewsWindow


def build_market_context(
    *,
    account: AccountSnapshot,
    ticks: dict[str, Tick],
    headlines: list[str],
    news_windows: list[NewsWindow],
    activity: TradingActivity | None = None,
    expiry_alerts: list[ExpiryAlert] | None = None,
    indicators: dict[str, Indicators] | None = None,
    symbols_without_history: list[str] | None = None,
) -> str:
    """Render a stable, parseable text blob. Goes AFTER the cached system prompt."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    lines: list[str] = [f"Current UTC time: {now}", ""]

    # Deliberately first. Everything else here is an opportunity; this is the
    # only section where doing nothing has an automatic, irreversible outcome.
    urgent = [a for a in (expiry_alerts or []) if a.needs_action]
    if urgent:
        lines.append("## ⚠ OPTION EXPIRY — ACTION REQUIRED")
        lines.extend(render_alerts(urgent))
        lines.append("")
        lines.append(
            "Alpaca auto-exercises anything $0.01 in the money and liquidates "
            "un-fundable positions in the final hour. Do Not Exercise cannot be "
            "filed through the API, so closing the position is the only way to "
            "choose a different outcome."
        )
        lines.append("")

    lines.append("## Account")
    lines.append(f"- Equity: ${account.equity_usd:,.2f}")
    lines.append(f"- Cash: ${account.cash_usd:,.2f} ({account.cash_pct:.1f}% of equity)")
    lines.append(f"- Buying power: ${account.buying_power_usd:,.2f}")
    lines.append(f"- Gross exposure: ${account.gross_exposure_usd:,.2f}")
    lines.append(f"- Realised P&L today: ${account.realised_pnl_today_usd:,.2f}")
    lines.append(
        f"- Open risk (loss if every stop filled): ${account.open_risk_usd:,.2f}"
    )
    lines.append("")

    if activity is not None:
        lines.append("## Recent trading activity")
        lines.append(f"- Trades today: {activity.trades_today}")
        lines.append(f"- Trades this week: {activity.trades_this_week}")
        lines.append("")

    lines.append("## Open positions")
    if not account.open_positions:
        lines.append("- (none)")
    else:
        for p in account.open_positions:
            current = f"{p.current_price:,.4f}" if p.current_price else "n/a"
            lines.append(
                f"- {p.direction.value} {p.qty:g} {p.symbol} @ {p.entry_price:,.4f} "
                f"(now {current}, P&L ${p.unrealised_pnl_usd:,.2f})"
            )
    lines.append("")

    lines.append("## Market snapshot")
    if not ticks:
        lines.append("- (none)")
    else:
        for symbol, tick in sorted(ticks.items()):
            lines.append(
                f"- {symbol}: bid {tick.bid:,.4f} / ask {tick.ask:,.4f} "
                f"(spread {tick.spread:,.4f})"
            )
    lines.append("")

    # Computed in Python from daily bars, in src/bot/indicators.py, and handed
    # over as answers rather than as bars to do arithmetic on. A model asked to
    # work out a 200-day average from a list will produce a number, and nobody
    # downstream can tell a correct one from an invented one.
    lines.append("## Indicators (computed from daily bars, not estimated)")
    if not indicators:
        lines.append("- (none)")
    else:
        for symbol in sorted(indicators):
            lines.extend(render_indicators(indicators[symbol]))
    if symbols_without_history:
        lines.append(
            "- NO PRICE HISTORY AVAILABLE for: "
            + ", ".join(sorted(symbols_without_history))
            + ". Treat these as having no indicators at all. Do not estimate a "
            "moving average, an ATR or a level for them from the single quote "
            "above, and propose nothing on them."
        )
    lines.append("")

    lines.append("## Recent headlines")
    if not headlines:
        lines.append("- (none)")
    else:
        for h in headlines:
            lines.append(f"- {h}")
    lines.append("")

    lines.append("## Upcoming news windows (<= 60 min)")
    if not news_windows:
        lines.append("- (none)")
    else:
        for w in news_windows:
            lines.append(
                f"- {w.timestamp.isoformat(timespec='minutes')} "
                f"affects: {', '.join(sorted(w.affected_symbols))}"
            )

    return "\n".join(lines)


def fetch_market_ticks(broker: Broker, symbols: list[str]) -> dict[str, Tick]:
    """Pull a tick for every allowed symbol, skipping any that error out."""
    result: dict[str, Tick] = {}
    for symbol in symbols:
        try:
            result[symbol] = broker.get_tick(symbol)
        except (KeyError, RuntimeError):
            continue
    return result


def fetch_indicators(
    broker: Broker, symbols: list[str]
) -> tuple[dict[str, Indicators], list[str]]:
    """Compute indicators for every allowed symbol, and name the ones that failed.

    Returns the indicators and, separately, the symbols that produced nothing.
    The second value is the point: a symbol dropped silently from the first dict
    is indistinguishable from a symbol nobody asked about, and the model would
    reason about its live quote with no history and no warning. Same principle
    as `reconcile`'s `risk_is_understated` and `FinnhubCalendar.is_degraded`.
    """
    found: dict[str, Indicators] = {}
    missing: list[str] = []

    for symbol in symbols:
        try:
            bars = broker.get_daily_bars(symbol)
        except (KeyError, RuntimeError, ValueError):
            missing.append(symbol)
            continue

        result = compute(symbol, bars)
        if result is None:
            missing.append(symbol)
        else:
            found[symbol] = result

    return found, missing
