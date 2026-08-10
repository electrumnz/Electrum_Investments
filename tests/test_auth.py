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
REFUSED = [*PROTECTED, "/live", "/logout", "/openapi.json"]

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
    assert r.headers["location"] == "/login"


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
    """It reports liveness and a trade count, nothing about positions or money."""
    r = guarded.get("/healthz")

    assert r.status_code == 200
    assert set(r.json()) == {"ok", "trades"}


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
