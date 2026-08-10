"""Economic calendar adapter — produces NewsWindow entries the risk gate consumes."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

import structlog

from ..risk import NewsWindow

if TYPE_CHECKING:  # `Env` and `Rules` are annotations only, and `config` is
    # imported by nearly everything — keeping it out of this module's runtime
    # imports keeps the feed layer at the bottom of the dependency graph.
    from ..config import Env, Rules

log = structlog.get_logger(__name__)


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


def build_calendar_feed(
    env: Env, rules: Rules, *, extra_symbols: Sequence[str] = ()
) -> CalendarFeed:
    """Finnhub's earnings calendar when a key is present, otherwise nothing.

    Unlike headlines this one feeds a risk rule: with no calendar,
    `RiskGate._news_blackout` has no windows and therefore never fires. That is
    a real gap rather than a preference, so it warns.

    **It lives here rather than in `main` because it has two callers now.** The
    decision loop had one and the MCP order path had none at all — so
    `check_order` and `place_order` ran the gate with no windows, and the
    earnings blackout has never applied to an order placed through chat or by
    the operator. A builder that only the loop could reach is how a gate rule
    ends up enforced on one path and not the other.

    The `FinnhubCalendar` import is local for the reason
    `DreamingRules.vault_caps` gives: this module defines the calendar
    PROTOCOL, and every consumer of the protocol should not have to load an
    HTTP adapter it may never call.

    **`extra_symbols` is how a granted symbol gets an earnings blackout.** The
    feed was built once with `rules.allowed_symbols`, so a symbol an adopted
    dream permits was never in the set windows were fetched for and
    `RiskGate._news_blackout` could not fire for it — the rule's logic was fine
    and its input was narrowed. It is a constructor argument rather than
    something a caller assigns to `.symbols` afterwards because the feed caches
    windows it has already filtered against its symbol list: mutating the
    attribute leaves that cache in place, which looks fixed and behaves
    inconsistently, and that is worse than the open gap. Callers rebuild the
    feed when the set changes.
    """
    from .finnhub import FinnhubCalendar

    if not env.finnhub_api_key:
        log.warning(
            "no_finnhub_key_news_blackout_inactive",
            detail=(
                "Without an earnings calendar the news blackout rule in "
                "config/rules.yaml cannot fire. Trades will not be held back "
                "around announcements."
            ),
        )
        return EmptyCalendar()
    return FinnhubCalendar(
        api_key=env.finnhub_api_key,
        symbols=sorted(set(rules.allowed_symbols) | {s for s in extra_symbols if s}),
    )
