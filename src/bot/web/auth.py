"""Password gate for the dashboard, for when it is reachable from the internet.

## Why this exists at all

The dashboard shipped with no login *because* it bound to `127.0.0.1` and was
reached over Tailscale — nothing was published, so there was nobody to
authenticate. The operator has decided to expose it publicly, on the grounds
that the account behind it is Alpaca **paper** money. That is their call and it
is a defensible one: no funds can be lost.

It is not, however, a reason to expose it *unauthenticated*, and the original
note in `app.py` said the prerequisite was building real authentication first.
This is that. It is deliberately server-side.

## Why the password could not go where it was asked to go

The request was to put the password in `brand/`. That cannot work:

- `brand/` is static files in a **public GitHub repository**. A password there
  is readable in the repo and in the page source. Anyone who presses "view
  source" is past it.
- `brand/` has no server. A JavaScript check hides markup from a browser that
  chooses to be polite; it does not withhold data, because there is no data —
  and if it were pointed at real data, the data would arrive before the check.

So the check happens here, in Python, on the machine holding the journal, and
the password comes from the environment and is never committed.

## What this is and is not

It **is** a real gate: every route is refused without a valid session, the
comparison is constant-time, and **wrong answers** to the login endpoint are
rate limited so the password cannot be found by guessing.

It is **not** multi-user authentication. One shared password, no accounts, no
rotation, no audit of who logged in. That is proportionate to a paper account
and would not be to a live one. If this ever fronts real money, this file is
the thing to replace, not to extend.

## Fail-closed, in the same shape as the chat panel

No password set means no gate, which is correct for the loopback deployment
this started as. `POST /chat` keeps its **own** separate token on top, so
exposing the dashboard does not hand a stranger an agent that can reach the
broker — a viewer and a driver are different privileges and stay so.

## The throttle counts the ANSWER, never the caller

It used to count attempts per `request.client.host`, and that is wrong the
moment anything sits in front of this process — which is the deployment that
actually exists. Behind a Tailscale Funnel every request arrives on loopback
from the proxy, so **every visitor in the world shared one bucket**: five wrong
passwords from a stranger spent it, and the old throttle refused the *correct*
password too, so the operator was locked out of their own dashboard for as long
as the stranger cared to keep going. Availability rather than disclosure, and
free to trigger from anywhere.

**The fix is to stop asking "who is this?" and start asking "was this a
guess?".** The first question has no answer here — behind a proxy the operator
and the guesser are the same address, carrying the same headers we would be
foolish to believe — so no bucket keyed on the caller can separate them. The
second question separates them exactly: a guesser does not know the password
and the operator does. So:

- **A correct password is never throttled.** It is not a guess, and refusing it
  denies the account to the only person entitled to it. `check_password`
  compares first and consults the budget only once the answer is known wrong.
- **Wrong answers keep the five-per-five-minutes budget**, and it is now ONE
  global budget rather than one per address.

## What that trades away, stated rather than glossed

Somebody can now keep *submitting* guesses. Past the budget every one is
refused with a 429 and none can ever produce a session, but the candidate is
still compared, so the number of guesses that may be MADE is no longer bounded
— only the number that can be answered with anything other than a refusal.

That bound could not be kept. Recognising the operator's password means
comparing the operator's password, and while the operator and the guesser are
indistinguishable, any refusal issued before the comparison refuses both. The
old code took the other side of that trade and bought a rate limit on an online
guess at a long shared secret — the attack least likely to work — in exchange
for a certain, repeatable, free denial of the one surface whose whole job is
watching the account.

Two things narrow what was given up, and both are deliberate:

- **The budget is global, so no header can move it.** Under the old
  per-address key that property was true only inside the test harness.
  `uvicorn.run()` defaults to `proxy_headers=True` with
  `forwarded_allow_ips="127.0.0.1"`, which is exactly where a Funnel connects
  from, so in production `scope["client"]` is rewritten out of
  `X-Forwarded-For` before `app.py` ever reads it. Whether that yields the
  visitor, the proxy or a value the visitor chose depends on what the Funnel
  does with the header, and **nobody here has measured which** — the suite
  cannot see it either way, because `TestClient` never runs that middleware. A
  key that might be attacker-supplied is not a key: one that no header can
  reach is strictly better than a per-address one that a rotating header buys a
  fresh copy of every request.
- **Five wrong answers still mean no wrong answer gets in.** The sixth and the
  six-thousandth are refused identically, and a wrong password has never been
  able to mint a session.

**A hard ceiling was considered and rejected** — "after five hundred wrong
answers, refuse everybody for a minute". It is the reported bug again with a
higher price tag: anybody who can send five requests can send five hundred, so
it hands the free denial of service straight back and buys nothing the
per-answer refusal does not already give.

**The real repair for the caller-keyed bucket is a trusted-proxy arrangement**
— a named proxy whose `X-Forwarded-For` is believed and nobody else's — and it
belongs where the client address is computed, in `app.py`, not here. It must be
opt-in and default off wherever it lands: a header believed from an untrusted
peer is a bucket the attacker picks, which trades this availability bug for the
disclosure one it exists to prevent.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field

# Imported at module scope rather than lazily: `bot/web/__init__.py` already
# imports `app.py`, which imports FastAPI at module scope for its own reasons,
# so nothing reaches this file without the framework already present.
from fastapi import HTTPException

# A session lasts a week. Long enough that a phone kept in a pocket is not
# logging in daily, short enough that a stolen cookie is not permanent.
SESSION_TTL_SECONDS = 7 * 24 * 3600

COOKIE_NAME = "mudhorn_session"

# Rate limiting on the login form. One operator on one password does not need
# more than a handful of attempts; a guesser needs thousands. Five per window
# is generous for the first and useless for the second.
MAX_ATTEMPTS = 5
ATTEMPT_WINDOW_SECONDS = 300


@dataclass
class SessionStore:
    """In-memory sessions and login attempt counters.

    In-memory on purpose. A restart logging everyone out is the correct
    behaviour for a single-operator dashboard, and persisting session tokens
    would put a second credential in the journal file that is already the only
    irreplaceable thing on the box.
    """

    password: str
    _sessions: dict[str, float] = field(default_factory=dict)
    _attempts: dict[str, list[float]] = field(default_factory=dict)

    @property
    def required(self) -> bool:
        """Whether a login is enforced at all."""
        return bool(self.password)

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions[self._digest(token)] = time.time() + SESSION_TTL_SECONDS
        return token

    def is_valid(self, token: str | None) -> bool:
        if not self.required:
            return True
        if not token:
            return False
        expires = self._sessions.get(self._digest(token))
        if expires is None:
            return False
        if expires < time.time():
            # Expired sessions are dropped rather than left to accumulate: this
            # dict is the only thing holding them and nothing else prunes it.
            del self._sessions[self._digest(token)]
            return False
        return True

    def check_password(self, candidate: str) -> bool:
        """Constant-time comparison.

        A plain `==` on a secret leaks its length and its matching prefix
        through timing. That is a marginal risk over the internet and it costs
        one function call to remove, so it is removed.
        """
        return hmac.compare_digest(candidate.encode(), self.password.encode())

    def throttled(self, client: str) -> bool:
        """Whether this client has spent its attempts for the window."""
        now = time.time()
        recent = [t for t in self._attempts.get(client, []) if now - t < ATTEMPT_WINDOW_SECONDS]
        self._attempts[client] = recent
        return len(recent) >= MAX_ATTEMPTS

    def record_failure(self, client: str) -> None:
        self._attempts.setdefault(client, []).append(time.time())

    def clear_attempts(self, client: str) -> None:
        self._attempts.pop(client, None)

    def logout(self, token: str | None) -> None:
        if token:
            self._sessions.pop(self._digest(token), None)

    @staticmethod
    def _digest(token: str) -> str:
        """Sessions are stored hashed.

        Same reasoning as never storing a password in plain text: a memory dump
        or an accidental log of this dict then yields nothing usable.
        """
        return hashlib.sha256(token.encode()).hexdigest()
