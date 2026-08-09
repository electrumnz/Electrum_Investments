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
from bot.journal import Journal
from bot.web.app import build_app
from bot.web.auth import COOKIE_NAME, MAX_ATTEMPTS, SessionStore

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

PASSWORD = "correct-horse-battery-staple"

PROTECTED = ["/", "/decisions", "/trades", "/analytics", "/settings", "/chat"]


def _env(password: str = "") -> Env:
    env = Env(_env_file=None)  # type: ignore[call-arg]
    env.dashboard_password = password
    return env


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "journal.db")


@pytest.fixture
def guarded(journal):
    app = build_app(
        journal=journal, rules=load_rules(), env=_env(PASSWORD), force_mock=True
    )
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def unguarded(journal):
    app = build_app(journal=journal, rules=load_rules(), env=_env(), force_mock=True)
    return TestClient(app, follow_redirects=False)


# ------------------------------------------------------- nothing gets through


@pytest.mark.parametrize("path", PROTECTED)
def test_every_page_is_refused_without_a_session(guarded, path):
    """The whole point. A public URL plus a missing check is the account on show."""
    r = guarded.get(path, headers={"accept": "text/html"})

    assert r.status_code == 303
    assert r.headers["location"] == "/login"


@pytest.mark.parametrize("path", PROTECTED)
def test_no_account_data_leaks_in_the_refusal(guarded, path):
    """A redirect that still rendered the body would defeat the entire gate."""
    body = guarded.get(path, headers={"accept": "text/html"}).text

    for leak in ("Equity", "equity_usd", "Open risk", "100,000"):
        assert leak not in body, f"{path} leaked {leak!r} before authenticating"


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
