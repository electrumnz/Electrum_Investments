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

from dataclasses import dataclass
from pathlib import Path

import structlog

from ..hermes import DEFAULT_TIMEOUT_SECONDS, DEFAULT_USER, HermesRunner
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

# The dreamer's own instance, when one is installed. A SECOND Hermes rather than
# a flag on the first, and the reason is not the character file:
#
# The account agent's Hermes registers this repo's MCP server, which exposes
# `place_order`. Sharing that instance means the only thing keeping a
# speculative agent away from the broker is the sentence in `souls/grogu.md`
# telling it not to — prose, where this repository uses a structure everywhere
# else. `RiskGate.evaluate` still runs on every order path either way, so the
# operator's four rules hold; but "it has no broker tool" and "it has one and
# was asked nicely" are different claims and only one is worth making.
#
# A second `HERMES_HOME` also gives the dreamer its own memory and its own
# native SOUL.md, since Hermes holds exactly one soul per instance.
#
# Absent, the dreamer falls back to the shared instance and the Dreaming page
# SAYS SO. It does not quietly claim an isolation it does not have — the same
# rule as `calendar_degraded` and the tailnet status: report the weaker fact
# rather than imply the stronger one.
DREAMER_BINARY = Path("/opt/mudhorn/deploy/run-dream.sh")

# Re-exported so existing callers and tests keep one import site. The
# mechanics live in `bot.hermes` because THREE instances share them and the
# `permitted` check is not something to have two copies of.
__all__ = ["DEFAULT_BINARY", "DREAMER_BINARY", "HISTORY_TURNS", "ChatReply", "HermesBridge"]

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
class HermesBridge(HermesRunner):
    """Runs one Hermes turn per call, in character.

    The process mechanics — `available`, `permitted`, and running the wrapper
    with the prompt on stdin — live in `bot.hermes.HermesRunner`, because three
    instances share them and `permitted` is not a check to have two copies of.
    See that module for why the two availability questions default in opposite
    directions.

    What this adds is the CONVERSATION: a soul, replayed history, and a
    briefing of figures the agent must not derive.
    """

    binary: Path = DEFAULT_BINARY
    run_as: str = DEFAULT_USER
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def ask(
        self,
        message: str,
        history: list[tuple[str, str]] | None = None,
        soul: Soul | None = None,
        operator: str = "",
        briefing: str = "",
    ) -> ChatReply:
        """One Hermes turn, optionally in character.

        The soul is prepended to the prompt rather than passed as a flag,
        because the wrapper takes no arguments on purpose: the sudoers rule
        names `run-chat.sh` with nothing after it, so everything this process
        wants to say goes down stdin where it cannot be read as an option.

        A missing soul is not an error. `load_soul` returns an absent one, whose
        prefix is empty, and the agent answers plainly. See `souls.py` for why
        that is the right failure direction.

        `briefing` is FIGURES the agent must not derive, rendered by the caller
        and placed between the character and the conversation. The settings
        agent is what needs it: an agent asked to work out what doubling a
        per-trade cap costs will produce a number, state it confidently, and be
        believed. So `settings_agent.render_briefing` computes the limits and
        their consequences in Python and this hands them over — the same rule as
        `indicators.py`, arriving at the chat surface. It is a per-call argument
        rather than something this module assembles, because the bridge holds no
        agent logic and must not become the second place any of it lives.
        """
        if not message.strip():
            return ChatReply.failed("empty message")
        if not self.available:
            return ChatReply.failed(
                f"The chat wrapper is not present at {self.binary}. "
                "See docs/HERMES_SETUP.md."
            )

        prompt = _with_history(message, history or [])
        # After the character and before the conversation: the agent should read
        # the figures as context it was handed, not as something it said.
        if briefing.strip():
            prompt = f"{briefing.strip()}\n\n{prompt}"
        if soul is not None and soul.found:
            prompt = f"{soul.prompt_prefix(operator)}\n{prompt}"

        # The prompt is not in argv at all — it goes down stdin, and the
        # sudoers rule permits this wrapper with no arguments, so there is
        # nothing for a crafted question to be mistaken for. `HermesRunner.run`
        # holds that and the no-shell rule; see `bot/hermes.py`.
        result = self.run(prompt)
        if not result.ok:
            return ChatReply.failed(result.error)
        return ChatReply(text=result.text)


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
