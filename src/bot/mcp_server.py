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

import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
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
from .jobs import read as read_jobs
from .jobs import render as render_jobs
from .journal import Journal
from .metrics import build_report, render_excursions, render_summary
from .models import AccountSnapshot, AssetClass, Direction, OrderProposal
from .news_history import NewsItem
from .news_history import recall as recall_news
from .news_history import render as render_news
from .options import alerts_for_positions, parse_occ_symbol, render_alerts
from .position_actions import (
    ActionOutcome,
    close_with_reason,
    detect_unexplained_moves,
    loop_execution_state,
)
from .position_actions import (
    place_protective_stop as place_protective_stop_action,
)
from .position_actions import tighten_stop as tighten_stop_action
from .reconcile import apply_journal_state, record_fill
from .risk import RiskGate
from .souls import GROGU, YODA
from .stand_down import describe
from .triggers import render as render_watches

server = MCPServer(
    name="electrum-bot",
    instructions=(
        "Risk gate and paper-trading controls for the Electrum trading bot. "
        "Always call check_order before proposing a trade to the user, and use "
        "place_order rather than any other order tool — it is the only path that "
        "enforces config/rules.yaml.\n\n"
        "**Open positions are yours to manage, not yours to comment on.** "
        "tighten_stop pulls a stop closer to entry and "
        "close_position_with_reason closes one, both immediately and both "
        "requiring a reason that is stored with the move. Neither goes through "
        "the risk gate, because the gate vets orders that OPEN exposure and "
        "these reduce it — which is also why a stand-down never blocks them. "
        "Widening a stop is refused by code on either side of the market: it is "
        "the one position move that increases the loss at unchanged size, and "
        "nothing else in the system would catch it. If the risk on a position "
        "is the problem, close part of it. get_position_actions reads the "
        "record back, and reports stops or quantities that changed with no "
        "reason on file.\n\n"
        "This server also carries the market information the bot reads. "
        "get_recent_news returns the headlines, watched-account posts and "
        "earnings windows the loop was actually shown, recorded with the time "
        "it saw each one. It is a recording rather than a live news search, so "
        "quote the ages and never present a six-hour-old headline as current — "
        "but do not answer 'I have no access to news' either, because this is "
        "the news this bot trades on.\n\n"
        "get_loop_activity answers whether the decision loop was actually "
        "running, and what it was doing at a given moment. Four states and "
        "none is a version of another: a pass that RAN and proposed nothing "
        "stood pat, a SKIPPED pass never looked because the market was shut, a "
        "FAILED pass got no decision at all, and a moment nothing covers means "
        "the loop was stopped, restarting, or its record was lost. Never "
        "report has_jobs=false as 'it ran and found nothing to do'.\n\n"
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
        "list as 'nothing is happening' without checking store_readable first.\n\n"
        "**Neither chat agent can create a dream, and raise_consideration is "
        "not a way to.** It records a NOTE to the dreamer — a spark, why now, "
        "and the operator's own words verbatim — which the dreamer reads on its "
        "own run and may ignore. Nothing it writes is on a shelf, offered, or "
        "permitted, so never tell the operator a dream now exists; "
        "list_considerations reads them back. Put one up when you have a link "
        "the operator does not, and remember that 'I have nothing worth passing "
        "on here' is a good answer."
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

    audit_failure = _record_event(
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
        "audit_not_recorded": audit_failure,
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
    """Close a position WITHOUT recording why. Prefer close_position_with_reason.

    This still works, and it is left in place because closing a position must
    never be harder than opening one — a stand-down that could not be exited
    would be worse than the losing streak that caused it, and the same is true
    of a tool that made getting out conditional on writing a sentence first.

    But it leaves no reason on file, and that is now a gap rather than a
    neutral difference. `record_exit` stores a price, a time and a realised
    figure, so afterwards a stop-hit, a target-hit and a deliberate close by
    hand are indistinguishable — and the interesting one is the third, because
    that is the plan being abandoned. `close_position_with_reason` captures it
    at the only moment anybody knows it.

    A close through this tool shows on the Board as an unexplained move.

    Args:
        symbol: Ticker of the position to close.
    """
    result = _session.broker.close_position(symbol.upper())
    audit_failure = _record_event(
        "mcp_close_position",
        {"symbol": symbol.upper(), "accepted": result.accepted, "error": result.error},
    )
    return {
        "closed": result.accepted,
        "audit_not_recorded": audit_failure,
        "order_id": result.order_id,
        "error": result.error,
        "reason_recorded": False,
        "note": (
            "No reason was recorded for this close. Use "
            "close_position_with_reason instead — the reason is the one thing "
            "nothing else can reconstruct afterwards."
        ),
    }


def _record_event(name: str, payload: dict[str, Any]) -> str | None:
    """Write one audit line, and never let that write destroy the outcome.

    Every order tool records its event LAST — after the broker has answered and
    after the journal has been written. So an exception here undoes nothing. It
    replaces a completed action's result with a traceback, which is the exact
    failure this repository is most careful about: **a live order the caller has
    been told did not happen.**

    Measured on the droplet 30 Aug 2026. `mudhorn-web.service` was missing
    `/opt/mudhorn/audit` from its `ReadWritePaths`, so the first real use of
    `place_protective_stop` came back as
    `OSError: [Errno 30] Read-only file system: 'audit/2026-08-30.jsonl'`. The
    sandbox is fixed, and the masking is a second defect: the tool had already
    finished and its answer — in that instance a REFUSAL, with the reasons that
    explained it — was thrown away and replaced by the traceback. Had it been a
    placement instead, the order would have been resting at Alpaca under an
    error message.

    So this returns a sentence instead of raising: `None` when the line was
    written, and a description when it was not. The caller carries that into its
    response beside the real outcome, because "the action happened and the audit
    log does not know" is a fact worth reporting loudly and is never a reason to
    hide the action itself.

    It catches broadly on purpose, the same reasoning as `fetch_market_ticks`
    and `data/_http.fetch_json`: there is no failure of a log write worth
    destroying an order result over, and an unanticipated one is exactly the
    case where destroying it would be worst.
    """
    try:
        _session.audit.record_event(name, payload)
    except Exception as exc:  # broad on purpose — see the docstring
        return (
            f"THE ACTION ABOVE IS REAL AND THE AUDIT LOG DOES NOT KNOW. Writing "
            f"the {name!r} event failed: {type(exc).__name__}: {exc}. Nothing was "
            "undone — read the result beside this, not around it. The audit log "
            "is the only record of a refusal, so this decision is missing from "
            "the Decisions page and from every history query."
        )
    return None


def _action_payload(
    outcome: ActionOutcome, audit_failure: str | None = None
) -> dict[str, Any]:
    """One shape for both moves, so a caller reads them the same way.

    A refusal is an ANSWER rather than an error, exactly as `_move_payload`
    treats a refused dream move and as `check_order` treats a refused proposal.
    Every reason is carried, because the refusal paths here collect all of them
    rather than short-circuiting.
    """
    record = outcome.record
    return {
        "ok": outcome.ok,
        # First, and never folded into `note`. A caller skimming for the
        # outcome must not have to read past it to find out the record of this
        # action is missing. `None` is the ordinary case.
        "audit_not_recorded": audit_failure,
        "symbol": outcome.symbol,
        "action": str(outcome.action),
        "reasons": outcome.reasons,
        "warnings": outcome.warnings,
        "detail": outcome.detail,
        "reached_broker": outcome.reached_broker,
        "broker_order_id": outcome.broker_order_id,
        "risk_before_usd": (
            round(outcome.risk_before_usd, 2)
            if outcome.risk_before_usd is not None
            else None
        ),
        "risk_after_usd": (
            round(outcome.risk_after_usd, 2)
            if outcome.risk_after_usd is not None
            else None
        ),
        "risk_reduced_usd": (
            round(outcome.risk_reduced_usd, 2)
            if outcome.risk_reduced_usd is not None
            else None
        ),
        "recorded_action_id": record.id if record else None,
        "record": record.model_dump(mode="json") if record else None,
        "note": (
            ""
            if outcome.ok
            else (
                "This was refused by deterministic code, not by a judgement "
                "call. Fix what it names or leave the position alone; asking "
                "again will not change the answer."
            )
        ),
    }


@server.tool()
def close_position_with_reason(symbol: str, reason: str) -> dict[str, Any]:
    """Close the whole position in a symbol, recording WHY it was closed.

    **This is the close to use.** It does exactly what close_position does at
    the broker and additionally writes the one thing nothing else can
    reconstruct: the reason. Afterwards the journal can tell a deliberate exit
    from a stop-hit, and the interesting case — closed by hand before either
    level, which is the plan being abandoned — stops being invisible.

    Closing is never gated. `RiskGate` vets proposals that OPEN exposure and
    never sees this, deliberately: a stand-down that froze position management
    would strand open trades with no way out. Nothing here is refused except a
    blank reason and a broker refusal.

    A position the journal has never seen is still closed. The record is written
    with no trade behind it and the response says so, because refusing to reduce
    exposure over incomplete paperwork would be the wrong way round.

    The exit price, the time and the realised figure are written by the next
    reconcile cycle, not here — a fill is not atomic, and one poll during it is
    a reading rather than an outcome.

    Args:
        symbol: Ticker of the position to close.
        reason: Why it is being closed. One sentence. It does not have to be a
            good reason — nothing here judges it — but it cannot be blank, and
            a blank one is refused rather than stored empty.
    """
    outcome = close_with_reason(
        _session.journal,
        _session.broker,
        symbol=symbol,
        reason=reason,
        actor="trader",
    )
    audit_failure = _record_event(
        "mcp_close_position_with_reason",
        {
            "symbol": outcome.symbol,
            "ok": outcome.ok,
            "reason": reason,
            "reasons": outcome.reasons,
            "order_id": outcome.broker_order_id,
        },
    )
    return _action_payload(outcome, audit_failure)


@server.tool()
def tighten_stop(symbol: str, new_stop_price: float, reason: str) -> dict[str, Any]:
    """Pull an open position's stop CLOSER to entry, and record why.

    **Tighter only, and this is enforced in code rather than asked for.** On a
    long the stop tightens UPWARD, on a short DOWNWARD. A level further from
    entry than the one in force is refused outright, on either side of the
    market, and no wording in `reason` changes that.

    The reason it is absolute: `RiskGate.evaluate` gates proposals that OPEN
    exposure and never sees a position move, because a stand-down that froze
    position management would strand open trades with no way out. That
    exemption is safe only for moves that reduce exposure. Widening a stop is
    the one position move that increases the loss at unchanged size, on a live
    position, with no gate anywhere in the system behind it — so it is
    impossible through this path rather than discouraged.

    If the risk on a position is the problem, close part of it. Do not buy room
    by moving the stop.

    What happens: the resting stop leg at the broker is REPLACED, in one
    server-side operation — never cancelled and re-placed, which would leave the
    position with no stop at all in between. The new leg has a new order id. The
    journal's stop in force is updated in the same transaction as the record, so
    the two cannot come apart, and open risk falls by the amount the response
    reports.

    Refused when the reason is blank, when the symbol has no open journal row
    (the stop in force would be unknowable), when the move widens or changes
    nothing, when more than one stop leg is resting, when the resting orders
    could not be read, or when the broker refuses the replace — in which case
    the original stop is still there and nothing was written.

    A symbol with no resting stop leg is NOT refused, and the response says so
    loudly: the move lands in the journal only, where `stop_watch` reports a
    breach against it on the loop's pulse. That is the normal arrangement for
    crypto, which Alpaca accepts no bracket on. On an equity it means nothing at
    the broker is protecting the position.

    Args:
        symbol: Ticker of the open position.
        new_stop_price: The new trigger, as a number. Closer to entry than the
            stop currently in force. Call get_positions or get_risk_status first
            if you are not certain where that is.
        reason: Why it is being tightened. One sentence, and it cannot be blank.
    """
    outcome = tighten_stop_action(
        _session.journal,
        _session.broker,
        symbol=symbol,
        new_stop=new_stop_price,
        reason=reason,
        actor="trader",
    )
    audit_failure = _record_event(
        "mcp_tighten_stop",
        {
            "symbol": outcome.symbol,
            "ok": outcome.ok,
            "new_stop_price": new_stop_price,
            "reason": reason,
            "reasons": outcome.reasons,
            "reached_broker": outcome.reached_broker,
            "broker_order_id": outcome.broker_order_id,
        },
    )
    return _action_payload(outcome, audit_failure)


@server.tool()
def place_protective_stop(symbol: str, stop_price: float, reason: str) -> dict[str, Any]:
    """Rest a stop at the broker on a held position that has NONE, and record why.

    **Use this, not tighten_stop, when nothing is resting.** `tighten_stop`
    replaces a leg that already exists; with none it writes a journal figure and
    places nothing, returning `reached_broker: false`. That is worse than the
    gap it appears to close, because the risk caps then count protection that
    does not exist. This one submits a real GTC stop order and only writes the
    journal after the broker confirms it.

    Check `get_risk_status` first: `positions_without_a_resting_stop` is the
    list this tool is for.

    **The quantity is taken from the BROKER, not from you and not from the
    journal.** The broker is authoritative about what exists; the journal only
    knows what was intended, and where the two differ a stop sized off the
    intention leaves the difference naked. You do not pass a quantity for that
    reason.

    Ungated, like closing and tightening, because it can only REDUCE what is at
    risk — a position with no stop loses whatever the market takes, one with a
    stop loses at most the distance to it.

    Refused when the reason is blank, when the stop is not positive, when the
    resting orders could not be read (a leg that could not be listed is not a
    leg that is absent), when a stop leg is ALREADY resting (that is
    `tighten_stop`'s job), when the broker holds no position in the symbol, and
    when the stop sits on the wrong side of the market — below the price on a
    short or above it on a long would trigger on submission and become a market
    order, which is a close wearing a stop's clothes. If that is what you meant,
    say `close_position_with_reason`.

    A symbol with no open journal row is still protected, and the response says
    so: the stop reaches the broker, but `open_risk_usd` still cannot count the
    position until it is journalled.

    Args:
        symbol: Ticker of the held, unprotected position.
        stop_price: The trigger, as a number. Below the market for a long,
            above it for a short.
        reason: Why this level. One sentence, and it cannot be blank.
    """
    outcome = place_protective_stop_action(
        _session.journal,
        _session.broker,
        symbol=symbol,
        stop_price=stop_price,
        reason=reason,
        actor="trader",
    )
    audit_failure = _record_event(
        "mcp_place_protective_stop",
        {
            "symbol": outcome.symbol,
            "ok": outcome.ok,
            "stop_price": stop_price,
            "reason": reason,
            "reasons": outcome.reasons,
            "warnings": outcome.warnings,
            "reached_broker": outcome.reached_broker,
            "broker_order_id": outcome.broker_order_id,
        },
    )
    return _action_payload(outcome, audit_failure)


@server.tool()
def get_position_actions(symbol: str = "", limit: int = 25) -> dict[str, Any]:
    """Every recorded move on an open position, newest first, with its reason.

    This is the record `tighten_stop` and `close_position_with_reason` write. A
    record nothing can read back is not a record, and this is also the only
    place a stop's history exists: the journal stores the level in force and the
    broker stores the leg resting now, and neither says where either came from.

    It reports the inverse too, and that half is the point. `unexplained_moves`
    lists stops and quantities that differ from what the journal records with
    **no action explaining them** — a stop pulled in through Alpaca's web UI, a
    partial fill nobody journalled, a bracket leg cancelled by hand. A feature
    that only showed the moves it managed to capture would hide its own
    failures.

    Read the two caveat lists before concluding anything from an empty
    `unexplained_moves`:

    - `stop_trigger_unreadable` — a stop leg is resting and the broker reported
      no trigger price. Whether it moved is UNKNOWN, not fine.
    - `positions_without_a_resting_stop` — the position is there and no stop
      order is. Expected on crypto, which Alpaca accepts no bracket on, so
      crypto is excluded from the list. On an equity it means nothing at the
      broker would close the position.

    If `broker_orders_readable` is false the resting orders could not be
    fetched at all, and an empty `unexplained_moves` then means nothing
    whatsoever.

    Args:
        symbol: Only this ticker. Empty means every symbol.
        limit: Maximum recorded moves returned (default 25).
    """
    actions = _session.journal.position_actions(
        symbol=symbol.strip().upper() or None, limit=max(1, limit)
    )
    account = _session.account()
    orders = _session.broker.get_open_orders()
    report = detect_unexplained_moves(
        positions=account.open_positions,
        orders=orders,
        open_trades=_session.journal.open_trades(),
        actions=_session.journal.position_actions(limit=200),
        orders_degraded=_session.broker.orders_degraded,
    )
    enabled, sentence = loop_execution_state(_session.rules)

    return {
        "symbol_filter": symbol.strip().upper() or "all symbols",
        "recorded_moves": len(actions),
        "actions": [
            {
                **a.model_dump(mode="json"),
                "summary": a.describe(),
            }
            for a in actions
        ],
        "broker_orders_readable": report.can_check,
        "unexplained_moves": [m.describe() for m in report.moves],
        "stop_trigger_unreadable": report.unreadable_stops,
        "positions_without_a_resting_stop": report.positions_without_a_resting_stop,
        # A leg that moves its own trigger by design. Its level differs from
        # the journal's on every cycle it trails, so it is deliberately not
        # compared — and named here, because otherwise "nothing differed" and
        # "this one is not checked" are the same silence.
        "stop_is_trailing": report.trailing_stops,
        # Trailing, with no trail size reported. The current trigger can be
        # read and where it goes next cannot.
        "trail_size_unreadable": report.unreadable_trails,
        # Named so an agent describing its own powers describes them correctly.
        # It has the two tools regardless; what this flag decides is whether the
        # unattended LOOP may act on the plans it writes.
        "loop_may_act_unattended": enabled,
        "loop_execution_note": sentence,
        "note": (
            "An empty actions list means no move has been recorded, which is "
            "the ordinary state. An empty unexplained_moves list means nothing "
            "was found to differ — but only if broker_orders_readable is true, "
            "and only outside whatever the caveat lists name. A symbol in "
            "stop_is_trailing was not compared at all: its trigger moves by "
            "design, so quote it as a reading taken now rather than as a level "
            "anybody chose."
        ),
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

    **An item with `age_is_a_floor` true was already on file in the oldest cycle
    inside the window, so its age is a MINIMUM and the real one is unknown.**
    Say "at least N hours" for those, or ask again with a wider `hours`. The
    error runs towards making a story look fresher than it is, which is the
    same failure in a subtler place.

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
                # The window cut this item's history off, so the figure above
                # is a lower bound. Named per item rather than only in the
                # readout, because a caller that quotes one row must be able to
                # see it from that row.
                "age_is_a_floor": i.at_window_edge,
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
        # Said once for the whole answer as well as per item, so a summary can
        # carry the caveat without walking the lists.
        "some_ages_are_floors": result.ages_are_floors,
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
def get_loop_activity(hours: float = 24.0, at: str = "") -> dict[str, Any]:
    """Was the decision loop actually running, and what was it doing?

    Answers the question that used to need `journalctl | grep cycle_complete`
    on the box — which no agent can reach, and which holds only the passes that
    FINISHED. A cycle skipped with the market shut and a cycle whose model call
    failed never appeared there at all.

    **Four states, and collapsing any two of them is the failure this tool
    exists to prevent:**

    - `ran` — the loop completed a pass and got a decision. `proposals: 0` on
      one of these is a QUIET cycle: it looked and stood pat, which is
      frequently the right answer.
    - `skipped` — no enabled instrument class was in session, so the model was
      deliberately not asked. It did not look. The counts are `null` rather
      than `0` for exactly that reason.
    - `failed` — the pass could not get a decision. Never report this as a
      cycle that decided to do nothing.
    - **nothing covers that moment** — not an outcome at all. The loop was
      stopped, restarting, or the record was lost.

    And within that last one, `coverage` separates two more that look alike:
    `not_recorded` means nothing accounts for the moment, `out_of_range` means
    the read did not reach far enough to have an opinion about it. Never report
    the second as the first.

    If `has_jobs` is false, the loop recorded no pass at all in the window. That
    is NOT "it ran and found nothing to do" — say so plainly. If `is_degraded`
    is true the counts are known to be incomplete and every absence in them is
    unexamined rather than established.

    Args:
        hours: How far back to look (default 24).
        at: Optional ISO-8601 moment — "what was it doing at 14:15?". Empty
            asks about now.
    """
    history = read_jobs(_session.audit, hours=max(0.1, hours))

    moment: datetime | None = None
    moment_error = ""
    if at.strip():
        moment = _parse_ts(at.strip())
        if moment is None:
            # Refused rather than silently answered about now. A question about
            # 14:15 answered about this instant is a confident wrong answer,
            # which is the one failure mode this whole module is arranged
            # against.
            moment_error = (
                f"{at!r} is not an ISO-8601 timestamp, so no moment was looked "
                "up and the answer below is about now. The window counts are "
                "still the real ones."
            )

    # `window_to` rather than a fresh clock read when nobody named a moment.
    # `JobHistory.at` refuses anything past the end of the span it read, and a
    # second `datetime.now()` is always a hair past it — so "what is it doing
    # now" answered `out_of_range` every single time. The instant the log was
    # read IS now, as far as the record can speak.
    answer = history.at(moment or history.window_to or datetime.now(UTC))

    def _job(job: Any) -> dict[str, Any] | None:
        if job is None:
            return None
        return {
            "outcome": job.outcome.value,
            "started_at": job.started_at.isoformat(timespec="seconds"),
            "duration_seconds": round(job.duration_seconds, 3),
            "interval_seconds": job.interval_seconds,
            "detail": job.detail,
            # `null` on a skip and on a failure, never 0 — those passes did not
            # look, so a zero would be a claim they never made.
            "proposals": job.proposals,
            "approved": job.approved,
            "executed": job.executed,
            "quiet": job.quiet,
            "equity_usd": job.standing.equity_usd,
            "open_positions": job.standing.open_positions,
            "open_risk_usd": job.standing.open_risk_usd,
            "stand_down_stage": job.standing.stand_down_stage,
        }

    return {
        "window_hours": history.window_hours,
        # Separate from "are the counts zero", the same way `has_cycles` is in
        # get_recent_news. No passes recorded and passes that all stood pat are
        # opposite findings.
        "has_jobs": history.has_jobs,
        "passes": len(history.jobs),
        "ran": history.ran,
        "skipped": history.skipped,
        "failed": history.failed,
        "quiet": history.quiet,
        "asked_about": answer.moment.isoformat(timespec="seconds"),
        "coverage": answer.coverage.value,
        "at_that_moment": answer.describe(),
        "job_covering_that_moment": _job(answer.job),
        "nearest_pass_before": _job(answer.nearest_before),
        "nearest_pass_after": _job(answer.nearest_after),
        "latest_pass": _job(history.latest),
        "gaps": [
            {
                "after": gap.after.isoformat(timespec="minutes"),
                "before": gap.before.isoformat(timespec="minutes"),
                "minutes": round(gap.seconds / 60.0, 1),
                # `null` when the pass before the gap never stated its cadence.
                # A count derived from an assumed interval is a made-up number.
                "passes_missed": gap.missed,
                "explained_by_shutdown": gap.explained_by_shutdown,
            }
            for gap in history.gaps
        ],
        "is_degraded": history.is_degraded,
        "read_hit_its_limit": history.truncated,
        "unreadable_records": history.unreadable_records,
        "malformed_lines": history.malformed_lines,
        "unreadable_files": history.unreadable_files,
        "moment_error": moment_error,
        "readout": render_jobs(history),
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


# ------------------------------------------- a consideration put to the dreamer
#
# **The operator's correction, and it changes the shape rather than the wording:
# "Chat agents can't raise Dream, the agent can merely put it to consideration,
# hence the chat log."**
#
# A first pass at this gave chat a tool that wrote a `Dream`. That was wrong,
# and not as a matter of taste. A dream is the FIRST LINK of a chain that ends
# in a live trading permission — dream, prophecy, vault, adopted,
# `grants.resolve_granted_symbols`, and then a symbol `RiskGate` will admit that
# `config/rules.yaml` never listed. Handing a conversational surface a tool that
# inserts at the top of that chain means a signed-in user can talk a model into
# the first link of a permission path. Every step after it still gates, so no
# rule would have been broken today — but "it cannot create one" and "it can
# create one and the later steps will catch it" are different claims, and only
# the first is worth making. Same reasoning that put the dreamer on its own
# Hermes instance rather than trusting a sentence in `souls/grogu.md`.
#
# So what chat writes is a CONSIDERATION: a note addressed to the dreamer,
# saying here is a spark, here is why now, and here is what the operator
# actually said. The dreamer picks it up on its own run and decides for itself
# whether any of it becomes a seed. **It may ignore it**, and that is the
# feature: the operator can point at something and the thing that dreams still
# chooses.
#
# Three fields a `Dream` has are deliberately ABSENT here, and they are the
# three that make a dream capable of becoming a permission:
#
# - `symbols` — the only field on a dream that can become a grant.
# - `asset_class_key` — which risk limits such a grant would run under.
# - `chain` — the hops. Requiring them was what made the first attempt a
#   dream-authoring tool; producing them is the dreamer's job, not chat's.
#
# A consideration carrying any of those would be a dream wearing a different
# noun, so `tests/test_mcp_server.py` asserts the field sets do not overlap —
# the same blunt net `tests/test_dreaming.py` throws over `Dream` and
# `OrderProposal`.
#
# **It is written to the AUDIT LOG, not to `data/dreams.db`.** That is the
# containment, and it is structural rather than careful: nothing in
# `dreaming.py` reads the audit log, so there is no code path that turns one of
# these into a row on a shelf. It is also the right shape on its own terms —
# append-only, never migrated, tolerant of a torn final line, already backed up
# by `deploy/backup-journal.sh`, and already indexed into `insight.py`'s
# `events` table, so `query_history` can answer "what has chat put up lately"
# across the whole history rather than a window.
#
# What survives from the first attempt, because all of it applies to a note as
# much as to a dream: `why_now` in the agent's own words, `prompted_by` as the
# operator's verbatim phrasing, `origin` recording `chat:grogu` or `chat:yoda`,
# and a small daily cap. `prompt_echo` survives too — arithmetic over words,
# REPORTED and never enforced, the same posture as `Verification` counting
# `checked` flags rather than asking a model how well sourced it feels. A guard
# on that number could be walked around by rewording, and the walk-around would
# leave the record looking clean; a record that tells on itself cannot be.

# Which chat agents may put something up. Both, because the operator's reason
# for wanting this was "two brains are better than one" — and deliberately not
# `armorer`, whose whole job is arguing about `config/rules.yaml`.
#
# An unrecognised speaker is REFUSED rather than defaulted, which inverts the
# rule `bot.web.app.chat` applies to `load_soul`, and the inversion is about
# what the field is for in each place. There, getting it wrong costs the wrong
# voice on one answer. Here it is the whole attribution on a note the dreamer
# will read later, and a misattributed record is worse than a refused one.
CHAT_RAISERS: frozenset[str] = frozenset({GROGU, YODA})

# The surface half of `origin`. A dreamt dream carries whatever the model said
# the spark was; a consideration carries `chat:<soul>`, so a reader can always
# tell a thing said in conversation from a thing the dreamer worked out.
CHAT_ORIGIN_PREFIX = "chat:"

# The audit `kind` these are stored under. One string, named once, because it is
# what every reader — this module, `query_history`, and anything that later
# renders these on the Dreaming page — has to agree on.
CONSIDERATION_EVENT = "chat_consideration"

# How many may be put up in one UTC day. Small on purpose.
#
# Counted across BOTH agents rather than per speaker, because the scarce thing
# is the dreamer's attention rather than either agent's quota — a per-speaker
# cap would simply be six.
MAX_CONSIDERATIONS_PER_DAY = 3

# How many audit events to scan when counting today's, and how many days of
# dated files to open. Counted from the log rather than held as a counter, so
# the figure cannot drift out of step with what is actually on file.
_CONSIDERATION_SCAN = 400

# Words too common to say anything about whether a spark is the operator's
# sentence given back. Deliberately short: a longer list starts encoding a view
# about which words are meaningful, and the ratio is only ever an observation.
_ECHO_STOPWORDS = frozenset(
    {
        "the", "and", "but", "for", "are", "was", "were", "have", "has", "had",
        "what", "about", "with", "that", "this", "from", "into", "your", "you",
        "its", "can", "could", "would", "should", "any", "some", "them", "they",
        "there", "then", "how", "why", "who", "when", "where", "which", "does",
        "did", "not", "out", "get", "got", "one", "two", "all", "more", "most",
        "much", "very", "just", "like", "look", "see", "think", "thoughts",
    }
)


class RaiseRefusal(StrEnum):
    """Why a consideration was not recorded. Machine-readable, like `MoveRefusal`.

    Every one is an ordinary answer rather than an error, and they are collected
    rather than short-circuited — the property `RiskGate` and `DreamStore` both
    have, for the same reason: an agent told one thing wrong at a time asks
    repeatedly, and an unattended surface must not encourage that.
    """

    UNKNOWN_SPEAKER = "unknown_speaker"
    NEEDS_SPARK = "needs_spark"
    NEEDS_WHY_NOW = "needs_why_now"
    DAILY_CAP_REACHED = "daily_cap_reached"


# Every field a consideration has. Named as a constant rather than left implicit
# in a dict literal so the containment test has something to assert against:
# none of these may be a field of `Dream` that can become a permission. See the
# block comment above, and `test_a_consideration_shares_no_field_with_a_grant`.
CONSIDERATION_FIELDS: frozenset[str] = frozenset(
    {"origin", "speaker", "spark", "why_now", "prompted_by", "prompt_echo", "at"}
)


def _content_words(text: str) -> set[str]:
    """Words worth comparing. Lowercased, punctuation dropped, stopwords removed."""
    return {
        word
        for word in re.findall(r"[a-z0-9]+", text.lower())
        if len(word) >= 3 and word not in _ECHO_STOPWORDS
    }


def _prompt_echo(prompted_by: str, spark: str) -> float | None:
    """What share of the operator's own words came straight back in the spark.

    Arithmetic, reported and never enforced. A consideration whose spark is
    nearly the operator's own sentence is one nobody actually had, and the
    operator was explicit that this should be VISIBLE rather than prevented: a
    guard on a number can be walked around by rewording, and the walk-around
    would leave the record looking clean.

    `None` when nothing was quoted, never `0.0`. An unprompted note has no ratio
    rather than a perfect one, which is the `has_cycles` rule again: the
    flattering reading must not be what an absence of evidence looks like.
    """
    prompt = _content_words(prompted_by)
    if not prompt:
        return None
    return round(len(prompt & _content_words(spark)) / len(prompt), 2)


def _utc_day(moment: datetime) -> str:
    """The UTC calendar day of a moment, as `YYYY-MM-DD`.

    A naive timestamp is read as UTC rather than raising. Everything here
    reasons in UTC, and a `TypeError` out of a date comparison would take the
    tool down over a line somebody hand-edited.
    """
    aware = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return aware.astimezone(UTC).date().isoformat()


def _considerations(days: int) -> tuple[list[dict[str, Any]], bool]:
    """Every consideration on file across `days`, newest first, and whether the
    read was degraded.

    Returned as plain rows plus the degraded flag rather than as a bare list,
    because a log that could not be fully read produces the same empty list as a
    quiet week — the `FinnhubCalendar.is_degraded` lesson, and the reason
    `list_considerations` reports `record_is_incomplete` beside the count.
    """
    view = _session.audit.read(limit=_CONSIDERATION_SCAN, days=max(1, days))
    rows = [
        dict(event.payload, at=event.timestamp.isoformat(timespec="minutes"))
        for event in view.events
        if event.kind == CONSIDERATION_EVENT
    ]
    return rows, view.is_degraded


def _considerations_today(now: datetime) -> list[dict[str, Any]]:
    """Today's, for the cap. Counted off the log, never off a counter."""
    today = _utc_day(now)
    rows, _ = _considerations(days=2)
    return [r for r in rows if str(r.get("at", ""))[:10] == today]


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
        # Three separate questions about the same link, and collapsing any two
        # of them produces a plausible wrong answer.
        #
        # `awaits_settlement` is False when every hop is checked — there is no
        # link waiting on anything, which is a good state and not a gap.
        # `weakest_hop_resolved` False WHILE awaiting settlement is the
        # different case: the model named a hop number out of range, or prose
        # matching no claim, so which link is weakest was never established.
        # That is deliberately not clamped upstream, so it must not be reported
        # as "no weak link" here either — same missing-versus-absent rule as
        # `calendar_degraded` and `stops_unchecked`.
        #
        # `weakest_hop_pinned` is what actually decides whether a kept dream
        # can leave the workbench: a condition has to claim to settle that hop.
        # A reader given only `all_conditions_met` cannot tell a dream that is
        # one grading away from the vault from one that will never promote.
        "weakest_hop_index": dream.weakest_hop_index,
        "weakest_hop_resolved": dream.resolved_weakest_hop is not None,
        "awaits_settlement": dream.awaits_settlement,
        "weakest_hop_pinned": any(
            condition.settles(dream.resolved_weakest_hop)
            for condition in dream.conditions
        ),
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
                # The checkable half. `null` means no THRESHOLD, which is
                # "code cannot grade this" and never "did not hold" — and no
                # longer implies nothing can settle it at all, since an
                # observation is settled by a person instead.
                "trigger": (t.render() if (t := c.as_trigger()) is not None else None),
                "is_checkable": c.is_checkable,
                # The other shape of pre-registration, and the fields a caller
                # needs to say WHAT somebody has to go and look at. Reporting
                # only `is_checkable` left an agent describing an
                # operator-settled claim as unsettleable prose.
                "is_observable": c.is_observable,
                "subject": c.subject or None,
                "observable": c.observable or None,
                "observe_by": (
                    c.observe_by.isoformat(timespec="minutes") if c.observe_by else None
                ),
                # Five states, because `fulfilled` alone cannot tell RULED_OUT
                # from nobody having looked, or an elapsed review date from a
                # claim with no way to settle it at all.
                "state": c.state(now).value,
                "fulfilled": c.fulfilled,
                # An answer, not a failure to answer. A caller shown only
                # `fulfilled: false` reads a refuted claim as an open one.
                "ruled_out": c.ruled_out,
                "answered_by": c.observed_by or None,
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
    audit_failure = _record_event(
        "mcp_adopt_dream",
        {
            "dream_id": dream_id,
            "ok": result.ok,
            "refusals": [str(r) for r in result.refusals],
            "symbols": list(symbols) if symbols else None,
            "asset_class": asset_class,
        },
    )
    payload["audit_not_recorded"] = audit_failure
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
    payload["audit_not_recorded"] = _record_event(
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
def raise_consideration(
    speaker: str,
    spark: str,
    why_now: str,
    prompted_by: str = "",
) -> dict[str, Any]:
    """Put something to the dreamer for consideration. It is a note, not a dream.

    **This does not create a dream and cannot.** It writes one line to the audit
    log saying: here is a spark, here is why now, and here is what the operator
    actually said. The dreamer reads it on its own run, on its own timer, and
    decides for itself whether any of it becomes a seed — **it may ignore it
    entirely**, and that is the point. You can point at something; the thing
    that dreams still chooses.

    The reason this is a note rather than a dream is worth knowing, because it
    tells you what to write. A dream is the first link of a chain that ends in a
    live trading permission — dream, prophecy, vault, adopted, and then a symbol
    the risk gate will admit that `config/rules.yaml` never listed. A
    conversation must not be able to insert at the top of that chain, so this
    carries no symbols, no instrument class and no causal hops. Do not try to
    smuggle a ticker into the prose; it grants nothing and it makes the note
    read as a recommendation, which is exactly what it is not.

    **Put one up when you have a LINK the operator does not.** A topic they just
    named is a subject, not a consideration. If what you would write is their
    sentence with a noun swapped, you have nothing to put up.

    **"I have nothing worth passing on here" is a good answer**, and it is the
    right one most of the time. Nothing counts these as productivity. One raised
    to be agreeable is worse than none, because the dreamer reads them and its
    attention is the scarce thing.

    `why_now` and `prompted_by` are stored side by side on purpose. Do not
    paraphrase, tidy or summarise the quote — a reader puts the operator's own
    words beside your spark and decides for themselves whether anything was
    added, and a smoothed quote takes that judgement away from them. Leave it
    empty only when nobody prompted you.

    Refused, with every reason at once, when the speaker is not a chat agent,
    when the spark or why_now is blank, or when three have already gone up
    today. A refusal is an ordinary answer and asking again will not change it.

    Read them back with list_considerations, or across the whole history with
    query_history: `SELECT ts, payload FROM events WHERE kind =
    'chat_consideration' ORDER BY ts DESC`.

    Args:
        speaker: Which agent is putting it up — "grogu" or "yoda". Recorded as
            `chat:<speaker>`; do not sign it as the other one.
        spark: The thought itself, in a sentence or two. Not a recommendation
            and not a trade — a place you think it is worth the dreamer looking.
        why_now: What prompted this, in your own words: what you noticed, and
            why it is worth the dreamer's attention today rather than at some
            point.
        prompted_by: The operator's own words, verbatim, when something they
            said led to this. Empty only when nothing did.
    """
    now = datetime.now(UTC)
    who = speaker.strip().lower()
    clean_spark = spark.strip()
    clean_why = why_now.strip()
    quote = prompted_by.strip()

    refusals: list[RaiseRefusal] = []
    if who not in CHAT_RAISERS:
        refusals.append(RaiseRefusal.UNKNOWN_SPEAKER)
    if not clean_spark:
        refusals.append(RaiseRefusal.NEEDS_SPARK)
    if not clean_why:
        refusals.append(RaiseRefusal.NEEDS_WHY_NOW)

    already = _considerations_today(now)
    if len(already) >= MAX_CONSIDERATIONS_PER_DAY:
        refusals.append(RaiseRefusal.DAILY_CAP_REACHED)

    origin = f"{CHAT_ORIGIN_PREFIX}{who}"
    echo = _prompt_echo(quote, clean_spark)
    payload: dict[str, Any] = {
        "recorded": not refusals,
        "refusals": [str(r) for r in refusals],
        "raised_today": len(already),
        "daily_cap": MAX_CONSIDERATIONS_PER_DAY,
    }

    if refusals:
        payload["note"] = (
            "Refused by deterministic code, not by a judgement call. Fix what it "
            "names or let the thought go; asking again will not change the "
            "answer. Nothing was written."
        )
        # Recorded even when nothing was put up, for the reason
        # `mcp_adopt_dream` records its refusals: how often this surface tries
        # and how often it is prompted is a fact about the surface, and a log
        # that only kept the successes could not answer it.
        payload["audit_not_recorded"] = _record_event(
            "chat_consideration_refused",
            {"speaker": who, "origin": origin, "refusals": payload["refusals"]},
        )
        return payload

    # The record itself. Deliberately these fields and no others — see
    # `CONSIDERATION_FIELDS` and the block comment above: no symbols, no
    # instrument class, no chain. Written to the audit log rather than to
    # `data/dreams.db`, so nothing in `dreaming.py` can read it as a shelf row.
    audit_failure = _record_event(
        CONSIDERATION_EVENT,
        {
            "origin": origin,
            "speaker": who,
            "spark": clean_spark,
            "why_now": clean_why,
            "prompted_by": quote,
            "prompt_echo": echo,
        },
    )
    # Unlike every other caller of `_record_event`, this one is not reporting a
    # completed action whose record went missing. The audit line IS the
    # consideration — nothing else is written anywhere — so a failed write means
    # nothing was raised, and `recorded` must go back to False rather than
    # claiming a note the dreamer will never read.
    if audit_failure is not None:
        payload["recorded"] = False
        payload["audit_not_recorded"] = audit_failure
        payload["note"] = (
            "NOTHING WAS RAISED. The consideration is written to the audit log "
            "and nowhere else, and that write failed: " + audit_failure
        )
        return payload

    payload.update(
        {
            "origin": origin,
            "speaker": who,
            "spark": clean_spark,
            "why_now": clean_why,
            "prompted_by": quote,
            "prompt_echo": echo,
            "raised_today": len(already) + 1,
            "at": now.isoformat(timespec="minutes"),
            "note": (
                "Recorded as a consideration, not as a dream. Nothing is on a "
                "shelf, nothing is offered and no symbol is permitted by this. "
                "The dreamer reads it on its own run and may ignore it. Say that "
                "plainly rather than telling the operator a dream now exists — "
                "list_dreams is what would show one, and it will not show this. "
                "prompt_echo is arithmetic on how much of the operator's own "
                "wording came back in the spark; it is reported for a reader to "
                "judge, never enforced."
            ),
        }
    )
    return payload


@server.tool()
def list_considerations(days: int = 14, limit: int = 20) -> dict[str, Any]:
    """What the chat surface has put to the dreamer lately, newest first.

    These are notes awaiting the dreamer, **not dreams**. None of them is on a
    shelf, none is offered to the trading agent and none permits a symbol. A
    consideration that has been picked up looks identical here to one that has
    not — the dreamer's own run is what turns any of this into a seed, and it is
    free to ignore every one.

    Each row carries `prompted_by`, the operator's own words verbatim when there
    were any, beside the agent's `spark`. Read the two together: one whose spark
    is the operator's sentence given back is one nobody actually had.
    `prompt_echo` is the arithmetic on that — reported, never enforced, and
    `null` rather than zero when nobody prompted it.

    An empty list is an ordinary state and says nothing on its own. Check
    `record_is_incomplete` first: a log that could not be fully read produces
    exactly the same empty list, and reporting that as a quiet week is the
    failure this tool has to avoid.

    Args:
        days: How many dated log files back to read (default 14).
        limit: Maximum rows returned (default 20).
    """
    rows, degraded = _considerations(days=days)
    now = datetime.now(UTC)
    shown = rows[: max(1, limit)]
    return {
        "days_read": max(1, days),
        "found": len(rows),
        "returned": len(shown),
        "raised_today": len(_considerations_today(now)),
        "daily_cap": MAX_CONSIDERATIONS_PER_DAY,
        "considerations": shown,
        # Say the record is incomplete rather than imply nothing happened.
        "record_is_incomplete": degraded,
        "note": (
            "Notes put to the dreamer, never dreams. Nothing here is on a shelf, "
            "offered, or permitted, and the dreamer may ignore any of it. An "
            "empty list means nothing was put up, which is the ordinary state — "
            "but it means nothing at all if record_is_incomplete is true."
        ),
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
