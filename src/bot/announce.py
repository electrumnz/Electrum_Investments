"""Say the things that are only true, and only sayable, at startup.

Two facts about a running process cannot be established from inside it later:
**which inference provider its model calls go to**, and **whether the dashboard
has a login**. A Tailscale Funnel and a local `curl` both arrive on loopback, so
the auth question is genuinely unanswerable after the fact; and a provider swap
leaves no trace in a log line about a cycle. The moment the process starts is
the only moment anyone can be told.

This module exists because that sentence was written in two docstrings and was
true in neither.

## The two defects it closes, both found on 13 Aug 2026

**The auth banner was buffered away.** `web/app.py` printed it with a plain
`print()` to stdout, then called `uvicorn.run()`, which blocks forever. Under
systemd stdout is a pipe rather than a terminal, so Python block-buffers it —
and an 8 KiB buffer holding 200 bytes is never flushed by a process that does
not exit. Measured: with `PYTHONUNBUFFERED` unset, a `print` to stdout is still
invisible a second later while the same text on stderr has already arrived.

The journal showed it exactly. `mudhorn-web` restarted at 06:54:25 and the most
recent banner was stamped 06:41:04 — the *previous* process flushing on its way
out. So the line describing the running process's auth mode appears when that
process dies, attached to the wrong instant, and the line for the process
actually serving requests is sitting in a buffer.

**The provider banner did not exist at all.** `Env.inference_provider` is a
carefully written property with five tests and a docstring promising it is "the
line a startup banner prints". Nothing in `src/` outside `config.py` ever called
it. Fully documented, fully tested, and dead — the same shape as
`Dream.is_offerable`, which `CLAUDE.md` already records as defined and never
called.

Both are one disease: **a claim about what the operator will be told, that is
not true.**

## Why a module rather than a `flush=True` in two places

`flush=True` fixes the instance. It does not stop the next entrypoint being
written with a plain `print`, and there are four processes that should announce
(`loop`, `dream`, `confer`, `web`) against one that did. A named function is
something a test can require, and `tests/test_announce.py` runs a real
subprocess with `PYTHONUNBUFFERED` removed to prove the text arrives *before*
the process exits — the only form of that test that would have failed yesterday.

**stderr, not stdout.** Python line-buffers stderr and block-buffers stdout when
neither is a terminal, so stderr is right by default rather than right because
somebody remembered a keyword argument. `flush=True` is passed as well, because
the property is worth holding twice and costs nothing at startup. Both systemd
units already carry `StandardError=journal`, so this lands in the same place the
old line did.
"""

from __future__ import annotations

import sys
from typing import TextIO

from .config import Env


def say(*lines: str, stream: TextIO | None = None) -> None:
    """Write startup lines where a blocked process cannot swallow them.

    Never raises. A banner that took a process down would be a worse failure
    than a banner nobody read, and this runs before anything else does.
    """
    out = stream if stream is not None else sys.stderr
    try:
        for line in lines:
            print(line, file=out, flush=True)
    except Exception:  # a diagnostic must not be able to end the process
        pass


def announce_inference(env: Env, *, stream: TextIO | None = None) -> None:
    """Which endpoint this process's model calls are configured for.

    The sentence comes from `Env.inference_provider`, which is written to be
    true rather than encouraging: while `PYTHON_MODEL_PATH_USES_DO` is False it
    says the switch is NOT IN FORCE for any Python model call, because it is
    not. A banner claiming otherwise is the confident partial answer this
    repository exists to refuse.

    `usable=False` is reported at the same volume rather than louder. It is a
    configuration finding, not an error, and the process is about to carry on
    either way — saying so plainly is the whole job.
    """
    provider = env.inference_provider
    say(provider.detail)


def announce_dashboard_auth(env: Env, *, stream: TextIO | None = None) -> None:
    """Whether the dashboard has a login, said at the only moment it can be.

    From inside the process a request from the internet and one from the same
    machine are identical — a reverse proxy or a Funnel forwards to loopback —
    so nothing later can establish this. It is not a warning either way: no
    password is correct for `127.0.0.1` plus Tailscale and wrong for anything
    reachable from the internet, and only the operator knows which they have.
    """
    if env.dashboard_password:
        say(
            "Dashboard password is SET: every page requires a login, and "
            "/chat needs DASHBOARD_CHAT_TOKEN on top of it.",
            stream=stream,
        )
    else:
        say(
            "Dashboard password is NOT set: there is no login. That is correct "
            "for 127.0.0.1 plus Tailscale, and wrong for anything reachable "
            "from the internet. Set DASHBOARD_PASSWORD before exposing this.",
            stream=stream,
        )
