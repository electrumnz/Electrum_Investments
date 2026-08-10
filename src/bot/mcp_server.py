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

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from .audit import AuditLog
from .broker import Broker
from .config import Env, Rules, load_rules
from .data.calendar import CalendarFeed, build_calendar_feed
from .dreaming import (
    DEFAULT_DREAMS_PATH,
    Adoption,
    Dream,
    DreamStore,
    MoveResult,
    Vault,
    VaultTTLs,
)
from .grants import resolve_granted_symbols
from .insight import DEFAULT_DB_PATH as INSIGHT_DB_PATH
from .insight import InsightIndex, run_query
from .journal import Journal
from .metrics import build_report, render_excursions, render_summary
from .models import AccountSnapshot, AssetClass, Direction, OrderProposal
from .news_history import NewsItem
from .news_history import recall as recall_news
from .news_history import render as render_news
from .options import alerts_for_positions, parse_occ_symbol, render_alerts
from .reconcile import apply_journal_state, record_fill
from .risk import RiskGate
from .stand_down import describe
from .triggers import render as render_watches

server = MCPServer(
    name="electrum-bot",
    instructions=(
        "Risk gate and paper-trading controls for the Electrum trading bot. "
        "Always call check_order before proposing a trade to the user, and use "
        "place_order rather than any other order tool — it is the only path that "
        "enforces config/rules.yaml.\n\n"
        "This server also carries the market information the bot reads. "
        "get_recent_news returns the headlines, watched-account posts and "
        "earnings windows the loop was actually shown, recorded with the time "
        "it saw each one. It is a recording rather than a live news search, so "
        "quote the ages and never present a six-hour-old headline as current — "
        "but do not answer 'I have no access to news' either, because this is "
        "the news this bot trades on.\n\n"
        "For anything outside a recent window, the full history is searchable: "
        "query_history runs read-only SQL over every decision, assessment, "
        "rejection reason and news item ever recorded, and describe_history "
        "returns the schema and the days covered. Prefer those over saying the "
        "history is unavailable — it is on disk and indexed.\n\n"
        "The dream vaults are here too. list_dreams, get_dream and "
        "dream_vault_status read them; adopt_dream and return_dream are the "
        "only two verbs the trading agent has, and post_dream_message records a "
        "turn of the conversation. None of them reaches the broker: adopting a "
        "dream grants a SYMBOL PERMISSION with an expiry on it, and every gate "
        "in config/rules.yaml still runs on anything traded under one. Quote "
        "the ages these tools report — a dream offered three months ago and one "
        "offered this morning are different facts — and never read an empty "
        "list as 'nothing is happening' without checking store_readable first."
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
        self._insight: InsightIndex | None = None
        self._insight_path = INSIGHT_DB_PATH
        self._dreams: DreamStore | None = None
        self._dreams_path = DEFAULT_DREAMS_PATH
        self._calendar: CalendarFeed | None = None

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
    def calendar(self) -> CalendarFeed:
        """The earnings calendar the news blackout gate reads.

        **This path ran without one, so `_news_blackout` had never fired on an
        order placed here.** The rule is in `config/rules.yaml`, the gate
        implements it, and `evaluate` was simply never handed any windows —
        which meant the loop held back around an announcement and an order
        placed by the operator or through chat did not. A gate rule enforced on
        one path and not another is worse than one nobody wrote, because the
        limits page says it applies.

        Built once per process and cached, exactly as the loop builds it once at
        start: `FinnhubCalendar` keeps a TTL cache because the free tier allows
        100 requests a day, and a fresh adapter per tool call would throw that
        cache away and spend the loop's quota.

        Failure is the feed's own to report. `FinnhubCalendar` catches its
        errors and answers with no windows plus `is_degraded`, so a bad fetch
        costs the blackout rather than the order path — the same direction as
        everything else here.
        """
        if self._calendar is None:
            self._calendar = build_calendar_feed(self.env, self.rules)
        return self._calendar

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

    @property
    def insight_path(self) -> Path:
        return self._insight_path

    @property
    def insight(self) -> InsightIndex:
        """The derived query index, built on first use.

        Lazy because it is only needed by the query tools, and a first call on
        a box with months of history does the full build. Every call after that
        indexes the delta, which is why the tools can refresh on each request
        rather than depending on a timer somebody has to remember to install.
        """
        if self._insight is None:
            self._insight = InsightIndex(
                self._insight_path, audit_dir=self._audit.base_dir
            )
        return self._insight

    @property
    def dreams(self) -> DreamStore:
        """The dream store, built on first use. May raise; callers use `_store`.

        Lazy for the reason the index is: most sessions never ask about dreams,
        and opening a SQLite file to answer a question about the account would
        be work nobody asked for. It is also the reason it can fail — a missing
        directory, a file somebody moved — and a store that will not open must
        reach the agent as a stated fact rather than as a traceback, which is
        what `_store` is for.

        `DreamStore` is resolved off the module rather than captured at import,
        so a test can patch it. `_dreams_path` follows the same pattern as
        `_insight_path`: every store here takes its path from the session so the
        suite never writes to the real `data/`.
        """
        if self._dreams is None:
            self._dreams = DreamStore(self._dreams_path)
        return self._dreams

    def account(self) -> AccountSnapshot:
        """Broker state with open risk filled in from the journal.

        The broker cannot supply open risk — Alpaca keeps stop-losses as
        separate orders — so every read goes through here rather than calling
        `broker.get_account()` directly, or the risk caps would have nothing to
        count.

        Delegates to `apply_journal_state` rather than keeping its own copy of
        the enrichment. This used to mirror it by hand, which was one line while
        there was one figure to fill in; it is three now — the total, the
        per-symbol breakdown the class caps read, and the positions whose risk
        is unknowable — and a hand-kept mirror is how one path quietly stops
        populating something the gate depends on.
        """
        return apply_journal_state(self.broker.get_account(), self.journal)

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
    rationale: str,
    take_profit_price: float | None = None,
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
    rationale: str,
    take_profit_price: float | None = None,
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
        take_profit_price: Optional. Above entry for a buy, below for a sell.
            Omit it for no target: the order goes out as an entry plus a
            stop rather than a bracket. A stop is required; an exit is not,
            and a level invented to fill this field becomes a real resting
            order at the broker.
        rationale: One sentence: the signal, and the level that invalidates it.
    """
    try:
        proposal = _build_proposal(
            symbol,
            direction,
            qty,
            limit_price,
            stop_loss_price,
            rationale,
            take_profit_price=take_profit_price,
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
        # The same hour of lookahead the decision loop uses. Without this the
        # earnings blackout in config/rules.yaml could not fire on an order
        # placed by the operator or through chat, while firing on every order
        # the loop proposed — one rule, enforced on one path.
        news_windows=_session.calendar.upcoming_windows(lookahead_minutes=60),
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
    rationale: str,
    take_profit_price: float | None = None,
) -> dict[str, Any]:
    """Vet an order and, only if it passes every rule, place it on the PAPER account.

    This is the only order path that enforces config/rules.yaml. A rejected
    proposal is not placed under any circumstances.

    Args mirror check_order.
    """
    checked = check_order(
        symbol,
        direction,
        qty,
        limit_price,
        stop_loss_price,
        rationale,
        take_profit_price=take_profit_price,
    )
    if not checked["approved"]:
        return {"placed": False, "reasons": checked["reasons"]}

    proposal = _build_proposal(
        symbol,
        direction,
        qty,
        limit_price,
        stop_loss_price,
        rationale,
        take_profit_price=take_profit_price,
    )
    result = _session.broker.place_order(proposal)

    # Journalled, exactly as `cmd_loop` journals its own fills. This was
    # missing, and the consequence is the bug fixed in `14b88c8` arriving
    # through a different door: Alpaca holds a stop-loss as a separate order,
    # so the broker cannot report what a position was designed to lose. The
    # journal is the only place that knows, and a position placed here without
    # an entry has an UNKNOWABLE stop.
    #
    # What that costs is not cosmetic. `AccountSnapshot.open_risk_usd` is what
    # the 2% total-risk cap counts against, so an unjournalled position is
    # exposure the cap cannot see — the next proposal is measured against a
    # total that is missing this one, and the Board renders the position with
    # its stop as "unknown" under an "open risk is understated" banner.
    #
    # `record_fill` returns None on a refusal, so a broker rejection writes
    # nothing. Recorded BEFORE the audit event so the audit line can carry the
    # trade id.
    trade_id = record_fill(
        _session.journal,
        proposal,
        result,
        # "manual" rather than the instrument's strategy label. Metrics group
        # by strategy, and folding an operator-directed trade into
        # `mean_reversion`'s record would corrupt the track record of a
        # strategy that did not propose it.
        strategy="manual",
        execution_mode=_session.env.execution_mode,
    )

    _session.audit.record_event(
        "mcp_place_order",
        {
            "proposal": proposal.model_dump(mode="json"),
            "accepted": result.accepted,
            "order_id": result.order_id,
            "error": result.error,
            "trade_id": trade_id,
        },
    )
    return {
        "placed": result.accepted,
        "order_id": result.order_id,
        "filled_price": result.filled_price,
        "filled_qty": result.filled_qty,
        "error": result.error,
        # Named in the response so a caller can see the position is tracked.
        # `None` beside `placed: true` means the fill is at the broker and the
        # journal does not know, which is the state the cap cannot count.
        "trade_id": trade_id,
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
def get_recent_news(hours: float = 24.0, limit: int = 40) -> dict[str, Any]:
    """Headlines, watched-account posts and earnings windows the bot has seen.

    **This is a recording, not a live news search.** It returns what the
    trading loop was actually shown and wrote into the audit log, with the age
    of each item attached. Nothing is fetched to answer the question, and the
    reason is a quota rather than a preference: Marketaux's free tier allows
    100 requests a day against a loop that already wakes 96 times, so a live
    fetch here would spend the loop's own allowance and leave it reasoning
    without headlines.

    Report the ages. An item first seen six hours ago is not "the latest news",
    and presenting it as current is the failure this tool has to avoid.

    If `cycles_read` is 0 the loop recorded nothing in the window — it was
    stopped, restarting, or the market was shut. That is NOT the same as there
    being no news, and it must not be reported as a quiet market. Read
    `readout` before summarising: it carries the caveats the lists do not.

    Args:
        hours: How far back to look (default 24).
        limit: Maximum items per feed (default 40).
    """
    now = datetime.now(UTC)

    # Read enough cycles to actually cover the window. The loop wakes every 15
    # minutes by default, but the interval is configurable, so this budgets for
    # a 5-minute one and pads. Reading too few would silently truncate the
    # window to its newest slice and report the rest as absent.
    cycle_budget = min(1000, max(64, int(hours * 12) + 16))
    day_budget = max(1, int(hours // 24) + 2)
    view = _session.audit.read(limit=cycle_budget, days=day_budget)
    result = recall_news(view, hours=hours, limit=limit, now=now)

    def _items(items: list[NewsItem]) -> list[dict[str, Any]]:
        return [
            {
                "text": i.text,
                "age_minutes": round(i.age_minutes(now), 1),
                "first_seen": i.first_seen.isoformat(timespec="minutes"),
                "last_seen": i.last_seen.isoformat(timespec="minutes"),
                "seen_in_cycles": i.cycles,
            }
            for i in items
        ]

    latest_age = result.latest_cycle_age_minutes(now)
    return {
        "source": (
            "The audit log — what the trading loop was shown on each cycle. "
            "Not a live news search, and not fetched just now."
        ),
        "window_hours": hours,
        "cycles_read": result.cycles_read,
        "cycles_without_inputs": result.cycles_without_inputs,
        "latest_cycle_at": (
            result.latest_cycle_at.isoformat(timespec="minutes")
            if result.latest_cycle_at
            else None
        ),
        "latest_cycle_age_minutes": (
            round(latest_age, 1) if latest_age is not None else None
        ),
        "reading_is_stale": result.is_stale(now),
        # Named rather than inferred from empty lists. A caller that reads
        # "no headlines" off an empty list when the loop never ran has invented
        # a quiet market out of a stopped process.
        "loop_recorded_nothing_in_window": not result.has_cycles,
        "headlines": _items(result.headlines),
        "social_posts": _items(result.social_posts),
        "news_windows": _items(result.news_windows),
        "feeds_degraded": {
            "social": result.social_degraded,
            "calendar": result.calendar_degraded,
            "note": (
                "A degraded feed returns an empty list that looks exactly like "
                "a quiet window. Say the record is incomplete rather than "
                "implying nothing happened."
            ),
        },
        "readout": render_news(result, now=now),
    }


@server.tool()
def get_recent_decisions(limit: int = 20, days: int = 7) -> dict[str, Any]:
    """Recent entries from the audit log, newest first, spanning several days.

    This is the only record of a REJECTED proposal: one the gate refused never
    becomes a trade, so it reaches neither the journal nor the broker.

    Reads across dated files rather than only today's. Today's file does not
    exist until the loop writes its first cycle, so a tool scoped to it
    reported "no decisions" every UTC midnight, on a Monday morning, and after
    any restart — with months of history sitting on disk unread.

    Args:
        limit: How many entries to return (default 20).
        days: How many dated files back to read (default 7).
    """
    view = _session.audit.read(limit=limit, days=max(1, days))
    return {
        "decisions": [
            {
                "timestamp": e.timestamp.isoformat(timespec="seconds"),
                "outcome": e.outcome,
                "approved": e.approved,
                "rejected": e.rejected,
                "decision": e.decision.model_dump(mode="json"),
            }
            for e in view.decisions
        ],
        "events": [
            {
                "kind": e.kind,
                "timestamp": e.timestamp.isoformat(timespec="seconds"),
                "payload": e.payload,
            }
            for e in view.events
        ],
        "days_read": max(1, days),
        # Counted, not swallowed. A log that quietly drops records is worse
        # than one that admits it lost some.
        "malformed_lines": view.malformed,
        "unreadable_files": view.unreadable_files,
        "record_is_incomplete": view.is_degraded,
    }


@server.tool()
def describe_history() -> dict[str, Any]:
    """Schema and coverage of the searchable decision history.

    Call this before writing a `query_history` query. It returns the live
    tables and columns, how many rows are in each, and the range of days
    covered, so a query can be written against what is actually there rather
    than against a guess.

    The index is derived from the audit log and rebuilt from it, so it is a
    cache and never the source. If a row looks wrong, the audit log is right.
    """
    index = _session.insight
    report = index.refresh()
    payload = index.describe()
    payload["freshly_indexed"] = {
        "decisions": report.decisions_indexed,
        "events": report.events_indexed,
        "malformed_lines": report.malformed,
        "unreadable_files": report.unreadable,
    }
    return payload


@server.tool()
def query_history(sql: str, limit: int = 50) -> dict[str, Any]:
    """Run a read-only SQL SELECT over the full decision history.

    This is the general-purpose way to ask something the other tools do not
    cover: what was decided about a symbol months ago, which rejection reason
    fires most often, how many watches named no trigger, what news was seen
    around a date. The window-based tools answer "lately"; this answers
    "ever".

    Call `describe_history` first for the live schema. In short:

      cycles(ts, day, outcome, proposal_count, approved_count, rejected_count,
             acted, notes, has_inputs, calendar_degraded, social_degraded)
      assessments(ts, symbol, stance, reasoning, waiting_for)
      proposals(ts, idx, symbol, direction, qty, limit_price, stop_loss_price,
                take_profit_price, risk_usd, notional_usd, rationale, approved)
      rejections(ts, idx, reason_idx, symbol, reason)
      position_plans(ts, symbol, action, thesis_intact, reasoning,
                     waiting_for, invalidation)
      readings(ts, symbol, kind, summary)      -- kind: 'daily' | 'intraday'
      news(kind, text, first_seen, last_seen, cycles)  -- deduped view
      events(ts, kind, payload)

    `stance` is take/watch/pass/blocked. `outcome` is
    held/executed/refused/approved/rejected. Timestamps are ISO-8601 UTC
    strings, so ordinary string comparison sorts them and `day` is the cheap
    way to group.

    Two things to be careful about when reporting results:

    - `readings.summary` is the rendered line the loop recorded, e.g.
      "close 580.12, sma20 574.30, atr 6.41, ...". It is searchable text and
      NOT parseable numbers. Do not extract a figure from it and present it as
      a computed value.
    - A `news` row's `first_seen` is when the bot first saw the item, which is
      not when the story broke. Quote the timestamp rather than implying it is
      current.

    An empty result means nothing matched. It does not mean the loop saw
    nothing — check `describe_history` for the days actually covered.

    Args:
        sql: One SELECT (or WITH ... SELECT) statement. Writes are refused.
        limit: Maximum rows to return (default 50, capped at 200).
    """
    _session.insight.refresh()
    result = run_query(_session.insight_path, sql, limit=limit)
    if not result.ok:
        return {
            "ok": False,
            "error": result.error,
            "hint": "Call describe_history for the tables and columns available.",
        }
    return {
        "ok": True,
        "columns": result.columns,
        "row_count": len(result.rows),
        "rows": result.rows,
        "truncated": result.truncated,
        "note": (
            "More rows matched than were returned; add LIMIT or narrow the "
            "query." if result.truncated else ""
        ),
    }


@server.tool()
def review_watches(days: int = 30, horizon_days: float = 5.0) -> dict[str, Any]:
    """Score the bot's own watch list: did each trigger fire, and did it act?

    A `watch` stance says "not yet, and here is what would change that". This
    checks each of those conditions against the figures recorded on later
    cycles, and reports whether a proposal followed.

    **This measures plan-following, not profit.** No estimate of what a trade
    would have made appears here and none should be inferred: that would need
    a fill, a size and an intraday path all assumed. What makes a trigger worth
    scoring is that it was written down BEFORE the fact.

    Read `readout` before summarising. Three distinctions in it matter:

    - `fired` with `acted: false` is the interesting case — the bot named a
      condition, the condition happened, and nothing followed.
    - `unknown` is not `not_fired`. It means the figure the trigger names was
      unavailable every time it was checked, so nothing can be concluded.
    - `pending` is not `not_fired` either. The horizon has not elapsed yet.

    If `can_grade_anything` is false, the evidence is absent — that is NOT the
    same as every watch having been honoured, and must not be reported as a
    clean record.

    Args:
        days: How far back to collect watches (default 30).
        horizon_days: How long a watch stays live before it expires unscored
            (default 5). A trigger that fires three weeks later is not the
            setup that was described.
    """
    _session.insight.refresh()
    report = _session.insight.watch_report(days=days, horizon_days=horizon_days)

    def _outcome(o: Any) -> dict[str, Any]:
        return {
            "symbol": o.symbol,
            "stated_at": o.stated_at.isoformat(timespec="minutes"),
            "trigger": o.trigger.render(),
            "waiting_for": o.waiting_for,
            "verdict": o.verdict.value,
            "fired_at": o.fired_at.isoformat(timespec="minutes") if o.fired_at else None,
            "fired_value": o.fired_value,
            "acted": o.acted,
            "cycles_checked": o.cycles_checked,
        }

    return {
        "window_days": days,
        "horizon_days": horizon_days,
        "can_grade_anything": report.can_grade_anything,
        "graded": report.graded,
        "fired": len(report.fired),
        "fired_without_a_proposal": len(report.missed),
        "watches_naming_nothing": report.watches_naming_nothing,
        "watches_with_prose_only": report.watches_with_prose_only,
        "cycles_with_numeric_readings": report.cycles_with_numeric_readings,
        "cycles_without_numeric_readings": report.cycles_without_numeric_readings,
        "missed": [_outcome(o) for o in report.missed],
        "outcomes": [_outcome(o) for o in report.outcomes],
        "readout": render_watches(report),
    }


@server.tool()
def search_news(text: str = "", kind: str = "", days: int = 0, limit: int = 40) -> dict[str, Any]:
    """Search everything the bot has ever been shown, not just a recent window.

    `get_recent_news` answers "what has it seen lately". This answers "has it
    ever seen anything about X". Both read the recording rather than fetching:
    the Marketaux quota belongs to the trading loop.

    Every item carries when it was first and last seen. Quote those — an item
    first seen in March is not news today.

    Args:
        text: Case-insensitive substring, e.g. a ticker or a company name.
              Empty matches everything.
        kind: 'headline', 'social' or 'window'. Empty matches all three.
        days: Only items first seen in the last N days. 0 means all history.
        limit: Maximum items (default 40).
    """
    _session.insight.refresh()

    where: list[str] = []
    params: list[Any] = []
    if text.strip():
        where.append("text LIKE ?")
        params.append(f"%{text.strip()}%")
    if kind.strip():
        where.append("kind = ?")
        params.append(kind.strip().lower())
    if days > 0:
        where.append("first_seen >= ?")
        params.append((datetime.now(UTC) - timedelta(days=days)).isoformat())

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    sql = (
        f"SELECT kind, text, first_seen, last_seen, cycles FROM news {clause} "
        f"ORDER BY first_seen DESC LIMIT {max(1, min(limit, 200))}"
    )
    result = run_query(_session.insight_path, sql, params=params, limit=limit)
    if not result.ok:
        return {"ok": False, "error": result.error}

    now = datetime.now(UTC)
    items = []
    for row in result.rows:
        first = _parse_ts(row.get("first_seen"))
        items.append(
            {
                **row,
                "age_hours": (
                    round((now - first).total_seconds() / 3600, 1)
                    if first
                    else None
                ),
            }
        )
    return {
        "ok": True,
        "filters": {"text": text, "kind": kind or "all", "days": days or "all history"},
        "item_count": len(items),
        "items": items,
        "source": (
            "The audit log — items the trading loop was shown on some cycle. "
            "Not a live news search. An empty result means nothing recorded "
            "matched, not that nothing was published."
        ),
    }


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ------------------------------------------------------------------ the dreams
#
# **These tools reach `DreamStore` and nothing else.** No broker, no
# `OrderProposal`, no call into `place_order`, and no import that could become
# one. That is the argument `dreaming.py` and `confer.py` are built on, arriving
# on the tool surface: a `Dream` carries no quantity, no entry, no stop and no
# side, so nothing turns one into an order without somebody writing new fields
# and new validation by hand.
#
# What adoption buys is a SYMBOL PERMISSION with an expiry on it. Everything
# that decides whether a considered trade actually happens is untouched —
# `RiskGate.evaluate` still runs on every order path, the stop is still required
# and still validated, and the size still follows from it.
#
# Two verbs, because the trading agent is the one with a route to the broker and
# therefore gets the smaller set: `adopt_dream` (vault to adopted) and
# `return_dream` (adopted back to the vault, with a stated reason). There is
# deliberately no `move_dream` and no `delete_dream` here. `DreamStore` refuses
# the trader both anyway; the absence of the tool is the readable half of that
# guarantee, in the same way `TraderPowers` is.

# How close to lapsing counts as "expiring soon".
#
# Ten days, the same notice period as the Tailscale banner and for the same
# reason: the failure is notice followed by a loss of capability, and the notice
# period is the only time anything can be done about it. A grant that lapses
# unannounced leaves a position held under a permission that no longer exists.
GRANT_WARNING_DAYS = 10.0

# What a caller has to read before concluding anything from an empty list.
# Written once and attached to every readout here, because the failure it
# prevents is the one `news_history.has_cycles` and
# `WatchReport.can_grade_anything` exist for: a shelf that is empty and a store
# that could not be read produce the same empty list, and only one of them says
# anything about the dreamer.
EMPTY_VS_UNREADABLE = (
    "An empty list means that shelf is empty, which is an ordinary state. It "
    "means nothing at all unless store_readable is true — if the store could "
    "not be read, say so rather than reporting a quiet vault."
)


def _store() -> tuple[DreamStore | None, str]:
    """The dream store, or the reason it could not be opened. Never raises.

    Broad on purpose, exactly like `fetch_market_ticks` and
    `grants.resolve_granted_symbols`. This is a SQLite open behind an agent
    turn: the failures are `sqlite3.Error`, a permissions problem, a directory
    that is not there. Any of them propagating would reach the agent as a tool
    crash, and an agent handed a crash says "I cannot see the dreams" — which is
    indistinguishable, in the transcript, from "there are no dreams".
    """
    try:
        return _session.dreams, ""
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _unreadable(detail: str) -> dict[str, Any]:
    """The one shape every tool here returns when the store will not open."""
    return {
        "store_readable": False,
        "error": detail,
        "note": (
            "The dream store could not be read, so nothing below is known. "
            "This is NOT an empty vault and must not be reported as one."
        ),
    }


def _days(then: datetime, now: datetime) -> float:
    """Days between two moments, to one decimal so an hour stays visible."""
    return round((now - then).total_seconds() / 86400.0, 1)


def _ttls() -> VaultTTLs:
    """The TTLs from `config/rules.yaml`, never the dataclass defaults.

    The file an operator edits is the file that decides. Reading the defaults
    here would let this tool and the Settings page disagree about when a dream
    expires, and the one nobody is reading would be the wrong one.
    """
    return _session.rules.dreaming.vault_ttls()


def _expiry(dream: Dream, now: datetime, ttls: VaultTTLs) -> dict[str, Any]:
    """When this dream ages out of the shelf it is on.

    Measured from `vault_entered_at` and never from `created_at`: a dream pulled
    back out for another pass gets a fresh clock, because the alternative
    punishes exactly the reworking the arrangement wants to encourage.

    A shelf with no TTL — the archive — reports `None` rather than a number, and
    `expires_in_days: null` means never, not today.
    """
    days = ttls.days_for(dream.vault)
    if days is None:
        return {"expires_in_days": None, "expired": False, "ttl_days": None}
    remaining = days - (now - dream.vault_entered_at).total_seconds() / 86400.0
    return {
        "expires_in_days": round(remaining, 1),
        "expired": remaining <= 0,
        "ttl_days": days,
    }


def _dream_brief(dream: Dream, now: datetime, ttls: VaultTTLs) -> dict[str, Any]:
    """One row in a list. Every age it can state, it states.

    `has_conditions` travels beside `all_conditions_met` deliberately: a dream
    with no conditions is False for the second, and a reader given only that
    reads "not yet" where the truth is "nothing was ever claimed".
    """
    return {
        "id": dream.id,
        "title": dream.title,
        "vault": str(dream.vault),
        "stage": str(dream.stage),
        "verdict": str(dream.verdict) if dream.verdict else None,
        # Arithmetic over the `checked` flags on the chain, never the model's
        # own opinion of its sourcing.
        "verification": str(dream.verification),
        "weakest_hop": dream.weakest_hop,
        "hops": len(dream.chain),
        "unchecked_hops": len(dream.unverified_hops),
        "symbols": list(dream.symbols),
        "asset_class_key": dream.asset_class_key,
        "instruments": list(dream.instruments),
        "has_conditions": dream.has_conditions,
        "conditions": len(dream.conditions),
        "conditions_met": dream.conditions_met,
        "all_conditions_met": dream.all_conditions_met,
        "wisp": dream.wisp,
        "created_at": dream.created_at.isoformat(timespec="minutes"),
        "updated_at": dream.updated_at.isoformat(timespec="minutes"),
        "age_days": _days(dream.created_at, now),
        "vault_entered_at": dream.vault_entered_at.isoformat(timespec="minutes"),
        "days_on_this_shelf": _days(dream.vault_entered_at, now),
        **_expiry(dream, now, ttls),
    }


def _adoption_row(adoption: Adoption, now: datetime) -> dict[str, Any]:
    """One grant, with its clock. `live` is arithmetic, never a stored flag."""
    expires = adoption.expires_at
    return {
        "dream_id": adoption.dream_id,
        "symbols_granted": list(adoption.symbols_granted),
        "asset_class": adoption.asset_class,
        "adopted_at": adoption.adopted_at.isoformat(timespec="minutes"),
        "held_for_days": _days(adoption.adopted_at, now),
        "expires_at": expires.isoformat(timespec="minutes") if expires else None,
        "expires_in_days": (
            round((expires - now).total_seconds() / 86400.0, 1) if expires else None
        ),
        "returned_at": (
            adoption.returned_at.isoformat(timespec="minutes")
            if adoption.returned_at
            else None
        ),
        "return_reason": adoption.return_reason,
        "live": adoption.is_live(now),
    }


def _move_payload(result: MoveResult) -> dict[str, Any]:
    """A `MoveResult` as the agent reads it. A refusal is an answer, not an error.

    Every refusal reason is carried, because `DreamStore` collects them rather
    than short-circuiting — the same property `RiskGate` has, for the same
    reason. An agent told one thing wrong with a move fixes it, asks again and
    is told the second; an agent that has to ask repeatedly is what an
    unattended surface must not encourage.
    """
    return {
        "ok": result.ok,
        "dream_id": result.dream_id,
        "moved_from": str(result.moved_from) if result.moved_from else None,
        "moved_to": str(result.moved_to) if result.moved_to else None,
        "refusals": [str(r) for r in result.refusals],
        "detail": result.detail,
        "note": (
            ""
            if result.ok
            else (
                "This was refused by the dream store, which is deterministic "
                "code. Fix what it names or leave the dream where it is; the "
                "answer will not change if it is asked again."
            )
        ),
    }


@server.tool()
def list_dreams(vault: str | None = None, limit: int = 20) -> dict[str, Any]:
    """What is on each dream shelf, newest first, with an age on every row.

    Five shelves. `workbench` is being dreamt about now, `prophecy` is a
    long-horizon claim with conditions attached, `vault` is the only shelf the
    trading agent can see and where the two agents talk, `adopted` is what the
    trading agent has taken, and `archive` is retired.

    **Quote the ages.** A dream offered three months ago and one offered this
    morning are different facts, and only the second could be described as news.
    `days_on_this_shelf` is the figure that matters for an offer nobody has
    answered; `expires_in_days` is how long it has before it ages out.

    A dream is speculative by construction. `verification` is arithmetic over
    which links in the chain name a source — `unverified` means at least one hop
    is an assumption — and `weakest_hop` is the sentence that could kill it.
    Never present a chain as established fact.

    An empty shelf is an ordinary state and says nothing on its own. Check
    `store_readable` first: an unreadable store produces the same empty lists,
    and reporting that as a quiet vault is the failure this tool must avoid.

    Args:
        vault: One of workbench, prophecy, vault, adopted, archive. Omit for
            every shelf at once.
        limit: Maximum dreams returned per shelf (default 20).
    """
    store, error = _store()
    if store is None:
        return _unreadable(error)

    if vault:
        try:
            wanted = [Vault(vault.strip().lower())]
        except ValueError:
            # A refusal, not an exception. A mistyped shelf name is an ordinary
            # thing for an agent to do, and the useful answer names the shelves.
            return {
                "store_readable": True,
                "error": f"'{vault}' is not a shelf.",
                "valid_vaults": [str(v) for v in Vault],
            }
    else:
        wanted = list(Vault)

    now = datetime.now(UTC)
    ttls = _ttls()
    counts = store.counts_by_vault()
    caps = _session.rules.dreaming.vault_caps()

    shelves: dict[str, Any] = {}
    for shelf in wanted:
        dreams = store.in_vault(shelf, limit=max(1, limit))
        cap = caps.limit_for(shelf)
        shelves[str(shelf)] = {
            "held": counts.get(shelf, 0),
            "cap": cap,
            "full": cap is not None and counts.get(shelf, 0) >= cap,
            "returned": len(dreams),
            "dreams": [_dream_brief(d, now, ttls) for d in dreams],
        }

    return {
        "store_readable": True,
        "error": "",
        "asked_at": now.isoformat(timespec="minutes"),
        "vault_filter": str(wanted[0]) if vault else "all shelves",
        # Every shelf is a key even when it is empty, so a zero is a stated fact
        # rather than a missing one. Same reason `stop_watch` puts a breach
        # count of zero on every cycle line.
        "counts_by_vault": {str(v): counts.get(v, 0) for v in Vault},
        "shelves": shelves,
        "note": EMPTY_VS_UNREADABLE,
    }


@server.tool()
def get_dream(dream_id: int) -> dict[str, Any]:
    """One dream in full: the chain, the conditions, the verdict, the transcript.

    The causal chain is the point of a dream and it comes back hop by hop, each
    with whether anybody actually checked it and what against. A chain is a
    hypothesis precisely because every link is a separate claim that can be
    attacked on its own, so read `unchecked_hops` and `weakest_hop` before
    repeating any of it and never present an unchecked hop as a fact.

    `messages` is the agent-to-agent transcript, oldest first, because a
    negotiation read newest-first is a negotiation read backwards. Every turn
    carries its age: a message from March is not part of a conversation
    happening now. `adoptions` is every grant this dream has carried, live or
    handed back.

    A missing dream comes back as `found: false` rather than as an error.

    Args:
        dream_id: The id from list_dreams.
    """
    store, error = _store()
    if store is None:
        return _unreadable(error)

    dream = store.get(dream_id)
    if dream is None:
        return {
            "store_readable": True,
            "found": False,
            "dream_id": dream_id,
            "note": (
                "No dream with that id. It may have been retired by the "
                "dreamer; call list_dreams for what is actually there."
            ),
        }

    now = datetime.now(UTC)
    return {
        "store_readable": True,
        "found": True,
        **_dream_brief(dream, now, _ttls()),
        "seed": dream.seed,
        "origin": dream.origin,
        "trigger": dream.trigger,
        "chain": [
            {"claim": hop.claim, "checked": hop.checked, "source": hop.source}
            for hop in dream.chain
        ],
        "condition_detail": [
            {
                "text": c.text,
                # The checkable half. `null` means the condition is prose only,
                # which is "cannot be graded" and never "did not hold".
                "trigger": (t.render() if (t := c.as_trigger()) is not None else None),
                "is_checkable": c.is_checkable,
                "fulfilled": c.fulfilled,
                "fulfilled_at": (
                    c.fulfilled_at.isoformat(timespec="minutes")
                    if c.fulfilled_at
                    else None
                ),
                "note": c.note,
            }
            for c in dream.conditions
        ],
        "thoughts": [
            {
                "stage": str(t.stage),
                "text": t.text,
                "at": t.at.isoformat(timespec="minutes"),
                "age_days": _days(t.at, now),
                "by": t.by,
            }
            for t in dream.thoughts
        ],
        "messages": [
            {
                "speaker": m.speaker,
                "kind": m.kind,
                "text": m.text,
                "at": m.at.isoformat(timespec="minutes"),
                "age_days": _days(m.at, now),
            }
            for m in store.messages(dream_id)
        ],
        "adoptions": [_adoption_row(a, now) for a in store.adoptions(dream_id)],
        "note": (
            "The chain is speculative by construction. Quote the verification "
            "badge and the weakest hop alongside it, and quote the ages."
        ),
    }


@server.tool()
def dream_vault_status() -> dict[str, Any]:
    """The shelves against their caps, the live grants, and what lapses soon.

    Three questions, answered apart because they fail apart:

    - **What is held**, per shelf, against the cap in config/rules.yaml. A full
      shelf refuses a move and says so. The cap is a working constraint about
      what a person can hold in their head, not a risk rule — except `adopted`,
      which matches max_concurrent_positions so that an adoption the account has
      no slot to trade cannot be promised.
    - **What is permitted right now.** `symbols_in_force` is what the risk gate
      will actually honour and it is a SUBSET of what the adoptions claim: a
      grant naming an instrument class that is disabled in config/rules.yaml
      grants nothing, and a symbol already on the allowed list is not a grant
      at all because it was permitted anyway. `symbols_claimed_by_adoptions` is
      the raw store answer, and the gap between the two is the rules doing their
      job rather than an error.
    - **What is about to lapse**, with the days remaining. Expiry withdraws the
      right to OPEN a position in a granted symbol and never closes anything: a
      position held under a lapsed grant stands and is still the operator's to
      manage.

    An empty `symbols_in_force` is the normal state and means the account is
    trading exactly what config/rules.yaml already allows. An unreadable store
    is reported as `store_readable: false` and must not be summarised as a
    quiet vault.
    """
    store, error = _store()
    if store is None:
        return _unreadable(error)

    now = datetime.now(UTC)
    rules = _session.rules
    ttls = _ttls()
    caps = rules.dreaming.vault_caps()
    counts = store.counts_by_vault()

    live = [a for a in store.adoptions() if a.is_live(now)]
    claimed = store.granted_symbols(now)
    in_force = resolve_granted_symbols(store, rules, now=now)

    expiring = [
        _adoption_row(a, now)
        for a in live
        if a.expires_at is not None
        and (a.expires_at - now).total_seconds() / 86400.0 <= GRANT_WARNING_DAYS
    ]

    # Already past their shelf's TTL. `DreamStore.expired` is a pure read — it
    # marks nothing and deletes nothing — so this is a report, and
    # `electrum-bot vault-expire` is what acts on it.
    expired = store.expired(now, ttls)

    return {
        "store_readable": True,
        "asked_at": now.isoformat(timespec="minutes"),
        "shelves": {
            str(v): {
                "held": counts.get(v, 0),
                "cap": caps.limit_for(v),
                "ttl_days": ttls.days_for(v),
                "full": (
                    caps.limit_for(v) is not None
                    and counts.get(v, 0) >= (caps.limit_for(v) or 0)
                ),
            }
            for v in Vault
        },
        "grants_enabled": rules.dreaming.allow_symbol_grants,
        "max_granted_symbols": rules.dreaming.max_granted_symbols,
        "live_adoptions": [_adoption_row(a, now) for a in live],
        "symbols_in_force": dict(sorted(in_force.items())),
        "symbols_claimed_by_adoptions": dict(sorted(claimed.items())),
        "expiring_within_days": GRANT_WARNING_DAYS,
        "grants_expiring_soon": expiring,
        "dreams_past_their_ttl": [
            {
                "id": d.id,
                "title": d.title,
                "vault": str(d.vault),
                "days_on_this_shelf": _days(d.vault_entered_at, now),
            }
            for d in expired
        ],
        "note": (
            "A permission is not an order. An adopted dream widens which "
            "symbols may be CONSIDERED; risk, concentration, session, "
            "concurrency and cooldown all still apply under that symbol's own "
            "class limits, and every order still goes through place_order. "
            + EMPTY_VS_UNREADABLE
        ),
    }


@server.tool()
def adopt_dream(
    dream_id: int, symbols: list[str] | None = None, asset_class: str = ""
) -> dict[str, Any]:
    """Take a dream out of the vault, granting its symbols until the grant lapses.

    **This is a permission, not a trade.** It adds the named symbols to what may
    be considered, for as long as the adoption is live, under the existing
    limits of an already-enabled instrument class. It cannot enable a class,
    cannot raise a cap and cannot skip a gate. Nothing is bought, nothing is
    sized and no position is opened — that is check_order and place_order,
    exactly as for any other symbol.

    Refused, with every reason at once, when the dream is not in the vault (the
    only shelf visible from here), when it names no symbols, when its instrument
    class is unresolved, when the adopted shelf is full, or when you ask for
    symbols or a class the dream does not itself claim. A full shelf is an
    ordinary answer: the cap matches max_concurrent_positions, so a fourth
    adoption would be a promise the account has no slot to keep. A refusal comes
    back as `ok: false` with its reasons — it is not an error, and asking again
    will not change it.

    **You cannot grant yourself a symbol here.** The store checks every adoption
    against what the dream offers, so `symbols` may only ever narrow that list
    and `asset_class` may only restate it. A dream is the argument the dreamer
    won; adoption accepts it or takes less of it.

    The grant is time-boxed. Report `expires_at` and `expires_in_days` when
    describing what was taken, because a permission with no stated end is one
    nobody revisits.

    Args:
        dream_id: The id from list_dreams.
        symbols: Take only some of what the dream offers. Must be a subset of
            its own symbols; anything else is refused. Omit to take the whole
            offer as the dreamer made it.
        asset_class: The instruments key from config/rules.yaml — "us_equity",
            "crypto". It must match the dream's own; omit it and the dream's is
            used. An unresolved class grants nothing, because a symbol whose
            class is unknown is a symbol whose risk limits are unknown.
    """
    store, error = _store()
    if store is None:
        return _unreadable(error)

    now = datetime.now(UTC)
    result = store.adopt(
        dream_id,
        symbols=list(symbols) if symbols else None,
        asset_class=asset_class,
        at=now,
        # From the rules file rather than the dataclass defaults, so the cap and
        # the clock an operator can read are the ones actually applied.
        caps=_session.rules.dreaming.vault_caps(),
        ttl_days=_session.rules.dreaming.ttl_days.adopted,
    )

    payload = _move_payload(result)
    payload["store_readable"] = True
    if result.ok:
        grants = [a for a in store.adoptions(dream_id) if a.is_live(now)]
        payload["grant"] = _adoption_row(grants[0], now) if grants else None
        payload["symbols_in_force"] = dict(
            sorted(resolve_granted_symbols(store, _session.rules, now=now).items())
        )
        payload["note"] = (
            "The grant expires on the date above. A symbol whose grant has "
            "lapsed is refused like any other unlisted symbol, and expiry never "
            "closes a position that is already open. If symbols_in_force does "
            "not contain what was just granted, the rules did not honour it — "
            "most often because its instrument class is disabled."
        )
    # Recorded for the same reason `mcp_place_order` is: a permission that
    # leaves no trace is a permission nobody can audit afterwards, and a
    # refusal is as worth having on the record as a grant.
    _session.audit.record_event(
        "mcp_adopt_dream",
        {
            "dream_id": dream_id,
            "ok": result.ok,
            "refusals": [str(r) for r in result.refusals],
            "symbols": list(symbols) if symbols else None,
            "asset_class": asset_class,
        },
    )
    return payload


@server.tool()
def return_dream(dream_id: int, reason: str) -> dict[str, Any]:
    """Hand an adopted dream back to the vault, saying why. Withdraws the grant.

    The reason is required and a blank one is refused by the store. It does not
    have to be a good reason — nothing here judges it — but a return with no
    record is an argument reversed silently, and the record is most of what the
    dream vault is for. It is written into the transcript as a `return` turn,
    where the dreamer can read it.

    The symbol permission ends immediately. Any position already open in a
    returned symbol stands: closing is deliberately outside this path and
    nothing here will close one.

    Args:
        dream_id: The id from list_dreams.
        reason: Why it is going back. One sentence is enough.
    """
    store, error = _store()
    if store is None:
        return _unreadable(error)

    result = store.return_to_vault(dream_id, reason=reason)
    payload = _move_payload(result)
    payload["store_readable"] = True
    if result.ok:
        payload["note"] = (
            "The grant is withdrawn as of now. A position still open in one of "
            "its symbols is untouched and remains yours to manage; there are "
            "simply no new entries permitted in it."
        )
    _session.audit.record_event(
        "mcp_return_dream",
        {"dream_id": dream_id, "ok": result.ok, "reason": reason},
    )
    return payload


@server.tool()
def post_dream_message(
    dream_id: int, speaker: str, text: str, kind: str = "note"
) -> dict[str, Any]:
    """Record one turn of the conversation about a dream. Append-only.

    The transcript is most of why the dream vault is worth having: the
    interesting part of a negotiation is the point where somebody changed their
    mind, and a store keeping only the current position would throw exactly that
    away. Use this when a chat turn settles something — a question put to the
    dreamer, the answer, the reasoning behind taking a dream or leaving it.

    Nothing is overwritten and nothing can be edited afterwards, so write the
    turn as it was said.

    Args:
        dream_id: The id from list_dreams. A message on a dream that does not
            exist is refused, because it could never be read back.
        speaker: Who is speaking — conventionally "trader", "dreamer" or
            "operator". Recorded verbatim: it is a claim about who spoke rather
            than a verified identity, so do not sign a turn as somebody else.
        text: The turn itself. Long prose is trimmed rather than rejected; an
            empty turn is refused, because a blank line in a transcript is
            indistinguishable from a bug.
        kind: question, answer, offer, accept, return or note (default note).
            offer, accept and return are written by the store itself when an
            adoption starts or ends, so prefer question, answer or note here.
    """
    store, error = _store()
    if store is None:
        return _unreadable(error)

    if not text.strip():
        return {
            "store_readable": True,
            "posted": False,
            "dream_id": dream_id,
            "error": "An empty message is not a turn. Say something, or say nothing.",
        }

    # Checked here rather than left to the store, which has no foreign key and
    # would happily write a message hanging off an id that does not exist — a
    # turn nobody can read back, on a conversation that never happened.
    if store.get(dream_id) is None:
        return {
            "store_readable": True,
            "posted": False,
            "dream_id": dream_id,
            "error": (
                f"No dream with id {dream_id}, so there is nothing to say it "
                "about. Call list_dreams for what is there."
            ),
        }

    message = store.add_message(
        dream_id,
        speaker=speaker.strip() or "unknown",
        text=text,
        kind=kind.strip() or "note",
    )
    return {
        "store_readable": True,
        "posted": True,
        "dream_id": dream_id,
        "message_id": message.id,
        "speaker": message.speaker,
        "kind": message.kind,
        "text": message.text,
        "at": message.at.isoformat(timespec="minutes"),
        # Prose trims rather than being rejected, so a caller is told when the
        # stored turn is not the turn it sent.
        "truncated": message.text != text.strip(),
    }


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
