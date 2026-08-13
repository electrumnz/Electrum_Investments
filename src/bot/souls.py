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

import re
from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

# Repository root, from `src/bot/souls.py`. Overridable for tests, which must
# never depend on a file outside their own tmp_path.
DEFAULT_SOULS_DIR = Path(__file__).resolve().parents[2] / "souls"

# The three that exist. Named as constants so a typo at a call site is an
# AttributeError here rather than a silently voiceless agent there.
YODA = "yoda"
GROGU = "grogu"
ARMORER = "armorer"
# The researcher (TODO item 26). Deliberately NOT in `KNOWN_SOULS` below, and
# that is the interesting part rather than an omission — see the note there.
KUIIL = "kuiil"

# The set a request body is checked against before it reaches `load_soul`, which
# builds a path from the name. One list, here, rather than a literal repeated at
# every call site: a soul added to `souls/` and forgotten here is a character
# nobody can select, and a name NOT in this set reaching a path join is a
# traversal.
#
# An unknown name falls back to `YODA` at the call site rather than raising. The
# worst case of getting it wrong is the wrong voice; refusing the question
# outright would be the larger failure. See `bot.web.app.chat`.
KNOWN_SOULS = frozenset({YODA, GROGU, ARMORER})

# Every character file that ships, which is a DIFFERENT question from which
# ones a chat request may name, and the two sets stopped being the same when
# the researcher arrived.
#
# `KUIIL` is in this one and not in `KNOWN_SOULS`, deliberately. The researcher
# is not a voice on the Chat page: it runs on the dream timer, in its own
# Hermes home, with the `web` toolset and no MCP server — see
# `deploy/run-research.sh`. Letting a request body select it would put a
# character whose entire job is quoting other people's pages into the process
# that answers questions about the account, which is the wrong voice on the
# wrong tools.
#
# Both sets exist because the test that pins them is enumerating the DIRECTORY
# now rather than a hand-maintained tuple. A soul added to `souls/` and
# registered nowhere is a character nobody can reach; a soul in `KNOWN_SOULS`
# with no file is a name that falls back silently. Only one list can be
# checked against the filesystem, and it has to be this one.
ALL_SOULS = frozenset({YODA, GROGU, ARMORER, KUIIL})


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

        The rule about figures is the same one the soul files carry in their own
        What-to-avoid sections, restated here because this is the text that
        actually reaches the model. Saying it twice is cheap; a soul file edited
        on the box without it would be a persona with no guard rail.

        The sentence about length is here for a weaker but real reason. Each
        file sets its own budget in `## How long to be`, and a model handed a
        character reliably reads it as a licence to write more — the observed
        failure was a one-line figure arriving inside three paragraphs. So the
        instruction that a character is a way of saying something *shorter* sits
        in the wrapper, where it applies to every soul including one added
        later. It is style rather than safety, which is why it is one clause
        rather than a section.
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
            "You have a character, described below. Speak in it. Do not "
            "announce it, describe it or perform it: the character is in the "
            "word choice and the rhythm, and it is a reason to say something "
            "SHORTER rather than a licence to say more. Lead with the answer.\n\n"
            "It shapes how you say things and never what is true. Figures, "
            "symbols, dates and quantities are quoted exactly as the tools "
            "returned them, and anything you cannot say in character you say "
            "plainly instead.\n\n"
            f"{address}"
            f"--- begin character: {self.name} ---\n"
            f"{self.text.strip()}\n"
            f"--- end character: {self.name} ---\n"
        )


#: A soul name that can only ever be a filename inside `souls_dir`.
#:
#: Letters, digits, underscore and hyphen. No dot, no slash, no backslash, so
#: neither `..` nor an absolute path survives.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


def load_soul(name: str, *, souls_dir: Path | None = None) -> Soul:
    """Read one soul. Never raises.

    An unreadable soul is logged and reported as absent, for the reason in the
    module docstring: this is decoration on a surface whose job is to keep
    working.

    **A name that is not a bare token is refused here, not only at the call
    site.** `KNOWN_SOULS` above says a name outside it "reaching a path join is
    a traversal", and every caller today does check — `bot.web.app.chat` maps an
    unknown name to `YODA`, and the other three pass a constant. That is a
    guarantee held by four callers remembering, which is the shape that failed
    when `/live` was left out of the hand-maintained route list.

    Measured, so it is a fact rather than a worry: `load_soul("../CLAUDE")`
    returned a 180 KB file from the repository root, wrapped in
    `--- begin character ---` and ready to be sent to a model as a personality;
    `souls_dir / "/etc/anything.md"` discards the directory entirely, because a
    join with an absolute path does. The `.md` suffix narrows what can be read
    and does not stop it.

    Refusing yields `Soul.absent`, which is the same answer a missing file
    gives, so the failure direction is unchanged: a voiceless agent rather than
    a dead page. The call site's fallback to `YODA` still runs first and is
    still the better answer; this is the lock underneath it.
    """
    directory = souls_dir or DEFAULT_SOULS_DIR
    if not _SAFE_NAME.match(name):
        log.warning("soul_name_refused", soul=name)
        return Soul.absent(name)
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
