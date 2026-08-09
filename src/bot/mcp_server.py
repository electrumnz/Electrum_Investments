"""MCP server exposing the risk gate to Claude Code, Buzz, and any MCP client.

This is what makes the safety layer *reachable* without making it *bypassable*.
Alpaca's own MCP server can place orders directly, so this server is not a
gatekeeper in the network sense — it is the tool that tells you, in plain terms,
whether an order is allowed, and that refuses to place one that is not.

Point Claude at both servers and instruct it (via CLAUDE.md) to run every trade
idea through `check_order` first. To make the rule structural rather than
advisory, restrict Alpaca's server to read-only toolsets with `ALPACA_TOOLSETS`
and let `place_order` here be the only write path.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from .audit import AuditLog
from .broker import Broker
from .config import Env, Rules, load_rules
from .journal import Journal
from .metrics import build_report, render_excursions, render_summary
from .models import AccountSnapshot, AssetClass, Direction, OrderProposal
from .options import alerts_for_positions, parse_occ_symbol, render_alerts
from .risk import RiskGate
from .stand_down import describe

server = MCPServer(
    name="electrum-bot",
    instructions=(
        "Risk gate and paper-trading controls for the Electrum trading bot. "
        "Always call check_order before proposing a trade to the user, and use "
        "place_order rather than any other order tool — it is the only path that "
        "enforces config/rules.yaml."
    ),
)


class _Session:
    """Lazily-built broker, rules and risk gate shared across tool calls."""

    def __init__(self) -> None:
        self._env: Env | None = None
        self._rules: Rules | None = None
        self._broker: Broker | None = None
        self._gate: RiskGate | None = None
        self._journal: Journal | None = None
        self._audit = AuditLog()

    @property
    def env(self) -> Env:
        if self._env is None:
            self._env = Env()
            self._env.assert_paper_only()
        return self._env

    @property
    def rules(self) -> Rules:
        if self._rules is None:
            self._rules = load_rules()
        return self._rules

    @property
    def broker(self) -> Broker:
        if self._broker is None:
            # Imported here so the module stays importable without alpaca-py.
            from .main import build_broker

            self._broker = build_broker(self.env)
            self._broker.connect()
        return self._broker

    @property
    def journal(self) -> Journal:
        if self._journal is None:
            self._journal = Journal()
        return self._journal

    @property
    def gate(self) -> RiskGate:
        if self._gate is None:
            equity = self.broker.get_account().equity_usd
            self._gate = RiskGate(
                self.rules,
                equity_at_session_start=equity,
                execution_mode=self.env.execution_mode,
            )
        return self._gate

    @property
    def audit(self) -> AuditLog:
        return self._audit

    def account(self) -> AccountSnapshot:
        """Broker state with open risk filled in from the journal.

        The broker cannot supply open risk — Alpaca keeps stop-losses as
        separate orders — so every read goes through here rather than calling
        `broker.get_account()` directly, or the total-risk cap would have
        nothing to count.
        """
        snapshot = self.broker.get_account()
        snapshot.open_risk_usd = self.journal.open_risk_usd()
        return snapshot

    def reset(self) -> None:
        """Start a new trading session: re-baseline equity and clear the kill switch."""
        equity = self.broker.get_account().equity_usd
        self.gate.reset_daily(equity_at_session_start=equity)


_session = _Session()


def _build_proposal(
    symbol: str,
    direction: str,
    qty: float,
    limit_price: float,
    stop_loss_price: float,
    take_profit_price: float,
    rationale: str,
) -> OrderProposal:
    from .broker import is_crypto_symbol

    return OrderProposal(
        symbol=symbol.upper(),
        asset_class=AssetClass.CRYPTO if is_crypto_symbol(symbol) else AssetClass.EQUITY,
        direction=Direction(direction.lower()),
        qty=qty,
        limit_price=limit_price,
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
        rationale=rationale,
    )


@server.tool()
def check_order(
    symbol: str,
    direction: str,
    qty: float,
    limit_price: float,
    stop_loss_price: float,
    take_profit_price: float,
    rationale: str,
) -> dict[str, Any]:
    """Vet a proposed order against config/rules.yaml WITHOUT placing it.

    Returns approved=true only if every rule passes. When approved=false, every
    failing rule is listed — the proposal needs to satisfy all of them, and
    arguing with the result will not change it.

    Args:
        symbol: Ticker, e.g. SPY or AAPL. Crypto uses a slash, e.g. BTC/USD.
        direction: "buy" or "sell".
        qty: Number of shares or coin units. Fractional is allowed.
        limit_price: Limit price. Market orders are not supported.
        stop_loss_price: Must be below entry for a buy, above for a sell.
        take_profit_price: Must be above entry for a buy, below for a sell.
        rationale: One sentence: the signal, and the level that invalidates it.
    """
    try:
        proposal = _build_proposal(
            symbol, direction, qty, limit_price, stop_loss_price, take_profit_price, rationale
        )
    except Exception as e:  # malformed input is a rejection, not a crash
        return {"approved": False, "reasons": [f"invalid proposal: {e}"]}

    account = _session.account()
    activity = _session.broker.get_activity()
    try:
        tick = _session.broker.get_tick(proposal.symbol)
    except (KeyError, RuntimeError) as e:
        return {"approved": False, "reasons": [f"no market price for {proposal.symbol}: {e}"]}

    verdict = _session.gate.evaluate(
        proposal,
        account=account,
        tick=tick,
        activity=activity,
        stand_down=_session.journal.get_stand_down(),
    )
    return {
        "approved": verdict.approved,
        "reasons": verdict.reasons,
        "proposal": proposal.model_dump(mode="json"),
        "risk_usd": round(proposal.risk_usd, 2),
        "risk_pct_of_equity": round(
            proposal.risk_usd / account.equity_usd * 100 if account.equity_usd else 0.0, 3
        ),
        "notional_usd": round(proposal.notional_usd, 2),
        "notional_pct_of_equity": round(
            proposal.notional_usd / account.equity_usd * 100 if account.equity_usd else 0.0, 2
        ),
    }


@server.tool()
def place_order(
    symbol: str,
    direction: str,
    qty: float,
    limit_price: float,
    stop_loss_price: float,
    take_profit_price: float,
    rationale: str,
) -> dict[str, Any]:
    """Vet an order and, only if it passes every rule, place it on the PAPER account.

    This is the only order path that enforces config/rules.yaml. A rejected
    proposal is not placed under any circumstances.

    Args mirror check_order.
    """
    checked = check_order(
        symbol, direction, qty, limit_price, stop_loss_price, take_profit_price, rationale
    )
    if not checked["approved"]:
        return {"placed": False, "reasons": checked["reasons"]}

    proposal = _build_proposal(
        symbol, direction, qty, limit_price, stop_loss_price, take_profit_price, rationale
    )
    result = _session.broker.place_order(proposal)

    _session.audit.record_event(
        "mcp_place_order",
        {
            "proposal": proposal.model_dump(mode="json"),
            "accepted": result.accepted,
            "order_id": result.order_id,
            "error": result.error,
        },
    )
    return {
        "placed": result.accepted,
        "order_id": result.order_id,
        "filled_price": result.filled_price,
        "filled_qty": result.filled_qty,
        "error": result.error,
    }


@server.tool()
def close_position(symbol: str) -> dict[str, Any]:
    """Close the entire open position in a symbol on the paper account."""
    result = _session.broker.close_position(symbol.upper())
    _session.audit.record_event(
        "mcp_close_position",
        {"symbol": symbol.upper(), "accepted": result.accepted, "error": result.error},
    )
    return {
        "closed": result.accepted,
        "order_id": result.order_id,
        "error": result.error,
    }


def _untracked_position_warning(account: AccountSnapshot) -> dict[str, Any]:
    """Flag held positions the journal has no record of.

    Their planned stop is unknowable, so their risk cannot be counted and the
    reported open risk is lower than what is actually at risk. A cap running
    blind should say so rather than return a confident wrong number.
    """
    journalled = {t.symbol for t in _session.journal.open_trades()}
    untracked = sorted(p.symbol for p in account.open_positions if p.symbol not in journalled)
    if not untracked:
        return {"open_risk_is_complete": True}
    return {
        "open_risk_is_complete": False,
        "untracked_positions": untracked,
        "open_risk_warning": (
            f"{len(untracked)} held position(s) have no journal entry, so their "
            f"planned stop is unknown and their risk is NOT included above. "
            f"Actual open risk is higher than reported."
        ),
    }


@server.tool()
def get_risk_status() -> dict[str, Any]:
    """Show current account state against every limit in config/rules.yaml.

    Use this to explain *why* something would be blocked before proposing it.
    """
    account = _session.account()
    activity = _session.broker.get_activity()
    rules = _session.rules
    acct = rules.account

    return {
        "kill_switch_tripped": _session.gate.kill_switch_tripped,
        "equity_usd": round(account.equity_usd, 2),
        "cash_usd": round(account.cash_usd, 2),
        "cash_pct": round(account.cash_pct, 1),
        "gross_exposure_usd": round(account.gross_exposure_usd, 2),
        "open_positions": len(account.open_positions),
        "total_invested_usd": round(account.total_invested_usd, 2),
        "total_invested_pct": round(
            account.total_invested_usd / account.equity_usd * 100
            if account.equity_usd
            else 0.0,
            2,
        ),
        "open_risk_usd": round(account.open_risk_usd, 2),
        "open_risk_pct": round(
            account.open_risk_usd / account.equity_usd * 100 if account.equity_usd else 0.0,
            2,
        ),
        **_untracked_position_warning(account),
        "limits": {
            "min_equity_floor_usd": acct.min_equity_floor_usd,
            "max_risk_per_trade_pct": acct.max_risk_per_trade_pct,
            "max_total_risk_pct": acct.max_total_risk_pct,
            "max_position_pct": acct.max_position_pct,
            "max_concurrent_positions": acct.max_concurrent_positions,
            "daily_loss_kill_pct": acct.daily_loss_kill_pct,
        },
        "frequency": {
            "trades_today": activity.trades_today,
            "max_trades_per_day": rules.frequency.max_trades_per_day,
            "trades_this_week": activity.trades_this_week,
            "max_trades_per_week": rules.frequency.max_trades_per_week,
            "cooldown_seconds_per_symbol": (
                rules.frequency.min_seconds_between_trades_per_symbol
            ),
        },
        "margin": {
            "buying_power_usd": round(account.buying_power_usd, 2),
            "max_buying_power_utilisation_pct": (
                rules.margin.max_buying_power_utilisation_pct
            ),
            "gross_notional_pct": round(
                account.gross_exposure_usd / account.equity_usd * 100
                if account.equity_usd
                else 0.0,
                1,
            ),
            "max_gross_notional_pct": rules.margin.max_gross_notional_pct,
            "note": (
                "The Pattern Day Trader rule was retired by FINRA on 2026-06-04; "
                "the $25,000 threshold no longer applies. These are Intraday "
                "Margin Deficit guards instead."
            ),
        },
        "stand_down": describe(_session.journal.get_stand_down()),
    }


@server.tool()
def get_option_expiries() -> dict[str, Any]:
    """Report every open option position and how close it is to expiring.

    Worth checking every session. Alpaca auto-exercises anything $0.01 in the
    money at 6pm ET on expiry day, auto-assigns short positions the same way,
    and liquidates in-the-money positions the account cannot fund inside the
    final hour. "Do Not Exercise" cannot be filed through the API, so closing
    the position early is the only way to choose a different outcome.
    """
    account = _session.account()
    now = datetime.now(UTC)

    # Underlying marks let the alert say whether exercise is actually likely.
    # Missing quotes degrade the detail, never the timing of the warning.
    underlying_prices: dict[str, float] = {}
    for position in account.open_positions:
        contract = parse_occ_symbol(position.symbol)
        if contract is None:
            continue
        try:
            underlying_prices[contract.underlying] = _session.broker.get_tick(
                contract.underlying
            ).mid
        except (KeyError, RuntimeError):
            continue

    alerts = alerts_for_positions(
        [(p.symbol, p.qty) for p in account.open_positions],
        now=now,
        warn_days=_session.rules.options.warn_days_before_expiry,
        buying_power_usd=account.buying_power_usd,
        underlying_prices=underlying_prices,
    )
    needing_action = [a for a in alerts if a.needs_action]

    return {
        "option_positions": len(alerts),
        "needing_action": len(needing_action),
        "alerts": [a.model_dump(mode="json") for a in alerts],
        "summary": render_alerts(alerts),
    }


@server.tool()
def get_positions() -> list[dict[str, Any]]:
    """List every open position on the paper account."""
    account = _session.account()
    return [p.model_dump(mode="json") for p in account.open_positions]


@server.tool()
def get_rules() -> dict[str, Any]:
    """Return the active trading rules.

    These are enforced in code. Changing behaviour means editing
    config/rules.yaml and restarting — it cannot be done by asking.
    """
    return _session.rules.model_dump(mode="json")


@server.tool()
def get_journal_stats(days: int = 0, strategy: str = "") -> dict[str, Any]:
    """Performance metrics from the trade journal.

    Reports win rate, profit factor, expectancy, R-multiples and drawdown, plus
    an analysis of how well stops and targets were actually placed.

    That last part is the one worth reading. Win rate tells you whether the
    trades made money; MAE and MFE tell you whether the stop and target
    placement was sane, which is the thing most likely to be wrong and the only
    thing here that can show it.

    Args:
        days: Only include trades closed in the last N days. 0 means all time.
        strategy: Only include trades tagged with this strategy. Empty means all.
    """
    since = datetime.now(UTC) - timedelta(days=days) if days > 0 else None
    trades = _session.journal.closed_trades(
        since=since, strategy=strategy or None
    )
    report = build_report(trades)

    return {
        "filters": {"days": days or "all time", "strategy": strategy or "all"},
        "summary": {
            "trade_count": report.overall.trade_count,
            "win_rate": round(report.overall.win_rate, 4),
            "profit_factor": (
                round(report.overall.profit_factor, 3)
                if report.overall.profit_factor is not None
                else None
            ),
            "expectancy_usd": round(report.overall.expectancy_usd, 2),
            "expectancy_r": (
                round(report.overall.expectancy_r, 3)
                if report.overall.expectancy_r is not None
                else None
            ),
            "total_pnl_usd": round(report.overall.total_pnl_usd, 2),
            "max_drawdown_usd": round(report.overall.max_drawdown_usd, 2),
            "health": report.overall.health,
            "sample_is_thin": report.overall.sample_is_thin,
        },
        "stops_and_targets": {
            "sampled_trades": report.excursions.sampled_trades,
            "capture_ratio": (
                round(report.excursions.capture_ratio, 3)
                if report.excursions.capture_ratio is not None
                else None
            ),
            "mae_to_risk_ratio": (
                round(report.excursions.mae_to_risk_ratio, 3)
                if report.excursions.mae_to_risk_ratio is not None
                else None
            ),
            "target_verdict": report.excursions.target_verdict,
            "stop_verdict": report.excursions.stop_verdict,
        },
        "by_strategy": {
            name: {
                "trades": s.trade_count,
                "win_rate": round(s.win_rate, 3),
                "total_pnl_usd": round(s.total_pnl_usd, 2),
            }
            for name, s in report.by_strategy.items()
        },
        # Rendered lines carry the caveats (thin samples, excursion sampling)
        # that the raw numbers above do not.
        "readout": render_summary(report.overall) + render_excursions(report.excursions),
    }


@server.tool()
def get_trades(limit: int = 25, strategy: str = "") -> list[dict[str, Any]]:
    """Recent closed trades, newest last, with Claude's rationale on each.

    Args:
        limit: How many to return (default 25).
        strategy: Filter to one strategy. Empty means all.
    """
    trades = _session.journal.closed_trades(strategy=strategy or None)
    return [
        {
            "symbol": t.symbol,
            "strategy": t.strategy,
            "direction": t.direction.value,
            "qty": t.qty,
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "net_pnl_usd": round(t.net_pnl_usd, 2) if t.net_pnl_usd is not None else None,
            "r_multiple": round(t.r_multiple, 2) if t.r_multiple is not None else None,
            "mae_usd": round(t.mae_usd, 2),
            "mfe_usd": round(t.mfe_usd, 2),
            "entry_time": t.entry_time.isoformat(),
            "exit_time": t.exit_time.isoformat() if t.exit_time else None,
            "rationale": t.rationale,
        }
        for t in trades[-limit:]
    ]


@server.tool()
def get_stand_down_status() -> dict[str, Any]:
    """Whether a consecutive-loss stand-down is in force, and for how long.

    A stand-down suspends LIVE trading only. Paper trading continues exactly as
    normal, and closing positions or moving stops is never blocked.
    """
    state = _session.journal.get_stand_down()
    now = datetime.now(UTC)
    rules = _session.rules.stand_down
    return {
        "active": state.is_active(now),
        "stage": state.stage,
        "consecutive_losses": state.consecutive_losses,
        "trigger_at": rules.consecutive_losses_trigger,
        "loss_threshold_r": rules.loss_threshold_r,
        "ends_at": state.ends_at.isoformat() if state.ends_at else None,
        "days_remaining": round(state.days_remaining(now), 2),
        "summary": describe(state, now),
    }


@server.tool()
def get_recent_decisions(limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recent entries from today's audit log, newest last.

    Args:
        limit: How many entries to return (default 20).
    """
    path = Path("audit") / f"{datetime.now(UTC).date().isoformat()}.jsonl"
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries[-limit:]


@server.tool()
def reset_trading_session() -> dict[str, Any]:
    """Clear the daily loss kill-switch and re-baseline equity for a new session.

    Intended for the start of a trading day. Calling this after a losing session
    re-enables trading, so it is a deliberate act, not a way around the limit.
    """
    _session.reset()
    account = _session.account()
    return {
        "reset": True,
        "equity_at_session_start": round(account.equity_usd, 2),
        "kill_switch_tripped": _session.gate.kill_switch_tripped,
    }


def main() -> None:
    """Entry point for `electrum-bot-mcp`. Speaks MCP over stdio."""
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
