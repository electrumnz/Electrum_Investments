"""Bridge from the dashboard to Hermes.

The dashboard renders what happened; Hermes answers questions about it. This
module is the seam between them, and it is deliberately thin: **no agent logic
lives here.** Memory, approvals, the deny rules and the MCP tool surface all
stay in Hermes, where they are configured once and apply to every surface. A
second half-agent implemented in the web app would be a second place for those
rules to be wrong.

## Why a subprocess

Hermes offers a JSON-RPC/WebSocket gateway (`hermes serve`) that would be the
better long-term transport. Its protocol was not verifiable when this was
written, and guessing at method names in a component that reaches a brokerage
account is not a trade worth making, so this uses `hermes -z` — documented as
"single prompt in, final response text out, nothing else on stdout or stderr".

`ask()` is the only thing the route calls. Swapping the transport later means
replacing its body and nothing else.

## Why it runs as another user

The web app runs as `mudhorn`, which holds the broker credentials. Hermes runs
as `hermes`, which does not. Keeping that split means the agent's shell stays
uninteresting even though the process driving it is privileged. No escalation
is created in either direction: `mudhorn` already has everything the bot has,
and `hermes` gains nothing it did not already have.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import structlog

from ..souls import Soul

log = structlog.get_logger(__name__)

# The root-owned wrapper, NOT the Hermes binary itself. The flags are fixed in
# deploy/run-chat.sh so an untrusted prompt cannot become `--yolo`, and it sits
# under /opt/mudhorn where this process can stat it — /home/hermes is 0700, and
# `Path.exists()` raises rather than returning False on a directory it may not
# traverse, which was a 500 on the Chat page.
#
# That placement does NOT let the unit keep `ProtectHome`. Hermes runs from its
# own home, and systemd sandboxing is a mount namespace that every child
# inherits — sudo does not escape one — so the wrapper, running as `hermes`
# inside this service's namespace, still needs /home/hermes visible. The unit
# file explains it where someone tightening it would look.
DEFAULT_BINARY = Path("/opt/mudhorn/deploy/run-chat.sh")
DEFAULT_USER = "hermes"

# Generous: a question that fans out across MCP tools takes a while, and the
# failure we care about is a hang, not slowness.
DEFAULT_TIMEOUT_SECONDS = 180.0

# How much conversation to replay. `hermes -z` is one-shot, so continuity is the
# caller's job. Kept small on purpose — this is a question-and-answer surface,
# not somewhere to hold a long argument, and every replayed turn is paid for.
HISTORY_TURNS = 6


@dataclass(frozen=True)
class ChatReply:
    text: str
    ok: bool = True
    error: str = ""

    @classmethod
    def failed(cls, error: str) -> ChatReply:
        return cls(text="", ok=False, error=error)


@dataclass
class HermesBridge:
    """Runs one Hermes turn per call."""

    binary: Path = DEFAULT_BINARY
    run_as: str = DEFAULT_USER
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @property
    def available(self) -> bool:
        """Whether Hermes looks runnable from here. Never raises.

        `Path.exists()` does NOT return False when a parent directory refuses
        traversal — it raises `PermissionError`, and that took the whole Chat
        page down with a 500. `/home/hermes` is 0700 and owned by `hermes`,
        while this process runs as `mudhorn`, which is the user split working
        exactly as intended.

        **A permission error is answered optimistically, and that is the
        deliberate part.** It says something about this process's reach and
        nothing about whether Hermes is installed — and reach is not what
        matters here, because the binary is never executed directly. It is
        invoked through `sudo -u hermes`, which can stat and run a file this
        process cannot even see. Reporting "not installed" on an EACCES would
        put a confidently wrong message on the page about a working
        installation.

        So: a definite "no such file" is absent, a missing `sudo` is absent,
        and anything we cannot determine is left to the attempt itself, which
        reports the real error rather than a guess at it.
        """
        if shutil.which("sudo") is None:
            return False
        try:
            return self.binary.exists()
        except OSError:
            return True

    def ask(
        self,
        message: str,
        history: list[tuple[str, str]] | None = None,
        soul: Soul | None = None,
    ) -> ChatReply:
        """One Hermes turn, optionally in character.

        The soul is prepended to the prompt rather than passed as a flag,
        because the wrapper takes no arguments on purpose: the sudoers rule
        names `run-chat.sh` with nothing after it, so everything this process
        wants to say goes down stdin where it cannot be read as an option.

        A missing soul is not an error. `load_soul` returns an absent one, whose
        prefix is empty, and the agent answers plainly. See `souls.py` for why
        that is the right failure direction.
        """
        if not message.strip():
            return ChatReply.failed("empty message")
        if not self.available:
            return ChatReply.failed(
                f"The chat wrapper is not present at {self.binary}. "
                "See docs/HERMES_SETUP.md."
            )

        prompt = _with_history(message, history or [])
        if soul is not None and soul.found:
            prompt = f"{soul.prompt_prefix()}\n{prompt}"

        # argv list, never a shell string: the message is untrusted input and
        # this process holds the broker credentials. No shell means no quoting
        # bug can turn a question into a command.
        #
        # And the prompt is not in argv at all — it goes down stdin. The
        # sudoers rule permits this wrapper with no arguments, so there is
        # nothing for a crafted question to be mistaken for. See run-chat.sh.
        argv = ["sudo", "-n", "-u", self.run_as, "--", str(self.binary)]

        try:
            completed = subprocess.run(
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log.warning("hermes_timeout", timeout=self.timeout_seconds)
            return ChatReply.failed(
                f"Hermes did not answer within {self.timeout_seconds:.0f}s. "
                "It may be waiting on an approval prompt it cannot show here."
            )
        except OSError as exc:
            log.warning("hermes_spawn_failed", error=str(exc))
            return ChatReply.failed(f"Could not start Hermes: {exc}")

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            log.warning("hermes_nonzero_exit", code=completed.returncode, detail=detail[:500])
            return ChatReply.failed(detail[:1000] or f"Hermes exited {completed.returncode}")

        return ChatReply(text=completed.stdout.strip())


def _with_history(message: str, history: list[tuple[str, str]]) -> str:
    """Prepend recent turns, because `-z` carries no conversation state.

    Hermes' own persistent memory survives across calls; this covers the
    narrower case of "it" and "that" referring to the previous exchange.
    """
    if not history:
        return message

    lines = ["Earlier in this conversation:"]
    for user_said, agent_said in history[-HISTORY_TURNS:]:
        lines.append(f"  User: {user_said}")
        lines.append(f"  You: {agent_said}")
    lines.append("")
    lines.append(f"Now: {message}")
    return "\n".join(lines)
