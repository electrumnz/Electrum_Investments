"""Local dashboard for the trading journal.

Binds to `127.0.0.1` by default and has no login, because on a private
interface there is nobody to authenticate. To reach it from a phone, put the
machine on a [Tailscale](https://tailscale.com/) network and browse to its
private address: the page stays unpublished and no auth surface is created.

**Do not simply expose this on a public URL.** The reason there is no login is
that nothing is published. Publishing it without building real authentication
first would put a view of a brokerage account on the open internet.

Read-only by design. It reports what happened; the risk gate and
`config/rules.yaml` decide what may happen, and neither is reachable from here.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from typing import Any

from ..audit import AuditLog
from ..config import Env, Rules, load_rules
from ..journal import Journal
from ..metrics import build_report
from ..models import AccountSnapshot, WorkingOrder
from ..options import alerts_for_positions
from . import render
from .chat import HermesBridge


def build_app(
    *,
    journal: Journal | None = None,
    rules: Rules | None = None,
    env: Env | None = None,
    audit_log: AuditLog | None = None,
    force_mock: bool = False,
) -> Any:
    """Construct the FastAPI app. Dependencies are injectable so tests never
    touch a real journal or broker."""
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse

    app = FastAPI(title="Mudhorn Capital", docs_url=None, redoc_url=None)
    bridge = HermesBridge()

    resolved_env = env or Env()
    resolved_env.assert_paper_only()
    resolved_rules = rules or load_rules()
    resolved_journal = journal or Journal()
    audit = audit_log or AuditLog()

    def _account_orders_prices() -> tuple[AccountSnapshot, list[WorkingOrder], dict[str, float]]:
        """One broker session for everything the Board needs.

        Open risk is filled in from the journal, as on every other path that
        hands out a snapshot: the broker holds stop-losses as separate orders
        and cannot report it, so a snapshot that skipped this would understate
        it silently.

        Opening a second connection per widget would triple the round trips on
        a page refreshed by hand. Orders and quotes degrade to empty rather than
        failing the page: a broker hiccup should cost the pending list, not the
        equity figure next to it.
        """
        from ..main import build_broker

        broker = build_broker(resolved_env, force_mock=force_mock)
        broker.connect()
        try:
            snapshot = broker.get_account()
            snapshot.open_risk_usd = resolved_journal.open_risk_usd()
            try:
                orders = broker.get_open_orders()
            except Exception:
                orders = []
            prices: dict[str, float] = {}
            for symbol in {o.symbol for o in orders}:
                try:
                    prices[symbol] = broker.get_tick(symbol).mid
                except Exception:
                    continue
            return snapshot, orders, prices
        finally:
            broker.disconnect()

    def _page(title: str, active: str, body: str) -> str:
        return render.shell(title, active, body, env=resolved_env)

    @app.get("/", response_class=HTMLResponse)
    def board() -> str:
        account, orders, prices = _account_orders_prices()
        open_trades = resolved_journal.open_trades()

        journalled = {t.symbol for t in open_trades}
        held = {p.symbol for p in account.open_positions}
        untracked = sorted(held - journalled)
        stale = sorted(journalled - held)

        alerts = alerts_for_positions(
            [(p.symbol, p.qty) for p in account.open_positions],
            now=datetime.now(UTC),
            warn_days=resolved_rules.options.warn_days_before_expiry,
            buying_power_usd=account.buying_power_usd,
        )

        body = render.banners(
            resolved_journal.get_stand_down(), alerts, untracked, stale=stale
        ) + render.board(
            account,
            resolved_rules,
            resolved_journal.equity_curve(),
            open_trades,
            resolved_journal.get_stand_down(),
            resolved_journal.consecutive_losses(
                resolved_rules.stand_down.loss_threshold_r
            ),
            orders,
            prices,
        )
        return _page("Board", "/", body)

    @app.get("/decisions", response_class=HTMLResponse)
    def decisions() -> str:
        return _page("Decisions", "/decisions", render.decisions(audit.read(limit=60)))

    @app.get("/trades", response_class=HTMLResponse)
    def trades() -> str:
        closed = resolved_journal.closed_trades()
        return _page(
            "Trades", "/trades", render.trades_page(closed[-100:], build_report(closed))
        )

    @app.get("/analytics", response_class=HTMLResponse)
    def analytics() -> str:
        report = build_report(resolved_journal.closed_trades())
        return _page("Analytics", "/analytics", render.analytics_page(report))

    @app.get("/settings", response_class=HTMLResponse)
    def settings() -> str:
        return _page(
            "Settings",
            "/settings",
            render.settings_page(
                resolved_rules,
                resolved_env,
                chat_enabled=bool(resolved_env.dashboard_chat_token),
            ),
        )

    @app.get("/chat", response_class=HTMLResponse)
    def chat_view() -> str:
        return _page(
            "Chat",
            "/chat",
            render.chat_page(
                enabled=bool(resolved_env.dashboard_chat_token),
                token=resolved_env.dashboard_chat_token,
                hermes_available=bridge.available,
            ),
        )

    @app.post("/chat", response_class=JSONResponse)
    def chat(payload: dict[str, Any]) -> dict[str, Any]:
        """Ask Hermes a question. The one non-GET route in this application.

        The dashboard was read-only and a test enforced it. That changed
        deliberately, not incidentally, so the reasoning belongs here:

        Rendering equity is safe to expose because the worst case is disclosure.
        Driving an agent is not — the worst case is action. The page is still
        loopback-bound and still reached over Tailscale, so nothing new is
        internet-facing, but the margin is thinner and the mitigations are
        correspondingly explicit:

        - chat is **off** unless `DASHBOARD_CHAT_TOKEN` is set, so a deploy never
          switches it on by itself
        - Hermes runs as a different, unprivileged user; this process holding the
          broker credentials does not lend them to the agent
        - every tool the agent can reach still runs `RiskGate.evaluate` first

        The token is embedded in the page, so it does not defend against someone
        who can already load the dashboard — they could POST regardless. What it
        buys is that enabling this is a decision, and that a device which never
        loaded the page cannot drive the agent blind.

        Takes the body as a plain `dict` rather than the more obvious
        `request: Request`. This module imports FastAPI lazily inside
        `build_app` so that importing it does not require FastAPI installed, and
        `from __future__ import annotations` turns every annotation into a
        string that FastAPI resolves against *module* globals. A function-local
        `Request` is therefore invisible to it, and the parameter is silently
        treated as a query field — every POST 422s with
        `{"loc": ["query", "request"]}`. `dict[str, Any]` resolves from module
        scope and means the same thing here.
        """
        if not resolved_env.dashboard_chat_token:
            raise HTTPException(status_code=404, detail="chat is not enabled")

        if payload.get("token") != resolved_env.dashboard_chat_token:
            raise HTTPException(status_code=403, detail="bad token")

        history = [
            (str(t.get("user", "")), str(t.get("agent", "")))
            for t in payload.get("history") or []
        ]
        reply = bridge.ask(str(payload.get("message", "")), history)
        return {"ok": reply.ok, "text": reply.text, "error": reply.error}

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "trades": len(resolved_journal.closed_trades())}

    return app


def main() -> int:
    """Entry point for `electrum-bot-web`."""
    parser = argparse.ArgumentParser(prog="electrum-bot-web")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address. Leave as 127.0.0.1 and use Tailscale for remote "
        "access rather than binding to 0.0.0.0 on a public box.",
    )
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--mock", action="store_true", help="Use the MockBroker")
    args = parser.parse_args()

    import uvicorn

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"WARNING: binding to {args.host} exposes this dashboard beyond the "
            f"local machine. There is no login. Prefer 127.0.0.1 plus Tailscale."
        )

    uvicorn.run(build_app(force_mock=args.mock), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
