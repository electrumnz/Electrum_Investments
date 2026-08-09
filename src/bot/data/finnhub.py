"""Finnhub earnings calendar — the feed a risk rule actually reads.

`RiskGate._news_blackout` refuses to open a position within
`news_blackout_minutes_before/after` of a `NewsWindow`. Until now the only
calendar was `EmptyCalendar`, so that rule existed and never once fired. Wiring
this in turns it on.

That makes this feed different in kind from `marketaux.py`. Headlines failing
costs context; **this** failing costs a risk control, silently, because zero
windows and "we could not ask" look identical to the gate. So the adapter
tracks whether its last fetch succeeded and the loop reports it, in the same
spirit as `reconcile`'s `risk_is_understated`: prefer saying the number is
unknown over implying it is zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from ..risk import NewsWindow
from ._http import DEFAULT_TIMEOUT_SECONDS, JsonGetter, TtlCache, fetch_json, httpx_getter

log = structlog.get_logger(__name__)

FINNHUB_EARNINGS_URL = "https://finnhub.io/api/v1/calendar/earnings"

# The calendar changes at most daily, and a stale entry is far cheaper than a
# rate limit. Finnhub's free tier is generous (60/min) but there is nothing to
# gain from asking every quarter of an hour.
DEFAULT_CACHE_TTL_SECONDS = 6 * 3600.0

# Earnings are timed relative to the US equities session, so the conversion has
# to respect daylight saving. zoneinfo is stdlib on 3.11+, so this costs nothing.
_ET = ZoneInfo("America/New_York")
_MARKET_OPEN = (9, 30)
_MARKET_CLOSE = (16, 0)

# How far ahead to pull. A week covers the lookahead the loop asks for with room
# for the cache to go stale without opening a gap.
_CALENDAR_DAYS_AHEAD = 7


@dataclass
class FinnhubCalendar:
    """Earnings announcements as `NewsWindow`s. Satisfies `calendar.CalendarFeed`."""

    api_key: str
    symbols: list[str] = field(default_factory=list)
    getter: JsonGetter = field(default_factory=lambda: httpx_getter(DEFAULT_TIMEOUT_SECONDS))
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS
    _cache: TtlCache = field(init=False)
    _last_fetch_ok: bool = field(default=True, init=False)

    def __post_init__(self) -> None:
        self._cache = TtlCache(ttl_seconds=self.cache_ttl_seconds)

    @property
    def is_degraded(self) -> bool:
        """True when the last fetch failed, so the blackout gate is running blind.

        The loop surfaces this. A caller that ignores it is asserting there are
        no upcoming announcements, which is not what a failed fetch established.
        """
        return not self._last_fetch_ok

    def upcoming_windows(self, *, lookahead_minutes: int) -> list[NewsWindow]:
        now = datetime.now(UTC)
        windows = self._all_windows(now)
        cutoff = now + timedelta(minutes=lookahead_minutes)
        return [w for w in windows if now <= w.timestamp <= cutoff]

    def _all_windows(self, now: datetime) -> list[NewsWindow]:
        cached, value = self._cache.get()
        if cached:
            return list(value)

        result = fetch_json(
            self.getter,
            FINNHUB_EARNINGS_URL,
            {
                "token": self.api_key,
                "from": now.date().isoformat(),
                "to": (now.date() + timedelta(days=_CALENDAR_DAYS_AHEAD)).isoformat(),
            },
            feed="finnhub_earnings",
        )
        if not result.ok:
            self._last_fetch_ok = False
            log.warning(
                "news_blackout_running_blind",
                detail=(
                    "Earnings calendar unavailable; the news blackout gate has "
                    "no windows to check against this cycle."
                ),
            )
            # Not cached: a failure must be retried, not held for six hours.
            return []

        self._last_fetch_ok = True
        windows = _parse(result.payload, wanted=set(self.symbols))
        self._cache.put(windows)
        return windows


def _parse(payload: Any, *, wanted: set[str]) -> list[NewsWindow]:
    """Turn Finnhub's earnings calendar into windows, one per announcement."""
    if not isinstance(payload, dict):
        return []
    entries = payload.get("earningsCalendar")
    if not isinstance(entries, list):
        return []

    windows: list[NewsWindow] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        symbol = str(entry.get("symbol") or "").strip().upper()
        if not symbol or (wanted and symbol not in wanted):
            continue
        try:
            day = date.fromisoformat(str(entry.get("date")))
        except ValueError:
            continue

        for moment in _moments(day, str(entry.get("hour") or "").strip().lower()):
            windows.append(NewsWindow(timestamp=moment, affected_symbols=frozenset({symbol})))

    return windows


def _at(day: date, hhmm: tuple[int, int]) -> datetime:
    return datetime(day.year, day.month, day.day, hhmm[0], hhmm[1], tzinfo=_ET).astimezone(UTC)


def _moments(day: date, hour_code: str) -> list[datetime]:
    """When an announcement lands, from Finnhub's `hour` field.

    `bmo` and `amc` are precise enough to point at: the market open and the
    close respectively, since that is when the price actually reacts.

    Anything else — `dmh`, or an empty field, both of which occur — means the
    announcement is somewhere inside the session and we do not know where.
    Picking a plausible midpoint would be inventing precision we do not have,
    and picking nothing would fail open on a risk control. So the session is
    covered end to end at half-hour spacing, which the gate's +/-15 minute
    window turns into continuous cover: that symbol is simply not traded that
    day. Over-blocking one symbol for one session is the cheap error here.
    """
    if hour_code == "bmo":
        return [_at(day, _MARKET_OPEN)]
    if hour_code == "amc":
        return [_at(day, _MARKET_CLOSE)]

    start = _at(day, _MARKET_OPEN)
    end = _at(day, _MARKET_CLOSE)
    moments: list[datetime] = []
    cursor = start
    while cursor <= end:
        moments.append(cursor)
        cursor += timedelta(minutes=30)
    return moments
