"""Which days Tokyo, Sydney and Auckland actually trade, and between what hours.

`session_calendar.py` answers this for New York, out of Alpaca's own trading
calendar. That calendar is the US equity session and speaks for nothing else,
so the other three clocks on the tape were **weekday-shaped**: open Monday to
Friday during their local hours, and therefore open on Boxing Day. A badge that
says the ASX is trading on 28 December is a plausible wrong figure on the one
strip whose entire job is orientation, which is the failure this repository
exists to refuse.

The obvious repair — three hardcoded holiday lists — is worse than the gap.
They go stale in silence, and a stale list still *looks* answered, whereas a
stated limit at least tells a reader what they have. So the rules come from
`exchange_calendars`, which ships XTKS, XASX and XNZE with real holiday rules
and computes them offline.

Four properties are load-bearing, and every one of them is a rule already used
elsewhere in this repository.

- **The dependency is OPTIONAL and its absence is the old behaviour, exactly.**
  It is imported lazily, inside a function, and an `ImportError` answers `None`
  — "could not ask" — which `ClockFace.is_open` falls back through to the
  weekday-shaped computation it has always used. That matters because this is a
  package on the box that runs the trading loop, added to colour a badge for
  three markets the bot does not trade. It has to be droppable without anything
  breaking, and it has to be droppable *silently* in the sense that nothing
  raises — but never silently in the sense that the badge keeps claiming to
  know. `ClockFace.tracks_holidays` goes False again with it.
- **There is no network call, and that is measured rather than read.**
  `exchange_calendars` compiles its rules from code shipped in the wheel.
  `tests/test_market_clock.py` blocks `socket.socket`, `socket.create_connection`
  and `socket.getaddrinfo` before touching it, so a release that started
  fetching would fail the suite rather than put a network call on the render
  path. Tests may not touch the network, and this must not become the exception.
- **Nothing here gates anything.** `RiskGate` stays deterministic and reads no
  calendar, live or cached; these are display badges for exchanges the bot
  holds no position on. The same rule `session_calendar` states, and for the
  same reason: a gate that can fail is a gate that can fail open.
- **`None` means "could not ask" and an empty tuple means "does not trade".**
  Not installed, an unrecognised code, a date outside the rules the library
  actually ships — all `None`, and a caller must degrade rather than read any
  of them as a holiday. Same three-valued rule as `SessionCalendar.is_trading_day`,
  `FinnhubCalendar.is_degraded` and `Broker.orders_degraded`.

**What this deliberately does NOT do is answer for New York.** Alpaca's
calendar already does, through `session_calendar`, and it is the broker this
bot actually trades through — so its answer is the one that matters when the
two disagree. A second source for the same fact is a second fact that can
disagree with the first, which is the reasoning that keeps `Adoption.is_live`
computed and `Dream`'s back-reference derived. `ClockFace.calendar_code` is
empty for New York on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import structlog

__all__ = [
    "covers",
    "is_available",
    "session_windows",
    "unavailable_reason",
    "warm",
]

log = structlog.get_logger()


@dataclass
class _Source:
    """One exchange's rules, built once and then queried as a pure lookup.

    Mutable because `windows` is a memo — the same three dates are asked about
    on every tape render, several times a minute, and each answer costs a pair
    of pandas timestamp conversions. Everything else on it is fixed at build.
    """

    code: str
    calendar: Any
    zone: ZoneInfo
    first: date
    last: date
    windows: dict[date, tuple[tuple[time, time], ...]] = field(default_factory=dict)


#: Built sources, keyed by MIC. A `None` value is a source that was TRIED and
#: could not be built, which is why the key is present at all — without it every
#: caller re-attempts an import that has already failed, which is the hot-retry
#: shape `SessionCalendar.RETRY_AFTER` exists to stop. Here there is no endpoint
#: to hammer and no transient failure to heal from: a missing package does not
#: appear mid-process, so one attempt is the whole story and there is nothing to
#: back off from.
_SOURCES: dict[str, _Source | None] = {}

#: Why a source is unavailable, in a sentence a surface can print. The Settings
#: card lesson: a panel that knows it is empty must not guess at the cause, and
#: "not installed" and "that exchange has no rules here" are different answers
#: an operator would act on differently.
_REASONS: dict[str, str] = {}


def _build(code: str) -> _Source | None:
    """Import the library and construct one calendar. Never raises.

    The import is here rather than at module scope for two reasons. It pulls in
    pandas, which costs about a third of a second and belongs to a code path
    most deployments never reach; and a module-level import of an optional
    dependency makes `import bot.market_clock` fail on a box that does not have
    it, which would turn a badge caveat into an outage.
    """
    if not code:
        # A face with no exchange, or New York, which is answered by Alpaca.
        _REASONS[code] = "no exchange calendar is claimed for this clock"
        return None

    try:
        import exchange_calendars
    except ImportError:
        _REASONS[code] = (
            "`exchange_calendars` is not installed, so public holidays are not "
            "tracked for this exchange. Install the `calendars` extra to change "
            "that; the badge falls back to regular weekday hours without it."
        )
        log.info("exchange_hours_unavailable", code=code, reason="not_installed")
        return None

    # Broad on purpose, and for the `context.fetch_indicators` reason: the
    # failures on offer here are `InvalidCalendarName`, whatever pandas raises
    # on a bad bound, and anything a future release adds. None of them is worth
    # taking a page down for, and an unanticipated one is exactly the case where
    # raising would be worst.
    try:
        calendar = exchange_calendars.get_calendar(code)
        zone = calendar.tz
        first: date = calendar.first_session.date()
        last: date = calendar.last_session.date()
    except Exception as exc:
        _REASONS[code] = (
            f"`exchange_calendars` could not build a calendar for {code}, so "
            "public holidays are not tracked for this exchange."
        )
        log.warning("exchange_hours_build_failed", code=code, error=str(exc))
        return None

    if not isinstance(zone, ZoneInfo):
        # Older releases handed back a pytz zone. Converting by NAME keeps the
        # daylight-saving arithmetic in one library rather than mixing two.
        zone = ZoneInfo(str(zone))

    log.info(
        "exchange_hours_loaded",
        code=code,
        zone=str(zone),
        covers=f"{first.isoformat()}..{last.isoformat()}",
    )
    return _Source(code=code, calendar=calendar, zone=zone, first=first, last=last)


def _source(code: str) -> _Source | None:
    """The built calendar for a MIC, building it on first ask.

    Unlocked on purpose. FastAPI runs `def` routes in a threadpool, so two
    renders can arrive here at once — and the worst that costs is building the
    same calendar twice, because the result is identical and the dict write is
    a single bytecode operation. A lock would buy nothing and would put a
    contended primitive on a render path.
    """
    if code not in _SOURCES:
        _SOURCES[code] = _build(code)
    return _SOURCES[code]


def _local(stamp: Any, zone: ZoneInfo) -> time:
    """A UTC timestamp from the library as a naive local clock time.

    The library stores every session boundary in UTC and converts, so this is
    daylight-saving correct in both hemispheres with no diary entry — which is
    the same property `ClockFace.sessions` gets by being declared in local time,
    arrived at from the other direction.
    """
    moment: datetime = stamp.to_pydatetime().astimezone(zone)
    return moment.timetz().replace(tzinfo=None)


def is_available(code: str) -> bool:
    """Whether this repository has any holiday rules for that exchange at all.

    Deliberately NOT a claim about a particular date — see `covers`. This is the
    coarse question `ClockFace.tracks_holidays` asks, because that is a property
    of the exchange and a badge tooltip has no date to hand.
    """
    return _source(code) is not None


def unavailable_reason(code: str) -> str | None:
    """Why there are no rules, or `None` when there are.

    A surface that says "holidays not tracked" is more useful when it can say
    which of the two reasons applies: the package is absent, or this exchange
    was never in it. Guessing between them is the mistake the Settings calendar
    card made when it blamed an unopened Board for an empty broker calendar.
    """
    if _source(code) is not None:
        return None
    return _REASONS.get(code)


def covers(code: str, day: date) -> bool:
    """Whether an answer about this DATE is available at all.

    `exchange_calendars` bounds each calendar to roughly the last twenty years
    and one year ahead, so a question about 2035 is outside the rules it ships
    rather than a quiet day. Out of range is `None` from `session_windows`, not
    "holiday" — the same rule as `SessionCalendar.covers` and `has_cycles`.

    Unreachable in practice for a tape rendering the present moment, which is
    exactly why it is worth stating: the case that cannot happen is the one
    nobody notices has started happening.
    """
    source = _source(code)
    return source is not None and source.first <= day <= source.last


def session_windows(code: str, day: date) -> tuple[tuple[time, time], ...] | None:
    """That exchange's real trading windows on that date, in ITS local time.

    Three distinct answers and none of them collapses into another:

    - `None` — could not ask. No library, an unknown exchange, or a date
      outside the shipped rules. The caller must fall back rather than treat it
      as a closure.
    - `()` — asked, and the exchange does not trade that day. A weekend, or the
      holiday this module was written for.
    - one or more windows — the session, split where the exchange breaks for
      lunch. Tokyo does, 11:30 to 12:30 JST, and a single 09:00-15:30 window
      would show it trading through an hour it is shut.

    An early close arrives here as a *shorter window* rather than as a flag.
    The ASX and the NZX both shut at lunchtime on Christmas Eve and New Year's
    Eve, and a badge reading the real close is right about those afternoons
    without anything having to know they are unusual. Naming them as unusual
    needs a notion of the exchange's *usual* close, and the only honest source
    for that is this same library — so it belongs here if it is ever wanted,
    never in a constant beside it.
    """
    source = _source(code)
    if source is None or not (source.first <= day <= source.last):
        return None

    cached = source.windows.get(day)
    if cached is not None:
        return cached

    try:
        calendar = source.calendar
        if not calendar.is_session(day):
            windows: tuple[tuple[time, time], ...] = ()
        else:
            opens = _local(calendar.session_open(day), source.zone)
            closes = _local(calendar.session_close(day), source.zone)
            pause_start = (
                calendar.session_break_start(day) if calendar.has_break else None
            )
            # NaT is the library's "this session has no break", and it is the
            # only value that compares unequal to itself. Checking it this way
            # keeps pandas out of this module's imports entirely.
            if pause_start is None or pause_start != pause_start:
                windows = ((opens, closes),)
            else:
                pause_end = calendar.session_break_end(day)
                windows = (
                    (opens, _local(pause_start, source.zone)),
                    (_local(pause_end, source.zone), closes),
                )
    # Broad again, and for the same reason: a badge must not take a page down.
    except Exception as exc:
        log.warning(
            "exchange_hours_query_failed",
            code=code,
            day=day.isoformat(),
            error=str(exc),
        )
        return None

    source.windows[day] = windows
    return windows


def warm(*codes: str) -> None:
    """Build these calendars now, so the first render does not pay for them.

    Roughly half a second, once per process: a third of that is importing
    pandas and the rest is compiling twenty years of holiday rules per
    exchange. It is not a network call and nothing depends on it having run —
    every query builds what it needs — so this is a courtesy for a caller that
    knows a quiet moment, not a step anything is broken without.

    Nothing calls it today. Wiring it into web startup is a change in the web
    lane, and a hook nobody has to remember beats a stall nobody can explain.
    """
    for code in codes:
        _source(code)
