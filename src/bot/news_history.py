"""What the bot has actually read, recalled from the audit log.

The Chat page could not answer "what's the latest news", and the cause was not
memory — it was that nothing exposed the feeds at all. The loop fetches
headlines, watched-account posts and the earnings calendar on every cycle, and
`MarketInputs` writes all three into the audit log beside the decision they
informed. Until this module the only thing that ever read them back was the
Decisions page.

## Why this reads the record instead of fetching

The obvious fix — hand the chat surface a live news tool — is the wrong one,
and the reason is a quota rather than a preference. Marketaux's free tier
allows **100 requests a day** against a loop that already wakes 96 times, which
is why `marketaux.py` caches for 30 minutes at all. A question typed into the
dashboard that fetched fresh would spend the trading loop's own allowance, and
it would fail in the worst available direction: the loop reasons with no
headlines, on exactly the day somebody was asking about the news. The X feed
carries a monthly cap on posts retrieved and fails the same way.

So the store is the source. Everything here is a pure function over an
`AuditView` and touches no network.

## What that changes about the answer

This is a **recording of what the trading loop was shown**, not a live news
search, and that distinction has to survive all the way to the operator or the
tool becomes a confident wrong answer wearing a timestamp. Three properties
carry it, and none is decoration:

- **Every item states its age.** A headline first seen six hours ago is not
  "the latest news". Rendering it without a time is precisely how it becomes
  that.
- **No cycles recorded is not "no news".** The Finnhub lesson in a third
  costume: an empty window means the loop was shut, or restarting, or the
  market was closed — never that the world was quiet. `has_cycles` is separate
  from an empty list on purpose, and a caller that collapses the two is lying
  about the one case this exists to distinguish.
- **Cycles that predate the recording are counted and named.** The audit log is
  append-only and never migrated, so a window can hold decisions written before
  `MarketInputs` existed. Those are cycles whose feeds are simply not on file,
  which is not the same as cycles that saw nothing.

The degraded flags travel for the same reason, and they are **any** rather than
latest: the claim being made is that this list is the complete set of what was
seen over the window, and one failed fetch anywhere in it makes that false.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .audit import AuditView

# What a caller gets when it asks for "recent" without saying what that means.
DEFAULT_WINDOW_HOURS = 24.0
DEFAULT_LIMIT = 40

# Past this, a recall is describing a loop that has stopped rather than a quiet
# market. The decision interval is 15 minutes, so an hour is four missed cycles
# — comfortably past a slow fetch and well short of crying wolf over one.
STALE_AFTER_MINUTES = 60.0


@dataclass(frozen=True)
class NewsItem:
    """One headline, post or calendar window, and when the bot saw it.

    Deduped across cycles, which is the point of the type. The 30-minute
    Marketaux cache means an identical headline appears in two or three
    consecutive cycles, so a caller counting rows would report one story as
    three. `cycles` keeps that information without inflating the list.

    `first_seen` and `last_seen` answer different questions and both are worth
    having: the first is how old the story is, the second is whether it is
    still in the feed or has already dropped out of it.
    """

    text: str
    first_seen: datetime
    last_seen: datetime
    cycles: int = 1

    def age_minutes(self, now: datetime) -> float:
        """How long ago this first appeared. Never negative, never guessed."""
        return max(0.0, (now - self.first_seen).total_seconds() / 60.0)


@dataclass(frozen=True)
class NewsRecall:
    """Everything the loop was shown in a window, plus what could not be read."""

    window_hours: float = DEFAULT_WINDOW_HOURS

    # Cycles inside the window whose inputs are on file.
    cycles_read: int = 0
    # Cycles inside the window written before `MarketInputs` existed. Their
    # feeds are not on file, which is a different thing from having seen
    # nothing, and collapsing the two would report a gap as a quiet market.
    cycles_without_inputs: int = 0

    latest_cycle_at: datetime | None = None
    oldest_cycle_at: datetime | None = None

    headlines: list[NewsItem] = field(default_factory=list)
    social_posts: list[NewsItem] = field(default_factory=list)
    news_windows: list[NewsItem] = field(default_factory=list)

    calendar_degraded: bool = False
    social_degraded: bool = False
    malformed_lines: int = 0
    unreadable_files: list[str] = field(default_factory=list)

    @property
    def has_cycles(self) -> bool:
        """Whether the loop recorded any feeds at all inside the window.

        Deliberately not the same question as "are the lists empty". False here
        means nobody looked; empty lists with this True means somebody looked
        and there was nothing. Only one of those is news about the market.
        """
        return self.cycles_read > 0

    @property
    def is_degraded(self) -> bool:
        """Whether this recall is known to be incomplete."""
        return (
            self.calendar_degraded
            or self.social_degraded
            or self.malformed_lines > 0
            or bool(self.unreadable_files)
        )

    def latest_cycle_age_minutes(self, now: datetime) -> float | None:
        """Minutes since the newest recorded cycle, or None if there is none."""
        if self.latest_cycle_at is None:
            return None
        return max(0.0, (now - self.latest_cycle_at).total_seconds() / 60.0)

    def is_stale(self, now: datetime) -> bool:
        """Whether the newest cycle is old enough to suggest the loop stopped.

        A recall with no cycles is not "stale" — it is empty, and
        `has_cycles` is the field that says so. Reporting both as the same
        state would let a box that has never run look like one that paused.
        """
        age = self.latest_cycle_age_minutes(now)
        return age is not None and age > STALE_AFTER_MINUTES


def recall(
    view: AuditView,
    *,
    hours: float = DEFAULT_WINDOW_HOURS,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> NewsRecall:
    """Collapse an `AuditView` into what the loop was shown over `hours`.

    Pure, and offline by construction: the only input is a view somebody else
    already read off disk.

    Items are ordered newest-first by when they were **first** seen, not by
    when they were last seen. A story that appeared six hours ago and is still
    sitting in the 30-minute cache is not newer than one that broke ten minutes
    ago, and sorting on `last_seen` would say it was.
    """
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(hours=hours)

    headlines: dict[str, _Seen] = {}
    posts: dict[str, _Seen] = {}
    windows: dict[str, _Seen] = {}

    cycles_read = 0
    cycles_without_inputs = 0
    latest: datetime | None = None
    oldest: datetime | None = None
    calendar_degraded = False
    social_degraded = False

    for entry in view.decisions:
        stamp = _aware(entry.timestamp)
        if stamp < cutoff:
            # `AuditView.decisions` is newest-first, so everything past here is
            # older still. Breaking rather than continuing keeps a long history
            # free, the same way `AuditLog.read` stops once it has enough.
            break

        inputs = entry.decision.inputs
        if inputs is None:
            cycles_without_inputs += 1
            continue

        cycles_read += 1
        latest = stamp if latest is None else max(latest, stamp)
        oldest = stamp if oldest is None else min(oldest, stamp)

        # Any failed fetch in the window, not merely the newest one. The claim
        # is completeness over the whole span, and one bad cycle breaks it.
        calendar_degraded = calendar_degraded or inputs.calendar_degraded
        social_degraded = social_degraded or inputs.social_degraded

        _absorb(headlines, inputs.headlines, stamp)
        _absorb(posts, inputs.social_posts, stamp)
        _absorb(windows, inputs.news_windows, stamp)

    return NewsRecall(
        window_hours=hours,
        cycles_read=cycles_read,
        cycles_without_inputs=cycles_without_inputs,
        latest_cycle_at=latest,
        oldest_cycle_at=oldest,
        headlines=_ordered(headlines, limit),
        social_posts=_ordered(posts, limit),
        news_windows=_ordered(windows, limit),
        calendar_degraded=calendar_degraded,
        social_degraded=social_degraded,
        malformed_lines=view.malformed,
        unreadable_files=list(view.unreadable_files),
    )


def render(result: NewsRecall, *, now: datetime | None = None) -> list[str]:
    """Plain lines carrying the caveats the raw lists do not.

    Written for a reader that will quote it. Every caveat that makes the
    difference between "the bot saw nothing" and "the bot was not looking"
    appears here as a sentence, because a caller handed only the lists will
    reach for the first reading every time.
    """
    moment = now or datetime.now(UTC)
    lines: list[str] = []

    if not result.has_cycles:
        lines.append(
            f"No cycle recorded its feeds in the last {result.window_hours:g}h. "
            "That means the loop was not running, was restarting, or the market "
            "was shut — it does NOT mean there was no news."
        )
        if result.cycles_without_inputs:
            lines.append(
                f"{result.cycles_without_inputs} cycle(s) in the window predate "
                "input recording, so what they were shown is not on file."
            )
        return lines

    age = result.latest_cycle_age_minutes(moment)
    lines.append(
        f"{result.cycles_read} cycle(s) in the last {result.window_hours:g}h. "
        f"Newest reading is {age:.0f} minutes old."
        if age is not None
        else f"{result.cycles_read} cycle(s) in the last {result.window_hours:g}h."
    )
    lines.append(
        "This is what the trading loop was shown and recorded, not a live news "
        "search. Nothing here was fetched to answer this question."
    )

    if result.is_stale(moment):
        lines.append(
            "The newest reading is over an hour old. Check the loop is running "
            "before treating any of this as current."
        )
    if result.cycles_without_inputs:
        lines.append(
            f"{result.cycles_without_inputs} cycle(s) in the window predate "
            "input recording and contributed nothing here."
        )
    if result.social_degraded:
        lines.append(
            "The X feed reported a failed fetch in this window, so the post list "
            "is incomplete. An empty list does NOT mean nothing was posted."
        )
    if result.calendar_degraded:
        lines.append(
            "The earnings calendar was degraded in this window, so the blackout "
            "windows below are incomplete. Zero windows is not the same as no "
            "announcements."
        )
    if result.malformed_lines or result.unreadable_files:
        lines.append(
            f"{result.malformed_lines} unparseable audit line(s) and "
            f"{len(result.unreadable_files)} unreadable file(s) were skipped."
        )
    if not (result.headlines or result.social_posts or result.news_windows):
        lines.append(
            "The loop looked and every feed came back empty. That is a real "
            "reading, unlike the case above."
        )

    return lines


# --------------------------------------------------------------------- private


@dataclass
class _Seen:
    first: datetime
    last: datetime
    cycles: int = 1


def _absorb(store: dict[str, _Seen], texts: list[str], stamp: datetime) -> None:
    """Fold one cycle's list into the running dedupe, ignoring blank lines."""
    for raw in texts:
        text = raw.strip()
        if not text:
            continue
        seen = store.get(text)
        if seen is None:
            store[text] = _Seen(first=stamp, last=stamp)
            continue
        seen.first = min(seen.first, stamp)
        seen.last = max(seen.last, stamp)
        seen.cycles += 1


def _ordered(store: dict[str, _Seen], limit: int) -> list[NewsItem]:
    items = [
        NewsItem(text=text, first_seen=s.first, last_seen=s.last, cycles=s.cycles)
        for text, s in store.items()
    ]
    items.sort(key=lambda i: (i.first_seen, i.last_seen), reverse=True)
    return items[: max(0, limit)]


def _aware(stamp: datetime) -> datetime:
    """Treat a naive timestamp as UTC rather than raising on the comparison.

    Everything the loop writes is timezone-aware, but the log is append-only
    and read tolerantly, so a hand-edited or older line must not take the
    reader down. Same principle as `audit._timestamp`.
    """
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
