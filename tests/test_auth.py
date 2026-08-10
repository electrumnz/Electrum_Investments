"""The dashboard password gate.

The dashboard had no login because it bound to loopback and nothing was
published. The operator has chosen to expose it, on the basis that the account
behind it is paper money. That makes this file the thing standing between a
public URL and a view of a brokerage account, so the assertions are about what
an unauthenticated request can reach — not about the happy path.

Every test here is the negative case first: no session, no data.
"""

from __future__ import annotations

import pytest

from bot.config import Env, load_rules
from bot.dreaming import DreamStore
from bot.journal import Journal
from bot.web import live
from bot.web.app import build_app
from bot.web.auth import COOKIE_NAME, MAX_ATTEMPTS, SessionStore

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

PASSWORD = "correct-horse-battery-staple"

PROTECTED = [
    "/", "/decisions", "/trades", "/analytics", "/dreaming", "/settings", "/chat",
]

# Everything that is not a page but must still be refused.
#
# `/live` serves equity, cash, buying power, every open position and every
# resting order as JSON, and it was missing from this file entirely. That is
# the shape of the miss worth naming: it is not a *page*, so it never came up
# when somebody wrote "every page is refused". Moving it into `OPEN_PATHS`
# would have published the account with the whole suite green.
#
# A SEPARATE list rather than more entries in PROTECTED, because the loopback
# test below asserts a 200 body: `/live` is an endless event stream that never
# returns, and `/logout` and `/openapi.json` are not pages. The refusal tests
# are safe on all three precisely because the middleware answers before the
# route runs.
REFUSED = [*PROTECTED, "/live", "/logout", "/openapi.json", "/session"]

# The only routes an unauthenticated request may reach, each for a stated
# reason. `test_no_route_escapes_the_lists` is what makes this file complete
# rather than merely long: a route added later belongs to one of these two
# lists, and until somebody says which, the suite fails.
OPEN_BY_DESIGN = {
    "/login",    # the gate itself. Reveals nothing; asserted below.
    "/healthz",  # liveness for a monitor. Trade counts, never positions or money.
}


def _env(password: str = "") -> Env:
    env = Env(_env_file=None)  # type: ignore[call-arg]
    env.dashboard_password = password
    return env


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "journal.db")


@pytest.fixture
def dreams(tmp_path):
    """Never the real store. Same rule as the journal: a test that wrote to
    data/ would leave a file on the developer's machine that the next run
    then reads."""
    return DreamStore(tmp_path / "dreams.db")


@pytest.fixture
def poller(journal):
    """Primed with one synchronous read: the Board reads the poller, not the
    broker, so without this every page renders its cold-start shell."""
    p = live.build_poller(journal=journal, env=_env(), force_mock=True)
    p.poll_once()
    return p


@pytest.fixture
def guarded(journal, dreams, poller):
    app = build_app(
        journal=journal, rules=load_rules(), env=_env(PASSWORD),
        dreams=dreams, poller=poller, force_mock=True
    )
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def unguarded(journal, dreams, poller):
    app = build_app(
        journal=journal, rules=load_rules(), env=_env(),
        dreams=dreams, poller=poller, force_mock=True
    )
    return TestClient(app, follow_redirects=False)


# ------------------------------------------------------- nothing gets through


@pytest.mark.parametrize("path", REFUSED)
def test_every_page_is_refused_without_a_session(guarded, path):
    """The whole point. A public URL plus a missing check is the account on show."""
    r = guarded.get(path, headers={"accept": "text/html"})

    assert r.status_code == 303
    # The requested path rides along so a deep link survives the sign-in, so
    # the assertion is on where the redirect GOES rather than on the exact
    # string. `/` carries no `next` because it is already the fallback.
    assert r.headers["location"].split("?")[0] == "/login"


@pytest.mark.parametrize("path", REFUSED)
def test_no_account_data_leaks_in_the_refusal(guarded, path):
    """A redirect that still rendered the body would defeat the entire gate."""
    body = guarded.get(path, headers={"accept": "text/html"}).text

    for leak in ("Equity", "equity_usd", "Open risk", "100,000"):
        assert leak not in body, f"{path} leaked {leak!r} before authenticating"


def test_no_route_escapes_the_lists(guarded):
    """The lists above are only as good as their completeness.

    `/live` was absent from this file for its whole life, and no test could
    notice: every assertion here iterates a list that did not mention it. A
    hand-maintained list of what must be protected fails exactly the way the
    middleware was chosen over per-route dependencies to avoid — silently, on
    the route somebody forgot.

    So the routes are enumerated from the application instead. A new one has to
    be classified as refused or as deliberately open before this passes, which
    is a decision somebody makes rather than one that gets made by omission.
    """
    paths = {
        r.path
        for r in guarded.app.routes
        if isinstance(getattr(r, "path", None), str) and r.path.startswith("/")
    }
    unclassified = paths - set(REFUSED) - OPEN_BY_DESIGN

    assert not unclassified, (
        f"{sorted(unclassified)} is neither refused nor deliberately open. "
        "Add it to REFUSED, or to OPEN_BY_DESIGN with a reason."
    )


def test_a_non_browser_client_gets_401_rather_than_html(guarded):
    """An API caller redirected to a login form sees a confusing 200 of markup."""
    r = guarded.get("/", headers={"accept": "application/json"})

    assert r.status_code == 401
    assert r.json()["error"] == "authentication required"


def test_the_chat_endpoint_is_behind_the_password_too(guarded):
    """Chat drives an agent that can reach the broker. It is the last thing to leave open."""
    r = guarded.post("/chat", json={"message": "what is my equity"})

    assert r.status_code == 401


def test_a_forged_cookie_is_refused(guarded):
    r = guarded.get(
        "/", headers={"accept": "text/html"}, cookies={COOKIE_NAME: "made-up"}
    )

    assert r.status_code == 303


def test_the_login_page_reveals_nothing_about_the_account(guarded):
    """Everything on it is already public in the repo, which is the requirement."""
    body = guarded.get("/login").text

    assert "Operator sign-in" in body
    for leak in ("Equity", "Open risk", "SPY", "100,000"):
        assert leak not in body


# ------------------------------------------------------------ signing in


def test_the_right_password_opens_everything(guarded):
    r = guarded.post("/login", data={"password": PASSWORD})

    assert r.status_code == 303
    assert r.headers["location"] == "/"

    page = guarded.get("/", headers={"accept": "text/html"})
    assert page.status_code == 200
    assert "MUDHORN" in page.text


def test_the_wrong_password_does_not(guarded):
    r = guarded.post("/login", data={"password": "hunter2"})

    assert r.status_code == 401
    assert COOKIE_NAME not in r.cookies
    assert guarded.get("/", headers={"accept": "text/html"}).status_code == 303


def test_the_session_cookie_is_httponly(guarded):
    """Script-readable would mean any injected script can walk off with it."""
    r = guarded.post("/login", data={"password": PASSWORD})

    assert "httponly" in r.headers["set-cookie"].lower()


def test_signing_out_invalidates_the_session(guarded):
    guarded.post("/login", data={"password": PASSWORD})
    assert guarded.get("/", headers={"accept": "text/html"}).status_code == 200

    guarded.get("/logout")

    assert guarded.get("/", headers={"accept": "text/html"}).status_code == 303


def test_guessing_is_rate_limited(guarded):
    """One operator needs a handful of attempts. A guesser needs thousands."""
    for _ in range(MAX_ATTEMPTS):
        guarded.post("/login", data={"password": "wrong"})

    blocked = guarded.post("/login", data={"password": "wrong"})
    assert blocked.status_code == 429

    # And the lockout does not distinguish itself from a wrong password by
    # letting the correct one through while throttled.
    assert guarded.post("/login", data={"password": PASSWORD}).status_code == 429


# ------------------------------------------------- the loopback deployment


@pytest.mark.parametrize("path", PROTECTED)
def test_no_password_means_no_gate_which_is_correct_on_loopback(unguarded, path):
    """The original arrangement must keep working unchanged.

    Bound to 127.0.0.1 and reached over Tailscale, there is nobody to
    authenticate, and a login screen would be a credential to lose for no gain.
    """
    assert unguarded.get(path, headers={"accept": "text/html"}).status_code == 200


def test_the_login_page_redirects_away_when_no_password_is_configured(unguarded):
    r = unguarded.get("/login")

    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_healthz_stays_open_so_a_probe_still_works(guarded):
    """It reports liveness and trade counts, nothing about positions or money.

    Both counts are NAMED. It used to answer `{"ok": true, "trades": 0}` with a
    21-share short resting at the broker: literally correct, because the figure
    counts closed trades, and it reads as "this bot has never traded". A
    monitoring key that can only ever say what has finished is the confident
    partial answer this repository refuses everywhere else, arriving through
    the one route that is deliberately unauthenticated.

    What must stay true is the second half — this is an open route, so a count
    is the most it may ever carry. No symbol, no size, no money.
    """
    r = guarded.get("/healthz")

    assert r.status_code == 200
    assert set(r.json()) == {"ok", "closed_trades", "open_trades"}
    assert "trades" not in r.json(), "the unqualified key is what read as a claim"


# ---------------------------------------------------------------- the store


def test_password_comparison_is_constant_time():
    """A plain == leaks the matching prefix through timing."""
    import inspect

    source = inspect.getsource(SessionStore.check_password)
    assert "compare_digest" in source


def test_session_tokens_are_not_stored_in_the_clear():
    """A memory dump or a stray log of this dict should yield nothing usable."""
    store = SessionStore(password=PASSWORD)
    token = store.issue()

    assert token not in store._sessions
    assert store.is_valid(token)


def test_an_expired_session_is_refused_and_dropped():
    import time

    store = SessionStore(password=PASSWORD)
    token = store.issue()
    store._sessions[SessionStore._digest(token)] = time.time() - 1

    assert not store.is_valid(token)
    assert store._sessions == {}


# ------------------------------------------------ a deep link survives the gate


def test_the_requested_path_rides_through_the_sign_in(guarded):
    """A bookmark or a shared link is the normal way into a page that is not
    the Board, and every one of them used to land on the Board."""
    refused = guarded.get("/trades", headers={"accept": "text/html"})
    assert refused.headers["location"] == "/login?next=/trades"

    form = guarded.get("/login?next=/trades")
    assert 'name="next" value="/trades"' in form.text

    signed_in = guarded.post("/login", data={"password": PASSWORD, "next": "/trades"})
    assert signed_in.status_code == 303
    assert signed_in.headers["location"] == "/trades"


def test_the_board_carries_no_next_because_it_is_already_the_fallback(guarded):
    assert guarded.get("/", headers={"accept": "text/html"}).headers["location"] == "/login"


@pytest.mark.parametrize(
    "hostile",
    [
        "https://evil.example/steal",
        "//evil.example/steal",
        "http://evil.example",
        "/../../etc/passwd",
        "/not-a-route",
        "javascript:alert(1)",
        "",
    ],
)
def test_a_next_that_is_not_one_of_our_own_routes_falls_back_to_the_board(
    guarded, hostile
):
    """An unchecked `next` is the textbook open redirect on a login form.

    It turns this sign-in page into the front door to somebody else's, wearing
    the operator's URL and whatever trust they place in it. The check is
    against the application's OWN routes — enumerated, not listed by hand, for
    the same reason `test_no_route_escapes_the_lists` enumerates them.

    The fallback is the Board, which is where every sign-in landed before any
    of this existed. Getting it wrong costs the deep link, never an error page.
    """
    r = guarded.post("/login", data={"password": PASSWORD, "next": hostile})

    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_the_form_field_is_revalidated_and_not_trusted_because_we_put_it_there(guarded):
    """The redirect writes it, the browser holds it, the form posts it back.

    Everything between the two is the client's to edit, so the value is checked
    again on the way out rather than believed because this app produced it.
    """
    import inspect

    from bot.web import app as app_module

    source = inspect.getsource(app_module.build_app)
    assert source.count("_safe_next(") >= 3, "one of the three checks is missing"


def test_the_gate_and_the_stream_are_not_landing_places(guarded):
    """Both are real GET routes, and neither is somewhere to be sent.

    `/login` would bounce forever and `/live` is an endless event stream a
    browser told to navigate to it would sit on with nothing on screen.
    """
    for path in ("/login", "/logout", "/live"):
        r = guarded.post("/login", data={"password": PASSWORD, "next": path})
        assert r.headers["location"] == "/", path
        guarded.get("/logout")


# ----------------------------------------------------------- a mistyped URL


def test_a_wrong_path_gets_the_deck_not_raw_json(guarded):
    """`{"detail":"Not Found"}` with no nav and no styling, on a deck where
    every other surface is themed."""
    guarded.post("/login", data={"password": PASSWORD})

    r = guarded.get("/definitely-not-a-page", headers={"accept": "text/html"})

    assert r.status_code == 404
    assert "No such page" in r.text
    assert "/definitely-not-a-page" in r.text
    # The nav is the point: there has to be a way back that is not the browser
    # button.
    assert 'href="/decisions"' in r.text
    assert "MUDHORN" in r.text


def test_an_api_client_still_gets_json_for_a_wrong_path(guarded):
    """The same split the auth middleware already makes correctly. Answering
    markup to a caller that asked for JSON turns a clear 404 into something it
    has to parse to understand."""
    guarded.post("/login", data={"password": PASSWORD})

    r = guarded.get("/definitely-not-a-page", headers={"accept": "application/json"})

    assert r.status_code == 404
    assert r.json() == {"detail": "Not Found"}


# ------------------------------------------- the session probe the Board needs


def test_the_session_probe_answers_401_when_signed_out(guarded):
    """`EventSource` cannot see an HTTP status, so a stream refused after a
    session lapses looks exactly like the network dropping. This route is how
    the Board finds out which it was."""
    r = guarded.get("/session", headers={"accept": "application/json"})

    assert r.status_code == 401


def test_the_session_probe_answers_200_when_signed_in(guarded):
    guarded.post("/login", data={"password": PASSWORD})

    r = guarded.get("/session", headers={"accept": "application/json"})

    assert r.status_code == 200
    assert r.json() == {"signed_in": True}


def test_the_session_probe_carries_nothing_about_the_account(guarded):
    """The useful answer is the status code. A probe that grew a payload would
    be a second surface serving the account, reached from a page's script."""
    guarded.post("/login", data={"password": PASSWORD})
    body = guarded.get("/session", headers={"accept": "application/json"}).text

    for leak in ("equity", "Equity", "SPY", "100,000", "position"):
        assert leak not in body


def test_a_junk_page_number_gets_the_trail_rather_than_a_422(unguarded):
    """`?page=abc` annotated `int` answers a raw 422 JSON body.

    That is the same defect the 404 handler exists to fix — a machine-readable
    error shown to a person — arriving through a typo in a query string. A page
    number nobody can parse is page one.
    """
    for junk in ("abc", "", "1.5", "-4", "99999"):
        r = unguarded.get(f"/decisions?page={junk}", headers={"accept": "text/html"})
        assert r.status_code == 200, junk
        assert "Decisions" in r.text
