"""Economic calendar adapter — produces NewsWindow entries the risk gate consumes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from ..risk import NewsWindow


class CalendarFeed(Protocol):
    def upcoming_windows(self, *, lookahead_minutes: int) -> list[NewsWindow]: ...


class EmptyCalendar:
    """No-op adapter — used in Phase A before a real feed is wired up."""

    def upcoming_windows(self, *, lookahead_minutes: int) -> list[NewsWindow]:
        del lookahead_minutes
        return []


class StaticCalendar:
    """Test adapter — returns whatever windows you constructed it with."""

    def __init__(self, windows: list[NewsWindow]) -> None:
        self._windows = list(windows)

    def upcoming_windows(self, *, lookahead_minutes: int) -> list[NewsWindow]:
        now = datetime.now(UTC)
        from datetime import timedelta

        cutoff = now + timedelta(minutes=lookahead_minutes)
        return [w for w in self._windows if now <= w.timestamp <= cutoff]
