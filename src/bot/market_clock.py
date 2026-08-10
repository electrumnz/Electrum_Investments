"""What time it is where the market is, and what the market is doing.

This exists because of a real confusion, and the confusion is the point. Asked
to place a trade "the market should be open right now", at 04:49 New York time
on a Monday. Alpaca *was* open — pre-market runs from 04:00 ET — so the answer
was not simply "no". It was: open for a session this bot does not trade, four
and a half hours before the one it does.

Three different clocks were in play at once. Alpaca's sessions, the gate's
window in `config/rules.yaml`, and the operator sitting in New Zealand where
it was Monday evening. Getting any two of them confused produces an order that
rests until the next open and fills at a price nobody looked at.

So the answers live in one place, computed rather than remembered.

**Nothing here gates anything.** `RiskGate` keeps its own session check and
must: this module knows about Alpaca's four sessions, which is more than the
gate is allowed to care about, and a display agreeing with the gate by
construction would hide the day they disagree. `bot_window_open` is derived
from the same `windows_by_day` the gate reads, so the two cannot drift, but it
is reported beside the market phase rather than substituted for it.

**Holidays are not covered, exactly as in the gate.** Thanksgiving reads as a
normal Thursday here. Closing that needs a calendar endpoint, which is a
network call, and this module is deliberately pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:  # a cache built on this module; imported for typing only
    from .session_calendar import SessionCalendar

__all__ = [
    "CLOCKS",
    "BrokerClock",
    "ClockFace",
    "MarketPhase",
    "MarketState",
    "TradingDay",
    "clock_faces",
    "is_continuous",
    "market_state",
    "render_sessions",
]

NY = ZoneInfo("America/New_York")

# Alpaca's own session boundaries, in New York time. Sources: Alpaca's 24/5
# announcement and its extended-hours docs. These are the BROKER's sessions and
# have nothing to do with `config/rules.yaml`.
OVERNIGHT_START = time(20, 0)   # 8pm ET, straight after the after-hours close
PRE_START = time(4, 0)          # 4am ET
REGULAR_START = time(9, 30)
REGULAR_END = time(16, 0)
POST_END = time(20, 0)


class MarketPhase(StrEnum):
    """Where the US equity market is in its day.

    Five values rather than "open" and "shut", because the gap between them is
    where the confusion lives. An order placed in `PRE` without
    `extended_hours` on the request does not trade in the pre-market — it rests
    until `REGULAR` and fills at a price nobody has seen.
    """

    WEEKEND = "weekend"      # Friday 8pm ET to Sunday 8pm ET. Nothing trades.
    OVERNIGHT = "overnight"  # 8pm-4am ET. 24/5 venue; limited securities.
    PRE = "pre"              # 4am-9:30am ET.
    OPEN = "open"            # 9:30am-4pm ET. The regular session.
    POST = "post"            # 4pm-8pm ET.


#: Human labels, kept beside the enum so a renderer never invents its own.
PHASE_LABELS: dict[MarketPhase, str] = {
    MarketPhase.WEEKEND: "Weekend",
    MarketPhase.OVERNIGHT: "Overnight",
    MarketPhase.PRE: "Pre-market",
    MarketPhase.OPEN: "Open",
    MarketPhase.POST: "After hours",
}


@dataclass(frozen=True)
class MarketState:
    """The market phase now, and when it changes next.

    `bot_window_open` is deliberately separate from `phase`. The market being
    open is not the same as this bot being willing to trade, and conflating
    them is the mistake this module was written after.
    """

    now: datetime
    phase: MarketPhase
    next_change: datetime
    next_phase: MarketPhase
    bot_window_open: bool
    bot_window_next: datetime | None

    @property
    def label(self) -> str:
        return PHASE_LABELS[self.phase]

    @property
    def next_label(self) -> str:
        return PHASE_LABELS[self.next_phase]

    @property
    def seconds_to_change(self) -> float:
        return (self.next_change - self.now).total_seconds()

    @property
    def is_tradeable_by_bot(self) -> bool:
        """The only question an operator actually needs answered.

        True requires BOTH that the gate's window is open and that the regular
        session is running. Either alone is a half-answer: the gate's window is
        clock arithmetic that knows nothing about the venue, and the venue
        being open says nothing about what `config/rules.yaml` permits.
        """
        return self.bot_window_open and self.phase is MarketPhase.OPEN


def _at(moment: datetime, clock: time) -> datetime:
    return moment.replace(
        hour=clock.hour, minute=clock.minute, second=0, microsecond=0
    )


def _phase_at(local: datetime) -> tuple[MarketPhase, datetime, MarketPhase]:
    """Phase, when it next changes, and what it changes to — all in NY time.

    Written as an explicit walk through the day rather than as a lookup, because
    the weekend boundaries are not symmetrical: trading stops Friday at 8pm and
    resumes Sunday at 8pm, so Saturday has no sessions at all while Sunday has
    one that starts in the evening.
    """
    weekday = local.weekday()          # Mon 0 ... Sun 6
    clock = local.timetz().replace(tzinfo=None)

    # --- the weekend, which is a shape rather than a pair of days ----------
    if weekday == 5:                                    # Saturday: nothing
        resume = _at(local + timedelta(days=1), OVERNIGHT_START)
        return MarketPhase.WEEKEND, resume, MarketPhase.OVERNIGHT
    if weekday == 6:                                    # Sunday
        if clock < OVERNIGHT_START:
            return (
                MarketPhase.WEEKEND,
                _at(local, OVERNIGHT_START),
                MarketPhase.OVERNIGHT,
            )
        return MarketPhase.OVERNIGHT, _at(local + timedelta(days=1), PRE_START), MarketPhase.PRE
    if weekday == 4 and clock >= POST_END:              # Friday after 8pm
        days = 2                                         # to Sunday evening
        return (
            MarketPhase.WEEKEND,
            _at(local + timedelta(days=days), OVERNIGHT_START),
            MarketPhase.OVERNIGHT,
        )

    # --- an ordinary trading day ------------------------------------------
    if clock < PRE_START:
        # Still last night's overnight session, which began yesterday evening.
        return MarketPhase.OVERNIGHT, _at(local, PRE_START), MarketPhase.PRE
    if clock < REGULAR_START:
        return MarketPhase.PRE, _at(local, REGULAR_START), MarketPhase.OPEN
    if clock < REGULAR_END:
        return MarketPhase.OPEN, _at(local, REGULAR_END), MarketPhase.POST
    if clock < POST_END:
        return MarketPhase.POST, _at(local, POST_END), MarketPhase.OVERNIGHT
    # After 8pm on Mon-Thu: overnight, running until 4am tomorrow.
    return MarketPhase.OVERNIGHT, _at(local + timedelta(days=1), PRE_START), MarketPhase.PRE


def _bot_window(
    now: datetime, windows_by_day: dict[int, list[tuple[int, int]]]
) -> tuple[bool, datetime | None]:
    """Is the gate's own session open, and when does it next open?

    Derived from the same `windows_by_day` the gate reads, so a display and a
    rejection cannot disagree about what the configured window is. Searches a
    fortnight ahead and gives up rather than looping: a config with no windows
    at all is a real possibility, and `None` says "not from here" instead of
    spinning.
    """
    utc = now.astimezone(UTC)
    for window in windows_by_day.get(utc.weekday(), []):
        if window[0] <= utc.hour < window[1]:
            return True, None

    probe = utc
    for _ in range(14):
        for start, _end in sorted(windows_by_day.get(probe.weekday(), [])):
            candidate = probe.replace(
                hour=start, minute=0, second=0, microsecond=0
            )
            if candidate > utc:
                return False, candidate
        probe = (probe + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    return False, None


def market_state(
    now: datetime,
    *,
    windows_by_day: dict[int, list[tuple[int, int]]] | None = None,
) -> MarketState:
    """The whole answer, from one timestamp.

    `windows_by_day` comes from `InstrumentRules`. Omitted, the bot window
    reports shut with no next opening — which is the honest reading for a
    caller that did not supply the rules, rather than a cheerful default.
    """
    local = now.astimezone(NY)
    phase, change_local, next_phase = _phase_at(local)
    open_now, next_open = (
        _bot_window(now, windows_by_day) if windows_by_day else (False, None)
    )
    return MarketState(
        now=now,
        phase=phase,
        next_change=change_local.astimezone(UTC),
        next_phase=next_phase,
        bot_window_open=open_now,
        bot_window_next=next_open,
    )


@dataclass(frozen=True)
class TradingDay:
    """One dated session from Alpaca's calendar, with its real open and close.

    The `close` is why this is worth fetching at all. `_phase_at` computes a
    16:00 New York close every trading day, and on a half-day — the Friday after
    Thanksgiving, Christmas Eve, 3rd July — the market shuts at 13:00. Three
    hours out, and nothing looks wrong while it is happening: the session is
    genuinely open, just not for as long as anything here assumed.

    A holiday at least reads as a suspicious silence. An early close does not.
    """

    date: date
    open_utc: datetime
    close_utc: datetime

    @property
    def open_local(self) -> datetime:
        return self.open_utc.astimezone(NY)

    @property
    def close_local(self) -> datetime:
        return self.close_utc.astimezone(NY)

    @property
    def is_early_close(self) -> bool:
        return self.close_local.timetz().replace(tzinfo=None) < REGULAR_END

    @property
    def is_late_open(self) -> bool:
        return self.open_local.timetz().replace(tzinfo=None) > REGULAR_START

    @property
    def is_unusual(self) -> bool:
        return self.is_early_close or self.is_late_open

    def render(self) -> str:
        stamp = (
            f"{self.date:%a %d %b} "
            f"{self.open_local:%H:%M}-{self.close_local:%H:%M} New York"
        )
        return f"{stamp} — EARLY CLOSE" if self.is_early_close else stamp


@dataclass(frozen=True)
class BrokerClock:
    """What Alpaca says about the regular session. Deliberately only that.

    `GET /v2/clock` answers one coarse question — is the REGULAR session open —
    and cannot tell pre-market from after-hours from overnight. Everything above
    computes all five phases offline and gets daylight saving right, so this is
    not a replacement for it.

    It is here for the one thing arithmetic over New York time structurally
    cannot know: **market holidays and early closes**. Thanksgiving is a
    Thursday and reads as an ordinary trading day to `_phase_at`; Alpaca's
    calendar knows it is shut. So the two are reported side by side and a
    disagreement is named rather than resolved — see `_broker_disagreement`.

    Nothing gates on this. It is a network call, and a network call must not
    reach `RiskGate`, which has to stay deterministic and must not fail open.
    """

    is_open: bool
    next_open: datetime
    next_close: datetime


def is_continuous(windows_by_day: dict[int, list[tuple[int, int]]]) -> bool:
    """A market with no closed hours at all: every day, midnight to midnight."""
    return len(windows_by_day) == 7 and all(
        windows == [(0, 24)] for windows in windows_by_day.values()
    )


def _until(seconds: float) -> str:
    """A duration a reader can act on. Never negative, never bare seconds."""
    if seconds <= 0:
        return "any moment"
    hours, remainder = divmod(int(seconds), 3600)
    minutes = remainder // 60
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def _broker_disagreement(state: MarketState, clock: BrokerClock) -> str | None:
    """Name a conflict between the computed phase and the broker's calendar.

    Deliberately does not pick a winner in the text it returns beyond saying
    which source is authoritative for the question at hand. The valuable case is
    the second one: this bot's clock says the regular session is running and the
    broker says it is shut, which is a holiday or an early close — the one thing
    the offline computation cannot see, and the reason `BrokerClock` exists.
    """
    if clock.is_open and state.phase is not MarketPhase.OPEN:
        return (
            f"NOTE: this bot's clock reads {state.label.lower()} while Alpaca "
            "reports the regular session OPEN. The broker is authoritative for "
            "whether an order fills now."
        )
    if not clock.is_open and state.phase is MarketPhase.OPEN:
        return (
            "NOTE: this bot's clock reads the regular session as open and "
            "Alpaca reports it CLOSED. That is almost always a market holiday "
            "or an early close, which this bot computes New York hours and "
            "cannot know about. Treat the market as SHUT: an order proposed now "
            f"would rest until {clock.next_open.isoformat(timespec='minutes')}."
        )
    return None


#: Explains what a proposal made outside the regular session actually becomes.
#: Every clause is a structural property of the order path or of how a stop
#: works at any broker, not a setting anyone chose — see `AlpacaBroker.place_order`.
OUT_OF_HOURS_MECHANICS = (
    "Out-of-hours mechanics, which are structural rather than configured:",
    "  - The entry is a limit order and is NOT sent with `extended_hours`, "
    "because Alpaca refuses that flag on a bracket or an OTO and every entry "
    "carries a stop. So the order RESTS and becomes eligible at the next "
    "regular open.",
    "  - It therefore fills at whatever prints at that open, which is not the "
    "quote in the Market snapshot above. An out-of-hours quote on the free IEX "
    "feed is thin and frequently one-sided; a limit read off it is a level that "
    "may not exist when the order actually becomes live.",
    "  - The stop is a real GTC order resting at the broker, but it CANNOT FIRE "
    "out of hours. A stop becomes a market order when triggered and "
    "extended-hours venues accept limit orders only, so a gap through the stop "
    "overnight fills at the open, not at the stop price. No broker offers a "
    "different answer to this.",
    "  - `stop_watch` reports a position already through its stop on every "
    "cycle. It reports; it never closes.",
    "So a proposal made now is a bet on the next open at an unknown price. That "
    "is a legitimate trade and the gate permits it — size it knowing the fill is "
    "unknown, and pass if the thesis only works at the price on screen now.",
)


def render_sessions(
    now: datetime,
    *,
    windows_by_class: dict[str, dict[int, list[tuple[int, int]]]],
    broker_clock: BrokerClock | None = None,
    calendar: SessionCalendar | None = None,
) -> list[str]:
    """What an order placed right now would actually do, per instrument class.

    Per class rather than once, because "out of hours" is not a property of the
    moment — it is a property of the moment AND the instrument. Crypto trades
    continuously; saying "the market is shut" beside a crypto symbol would be
    false, and a single global answer is the bug `sessions_utc` already had.

    `calendar` is the US equity trading calendar and applies only to classes
    that have sessions at all. It answers the two questions the arithmetic
    above cannot: which days are skipped, and which end early.
    """
    lines: list[str] = ["## Session — what an order placed now would actually do"]
    any_shut = False
    any_scheduled = False

    for name in sorted(windows_by_class):
        windows = windows_by_class[name]
        if is_continuous(windows):
            lines.append(
                f"- {name}: CONTINUOUS. No session boundaries — an order placed "
                "now is live now, and carries no broker-side bracket."
            )
            continue

        any_scheduled = True
        state = market_state(now, windows_by_day=windows)
        local = now.astimezone(NY)
        change = state.next_change.astimezone(NY)
        lines.append(
            f"- {name}: {state.label.upper()}. It is "
            f"{local.strftime('%H:%M %a %d %b')} in New York. "
            f"Next: {state.next_label.lower()} at "
            f"{change.strftime('%H:%M %a %d %b')} New York, in "
            f"{_until(state.seconds_to_change)}."
        )
        if state.phase is not MarketPhase.OPEN:
            any_shut = True
            if state.bot_window_next is not None:
                lines.append(
                    "    The gate's own window is shut too and next opens "
                    f"{state.bot_window_next.isoformat(timespec='minutes')}."
                )

    if broker_clock is not None:
        note = _broker_disagreement(market_state(now), broker_clock)
        if note is not None:
            lines.append(f"- {note}")
        # A holiday is the case the broker clock exists for, and it is exactly
        # the case the phase computation reads as OPEN — so the mechanics below
        # would have been withheld at the one moment they matter most. The
        # broker's answer is about the US equity session, so it only speaks for
        # a class that has sessions at all.
        if any_scheduled and not broker_clock.is_open:
            any_shut = True

    if any_scheduled and calendar is not None:
        local_date = now.astimezone(NY).date()
        lines.extend(calendar.render(local_date))
        # Two independent reasons the market can be shut while `_phase_at` reads
        # OPEN, and the calendar catches both without a live call — so a failed
        # `get_clock` does not take the holiday check down with it.
        if calendar.is_trading_day(local_date) is False:
            any_shut = True
        today = calendar.day(local_date)
        if today is not None and now >= today.close_utc:
            # Past an early close. The clock above still says OPEN for hours.
            any_shut = True
    elif any_scheduled and broker_clock is None:
        # The Finnhub lesson: an unavailable check must not read as a clean one.
        lines.append(
            "- Neither Alpaca's clock nor its trading calendar was read this "
            "cycle, so a market holiday or an early close would not show up "
            "above. The hours here are computed from New York time and assume "
            "an ordinary trading day."
        )

    if any_shut:
        lines.append("")
        lines.extend(OUT_OF_HOURS_MECHANICS)

    return lines


@dataclass(frozen=True)
class ClockFace:
    """One city's clock. `is_market` marks the zone the session is defined in."""

    label: str
    zone: str
    is_market: bool = False

    def at(self, now: datetime) -> datetime:
        return now.astimezone(ZoneInfo(self.zone))


#: The four the operator asked for, ordered west-to-east from the market out.
#: New York first because it is the zone every session boundary is defined in;
#: Auckland is where the operator is.
CLOCKS: tuple[ClockFace, ...] = (
    ClockFace("New York", "America/New_York", is_market=True),
    ClockFace("Los Angeles", "America/Los_Angeles"),
    ClockFace("Sydney", "Australia/Sydney"),
    ClockFace("Auckland", "Pacific/Auckland"),
)


def clock_faces(now: datetime) -> list[tuple[ClockFace, datetime]]:
    return [(face, face.at(now)) for face in CLOCKS]
