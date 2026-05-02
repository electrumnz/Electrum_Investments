"""Format current market state into a single text blob for Claude."""

from __future__ import annotations

from datetime import UTC, datetime

from .broker import Broker
from .models import AccountSnapshot, Tick
from .risk import NewsWindow


def build_market_context(
    *,
    account: AccountSnapshot,
    ticks: dict[str, Tick],
    headlines: list[str],
    news_windows: list[NewsWindow],
) -> str:
    """Render a stable, parseable text blob. Goes AFTER the cached system prompt."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    lines: list[str] = [f"Current UTC time: {now}", ""]

    lines.append("## Account")
    lines.append(f"- Equity: ${account.equity_usd:,.2f}")
    lines.append(f"- Balance: ${account.balance_usd:,.2f}")
    lines.append(f"- Margin used: ${account.margin_used_usd:,.2f}")
    lines.append(f"- Free margin: ${account.free_margin_usd:,.2f}")
    lines.append(f"- Realised P&L today: ${account.realised_pnl_today_usd:,.2f}")
    lines.append("")

    lines.append("## Open positions")
    if not account.open_positions:
        lines.append("- (none)")
    else:
        for p in account.open_positions:
            lines.append(
                f"- #{p.ticket} {p.direction.value} {p.size_lots} {p.symbol} "
                f"@ {p.open_price} (SL {p.stop_loss}, TP {p.take_profit}, "
                f"P&L ${p.current_pnl_usd:,.2f})"
            )
    lines.append("")

    lines.append("## Market snapshot")
    for symbol, tick in sorted(ticks.items()):
        lines.append(
            f"- {symbol}: bid {tick.bid} / ask {tick.ask} (spread {tick.spread:.5f})"
        )
    lines.append("")

    lines.append("## Recent headlines")
    if not headlines:
        lines.append("- (none)")
    else:
        for h in headlines:
            lines.append(f"- {h}")
    lines.append("")

    lines.append("## Upcoming news windows (≤ 60 min)")
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
