"""Loading the character files the agents speak in.

A soul is plain Markdown in `souls/`, prepended to an agent's prompt. See
`souls/README.md` for what one is; this module only finds it, reads it, and
refuses to let its absence take a surface down.

## Why this degrades instead of raising

The soul files are read from disk at call time rather than compiled in, so that
editing one changes an agent without a deploy. That convenience buys a real
failure mode: a file that is missing on the box, or a directory the service
account cannot read, would otherwise turn a personality into a 500 on the Chat
page.

So a missing soul yields `Soul.absent()`, which carries no voice and says
plainly that it is running without one. The agent still answers, still reaches
the same tools, and is still bound by the same risk gate, because none of that
was ever the soul's job. A voiceless answer is a small loss. A dead Chat page
on the surface an operator opens to find out what is wrong is a large one.

This mirrors `HermesBridge.available`: the optimistic branch is chosen wherever
being wrong would put a confident, incorrect message in front of someone.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

# Repository root, from `src/bot/souls.py`. Overridable for tests, which must
# never depend on a file outside their own tmp_path.
DEFAULT_SOULS_DIR = Path(__file__).resolve().parents[2] / "souls"

# The two that exist. Named as constants so a typo at a call site is an
# AttributeError here rather than a silently voiceless agent there.
YODA = "yoda"
GROGU = "grogu"


@dataclass(frozen=True)
class Soul:
    """A character file, or the honest absence of one."""

    name: str
    text: str
    found: bool = True

    @classmethod
    def absent(cls, name: str) -> Soul:
        return cls(name=name, text="", found=False)

    def prompt_prefix(self, operator: str = "") -> str:
        """What goes in front of the agent's own instructions.

        The framing sentence matters as much as the file. Without it the model
        receives a character sketch with no indication of what to do with it,
        and the most common result is that it starts describing the character
        rather than being it.

        The final clause is the same rule the soul files carry in their own
        Never sections, restated here because this is the text that actually
        reaches the model. Saying it twice is cheap; a soul file edited on the
        box without it would be a persona with no guard rail.
        """
        if not self.found:
            return ""

        # Who is being spoken to. Empty on a deployment that never set
        # OPERATOR_NAME, and the sentence is simply omitted rather than
        # addressing a blank. It reaches the model only from behind the login,
        # because that is the only place the name is rendered at all.
        address = (
            f"You are speaking to {operator.strip()}. Address them by name when "
            "it is natural to, and never more than once in a reply.\n\n"
            if operator.strip()
            else ""
        )
        return (
            "You have a character, described below. Speak in it.\n\n"
            "It shapes how you say things and never what is true. Figures, "
            "symbols, dates and quantities are quoted exactly as the tools "
            "returned them, and anything you cannot say in character you say "
            "plainly instead.\n\n"
            f"{address}"
            f"--- begin character: {self.name} ---\n"
            f"{self.text.strip()}\n"
            f"--- end character: {self.name} ---\n"
        )


def load_soul(name: str, *, souls_dir: Path | None = None) -> Soul:
    """Read one soul. Never raises.

    An unreadable soul is logged and reported as absent, for the reason in the
    module docstring: this is decoration on a surface whose job is to keep
    working.
    """
    directory = souls_dir or DEFAULT_SOULS_DIR
    path = directory / f"{name}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("soul_unreadable", soul=name, path=str(path), error=str(exc))
        return Soul.absent(name)

    if not text.strip():
        log.warning("soul_empty", soul=name, path=str(path))
        return Soul.absent(name)

    return Soul(name=name, text=text)
