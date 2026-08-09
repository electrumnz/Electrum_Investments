"""Operator dashboard for the trading journal.

Binds to `127.0.0.1`. Two supported ways to reach it:

- **Loopback plus Tailscale**, the original arrangement. Nothing is published,
  so there is nobody to authenticate and `DASHBOARD_PASSWORD` stays unset.
- **Exposed on a public URL with `DASHBOARD_PASSWORD` set.** Every route then
  requires a session; see `src/bot/web/auth.py` for what that gate is and is
  not.

The second is a deliberate operator decision, taken on the basis that the
account behind it is Alpaca **paper** money and nothing here can lose funds.
The earlier note in this file said publishing needed real authentication built
first rather than bolted on after — that is `auth.py`, and it is the
prerequisite being met, not waived.

**Exposing it without `DASHBOARD_PASSWORD` set is the thing to avoid**, because
the app cannot detect it: a reverse proxy or a Tailscale Funnel still arrives
on loopback, so from in here a public request and a local one look identical.
`electrum-bot-web` warns loudly at startup when no password is configured.

Read-only by design. It reports what happened; the risk gate and
`config/rules.yaml` decide what may happen, and neither is reachable from here.
`POST /chat` keeps its own separate token on top of the password, because
viewing an account and driving an agent that can reach the broker are different
privileges and should not be granted by one secret.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from typing import Any

# Module level, not inside `build_app`, and that matters rather than being
# tidiness. `from __future__ import annotations` turns every annotation into a
# string, and FastAPI resolves them against the MODULE globals. With `Request`
# imported inside the function it is unresolvable, so FastAPI falls back to
# treating `request: Request` as a query parameter and every such route answers
# 422 "field required". Nothing warns; the route simply stops working.
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ..audit import AuditLog
from ..config import Env, Rules, load_rules
from ..journal import Journal
from ..metrics import build_report
from ..models import AccountSnapshot, WorkingOrder
from ..options import alerts_for_positions
from ..tailnet import read as read_tailnet_status
from . import render
from .auth import COOKIE_NAME, SESSION_TTL_SECONDS, SessionStore
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
    app = FastAPI(title="Mudhorn Capital", docs_url=None, redoc_url=None)
    bridge = HermesBridge()

    resolved_env = env or Env()
    resolved_env.assert_paper_only()
    sessions = SessionStore(password=resolved_env.dashboard_password)

    # A middleware rather than a per-route dependency, deliberately. A
    # dependency is opt-in, so the failure mode is a route added later that
    # nobody remembered to decorate — and that route serves the account to
    # anyone. This way a new route is protected by default and exposing one
    # takes a deliberate edit to the allowlist below.
    OPEN_PATHS = frozenset({"/login", "/healthz"})

    @app.middleware("http")
    async def require_session(request: Request, call_next: Any) -> Any:
        if not sessions.required or request.url.path in OPEN_PATHS:
            return await call_next(request)
        if sessions.is_valid(request.cookies.get(COOKIE_NAME)):
            return await call_next(request)
        # A browser gets the form; anything else gets a status code it can act
        # on. Redirecting an API client to HTML would show up as a confusing
        # 200 full of markup.
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"error": "authentication required"}, status_code=401)
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
        return render.shell(
            title, active, body, env=resolved_env, exposed=sessions.required
        )

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> Any:
        if not sessions.required:
            return RedirectResponse("/", status_code=303)
        if sessions.is_valid(request.cookies.get(COOKIE_NAME)):
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(render.login_page(env=resolved_env))

    @app.post("/login", response_class=HTMLResponse)
    async def login(request: Request) -> Any:
        if not sessions.required:
            return RedirectResponse("/", status_code=303)

        client = request.client.host if request.client else "unknown"
        if sessions.throttled(client):
            # 429 rather than "wrong password", so a guesser cannot use the
            # response to tell a locked-out attempt from a failed one.
            return HTMLResponse(
                render.login_page(
                    env=resolved_env,
                    error="Too many attempts. Wait five minutes and try again.",
                ),
                status_code=429,
            )

        form = await request.form()
        if not sessions.check_password(str(form.get("password", ""))):
            sessions.record_failure(client)
            return HTMLResponse(
                render.login_page(env=resolved_env, error="Incorrect password."),
                status_code=401,
            )

        sessions.clear_attempts(client)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            sessions.issue(),
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            # Set only when the request arrived over HTTPS, so the cookie still
            # works for a loopback test over plain HTTP. A public deployment is
            # behind TLS, so in the case that matters this is on.
            secure=request.url.scheme == "https",
        )
        return response

    @app.get("/logout")
    def logout(request: Request) -> Response:
        sessions.logout(request.cookies.get(COOKIE_NAME))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME)
        return response

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
            resolved_journal.get_stand_down(),
            alerts,
            untracked,
            stale=stale,
            # `None` when the check has never run, which renders nothing rather
            # than a false all-clear. A box without the timer installed should
            # not be told its link is healthy.
            tailnet=read_tailnet_status(),
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

    # Said at startup because the app cannot work it out later. A reverse proxy
    # or a Tailscale Funnel forwards to loopback, so from inside the process a
    # request from the internet and one from the same machine are identical.
    # The only moment anyone can be told is now.
    if Env().dashboard_password:
        print(
            "Dashboard password is SET: every page requires a login, and "
            "/chat needs DASHBOARD_CHAT_TOKEN on top of it."
        )
    else:
        print(
            "Dashboard password is NOT set: there is no login. That is correct "
            "for 127.0.0.1 plus Tailscale, and wrong for anything reachable "
            "from the internet. Set DASHBOARD_PASSWORD before exposing this."
        )

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"WARNING: binding to {args.host} exposes this dashboard beyond the "
            f"local machine. Prefer 127.0.0.1 plus Tailscale, or a Funnel with "
            f"DASHBOARD_PASSWORD set."
        )

    uvicorn.run(build_app(force_mock=args.mock), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
