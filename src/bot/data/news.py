"""News/sentiment adapter — supplies a short blurb of recent headlines for context.

## Two renderings of one item, and the second exists for the dreamer

`recent_headlines` returns plain lines and is what the DECISION LOOP reads.
`recent_attributed` returns the same items carrying the publisher and the URL,
and is what the DREAMER reads. Two methods rather than one richer line, for a
reason that is easy to mistake for tidiness and is not:

The loop's model cannot open a link. A URL on every headline would be input
tokens spent on something unreadable, ninety-six times a day — and worse, a
model handed a link it cannot follow is being invited to pretend it did.
`xfeed.Post.render` already says exactly that about permalinks, and it is right.

**That reasoning stops holding the moment a researcher exists that CAN open
one.** The dreamer writes causal chains whose hops are marked `checked` only
when a source can be named, and its whole difficulty is that it has no way to
name one. A headline the loop fetched this morning — with a publisher and a
live URL sitting in the API response — was being rendered into the dreamer's
prompt as bare prose, so the one hop in a chain with a real source behind it
could not be sourced. The attribution was fetched and then dropped by our own
parser, which is the same provenance laundering `research.Citation` refuses one
layer up.

So the loop keeps the lean line, the dreamer gets the attributed one, and
neither pays for the other's needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Sourced:
    """A feed item with enough attribution for somebody to go and check it.

    Shared by the headline feed and the post feed because the requirement is
    the same in both places and a second implementation would be a second set
    of whitespace rules to get wrong.

    **Every field is collapsed whitespace by the time it arrives here**, and
    that is the caller's job rather than this type's — see `marketaux._parse`
    for the reasoning at length. The short version: these are rendered as
    bullets into a markdown document a model reads, so a value carrying a
    newline can close its bullet and open its own `##` section.
    """

    line: str
    url: str = ""
    publisher: str = ""

    @property
    def is_attributable(self) -> bool:
        """Whether a reader could open the thing this line came from.

        A separate question from "is there a URL string", and named rather than
        applied silently, for the same reason `research.Citation` names it: an
        item that cannot be traced is one the dreamer must not cite, and it has
        to be able to tell the difference between that and having no item.
        """
        return bool(self.url.strip())

    def render(self) -> str:
        """The line with its source, or just the line when there is none.

        No placeholder for a missing URL. An empty attribution rendered as
        `(source unknown)` reads as a property of the story; it is a property
        of our parse, and a dreamer weighing whether it can cite something is
        better served by the absence than by a phrase about it.
        """
        if not self.is_attributable:
            return self.line
        who = f"{self.publisher} " if self.publisher else ""
        return f"{self.line} — {who}{self.url}"


class NewsFeed(Protocol):
    def recent_headlines(self, symbols: list[str], *, limit: int = 10) -> list[str]: ...

    def recent_attributed(self, symbols: list[str], *, limit: int = 10) -> list[Sourced]: ...


class EmptyNews:
    """No-op adapter — used in Phase A before a real feed is wired up."""

    def recent_headlines(self, symbols: list[str], *, limit: int = 10) -> list[str]:
        del symbols, limit
        return []

    def recent_attributed(self, symbols: list[str], *, limit: int = 10) -> list[Sourced]:
        del symbols, limit
        return []


class StaticNews:
    """Test adapter — returns canned headlines.

    `recent_attributed` answers with unattributable items rather than inventing
    a URL, which makes it the honest double for a feed whose payload carried no
    link. A test wanting attributed headlines constructs `Sourced` directly.
    """

    def __init__(self, headlines: list[str], attributed: list[Sourced] | None = None) -> None:
        self._headlines = list(headlines)
        self._attributed = list(attributed) if attributed is not None else None

    def recent_headlines(self, symbols: list[str], *, limit: int = 10) -> list[str]:
        del symbols
        return self._headlines[:limit]

    def recent_attributed(self, symbols: list[str], *, limit: int = 10) -> list[Sourced]:
        del symbols
        if self._attributed is not None:
            return self._attributed[:limit]
        return [Sourced(line=h) for h in self._headlines[:limit]]
