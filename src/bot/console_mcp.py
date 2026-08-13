"""A console for the box, reachable over HTTPS, with the shell switched OFF.

The operator asked for this in as many words: *"i just want to give you access
to the console"*, after a morning of pasting terminal output back and forth. It
is a fair ask and the copy-paste loop is how the 13 Aug deploy failure went
unnoticed for an hour.

## Why this shape and not SSH

Measured from the agent's container, not assumed: **outbound port 22 is
blocked**, no `ssh` client exists, and every egress goes through an HTTPS proxy.
So a shell session cannot be reached from there at all. The only channel that
can carry it is HTTPS, which means an MCP server here that the agent's client
dials — the reverse direction from SSH, and the reason this file exists rather
than a key in `authorized_keys`.

## What this must never become

**`mcp_server.py` exposes `place_order`. This one must never import it, and it
must never gain a broker path.** They are separate processes, separate
entrypoints and separate credentials for that reason. A console that could also
trade would put arbitrary shell and the order path behind one token, and the
whole architecture of this repository is that those are different privileges —
the same reasoning that puts the dreamer on its own Hermes instance and runs
chat as `hermes` rather than as `mudhorn`.

`tests/test_console_mcp.py` parses this module's AST and fails the build if it
imports `broker`, `risk`, `journal`, `mcp_server` or `models`.

## The shell is opt-in, and that is the whole safety argument

Six **named** operations are always available: update, service status, journal
tail, git state, disk and memory, and the wrapper self-test. Each one runs a
fixed argv with no interpolation from the caller except where a parameter is
validated against an allowlist. That set covers everything the operator and the
agent actually did by hand today.

`run_command` — arbitrary argv — exists and is **absent from the tool list**
unless `MUDHORN_CONSOLE_SHELL=1` is set in the unit. Off is the default and off
is the documented state. This is the `--execute` pattern and the chat-token
pattern again: anything that can act beyond a fixed set is switched on by a
person who decided to.

Even switched on it takes an **argv list, never a shell string**, so nothing is
word-split, globbed or passed to `sh -c`. A caller cannot reach `&&`, `|`, `>`
or `$(...)` through it.

## The token is the only thing between this and the internet

`MUDHORN_CONSOLE_TOKEN` is required and the process **refuses to start without
one**. That is deliberately unlike `DASHBOARD_PASSWORD`, where absent means no
gate and that is correct for a loopback deployment: a dashboard with no password
leaks figures, and this runs commands. There is no configuration of this server
where no-token is the right answer, so there is no such configuration.

Compared in constant time, and never echoed into a response or a log line —
the same rule as every other credential surface here.

It binds `127.0.0.1` like the dashboard. Reaching it from outside is a Tailscale
Funnel or a reverse proxy, which is a deliberate act with its own record.
"""

from __future__ import annotations

import hmac
import os
import subprocess
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

APP_DIR = Path(os.environ.get("MUDHORN_APP_DIR", "/opt/mudhorn"))

# Only these may be named to `service_status` and `journal_tail`. An allowlist
# rather than a pattern, because `systemctl status` takes globs and a caller who
# could pass one could enumerate every unit on the box.
UNITS = (
    "mudhorn-bot",
    "mudhorn-web",
    "mudhorn-backup.timer",
    "mudhorn-dream.timer",
    "mudhorn-confer.timer",
    "mudhorn-tailnet.timer",
)

# A command's whole output goes into a model's context, so it is bounded here
# rather than trusted to be small. `journalctl` on a busy unit is unbounded.
MAX_OUTPUT_CHARS = 20_000
DEFAULT_TIMEOUT = 300


def shell_enabled() -> bool:
    """Whether `run_command` is offered at all.

    Read at call time rather than captured at import, so the answer always
    describes the current environment rather than the one at startup.
    """
    return os.environ.get("MUDHORN_CONSOLE_SHELL", "").strip() == "1"


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    dropped = len(text) - MAX_OUTPUT_CHARS
    # Keep the TAIL. An error is at the end of a build log, and a truncated
    # head reads as a clean run — the same reason `journalctl` is read from
    # the bottom.
    return f"[... {dropped} characters dropped from the start ...]\n" + text[-MAX_OUTPUT_CHARS:]


def _run(argv: list[str], *, timeout: int = DEFAULT_TIMEOUT, cwd: Path | None = None) -> str:
    """Run a fixed argv and report what happened, including when it fails.

    **Never `shell=True` and never a string.** A list argv cannot be
    word-split, globbed, or reach `&&`, `|`, `>` or `$(...)`, which is what
    makes even the opt-in shell tool bounded.

    A non-zero exit is a RESULT, not an exception: the caller asked what
    happened and "it failed, here is stderr" is the answer. Raising would lose
    the output, which is the part worth having.
    """
    try:
        done = subprocess.run(  # fixed argv, never shell=True
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            check=False,
        )
    except FileNotFoundError:
        return f"$ {' '.join(argv)}\n\n(command not found: {argv[0]})"
    except subprocess.TimeoutExpired:
        return f"$ {' '.join(argv)}\n\n(timed out after {timeout}s)"
    except Exception as exc:  # a console must report, never take itself down
        return f"$ {' '.join(argv)}\n\n({type(exc).__name__}: {exc})"

    parts = [f"$ {' '.join(argv)}", f"exit={done.returncode}"]
    if done.stdout.strip():
        parts.append(done.stdout.rstrip())
    if done.stderr.strip():
        parts.append("--- stderr ---\n" + done.stderr.rstrip())
    return _clip("\n\n".join(parts))


server = MCPServer(
    name="mudhorn-console",
    instructions=(
        "Operate the Mudhorn droplet: deploy, read service state and logs, and "
        "check the git checkout. This server reaches NO broker and holds no "
        "trading credential — `place_order` lives in a different process. "
        "`run_command` is absent unless the operator switched it on."
    ),
)


@server.tool()
def deploy_update(stash: bool = False) -> str:
    """Pull, provision, restart and verify, via deploy/update.sh.

    Every step is asserted rather than assumed: the commit must actually move,
    the services must come back, and the deployed run-chat.sh must still refuse
    a model mismatch. A failed update stops before provisioning, so the box is
    left as it was rather than half-done.

    Set `stash` only when a dirty tree is expected and you have READ the diff.
    Local changes on the box are sometimes the only copy of something.
    """
    argv = [str(APP_DIR / "deploy" / "update.sh")]
    if stash:
        argv.append("--stash")
    return _run(argv, timeout=900)


@server.tool()
def service_status(unit: str = "mudhorn-bot") -> str:
    """Whether a unit is running, and what it last said.

    `unit` must be one of the known Mudhorn units. An allowlist rather than a
    pattern, because `systemctl status` accepts globs.
    """
    if unit not in UNITS:
        return f"unknown unit {unit!r}. Known: {', '.join(UNITS)}"
    return _run(["systemctl", "status", unit, "--no-pager", "--lines=20"], timeout=30)


@server.tool()
def journal_tail(unit: str = "mudhorn-bot", lines: int = 50, since_start: bool = False) -> str:
    """Recent log lines for a unit.

    `since_start` scopes to the CURRENT run rather than the last N lines, which
    matters after a restart: a unit that failed earlier logs far more than the
    live run, so `-n 50` shows the dead one. It is also how a startup banner is
    found, since those print once and scroll away.
    """
    if unit not in UNITS:
        return f"unknown unit {unit!r}. Known: {', '.join(UNITS)}"
    argv = ["journalctl", "-u", unit, "--no-pager"]
    if since_start:
        stamp = subprocess.run(
            ["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value", unit],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if stamp:
            argv += ["--since", stamp]
    argv += ["-n", str(max(1, min(lines, 500)))]
    return _run(argv, timeout=60)


@server.tool()
def git_state() -> str:
    """Branch, commit and working-tree state of the checkout.

    Reports STAGED and unstaged separately, because that distinction cost a
    deploy: `git diff` shows unstaged only, so a staged edit printed nothing
    while `git pull` kept refusing, and `git checkout -- <file>` restored the
    modified version back out of the index and reported success.
    """
    git = ["git", "-c", f"safe.directory={APP_DIR}", "-C", str(APP_DIR), "--no-pager"]
    return "\n\n".join(
        (
            _run([*git, "log", "-1", "--format=%h %s"], timeout=30),
            _run([*git, "status", "--porcelain=v1", "--branch"], timeout=30),
            _run([*git, "diff", "--stat", "HEAD"], timeout=30),
            _run([*git, "stash", "list"], timeout=30),
        )
    )


@server.tool()
def disk_and_memory() -> str:
    """Free disk and memory. The two things that end a droplet quietly."""
    return "\n\n".join((_run(["df", "-h", "/"], timeout=30), _run(["free", "-h"], timeout=30)))


@server.tool()
def wrapper_selftest() -> str:
    """Prove the deployed run-chat.sh still refuses a model mismatch.

    The same sandbox `update.sh` uses: a deliberately mismatched inference.env
    and .hermes/config.yaml in a temporary directory, the real wrapper run
    against them with a stub binary. **It touches /home/hermes not at all** —
    mutating an agent's credentials file to test it is not a trade worth making.

    A pass is exit 78 with both model names in the message. Anything else means
    the box is announcing a model it has not checked.
    """
    wrapper = APP_DIR / "deploy" / "run-chat.sh"
    if not wrapper.exists():
        return f"{wrapper} does not exist"

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        (home / ".hermes").mkdir()
        (home / "bin").mkdir()
        stub = home / "bin" / "hermes"
        stub.write_text('#!/bin/sh\necho "stub hermes answered"\n')
        stub.chmod(0o755)
        (home / "inference.env").write_text("DO_INFERENCE_KEY=sandbox\nDO_INFERENCE_MODEL=model-a\n")
        (home / ".hermes" / "config.yaml").write_text("model:\n  default: model-b\n")

        done = subprocess.run(
            [str(wrapper)],
            input="hi",
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={**os.environ, "HERMES_HOME": str(home), "HERMES_BIN": str(stub)},
        )

    verdict = (
        "PASS"
        if done.returncode == 78 and "model-a" in done.stderr and "model-b" in done.stderr
        else "FAIL"
    )
    return f"{verdict}: exit={done.returncode}\n\n{_clip(done.stderr.strip())}"


if shell_enabled():

    @server.tool()
    def run_command(argv: list[str], timeout: int = DEFAULT_TIMEOUT) -> str:
        """Run an arbitrary command. Only registered when the operator enabled it.

        **An argv LIST, never a shell string.** Nothing is word-split, globbed,
        or passed to `sh -c`, so `&&`, `|`, `>` and `$(...)` are unreachable
        through this. Pass `["systemctl", "status", "nginx"]`, not
        `"systemctl status nginx"`.
        """
        if not argv:
            return "argv is empty"
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            return "argv must be a list of strings"
        return _run(argv, timeout=max(1, min(timeout, 900)))


def _token() -> str:
    return os.environ.get("MUDHORN_CONSOLE_TOKEN", "").strip()


def build_app(token: str, *, port: int = 8788) -> Any:
    """The MCP app behind a constant-time bearer check.

    Written as middleware rather than a per-route dependency for the reason
    `web/auth.py` gives: a route added later is protected by default, where a
    list of guarded paths is a list somebody forgets to add to. There is no
    equivalent of `OPEN_PATHS` here, because nothing this server does should
    ever be reachable without the token.
    """
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    # **DNS-rebinding protection, and it MUST be told the public hostname.**
    #
    # The SDK validates the Host header and ships allowing loopback only, which
    # is right for a server dialled directly and wrong for every deployment
    # this one is built for: a Tailscale Funnel forwards to 127.0.0.1:8788 with
    # the ORIGINAL Host intact, so the request arrives saying
    # `mudhorn.tailc04415.ts.net` and is refused with "Invalid Host header".
    #
    # Found by curling the live Funnel rather than by reading. The failure is
    # worth describing because of its ORDER: the bearer gate below runs first,
    # so an unauthenticated probe still got a clean 401 and the endpoint looked
    # correct. Only a request with a VALID token reached the host check and
    # failed. A setup verified with a wrong token would have reported success.
    #
    # Names come from MUDHORN_CONSOLE_HOSTS, comma-separated. Loopback is always
    # allowed, so a local `--host 127.0.0.1` deployment needs no configuration
    # and the variable only carries what a proxy adds.
    # The Host header carries the PORT, so the allowlist has to as well: a list
    # holding `127.0.0.1` does not match a request saying `127.0.0.1:8803`.
    # Hence the port is passed in rather than assumed — the first version
    # hardcoded 8788 and refused its own loopback on any other port.
    hosts = [
        "127.0.0.1",
        "localhost",
        f"127.0.0.1:{port}",
        f"localhost:{port}",
    ]
    origins: list[str] = []
    for name in os.environ.get("MUDHORN_CONSOLE_HOSTS", "").split(","):
        name = name.strip()
        if not name:
            continue
        hosts.append(name)
        # A browser-originated request carries Origin as a full URL. Both
        # schemes are allowed rather than guessed at: the Funnel terminates TLS
        # and forwards plain HTTP, so which one arrives is not this process's
        # business to predict.
        origins.extend([f"https://{name}", f"http://{name}"])

    app = server.streamable_http_app(
        transport_security=TransportSecuritySettings(
            allowed_hosts=hosts,
            allowed_origins=origins,
        )
    )

    class BearerGate(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Any:
            # **Gate `/mcp` and let everything else 404. A 401 anywhere else is
            # a positive claim, and it is what stopped the connector attaching.**
            #
            # Measured from the box's own journal rather than reasoned about,
            # after two wrong guesses. The connector probes in this order:
            #
            #     GET  /.well-known/oauth-protected-resource   404
            #     GET  /.well-known/oauth-authorization-server 404
            #     POST /mcp                                    401
            #     POST /mcp?key=...                            200   <- it works
            #     POST /register                               401   <- it stops
            #
            # The MCP connection itself succeeded. What failed was the
            # registration fallback afterwards: a **401** on `/register` says
            # *this endpoint exists and you are not authorised*, so the client
            # concludes there is a sign-in service it cannot reach. **404** says
            # there is no registration here and the credential in the URL is the
            # whole story.
            #
            # Listing paths was the first fix and it was the wrong shape — it
            # covered `/.well-known/` and the next probe was somewhere else. So
            # the rule is inverted: **only the MCP endpoint is gated**, and every
            # other path falls through to the application, which mounts nothing
            # else and answers 404 by itself.
            #
            # That exempts nothing that works. A 404 discloses only that a
            # server exists, which the URL already told whoever is holding it,
            # and there is no route behind it to reach.
            if not request.url.path.rstrip("/").startswith("/mcp"):
                return await call_next(request)

            supplied = request.headers.get("authorization", "")
            prefix = "Bearer "
            offered = supplied[len(prefix) :] if supplied.startswith(prefix) else ""

            # **The query parameter exists because the client cannot send a
            # header.** claude.ai's "Add custom connector" form takes a URL and
            # OAuth credentials and offers no custom-header field at all, so a
            # bearer token has nowhere to go. The alternatives were implementing
            # an OAuth authorisation server for a single-operator console, or
            # this.
            #
            # **It is genuinely weaker and the docs say so rather than pretending
            # otherwise.** A URL travels further than a header: into browser
            # history, into `Referer`, into proxy and webserver access logs, and
            # into a screenshot of a settings page. A header does none of that.
            # What makes it acceptable HERE is narrow and worth stating: the
            # exposure is a Tailscale Funnel the operator controls, the token is
            # rotatable with one command, and the box is paper-trading. It would
            # not be acceptable for the order path, which is why the order path
            # is a different process this one cannot reach.
            #
            # The header is preferred when present, so a client that CAN send
            # one is never downgraded to the weaker channel.
            if not offered:
                offered = request.query_params.get("key", "")

            # compare_digest on every request, including the empty one, so a
            # missing credential and a wrong one take the same path and the same
            # time. The response never says which it was.
            if not hmac.compare_digest(offered, token):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    app.add_middleware(BearerGate)
    return app


def main() -> int:
    """Entry point for `electrum-bot-console`. Speaks MCP over streamable HTTP."""
    import argparse

    import uvicorn

    from .announce import say

    parser = argparse.ArgumentParser(description="Mudhorn console MCP server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()

    token = _token()
    if not token:
        say(
            "MUDHORN_CONSOLE_TOKEN is not set, and this server refuses to start "
            "without it. Unlike DASHBOARD_PASSWORD, there is no deployment where "
            "an ungated console is the right answer: this one runs commands.",
        )
        return 78
    if len(token) < 32:
        say(
            f"MUDHORN_CONSOLE_TOKEN is {len(token)} characters. This is the only "
            "thing between a public URL and a shell on the trading box. Use at "
            "least 32: `openssl rand -hex 32`.",
        )
        return 78

    say(
        f"Mudhorn console on {args.host}:{args.port}/mcp, app dir {APP_DIR}.",
        (
            "run_command is ENABLED (MUDHORN_CONSOLE_SHELL=1): arbitrary argv is "
            "reachable with this token."
            if shell_enabled()
            else "run_command is OFF. Six named operations only. Set "
            "MUDHORN_CONSOLE_SHELL=1 in the unit to offer arbitrary argv."
        ),
        "This server reaches no broker and holds no trading credential.",
    )
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        say(
            f"WARNING: binding to {args.host} publishes this beyond the local "
            "machine directly. Prefer 127.0.0.1 plus a Tailscale Funnel."
        )

    uvicorn.run(build_app(token, port=args.port), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
