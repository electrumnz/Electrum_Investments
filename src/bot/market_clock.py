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
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

__all__ = [
    "CLOCKS",
    "ClockFace",
    "MarketPhase",
    "MarketState",
    "clock_faces",
    "market_state",
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
