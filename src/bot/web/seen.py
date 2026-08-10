"""What changed since the operator last looked.

The dashboard gets opened two or three times a day, and the question that
brings somebody to it is "what has happened since I was last here". Nothing on
the deck answered that. Every page renders current state, so a proposal the
gate refused at 11:00 this morning is laid out exactly like one it refused
three days ago, and the operator has to remember where they got to.

This module is the marker that answers it, plus the partitioning that reads
history against it. It is backend only: it produces facts and sentences, and
`render.py` decides what they look like.

## Where the marker lives, and why it is not in the journal

In a cookie, and nowhere else on the box. Three reasons, heaviest first:

- **`data/journal.db` is the only irreplaceable file here.** A UI preference
  has no business in it. It would be snapshotted hourly by
  `deploy/backup-journal.sh`, restored alongside real trades, and would need a
  schema change the first time its shape moved — all of that carried for "when
  did you last look".
- **It has to work with no login at all.** `DASHBOARD_PASSWORD` is unset on the
  loopback deployment, so there is no session to hang state off. A cookie is
  carried identically whether or not the password gate is on.
- **It has to survive a restart, and `SessionStore` deliberately does not.**
  That store is in-memory on purpose, so every deploy empties it — and a deploy
  is precisely when an operator opens the dashboard to see what happened. A
  marker cleared by the restart it exists to describe is worse than no marker.

It is therefore per-browser rather than per-person. Two devices means two
markers, each answering "since *this* browser last looked", which is the honest
reading of the question rather than a defect to paper over.

## When the marker advances, which is the whole design

The obvious implementation — stamp the cookie with `now` on every page load —
destroys the feature it implements. The second page of a visit, and every
refresh after it, then reports that nothing has changed, so the answer is empty
in exactly the sitting it was wanted for. Worse, it is empty *silently*: an
empty delta and a quiet market render the same.

Two rules, and both matter:

- **A visit is a sitting, not a request.** The marker holds still while the
  operator moves Board → Decisions → Trades and refreshes twice. It moves only
  when this request is separated from the last one by more than `VISIT_GAP` —
  longer than any pause inside one sitting, far shorter than the hours between
  the two or three daily visits this is built for.
- **It advances to the previous request's time, never to `now`.** That request
  is the one that showed the operator the state of the world, so it is the
  latest moment they can be said to have seen. Stamping `now` would mark as
  seen anything recorded between the render and the next click — items that
  were never on screen, quietly dropped from the only list that would have
  named them.

So the cookie carries two timestamps: `marker`, which is what "since" means and
is stable across a sitting, and `last`, the previous request's time, which is
what the marker becomes when the sitting ends.

The known cost: a browser polling every twenty minutes never ends its sitting,
so the delta accumulates until it stops. That is the safe direction — an
over-long window shows things twice, where an over-short one hides them — and
it settles overnight. An explicit "mark as read" control would cut it, and is
deliberately not here: this dashboard is read-only apart from `POST /chat`, and
a button is not worth a second write route.

## First visit is a third state, and it is the one that is easy to get wrong

A visitor with no cookie has no marker. That is **not** "nothing has changed
since your last visit" — there was no last visit — and reporting it as an empty
delta would be the confident partial answer this repository keeps naming, in a
new place. `MarkerState` makes the distinction explicit, the counts are `None`
rather than `0` so a renderer that ignores the state prints something visibly
broken instead of something plausibly wrong, and `headline()` says which case
it is in a sentence.

A cookie that arrives damaged, or one stamped later than the current time, is a
fourth case that degrades into the same shape: `UNUSABLE`, no marker, and a
fresh cookie written so the next visit has one. We wrote that value ourselves,
so a future-dated one means this box's clock moved rather than a browser
lying — and re-stamping it from the corrected clock is the repair.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Generic, TypeVar

from ..audit import DecisionEntry
from ..models import Trade

# Its own cookie, not a field inside the session cookie in `auth.py`. Sessions
# are a credential with a week's life that a restart revokes; this is a
# preference that must outlive both.
COOKIE_NAME = "mudhorn_seen"

# Bumped if the encoding below ever changes shape. An old value then fails to
# parse and degrades to "no marker" rather than being misread as a new one —
# the failure that a version-less format invites is silent and wrong.
COOKIE_VERSION = "1"

# A year, so an absence never silently turns a returning operator into a first
# visitor. Nothing bounds it from the other side: a marker older than the
# history on file is handled by `counts_are_lower_bounds`, which says the
# figures are floors rather than quietly under-reporting them.
COOKIE_MAX_AGE_SECONDS = 365 * 24 * 3600

# How long a gap ends a sitting. Thirty minutes survives a phone call in the
# middle of a visit and is nowhere near the hours between the operator's two or
# three daily ones. It is deliberately not the 15-minute decision interval:
# tying it to the loop's cadence would make the marker's behaviour change
# whenever that cadence did.
VISIT_GAP = timedelta(minutes=30)

# Below this, "no cycles were recorded" is the expected reading rather than a
# symptom — the loop wakes every 15 minutes, so a ten-minute window holding
# nothing is normal. Past it, silence is worth a sentence, and the sentence
# refuses to guess which cause it is.
NO_CYCLE_CAVEAT_AFTER = timedelta(minutes=30)

T = TypeVar("T")


class MarkerState(StrEnum):
    """Which of the three answers this visitor gets, before any counting."""

    # A usable marker: "since" means something and the counts are real.
    MARKED = "marked"
    # No cookie. Distinct from an empty delta and must never be rendered as
    # one: nothing has been *compared*, rather than nothing having happened.
    FIRST_VISIT = "first_visit"
    # A cookie arrived and could not be trusted — damaged, or stamped ahead of
    # this box's clock. We know there was a previous visit and cannot say when,
    # which is a different fact from never having seen this browser before.
    UNUSABLE = "unusable"


@dataclass(frozen=True)
class Marker:
    """The moment a visitor was last shown the state of the world."""

    state: MarkerState
    # Set only when `state is MARKED`. The two travel together so a caller
    # cannot read a timestamp without the state that says whether it means
    # anything.
    at: datetime | None = None

    @property
    def known(self) -> bool:
        return self.state is MarkerState.MARKED and self.at is not None

    def is_new(self, stamp: datetime | None) -> bool | None:
        """Whether one item post-dates the marker. `None` when unanswerable.

        Three-valued for the same reason `AssessmentTrigger.holds` is: with no
        marker, or with an item carrying no timestamp, "not new" would be an
        answer we do not have. A renderer handed `None` must badge the row
        neither way.
        """
        if not self.known or self.at is None or stamp is None:
            return None
        return _aware(stamp) > self.at


@dataclass(frozen=True)
class Visit:
    """One request, read against the cookie it arrived with."""

    marker: Marker
    # What to write back. Always set, including when the marker is unusable —
    # that is how a damaged cookie repairs itself.
    cookie_value: str
    # Whether this request opens a sitting rather than continuing one. True on
    # a first visit and after a reset, because both begin one.
    is_new_visit: bool


@dataclass(frozen=True)
class Split(Generic[T]):
    """History cut at the marker. Input order is preserved in every bucket."""

    new: list[T] = field(default_factory=list)
    earlier: list[T] = field(default_factory=list)
    # Items with no timestamp to compare — an open trade has no exit time. They
    # are held apart rather than being swept into `earlier`, because "this has
    # not happened yet" and "this happened before you looked" are different
    # facts and only one of them belongs in a change list.
    undated: list[T] = field(default_factory=list)

    @property
    def reaches_past_marker(self) -> bool:
        """Whether the history supplied actually extends back beyond the marker.

        False with a non-empty `new` means the window ran out before the marker
        did, so the count is a floor. The audit reader takes a `limit` and a
        `days`, and the journal query takes a `limit`, so this is a real case
        rather than a theoretical one.
        """
        return bool(self.earlier)


@dataclass(frozen=True)
class SinceLastVisit:
    """The answer to "what changed", or an honest account of why there isn't one.

    The counts are `int | None` on purpose. Nothing here is measured when there
    is no marker, and a `0` in that case would be read as a measurement by the
    next person to render it — the same reason `IndicatorSnapshot` keeps `None`
    rather than storing a zero. A renderer that ignores `state` and prints
    `None` is visibly broken, which is the failure direction to prefer.
    """

    state: MarkerState
    since: datetime | None = None
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    new_decisions: int | None = None
    new_trades_closed: int | None = None
    new_rejections: int | None = None

    # The supplied history did not reach back to the marker, so every count is
    # a floor. Named rather than hidden: "3 cycles" and "at least 3 cycles" are
    # different claims.
    counts_are_lower_bounds: bool = False

    @property
    def is_reportable(self) -> bool:
        """Whether the counts mean anything. False for both no-marker states."""
        return self.state is MarkerState.MARKED

    @property
    def anything_new(self) -> bool:
        return bool(self.new_decisions or self.new_trades_closed or self.new_rejections)

    @property
    def needs_attention(self) -> bool:
        """Whether something happened that wants reading rather than skimming.

        A rejection and a closed trade qualify: one is visible on no other
        surface, the other is money that moved. Quiet cycles do not — doing
        nothing is frequently the correct output and flagging it would teach
        the operator to ignore the flag.

        Deliberately does NOT fire on "no cycles at all since you looked". That
        could be a stopped loop or a shut market, and nothing in this module can
        tell them apart; it is said as a caveat instead of asserted as an alarm.
        """
        return bool(self.new_rejections or self.new_trades_closed)

    def headline(self) -> str:
        """One sentence, written for someone who has forgotten all of this.

        Every state is answered here so a renderer can call this and be right
        without re-deriving which case it is in.
        """
        if self.state is MarkerState.FIRST_VISIT:
            return (
                "No previous visit is on record from this browser, so there is "
                "nothing to compare against yet. Everything below is the whole "
                "record rather than what has changed."
            )
        if self.state is MarkerState.UNUSABLE:
            return (
                "The marker from this browser could not be read, so what has "
                "changed since your last visit is unknown rather than nothing. "
                "It has been reset, and the next visit will have one."
            )

        age = _age(self.checked_at - self.since) if self.since else "some time"
        if not self.anything_new:
            return f"Nothing new since you last looked, {age} ago."
        return (
            f"Since you last looked, {age} ago: "
            f"{self.new_decisions} cycle(s), "
            f"{self.new_trades_closed} trade(s) closed, "
            f"{self.new_rejections} proposal(s) rejected."
        )

    def caveats(self) -> list[str]:
        """Anything that makes the headline less than the whole truth."""
        if not self.is_reportable:
            # The headline already is the whole answer in those states, and a
            # caveat under it would imply there were figures to qualify.
            return []

        out: list[str] = []
        if self.counts_are_lower_bounds:
            out.append(
                "The history on file does not reach back to your last visit, so "
                "these are floors rather than totals — more happened before the "
                "oldest record shown."
            )
        window = self.checked_at - self.since if self.since else timedelta(0)
        if self.new_decisions == 0 and window > NO_CYCLE_CAVEAT_AFTER:
            out.append(
                "No cycle was recorded at all in that window. The market may have "
                "been shut, or the loop may have stopped — this cannot tell which."
            )
        return out


# ------------------------------------------------------------------- the cookie


def observe(raw: str | None, *, now: datetime | None = None) -> Visit:
    """Read the cookie this request arrived with and work out the marker.

    Pure: give it a string and a moment, get back what "since" means and what
    to write back. Nothing here touches a request object, a response, or disk.

    Every failure lands in the same place — no marker, fresh cookie — because
    the alternative to degrading is raising, and raising here would take down a
    page over a preference.
    """
    moment = _floor(_aware(now or datetime.now(UTC)))

    if not raw:
        return Visit(Marker(MarkerState.FIRST_VISIT), _encode(None, moment), True)

    decoded = _decode(raw)
    if decoded is None:
        return Visit(Marker(MarkerState.UNUSABLE), _encode(None, moment), True)

    marker_at, last_at = decoded

    # A value stamped ahead of the clock, or an incoherent pair. We wrote this
    # cookie, so neither can be a browser's doing: either this box's clock moved
    # backwards or somebody edited the value. Trusting it would leave the
    # visitor with a marker in the future and therefore an empty delta on every
    # visit, for as long as the cookie lives, with nothing on screen to say so.
    incoherent = marker_at is not None and (marker_at > moment or marker_at > last_at)
    if last_at > moment or incoherent:
        return Visit(Marker(MarkerState.UNUSABLE), _encode(None, moment), True)

    if moment - last_at > VISIT_GAP:
        # A new sitting. The marker becomes the previous request's time, which
        # is the last moment this visitor was shown anything — never `moment`,
        # which would swallow whatever arrived while they were away.
        return Visit(Marker(MarkerState.MARKED, last_at), _encode(last_at, moment), True)

    # Same sitting: the marker holds still and only the activity stamp moves.
    # A cookie written on the first visit has no marker yet, so the whole of
    # that first sitting keeps reading as a first visit, which is what it is.
    state = MarkerState.MARKED if marker_at is not None else MarkerState.FIRST_VISIT
    return Visit(Marker(state, marker_at), _encode(marker_at, moment), False)


def from_cookies(cookies: Mapping[str, str], *, now: datetime | None = None) -> Visit:
    """`observe`, taking the request's cookie jar so callers need not name the key.

    Typed as a plain mapping, so this stays importable and testable without
    FastAPI — `request.cookies` satisfies it.
    """
    return observe(cookies.get(COOKIE_NAME), now=now)


def cookie_for(visit: Visit, *, secure: bool) -> dict[str, Any]:
    """Keyword arguments for `Response.set_cookie`, so the attributes live here.

    `httponly` is not about secrecy — the marker is not a credential. It keeps
    the advance policy above server-side: a script that could write this cookie
    could advance the marker on page load, which is precisely the design this
    module exists to avoid.

    `secure` is the caller's to decide, exactly as in `auth.py`: set it from
    `request.url.scheme == "https"` so a loopback deployment over plain HTTP
    still carries the cookie.
    """
    return {
        "key": COOKIE_NAME,
        "value": visit.cookie_value,
        "max_age": COOKIE_MAX_AGE_SECONDS,
        "path": "/",
        "httponly": True,
        "samesite": "lax",
        "secure": secure,
    }


# -------------------------------------------------------------- partitioning


def partition(
    items: Iterable[T], *, since: datetime, key: Callable[[T], datetime | None]
) -> Split[T]:
    """Cut a history at `since`. Strictly after it counts as new.

    Free of anything web-shaped so it can be tested with a list and a
    timestamp. Order is preserved in each half, so a caller that already sorted
    newest-first gets both halves back that way.

    The comparison is strict, and the marker is floored to a whole second when
    it is stored, so an item recorded inside that same second reads as new and
    is shown twice rather than not at all. Showing something again costs a
    glance; dropping it costs the only notice it would ever get.
    """
    moment = _aware(since)
    split: Split[T] = Split()
    for item in items:
        stamp = key(item)
        if stamp is None:
            split.undated.append(item)
        elif _aware(stamp) > moment:
            split.new.append(item)
        else:
            split.earlier.append(item)
    return split


def partition_decisions(
    entries: Sequence[DecisionEntry], *, since: datetime
) -> Split[DecisionEntry]:
    """Split an `AuditView.decisions` list — newest-first order is preserved."""
    return partition(entries, since=since, key=lambda e: e.timestamp)


def partition_trades(trades: Sequence[Trade], *, since: datetime) -> Split[Trade]:
    """Split trades on when they CLOSED, not when they opened.

    A trade opened last week and closed an hour ago is news; one opened an hour
    ago and still running is a position, not a change, and lands in `undated`
    where no count picks it up.
    """
    return partition(trades, since=since, key=lambda t: t.exit_time)


def summarise(
    marker: Marker,
    *,
    decisions: Sequence[DecisionEntry] = (),
    closed_trades: Sequence[Trade] = (),
    now: datetime | None = None,
) -> SinceLastVisit:
    """Count what arrived since the marker, or report that nothing can be counted.

    Pure over lists somebody else already read: `AuditLog.read().decisions` and
    `Journal.closed_trades()`. No network, no disk, no broker.
    """
    moment = _aware(now or datetime.now(UTC))

    if not marker.known or marker.at is None:
        # Every count stays None. There is no window, so there is nothing to
        # count — and a zero here would be read as a measurement.
        return SinceLastVisit(state=marker.state, checked_at=moment)

    cycles = partition_decisions(decisions, since=marker.at)
    trades = partition_trades(closed_trades, since=marker.at)

    return SinceLastVisit(
        state=MarkerState.MARKED,
        since=marker.at,
        checked_at=moment,
        new_decisions=len(cycles.new),
        new_trades_closed=len(trades.new),
        new_rejections=sum(entry.rejected for entry in cycles.new),
        # Either list running out before the marker makes every figure a floor.
        counts_are_lower_bounds=(
            (bool(cycles.new) and not cycles.reaches_past_marker)
            or (bool(trades.new) and not trades.reaches_past_marker)
        ),
    )


# --------------------------------------------------------------------- private


def _encode(marker_at: datetime | None, last_at: datetime) -> str:
    """`version:marker:last`, each timestamp whole epoch seconds, marker optional.

    Integers rather than ISO text: no timezone to get wrong, nothing in them a
    cookie has to quote, and a malformed one is obvious. `int()` truncates, so
    a marker is floored rather than rounded up — see `partition` for why that
    direction is the safe one.
    """
    marker = "" if marker_at is None else str(int(marker_at.timestamp()))
    return f"{COOKIE_VERSION}:{marker}:{int(last_at.timestamp())}"


def _decode(raw: str) -> tuple[datetime | None, datetime] | None:
    """The pair, or `None` for anything that is not exactly the format above."""
    parts = raw.split(":")
    if len(parts) != 3 or parts[0] != COOKIE_VERSION:
        return None

    marker_raw, last_raw = parts[1], parts[2]
    last = _from_epoch(last_raw)
    if last is None:
        return None
    if marker_raw == "":
        return None, last
    marker = _from_epoch(marker_raw)
    if marker is None:
        return None
    return marker, last


def _from_epoch(raw: str) -> datetime | None:
    # `isdigit` first, so a sign, a space or a float never reaches `int` — the
    # only values this ever writes are non-negative integers, and anything else
    # is a cookie somebody edited.
    if not raw.isdigit():
        return None
    try:
        return datetime.fromtimestamp(int(raw), UTC)
    except (ValueError, OverflowError, OSError):
        # A number far outside the representable range. Same treatment as
        # nonsense text: no marker, rather than an exception on a page load.
        return None


def _floor(stamp: datetime) -> datetime:
    """Drop sub-second precision so the value survives the cookie round trip."""
    return stamp.replace(microsecond=0)


def _aware(stamp: datetime) -> datetime:
    """Treat a naive timestamp as UTC rather than raising on the comparison.

    Everything written here is timezone-aware, but the audit log is read
    tolerantly and the journal has rows predating its timezone, so a naive
    value must cost nothing. Same principle as `audit._timestamp`.
    """
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _age(delta: timedelta) -> str:
    """A rough human age. Never negative, never more precise than it is."""
    seconds = max(0.0, delta.total_seconds())
    if seconds < 90:
        return "a moment"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f} minutes"
    hours = minutes / 60
    if hours < 36:
        return f"{hours:.0f} hours"
    return f"{hours / 24:.0f} days"
