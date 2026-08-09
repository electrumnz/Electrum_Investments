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
from pathlib import Path
from typing import Any

from ..config import DEFAULT_RULES_PATH, Env, Rules, load_rules
from ..journal import Journal
from ..metrics import build_report
from ..models import AccountSnapshot
from ..options import alerts_for_positions
from . import render


def build_app(
    *,
    journal: Journal | None = None,
    rules: Rules | None = None,
    env: Env | None = None,
    force_mock: bool = False,
) -> Any:
    """Construct the FastAPI app. Dependencies are injectable so tests never
    touch a real journal or broker."""
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse

    app = FastAPI(title="Mudhorn Capital", docs_url=None, redoc_url=None)

    resolved_env = env or Env()
    resolved_env.assert_paper_only()
    resolved_rules = rules or load_rules()
    resolved_journal = journal or Journal()

    def _account() -> AccountSnapshot:
        """Broker state with open risk filled in from the journal.

        Same rule as everywhere else: the broker cannot report open risk, so a
        snapshot that skipped this would understate it silently.
        """
        from ..main import build_broker

        broker = build_broker(resolved_env, force_mock=force_mock)
        broker.connect()
        try:
            snapshot = broker.get_account()
            snapshot.open_risk_usd = resolved_journal.open_risk_usd()
            return snapshot
        finally:
            broker.disconnect()

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        account = _account()
        closed = resolved_journal.closed_trades()
        report = build_report(closed)

        journalled = {t.symbol for t in resolved_journal.open_trades()}
        untracked = sorted(
            p.symbol for p in account.open_positions if p.symbol not in journalled
        )

        alerts = alerts_for_positions(
            [(p.symbol, p.qty) for p in account.open_positions],
            now=datetime.now(UTC),
            warn_days=resolved_rules.options.warn_days_before_expiry,
            buying_power_usd=account.buying_power_usd,
        )

        body = (
            '<section class="wrap reveal in" style="padding-top:2rem;border-bottom:0">'
            + render.banners(
                resolved_journal.get_stand_down(), alerts, account, untracked
            )
            + "</section>"
            + render.overview(
                account,
                report,
                resolved_journal.equity_curve(),
                resolved_rules.account.max_total_risk_pct,
            )
            + render.analytics(report)
            + render.trades(closed[-50:])
            + render.rules_view(_rules_text())
        )
        return render.page("Dashboard", body)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "trades": len(resolved_journal.closed_trades())}

    return app


def _rules_text(path: Path = DEFAULT_RULES_PATH) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        return f"could not read {path}: {e}"


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
