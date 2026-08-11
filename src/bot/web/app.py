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
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

# Module level, not inside `build_app`, and that matters rather than being
# tidiness. `from __future__ import annotations` turns every annotation into a
# string, and FastAPI resolves them against the MODULE globals. With `Request`
# imported inside the function it is unresolvable, so FastAPI falls back to
# treating `request: Request` as a query parameter and every such route answers
# 422 "field required". Nothing warns; the route simply stops working.
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .. import news_history
from ..audit import AuditLog
from ..config import DEFAULT_RULES_PATH, Env, Rules, load_rules
from ..dreaming import (
    Adoption,
    ConferenceDecision,
    DreamMessage,
    DreamStore,
    DreamSummary,
)
from ..journal import Journal
from ..market_clock import MarketState, market_state
from ..metrics import build_report
from ..models import AccountSnapshot, WorkingOrder
from ..options import alerts_for_positions
from ..position_actions import detect_unexplained_moves
from ..settings_agent import (
    DEFAULT_REQUESTS_PATH,
    DirectApplier,
    SettingsRequestStore,
    UnknownLimit,
    WrapperApplier,
    commit_message,
    decide,
    limits_for,
    preview,
    render_briefing,
)
from ..souls import ARMORER, GROGU, KNOWN_SOULS, YODA, load_soul
from ..tailnet import read as read_tailnet_status
from . import live, render, seen
from .auth import COOKIE_NAME, SESSION_TTL_SECONDS, SessionStore
from .chat import DREAMER_BINARY, HermesBridge

#: How many cycles the Decisions reader pulls behind one page of the trail.
#:
#: Wider than `render.DECISIONS_PER_PAGE` on purpose, so paging back is a slice
#: of something already read rather than a second walk of the log. The reader
#: stops as soon as it has this many and takes the newest dated file first, so
#: the cost is bounded by this number and not by how much history exists.
DECISION_WINDOW = 200

#: How many cycles Settings reads to describe the X feed's recent state.
#:
#: The loop wakes 96 times a day, so this covers `render.NEWS_WINDOW_HOURS` with
#: room for a faster `DECISION_INTERVAL_SECONDS`. The recall stops at the window
#: boundary regardless, so a generous number costs a parse of files already on
#: disk and buys the guarantee that the window is not silently short — which is
#: the failure mode that matters here, because a truncated read would report
#: "no failed fetch" over a span it never looked at.
NEWS_WINDOW_CYCLES = 400

#: How many conference verdicts the cross-dream feed shows.
#:
#: The two agents confer once a day, so thirty is about a month of exchanges —
#: long enough to see a pattern, short enough that the page does not become a
#: log. It bounds the FEED only: each card's own verdict is looked up per dream,
#: so a dream whose last exchange predates this window still renders it rather
#: than reading as never conferred.
CONFERENCE_FEED_ROWS = 30


def build_app(
    *,
    journal: Journal | None = None,
    rules: Rules | None = None,
    env: Env | None = None,
    audit_log: AuditLog | None = None,
    dreams: DreamStore | None = None,
    poller: live.LivePoller | None = None,
    settings_requests: SettingsRequestStore | None = None,
    # How a change actually reaches `config/rules.yaml`. `None` means the
    # production arrangement: `deploy/apply-settings.sh` through sudo, which is
    # absent on a laptop and on a box where the grant was never installed — so
    # the default degrades to "recorded, not applied" and says so, rather than
    # to a write this process could not make anyway.
    #
    # Injectable so the suite can prove both halves. A test that depended on
    # /opt/mudhorn not existing would pass for a reason that has nothing to do
    # with the code, and would start applying changes the day somebody ran the
    # suite on the droplet.
    settings_applier: WrapperApplier | DirectApplier | None = None,
    rules_path: Path | None = None,
    force_mock: bool = False,
) -> Any:
    """Construct the FastAPI app. Dependencies are injectable so tests never
    touch a real journal or broker."""
    app = FastAPI(title="Mudhorn Capital", docs_url=None, redoc_url=None)
    bridge = HermesBridge()
    # The dreamer's own instance when one is installed, so a speculative agent
    # has no broker tool at all rather than one it was asked not to use. See
    # DREAMER_BINARY. Absent, `/dreaming` falls back to `bridge` and the page
    # says which it is rather than claiming the stronger arrangement.
    dreamer = HermesBridge(binary=DREAMER_BINARY)

    resolved_env = env or Env()
    resolved_env.assert_paper_only()
    sessions = SessionStore(password=resolved_env.dashboard_password)

    # A middleware rather than a per-route dependency, deliberately. A
    # dependency is opt-in, so the failure mode is a route added later that
    # nobody remembered to decorate — and that route serves the account to
    # anyone. This way a new route is protected by default and exposing one
    # takes a deliberate edit to the allowlist below.
    OPEN_PATHS = frozenset({"/login", "/healthz"})

    # Neither is a landing place: `/login` is the gate itself, and `/live` is
    # an endless event stream that a browser told to navigate to it would sit
    # on forever. Everything else this app serves over GET is fair game.
    NOT_A_DESTINATION = frozenset({"/login", "/logout", "/live"})

    def _safe_next(raw: str | None) -> str:
        """Where to send the operator after signing in, checked first.

        Signing in from a deep link used to land on the Board unconditionally,
        which is the normal path for a bookmark or a link somebody was sent.
        Carrying the requested path fixes that and introduces the classic
        vulnerability of a login form in the same move: `?next=https://evil`
        makes this sign-in page the front door to somebody else's, wearing the
        operator's URL and their trust in it.

        So the value is honoured only when it is one of THIS application's own
        GET routes. Enumerated from `app.routes` rather than listed by hand,
        for the reason `tests/test_auth.py` gives about `/live`: a
        hand-maintained list of paths fails silently on the one somebody
        forgot, and a new page should be reachable through a deep link the day
        it is added rather than the day somebody remembers this function.

        Anything unrecognised — an absolute URL, a protocol-relative `//host`,
        a traversal, a route that does not exist — falls back to the Board,
        which is where every sign-in used to land anyway. The failure mode is
        the old behaviour, not an error page.
        """
        if not raw or not raw.startswith("/") or raw.startswith("//"):
            return "/"
        path = raw.split("?", 1)[0].split("#", 1)[0]
        # `getattr` rather than attribute access: `app.routes` is typed as
        # `list[BaseRoute]`, and `path` and `methods` live on the subclasses.
        # The same shape `tests/test_auth.py` uses to enumerate them.
        known = {
            str(getattr(r, "path", ""))
            for r in app.routes
            if isinstance(getattr(r, "path", None), str)
            and "GET" in (getattr(r, "methods", None) or set())
            and "{" not in str(getattr(r, "path", ""))
        }
        return path if path in known - NOT_A_DESTINATION else "/"

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
            # The requested path rides along so the sign-in returns the
            # operator to what they asked for. `_safe_next` is what makes that
            # safe, and it runs again on the way out — the value is not
            # trusted merely because this code put it there, because a form
            # field is under the client's control between the two.
            wanted = _safe_next(request.url.path)
            target = "/login" if wanted == "/" else f"/login?next={quote(wanted)}"
            return RedirectResponse(target, status_code=303)
        return JSONResponse({"error": "authentication required"}, status_code=401)
    # The path comes first, because the loaded rules are read FROM it. The
    # settings agent computes a diff against the file on disk and compares the
    # located line to the loaded value, so the two disagreeing would produce a
    # request argued against one file and applied to another.
    resolved_rules_path = rules_path or DEFAULT_RULES_PATH
    resolved_rules = rules or load_rules(resolved_rules_path)
    resolved_journal = journal or Journal()
    audit = audit_log or AuditLog()
    resolved_dreams = dreams or DreamStore()

    # The settings agent's store, opened LAZILY and never at construction.
    #
    # Two reasons, and the second is the one that keeps the suite honest. The
    # forge is off unless `DASHBOARD_CHAT_TOKEN` is set, so a deployment that
    # never enables it has no requests and needs no database; and constructing
    # a store eagerly here is exactly how `DreamStore` once started writing to
    # the repository's own `data/` from every test that built an app.
    # `tests/conftest.py` fails the suite over that now, and this arrangement
    # means it never has to.
    held_requests: list[SettingsRequestStore] = (
        [settings_requests] if settings_requests is not None else []
    )

    def _requests() -> SettingsRequestStore:
        if not held_requests:
            held_requests.append(SettingsRequestStore(DEFAULT_REQUESTS_PATH))
        return held_requests[0]

    # Resolved once. `WrapperApplier` holds no connection and no state — it
    # stats a path and shells out — so constructing it here costs nothing and
    # keeps "which route writes the file" a single answer for the process
    # rather than one taken per request.
    resolved_applier = settings_applier or WrapperApplier()

    # One poller for the whole application. Polling per connected browser would
    # multiply broker load for nothing, and every page render would still be
    # paying a round trip.
    # Injectable like the journal and the dream store, so a test can prime it
    # with one synchronous `poll_once()` and assert on a rendered Board without
    # standing up an event loop.
    resolved_poller = poller or live.build_poller(
        journal=resolved_journal, env=resolved_env, force_mock=force_mock,
        rules=resolved_rules,
    )
    live.install(app, resolved_poller)

    def _account_orders_prices() -> (
        tuple[
            AccountSnapshot | None,
            list[WorkingOrder],
            dict[str, float],
            datetime | None,
            bool,
            bool,
        ]
    ):
        """What the Board renders, taken from the poller and NEVER blocking.

        This used to open a broker session inline, which meant a slow Alpaca
        held the whole page with nothing on screen — and after a hyperspace
        jump that promised speed, which made the wait read as a broken deck
        rather than a slow one.

        The poller owns the broker conversation now. This reads whatever it has
        and returns immediately, so a page render costs no network at all.

        **`None` on a cold start is the point, not a gap.** The first request
        after a restart has no reading yet, and the honest answer is that it is
        unknown. Rendering zeros would be a lie the page has no way to walk
        back, and the stream fills the figures in a moment later.

        Open risk still comes from the journal rather than the broker, inside
        the poller: stop-losses are separate orders and Alpaca cannot report
        the aggregate, so any path handing out an `AccountSnapshot` has to do
        this or the total-risk cap silently counts nothing.

        **When the reading was taken comes back with it, and so does whether
        it is too old to present as current.** The poller idle-stops once
        nobody is watching and keeps its last reading, so the first load of a
        morning is served an overnight snapshot. Handing back the figures
        without their age let the page stamp them `as at <now>` and assert
        expiries and untracked positions off them, all in the present tense.
        A reading is only as good as the moment it was taken, so the moment
        travels with it.

        **The last flag is `orders_unavailable`, and dropping it was the same
        mistake one field along.** `AlpacaBroker.get_open_orders` catches its
        own errors and returns `[]`, so an order book that could not be read
        arrives here indistinguishable from an account with nothing resting.
        Anything comparing the broker's stops against the journal's has to know
        which of the two it is looking at, or an outage renders as a clean
        comparison — `FinnhubCalendar.is_degraded` in a third place.
        """
        # NOT `ensure_running()`. That schedules an asyncio task and this runs
        # in FastAPI's threadpool, where there is no loop to schedule onto. The
        # stream starts the poller when a browser connects, which is also the
        # behaviour worth having: a box nobody is watching does not talk to the
        # broker every five seconds all night.
        snapshot = resolved_poller.latest()
        if snapshot is None:
            # A cold start has read nothing, so the order book is not "empty",
            # it is unknown. `True` here rather than `False` for the same reason
            # the account comes back as `None`.
            return None, [], {}, None, False, True
        return (
            snapshot.account,
            snapshot.orders,
            snapshot.prices,
            snapshot.taken_at,
            resolved_poller.reading_is_stale(),
            snapshot.orders_unavailable,
        )

    def _market_now() -> MarketState:
        """The venue's phase and the gate's window, resolved once.

        The tape and the Board's "what it will do right now" both need it, and
        both must get the SAME reading — two `market_state` calls a few
        milliseconds apart can straddle a boundary and put a strip saying the
        session opens in a moment above a card saying it has opened.
        """
        equities = resolved_rules.instruments.get("us_equity")
        return market_state(
            datetime.now(UTC),
            windows_by_day=equities.windows_by_day if equities else None,
        )

    def _tape(market: MarketState) -> str:
        """The ticker strip, on EVERY page rather than only the Board.

        That is a deliberate change to a property this module used to have: a
        session that never opened the Board never talked to Alpaca at all,
        because only the Board carried `data-live` targets and only a page with
        those opens the stream. The tape carries them too, so any page now
        keeps the poller warm.

        The trade is small and the reasoning is that the old property was about
        an UNATTENDED box, not an attended one. Somebody reading the Settings
        page is watching; the poller's idle stop is what protects the overnight
        case, and it still does. What has not changed is that a render costs no
        network — this reads the poller's last snapshot and returns.
        """
        watchlist = resolved_rules.watchlist
        if not watchlist.enabled or not watchlist.symbols:
            return ""
        snapshot = resolved_poller.latest()
        return render.ticker_tape(
            market,
            # No reading yet renders the symbols with no quote rather than an
            # empty strip: a cold start should look like a tape waiting for
            # prices, not like a watchlist nobody configured.
            snapshot.ticker if snapshot else [
                live.TickerQuote(
                    symbol=sym, tradeable=sym in resolved_rules.allowed_symbols
                )
                for sym in watchlist.symbols
            ],
            # The kinds, so the strip reads as a mix rather than as sixteen of
            # the same thing, and the calendar, so a holiday greys out instead
            # of showing yesterday's close at full strength.
            watchlist=watchlist,
            calendar=resolved_poller.calendar,
        )

    def _page(
        title: str, active: str, body: str, market: MarketState | None = None
    ) -> str:
        return render.shell(
            title, active, body, env=resolved_env, exposed=sessions.required,
            tape=_tape(market or _market_now()),
        )

    @app.get("/login", response_class=HTMLResponse)
    def login_form(
        request: Request, next_path: str = Query("", alias="next")
    ) -> Any:
        wanted = _safe_next(next_path)
        if not sessions.required:
            return RedirectResponse(wanted, status_code=303)
        if sessions.is_valid(request.cookies.get(COOKIE_NAME)):
            return RedirectResponse(wanted, status_code=303)
        return HTMLResponse(render.login_page(env=resolved_env, next_path=wanted))

    @app.post("/login", response_class=HTMLResponse)
    async def login(request: Request) -> Any:
        if not sessions.required:
            return RedirectResponse("/", status_code=303)

        form = await request.form()
        # Re-validated here rather than trusted because the redirect put it in
        # the form. Everything between the two is the client's to edit.
        wanted = _safe_next(str(form.get("next", "")))

        client = request.client.host if request.client else "unknown"
        if sessions.throttled(client):
            # 429 rather than "wrong password", so a guesser cannot use the
            # response to tell a locked-out attempt from a failed one.
            return HTMLResponse(
                render.login_page(
                    env=resolved_env,
                    error="Too many attempts. Wait five minutes and try again.",
                    next_path=wanted,
                ),
                status_code=429,
            )

        if not sessions.check_password(str(form.get("password", ""))):
            sessions.record_failure(client)
            return HTMLResponse(
                render.login_page(
                    env=resolved_env,
                    error="Incorrect password.",
                    next_path=wanted,
                ),
                status_code=401,
            )

        sessions.clear_attempts(client)
        response = RedirectResponse(wanted, status_code=303)
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
    def board(request: Request, response: Response) -> str:
        # The marker is read and re-stamped on HTML page routes ONLY.
        #
        # Not on `/live`: an EventSource reconnects on its own and every
        # reconnect carries the cookie, so stamping there would hold a sitting
        # open for as long as the tab lived. Not on `/login` either — that would
        # advance the marker to a moment the operator was shown a password form
        # rather than the state of the world. And not on `/healthz`, where a
        # monitoring probe every thirty seconds would do the same thing.
        visit = seen.from_cookies(request.cookies)
        response.set_cookie(
            **seen.cookie_for(visit, secure=request.url.scheme == "https")
        )

        (
            account,
            orders,
            prices,
            read_at,
            reading_stale,
            orders_unavailable,
        ) = _account_orders_prices()
        open_trades = resolved_journal.open_trades()
        # One reading for the strip and for the card below it. See `_market_now`.
        market = _market_now()

        if account is None:
            # Cold start: no reading yet, so there is nothing to reconcile the
            # journal against and no position list to check for expiries. The
            # banners are all statements about a broker reading, so rendering
            # them here would be asserting things about an account nobody has
            # read. The waiting Board says exactly that instead.
            return _page(
                "Board",
                "/",
                render.since_last_visit(
                    seen.summarise(
                        visit.marker,
                        decisions=audit.read(limit=60).decisions,
                        closed_trades=resolved_journal.closed_trades(),
                    )
                )
                + render.board(
                    None, resolved_rules, [], open_trades,
                    resolved_journal.get_stand_down(), 0, env=resolved_env,
                    market=market,
                ),
                market,
            )

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

        body = render.since_last_visit(
            seen.summarise(
                visit.marker,
                decisions=audit.read(limit=60).decisions,
                closed_trades=resolved_journal.closed_trades(),
            )
        ) + render.banners(
            resolved_journal.get_stand_down(),
            alerts,
            untracked,
            stale=stale,
            # `None` when the check has never run, which renders nothing rather
            # than a false all-clear. A box without the timer installed should
            # not be told its link is healthy.
            tailnet=read_tailnet_status(),
            reading_stale=reading_stale,
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
            env=resolved_env,
            read_at=read_at,
            stale=reading_stale,
            # What the broker holds against what the journal says, and whether
            # anything on file explains the difference. Pure — it takes the
            # poller's last reading and a bounded read of the recorded actions,
            # so the render still costs no network.
            #
            # Computed here rather than read off the loop's `ReconcileResult`
            # because the dashboard and the loop are separate processes: a box
            # running the web unit with the loop stopped would otherwise show
            # the last comparison the loop happened to make, aged arbitrarily,
            # with nothing on screen to say when.
            unexplained=detect_unexplained_moves(
                positions=account.open_positions,
                orders=orders,
                open_trades=open_trades,
                # Newest first and bounded, for the reason `reconcile` gives:
                # an explanation for a level resting NOW is a recent move.
                actions=resolved_journal.position_actions(limit=200),
                orders_degraded=orders_unavailable,
            ),
            market=market,
        )
        return _page("Board", "/", body, market)

    @app.get("/decisions", response_class=HTMLResponse)
    def decisions(page: str = "1") -> str:
        """The trail, paged.

        The reader's window is deliberately wider than one page. It used to be
        `limit=60` against 40 rendered, so twenty cycles were read, counted in
        the header and never shown, and there was no way to ask for them. The
        loop wakes 96 times a day, so a page of 40 is under half a session and
        the rest has to be reachable rather than merely counted.

        `AuditLog.read` stops as soon as it has `limit` decisions and walks the
        dated files newest first, so the wider window costs a parse of files
        that are on disk anyway — measured at tens of milliseconds — and buys
        several pages of history behind one read.

        `page` is taken as a STRING and coerced here rather than annotated
        `int`. FastAPI would answer `?page=abc` with a 422 JSON body, which is
        the same raw-JSON-at-a-person defect the 404 handler below exists to
        fix, arriving through a typo in a query string. A page number nobody
        can parse is page one; `render.decisions` clamps the rest.
        """
        try:
            wanted = int(page)
        except ValueError:
            wanted = 1
        return _page(
            "Decisions",
            "/decisions",
            render.decisions(audit.read(limit=DECISION_WINDOW), page=wanted),
        )

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

    @app.get("/dreaming", response_class=HTMLResponse)
    def dreaming(request: Request) -> str:
        """The dreamer's deck.

        Read-only, like every other GET here, and its store is a different file
        from the journal on purpose: a hypothesis and a position must never be
        reachable by the same query. A store that cannot be opened costs the
        page its contents and nothing else, so it degrades to an empty deck
        rather than a 500 — this is the surface an operator opens to browse,
        and a broken speculative-notes table is not a reason to show them an
        error page.

        **The seen cookie is READ here and never re-stamped.** The marker's
        whole property is that it advances only to a moment the operator was
        provably shown the state of the world, and what they were shown here is
        a shelf of hypotheses — advancing it would mark the Board's decisions
        and closes as seen on the strength of a visit to a different page. So
        this takes the marker, uses it to decide whether a fusion is new enough
        to animate, and leaves it exactly where it was.

        Everything the page needs beyond the dreams themselves is gathered here
        rather than in the renderer, for the reason `news_windows` is resolved
        outside the risk gate: a render that opened a database is a render that
        can fail, and this one already degrades to an empty deck.
        """
        try:
            recent = resolved_dreams.recent(limit=40)
        except sqlite3.Error:
            recent = []

        titles: dict[int, str] = {}
        # Which fusions each dream on this page is a parent OF.
        #
        # Inverted from the children's own `parents` in one pass over what is
        # already loaded, rather than by calling `DreamStore.children_of` per
        # card: that answers for one id and SCANS the table to do it, so a card
        # per row would be one full scan per dream on the page.
        #
        # The window is what it costs. A parent whose fusion has scrolled out of
        # `recent` shows no back-link here — but `fuse` writes a note into every
        # parent's transcript naming the child, and that transcript is rendered,
        # so the relationship is never lost, only the shortcut to it.
        children: dict[int, list[int]] = {}
        transcripts: dict[int, list[DreamMessage]] = {}
        grants: dict[int, list[Adoption]] = {}
        try:
            for dream in recent:
                if dream.id is None:
                    continue
                titles[dream.id] = dream.title
                transcripts[dream.id] = resolved_dreams.messages(dream.id)
                for parent in dream.parents:
                    children.setdefault(parent, []).append(dream.id)
            # A parent that is not itself on this page still needs its title, or
            # a fusion's crest reads as a bare id.
            for wanted in {p for d in recent for p in d.parents} - set(titles):
                parent_dream = resolved_dreams.get(wanted)
                if parent_dream is not None:
                    titles[wanted] = parent_dream.title
            # One query for every adoption ever made, grouped here. Per-dream
            # would be one query per card for a table with a handful of rows.
            for adoption in resolved_dreams.adoptions():
                grants.setdefault(adoption.dream_id, []).append(adoption)
        except sqlite3.Error:
            # Same failure direction as the read above: the page loses the
            # trimmings and keeps the dreams, rather than becoming a 500.
            pass

        # Its own try, rather than inside the one above, because a failure here
        # has to be REPORTED rather than absorbed. Every other trimming degrades
        # to an absence that reads correctly as an absence; an empty verdict
        # mapping reads as "the two agents have never conferred about any of
        # these", which is an affirmative claim about the one thing on the card
        # that nothing else can establish. So the flag travels to the renderer
        # and the page says which of the two it is.
        verdicts: dict[int, ConferenceDecision] = {}
        conference: list[ConferenceDecision] = []
        conference_readable = True
        try:
            conference = resolved_dreams.conference_decisions(
                limit=CONFERENCE_FEED_ROWS
            )
            # Per dream rather than sliced out of the feed above. The feed is a
            # window and a dream whose last exchange predates it would come back
            # as never conferred — a wrong page bought by saving forty indexed
            # lookups on a table with a handful of rows.
            for dream in recent:
                if dream.id is None:
                    continue
                latest = resolved_dreams.latest_conference_decision(dream.id)
                if latest is not None:
                    verdicts[dream.id] = latest
        except sqlite3.Error:
            conference_readable = False
            verdicts = {}
            conference = []

        return _page(
            "Dreaming",
            "/dreaming",
            render.dreaming_page(
                recent,
                DreamSummary.of(recent),
                enabled=bool(resolved_env.dashboard_chat_token),
                token=resolved_env.dashboard_chat_token,
                # Either instance can serve the panel, so it is available
                # if either is. Which one, and what that means for its reach, is
                # rendered explicitly rather than left to be assumed.
                hermes_available=dreamer.available or bridge.available,
                isolated=dreamer.available,
                soul_found=load_soul(GROGU).found,
                titles=titles,
                fused_into=children,
                transcripts=transcripts,
                adoptions=grants,
                verdicts=verdicts,
                conference=conference,
                conference_readable=conference_readable,
                marker=seen.from_cookies(request.cookies).marker,
            ),
        )

    def _calendar_view(snapshot: live.LiveSnapshot | None) -> dict[str, Any]:
        if snapshot is None:
            # No reading at all. Distinct from a reading whose calendar was
            # empty, and the card says which — it must not guess a cause.
            return {"poller_has_read": False}
        return {
            "sessions_ahead": snapshot.sessions_ahead,
            "calendar_loaded": snapshot.calendar_loaded,
            "calendar_degraded": snapshot.calendar_degraded,
            "poller_has_read": True,
        }

    @app.get("/settings", response_class=HTMLResponse)
    def settings() -> str:
        return _page(
            "Settings",
            "/settings",
            render.settings_page(
                resolved_rules,
                resolved_env,
                chat_enabled=bool(resolved_env.dashboard_chat_token),
                # Read from the poller, never fetched here. Settings has no
                # `data-live` target and must not be the page that opens a
                # broker conversation — the same rule that stopped the Board
                # reading Alpaca inline. Before the first read this renders as
                # "not loaded", which is the honest answer rather than a blank.
                **_calendar_view(resolved_poller.latest()),
                # The X feed's OBSERVED state, out of the audit log rather than
                # off a live feed. This process never holds an `XFeed` — the
                # loop owns it — so "did a fetch fail" and "when was a post last
                # recorded" can only come from the record, and the alternative
                # is a card that reports configuration and calls it health.
                #
                # A file read, not a network call: the same rule that keeps the
                # broker off this page. `AuditLog.read` walks dated files newest
                # first and stops at the limit, measured at tens of
                # milliseconds.
                social_recall=news_history.recall(
                    audit.read(limit=NEWS_WINDOW_CYCLES),
                    hours=render.NEWS_WINDOW_HOURS,
                ),
                # The forge: the settings agent's panel and the change requests
                # already argued. Off with chat, because it drives an agent.
                #
                # The store is opened only when the forge is on, so a dashboard
                # without a chat token never creates the database at all — see
                # `_requests`.
                forge_enabled=bool(resolved_env.dashboard_chat_token),
                token=resolved_env.dashboard_chat_token,
                hermes_available=bridge.available,
                soul_found=load_soul(ARMORER).found,
                limits=limits_for(resolved_rules),
                rules_path=resolved_rules_path,
                requests=(
                    _requests().recent(limit=10)
                    if resolved_env.dashboard_chat_token
                    else []
                ),
            ),
        )

    @app.post("/settings/request", response_class=JSONResponse)
    def settings_request(payload: dict[str, Any]) -> dict[str, Any]:
        """Argue about a limit, record the argument, and apply it.

        This is the second write route on a dashboard that was wholly read-only,
        and the reasoning has to sit here rather than in a commit message.

        **It used to be unable to change anything, and the operator rejected
        that.** *"Settings agent can't edit settings?? That's broken."* So the
        change is applied in the conversation now — and the property that made
        the old arrangement worth having is kept rather than waived. `config/`
        is still root-owned (`deploy/bootstrap.sh`), this process still runs as
        `mudhorn`, and it still cannot write `config/rules.yaml` with its own
        hands. It asks root to, through `deploy/apply-settings.sh`: root-owned,
        named in a sudoers rule, taking **no arguments**, with the request id on
        **stdin** where nothing typed here can be read as a flag. That command
        re-validates the whole file through `Rules.load` on a staged copy before
        replacing it.

        **When there is no privileged route the request stays recorded and
        pending and the answer says so.** That is the previous behaviour kept as
        the fallback, never a silence — the same rule as the Dreaming page's
        isolation banner.

        **The asymmetry is enforced here and not only in the character.** A
        tightening change records and applies on the first ask. A loosening one
        states the arithmetic consequence and records NOTHING until `confirm`
        comes back true — so the operator has to have been shown the cost before
        they can accept it. `souls/armorer.md` shapes how that is said;
        `settings_agent.decide` decides it, because a soul is read off disk at
        call time and could have been edited.

        Behind `DASHBOARD_CHAT_TOKEN` as well as the dashboard password, for the
        same reason `POST /chat` is: viewing an account and driving an agent are
        different privileges, and one secret must not grant both. Off entirely
        without the token, so a deploy never switches it on.

        Takes a plain `dict` rather than `request: Request` for the reason
        `chat` gives — a function-local annotation is invisible to FastAPI and
        the parameter is silently treated as a query field.
        """
        if not resolved_env.dashboard_chat_token:
            raise HTTPException(status_code=404, detail="the settings agent is not enabled")
        if payload.get("token") != resolved_env.dashboard_chat_token:
            raise HTTPException(status_code=403, detail="bad token")

        try:
            outcome = decide(
                _requests(),
                resolved_rules,
                str(payload.get("key", "")),
                str(payload.get("value", "")),
                reason=str(payload.get("reason", "")),
                confirm=bool(payload.get("confirm")),
                # The conversation with the Armorer, as the browser holds it. An
                # argument the operator won is a fact worth keeping, and so is
                # one they lost; the objection is stored in its own column
                # either way, so an empty transcript costs context and never the
                # record.
                transcript=[
                    {"role": str(t.get("role", "")), "text": str(t.get("text", ""))}
                    for t in payload.get("transcript") or []
                ],
                equity_usd=_equity_reading(),
                rules_path=resolved_rules_path,
                applier=resolved_applier,
            )
        except (UnknownLimit, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

        argument = outcome.argument
        # Both values and the cut, on the UNCONFIRMED leg as well as the
        # recorded one. The confirmation window renders only what it is handed —
        # there is no arithmetic and no fetch in it — so a payload missing these
        # would leave it asking the operator to agree to a number with nothing
        # to compare it against, which is the thing it exists to fix.
        shown = preview(argument, rules_path=resolved_rules_path)
        view: dict[str, Any] = {
            "ok": True,
            "key": argument.key,
            "label": argument.fact.label,
            "unit": argument.fact.unit_label,
            "current": argument.current,
            "current_source": argument.current_source,
            "proposed": argument.proposed,
            "old_value": shown.old_value,
            "new_value": shown.new_value,
            "diff": shown.diff,
            "stance": str(argument.stance),
            "consequence": argument.consequence,
            "objection": argument.objection,
            "requires_confirmation": argument.requires_confirmation,
            "can_record": argument.can_record,
            "blocked_reason": argument.blocked_reason,
            "summary": outcome.summary,
            "recorded": None,
            "applied": None,
        }

        record = outcome.request
        if record is not None:
            view["recorded"] = {
                "id": record.id,
                "diff": record.diff,
                "commit_message": commit_message(record),
                "apply_command": record.apply_command,
                "revert_command": record.revert_command,
            }
        if outcome.applied is not None:
            # `ok` and `detail` both, always. "Applied" and "recorded but the
            # write failed" are different outcomes and a surface that collapsed
            # them would tell the operator a limit moved when it did not.
            view["applied"] = {
                "ok": outcome.applied.ok,
                "detail": outcome.applied.detail,
                "via": outcome.applied.via,
            }
        return view

    def _equity_reading() -> float | None:
        """Equity if the poller has read, and `None` if it has not.

        Never a default. Every money figure the settings agent states is
        computed against this, so an assumed $100,000 would put a number that
        looks like a measurement in front of an operator deciding what to risk.
        """
        snapshot = resolved_poller.latest()
        return snapshot.account.equity_usd if snapshot and snapshot.account else None

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
        """Ask Hermes a question.

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

        # Which character answers. Validated against a fixed set rather than
        # used as a filename: `load_soul` builds a path from this string, and an
        # unvalidated value from a request body reaching a path join is a
        # traversal waiting to happen. An unknown name falls back to the
        # account agent rather than erroring, because the worst case of getting
        # this wrong is the wrong voice, and refusing the question would be a
        # larger failure than answering it plainly.
        # `KNOWN_SOULS` rather than a literal set repeated here: a soul added to
        # `souls/` and forgotten at this call site is a character nobody can
        # select, and there is now more than one page selecting one.
        requested = str(payload.get("soul", YODA)).lower()
        soul = load_soul(requested if requested in KNOWN_SOULS else YODA)

        # The dreamer gets its own instance when there is one. Falling back
        # to the shared bridge keeps the panel working on a box that has not had
        # the second Hermes installed; the page it is embedded in states that
        # this is what is happening, so the fallback is disclosed rather than
        # silent.
        #
        # The Armorer speaks on the shared instance, and that is worth being
        # explicit about rather than quietly true: it has the same MCP reach as
        # the account agent, including `place_order`, and nothing about the
        # settings surface changes that. What keeps it away from a limit is not
        # its soul — `config/` is root-owned so the service account cannot edit
        # its own limits, and the only thing this surface writes is a request in
        # a different database.
        answering = dreamer if (soul.name == GROGU and dreamer.available) else bridge

        # The limit table, as FIGURES the agent must not derive. Only the
        # Armorer gets it: handing the account agent a table of caps it has no
        # reason to argue about would cost tokens on every question, and
        # `render_briefing` is the settings agent's own rendering rather than a
        # general-purpose one.
        briefing = (
            render_briefing(resolved_rules, equity_usd=_equity_reading())
            if soul.name == ARMORER
            else ""
        )

        # The operator's name reaches the agent only from here, which is
        # behind both the dashboard password and the chat token. The sign-in
        # page never carries it. See `Env.operator_name`.
        reply = answering.ask(
            str(payload.get("message", "")),
            history,
            soul=soul,
            operator=resolved_env.greeting_name,
            briefing=briefing,
        )
        return {"ok": reply.ok, "text": reply.text, "error": reply.error}

    @app.get("/session")
    def session_check() -> dict[str, Any]:
        """Is this browser still signed in? The route exists for the answer 401.

        It carries nothing — the useful reply is the status code, and the
        middleware produces it before this function ever runs.

        `EventSource` cannot see an HTTP status: a stream refused with 401
        after a session lapses looks to the client exactly like a network
        outage, so the Board settled on amber RECONNECTING forever with the
        last-good figures still on screen and nothing saying the operator was
        signed out. The browser tells us the failure was PERMANENT rather than
        transient (`readyState === CLOSED`); this is how the page finds out it
        was permanent because of authentication, rather than guessing.

        Deliberately not `/healthz`, which is open by design so a monitor can
        reach it — an open route can never answer this question.
        """
        return {"signed_in": True}

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        """Liveness. Names its counts, because one of them read as a claim.

        It used to return `{"ok": true, "trades": 0}` with a 21-share short
        open at the broker. Zero was literally correct — the figure counts
        CLOSED trades — and read as "this bot has never traded", which is the
        plausible-wrong-figure failure this repository avoids everywhere else,
        arriving through a monitoring key. Both counts are now named for what
        they count, and the open one is there so that "nothing has closed" can
        never again be the whole answer.
        """
        return {
            "ok": True,
            "closed_trades": len(resolved_journal.closed_trades()),
            "open_trades": len(resolved_journal.open_trades()),
        }

    @app.exception_handler(404)
    async def not_found(request: Request, exc: Any) -> Response:
        """A mistyped URL, in the deck rather than as a JSON object.

        The same split the auth middleware makes, and for the same reason: a
        browser gets the themed shell with the nav on it, and an API client
        keeps the JSON body it can act on. Answering markup to a caller that
        asked for JSON turns a clear 404 into something it has to parse to
        understand.

        `POST /chat` raises a 404 of its own when chat is not configured. That
        call comes from `fetch`, whose default `Accept` is `*/*`, so it lands
        on the JSON side — which is what its caller already handles.
        """
        if "text/html" not in request.headers.get("accept", ""):
            detail = getattr(exc, "detail", "Not Found")
            return JSONResponse({"detail": detail}, status_code=404)
        return HTMLResponse(
            # No active nav item: none of them is where you are.
            _page("Not found", "", render.not_found_page(request.url.path)),
            status_code=404,
        )

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
