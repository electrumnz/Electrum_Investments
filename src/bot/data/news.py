"""News/sentiment adapter — supplies a short blurb of recent headlines for context."""

from __future__ import annotations

from typing import Protocol


class NewsFeed(Protocol):
    def recent_headlines(self, symbols: list[str], *, limit: int = 10) -> list[str]: ...


class EmptyNews:
    """No-op adapter — used in Phase A before a real feed is wired up."""

    def recent_headlines(self, symbols: list[str], *, limit: int = 10) -> list[str]:
        del symbols, limit
        return []


class StaticNews:
    """Test adapter — returns canned headlines."""

    def __init__(self, headlines: list[str]) -> None:
        self._headlines = list(headlines)

    def recent_headlines(self, symbols: list[str], *, limit: int = 10) -> list[str]:
        del symbols
        return self._headlines[:limit]
