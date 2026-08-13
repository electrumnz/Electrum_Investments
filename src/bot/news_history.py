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

## What else is in that log

The second half of this module reads **considerations**: the notes a chat agent
put to the dreamer with `raise_consideration`. Different subject, identical
shape — a windowed, pure read over an `AuditView`, with the age travelling
beside each item and an empty list that says which kind of empty it is. It lives
here rather than in `dreaming.py` on purpose, because nothing in `dreaming.py`
reads the audit log and that is what keeps a conversation from writing a shelf
row. See the block comment above `CONSIDERATION_EVENT`.
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

    #: The newest cycle in the window that recorded at least one X post.
    #:
    #: Positive evidence that a read worked. Its ABSENCE is not evidence that
    #: one failed: the loop records a successful-but-empty fetch and no feed at
    #: all identically, so a watched account that said nothing looks the same as
    #: a feed that was never built. Anything reporting this has to say so.
    social_last_seen_at: datetime | None = None

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
        # Computed over every sighting rather than off the head of the ordered
        # list, which is sorted by `first_seen`: the most recently SEEN post is
        # frequently an old one still sitting in the feed.
        social_last_seen_at=max((s.last for s in posts.values()), default=None),
    )


@dataclass(frozen=True)
class Sightings:
    """Every distinct item in a view, with when it was first and last seen.

    The recall above answers "what has the loop been shown lately". This
    answers a different question, asked by a surface that is already rendering
    one particular cycle: **is this headline new, or has it been on file for
    hours?** The 30-minute Marketaux cache means most of what a cycle saw was
    also in the three cycles before it, and a page that rendered each of them
    as a fresh sighting would show one story forty times and call each of them
    news.

    No window and no limit, because the caller has already chosen the span by
    choosing what to read off disk. That is also the limit worth stating: this
    knows only about the cycles in the view it was handed, so an item first
    appearing in the OLDEST cycle here may well be older than that. Hence
    `oldest_cycle_at` travelling with it — a caller can tell "this is new" from
    "this is as old as anything I can see", and only the first is a claim.
    """

    headlines: dict[str, NewsItem] = field(default_factory=dict)
    social_posts: dict[str, NewsItem] = field(default_factory=dict)
    news_windows: dict[str, NewsItem] = field(default_factory=dict)

    #: The oldest recorded cycle in the view, or `None` if none had inputs.
    oldest_cycle_at: datetime | None = None

    def is_edge(self, stamp: datetime) -> bool:
        """Whether a cycle sits at the old end, where "new" cannot be claimed."""
        return self.oldest_cycle_at is None or _aware(stamp) <= self.oldest_cycle_at


def sightings(view: AuditView) -> Sightings:
    """Index a view by item text. Pure, and offline like everything else here."""
    headlines: dict[str, _Seen] = {}
    posts: dict[str, _Seen] = {}
    windows: dict[str, _Seen] = {}
    oldest: datetime | None = None

    for entry in view.decisions:
        inputs = entry.decision.inputs
        if inputs is None:
            continue
        stamp = _aware(entry.timestamp)
        oldest = stamp if oldest is None else min(oldest, stamp)
        _absorb(headlines, inputs.headlines, stamp)
        _absorb(posts, inputs.social_posts, stamp)
        _absorb(windows, inputs.news_windows, stamp)

    return Sightings(
        headlines=_as_items(headlines),
        social_posts=_as_items(posts),
        news_windows=_as_items(windows),
        oldest_cycle_at=oldest,
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


# ------------------------------------------------------- raised in conversation
#
# The chat agents can put something to the dreamer with `raise_consideration`.
# That tool writes ONE line to the audit log and never opens `data/dreams.db`,
# which is the whole containment: a dream is the first link of a chain that ends
# in a live trading permission, and a conversation must not be able to insert at
# the top of it.
#
# So this is the other half of that arrangement, and it is deliberately a READER
# in this module rather than anything in `dreaming.py`. Nothing there reads the
# audit log, nothing here writes a shelf row, and a consideration therefore
# becomes a dream only by the dreamer choosing to make one — exactly as it
# chooses its own seeds. Keep it that way: a function here that returned a
# `Dream`, or a call from `dreaming.py` into this module, would collapse the
# separation into a matter of discipline.
#
# Same rules as the recall above, for the same reasons. The age travels with the
# item, the record says when it is incomplete, and "nothing was put up" is a
# different answer from "nothing could be read".

#: The audit `kind` a consideration is stored under.
#:
#: The literal is duplicated from `mcp_server.CONSIDERATION_EVENT` rather than
#: imported, and that is a trade rather than an oversight: importing the MCP
#: server here would drag a whole tool surface and its module-level session into
#: a module whose entire claim is that it is pure functions over an `AuditView`.
#: Two places naming one string is two places that can disagree, so
#: `tests/test_dreamer.py` fails the build if they ever do.
CONSIDERATION_EVENT = "chat_consideration"

#: The audit `kind` a dream run writes to say which considerations it was shown.
#:
#: A new event kind rather than a column, because the log is append-only and
#: **must never be migrated** — a reader that rejected yesterday's format would
#: throw away the history it exists to preserve. Nothing already written is
#: touched; a run states what it saw, in the same way `loop_start` states that a
#: loop began.
CONSIDERATIONS_SEEN_EVENT = "dream_considerations_seen"

#: The payload field on that event: the stamps of the considerations that were
#: actually RENDERED into the prompt.
#:
#: The exact set, never a high-water mark. `seen.py` learned this the hard way in
#: its own shape — a marker stamped at `now` marks as seen whatever arrived
#: between the render and the write. Here the same trap has a second door: a
#: limit that trimmed the list would leave older unseen notes below a high-water
#: stamp, marked as considered by a run that never displayed them. A set of what
#: was on the page cannot over-claim.
SHOWN_FIELD = "shown"

#: What "recent" means for a consideration, unless a caller says otherwise.
#:
#: Two days rather than one, because the dreamer runs daily: a single failed run
#: must not lose a spark. Not much more than two, because anything older arriving
#: under the heading "raised in conversation" is being presented as current when
#: it is not — the confident-partial-answer failure arriving through the prompt.
DEFAULT_CONSIDERATION_HOURS = 48.0

#: How many to hand back. `raise_consideration` caps the surface at three a day,
#: so inside a two-day window this cannot bind through the tool; it bounds a
#: hand-written log rather than an ordinary one, and what it drops is reported.
DEFAULT_CONSIDERATION_LIMIT = 12


@dataclass(frozen=True)
class Consideration:
    """One thing a chat agent put to the dreamer, as it sits in the log.

    Note what it does NOT carry: no symbol, no instrument class, no chain, no
    verdict. That is not this reader being selective — those fields are not in
    the record, because `raise_consideration` never writes them. A consideration
    is a note, and the shape of the type says so.
    """

    at: datetime
    speaker: str
    spark: str
    why_now: str = ""
    origin: str = ""
    #: The operator's own words, verbatim, when something they said led to this.
    #: Empty when nobody prompted it — which is a real state, not a gap.
    prompted_by: str = ""
    #: What share of the operator's wording came straight back in the spark.
    #: `None` rather than `0.0` when nothing was quoted: the flattering reading
    #: must not be what an absence of evidence looks like.
    prompt_echo: float | None = None

    def age_minutes(self, now: datetime) -> float:
        """How long ago this was put up. Never negative, never guessed."""
        return max(0.0, (now - self.at).total_seconds() / 60.0)

    @property
    def key(self) -> str:
        """How a run records that this one was on the page.

        The log's own stamp. Two considerations cannot share it — they are
        separate tool calls, each stamped by `record_event` — and it survives a
        round trip through `isoformat`, which is what makes the seen set exact
        rather than approximate.
        """
        return self.at.isoformat()


@dataclass(frozen=True)
class ConsiderationRecall:
    """What is waiting for the dreamer, and what could not be established.

    `considerations` is what has NOT been shown to a dream run yet. Everything
    else on here exists so an empty list can be read correctly, because it has
    four causes and only one of them is "nobody had anything":

    - nobody put anything up — the ordinary state,
    - somebody did and a previous run was already shown it (`seen_previously`),
    - the log recorded nothing at all in the window, so the silence is the box's
      rather than the operator's (`has_record`),
    - the log could not be fully read (`is_degraded`).

    Same rule as `has_cycles` above and `can_grade_anything` in `triggers`: a
    caller handed only the list reaches for the first reading every time.
    """

    window_hours: float = DEFAULT_CONSIDERATION_HOURS

    #: Not yet shown to a dream run, newest first.
    considerations: list[Consideration] = field(default_factory=list)

    #: Inside the window and already shown on an earlier run. Counted rather
    #: than returned: re-offering one would be the dreamer nagged by the same
    #: note every day until it gave in, which is the operator steering it by
    #: accident.
    seen_previously: int = 0

    #: Dropped by `limit` after everything else. Named because they are unseen
    #: and are NOT marked seen by this run, so they come back next time.
    omitted_for_limit: int = 0

    #: Rows carrying no spark. Impossible through `raise_consideration`, which
    #: refuses a blank one deterministically, so this means a hand-edited log —
    #: and a row dropped in silence is exactly how "nothing was raised" becomes
    #: the wrong answer.
    rows_without_a_spark: int = 0

    #: Any audit record at all inside the window — a cycle, an event, anything.
    entries_in_window: int = 0

    malformed_lines: int = 0
    unreadable_files: list[str] = field(default_factory=list)

    @property
    def has_record(self) -> bool:
        """Whether the log has anything at all to say about this window.

        False means nothing was written: the box was off, the loop was stopped,
        or this is a fresh deployment. It does NOT mean the window was quiet,
        and a renderer that let an empty list say so would be inventing the one
        fact this cannot establish.
        """
        return self.entries_in_window > 0

    @property
    def is_degraded(self) -> bool:
        """Whether this reading is known to be incomplete."""
        return self.malformed_lines > 0 or bool(self.unreadable_files)

    @property
    def shown_keys(self) -> list[str]:
        """The stamps a run should record once these have been rendered.

        Exactly what is in `considerations` — so a caller that renders the list
        and writes this back cannot mark anything that was not on the page.
        """
        return [item.key for item in self.considerations]


def recall_considerations(
    view: AuditView,
    *,
    hours: float = DEFAULT_CONSIDERATION_HOURS,
    limit: int = DEFAULT_CONSIDERATION_LIMIT,
    now: datetime | None = None,
) -> ConsiderationRecall:
    """What the chat surface has put up and no dream run has been shown yet.

    Pure and offline, like everything else here: the only input is a view
    somebody else already read off disk. Read it with `AuditLog.read()`, which
    walks the dated files and reports what it could not parse — reading today's
    file alone empties at UTC midnight and after every restart, which is the bug
    `get_recent_decisions` shipped with.

    Newest first, because that is the order a reader wants and the order the
    ages then run in.
    """
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(hours=hours)

    # Collected across the WHOLE view rather than the window. A run that marked
    # something is always later than the note it marked, so window-filtering
    # would be harmless — but "harmless because of an argument" is how a set that
    # silently under-covers gets shipped, and the whole set is two lookups.
    shown: set[str] = set()
    for event in view.events:
        if event.kind != CONSIDERATIONS_SEEN_EVENT:
            continue
        raw = event.payload.get(SHOWN_FIELD)
        if isinstance(raw, list):
            shown.update(str(key) for key in raw)

    unseen: list[Consideration] = []
    seen_previously = 0
    blank = 0
    entries = 0

    for entry in view.decisions:
        if _aware(entry.timestamp) >= cutoff:
            entries += 1

    for event in view.events:
        stamp = _aware(event.timestamp)
        if stamp < cutoff:
            continue
        entries += 1
        if event.kind != CONSIDERATION_EVENT:
            continue
        item = _as_consideration(event.payload, stamp)
        if item is None:
            blank += 1
            continue
        if item.key in shown:
            seen_previously += 1
            continue
        unseen.append(item)

    unseen.sort(key=lambda c: c.at, reverse=True)
    kept = unseen[: max(0, limit)]

    return ConsiderationRecall(
        window_hours=hours,
        considerations=kept,
        seen_previously=seen_previously,
        omitted_for_limit=len(unseen) - len(kept),
        rows_without_a_spark=blank,
        entries_in_window=entries,
        malformed_lines=view.malformed,
        unreadable_files=list(view.unreadable_files),
    )


def describe_age(minutes: float) -> str:
    """An age a reader can act on, in the largest unit that still says something.

    Rendering every age in minutes is technically honest and practically
    unreadable at two days; rounding everything to days loses the difference
    between a spark from this morning and one from ten minutes ago, which is the
    whole reason the age travels at all.
    """
    if minutes < 1:
        return "just now"
    if minutes < 90:
        return f"{minutes:.0f} minutes ago"
    hours = minutes / 60.0
    if hours < 36:
        return f"{hours:.0f} hours ago"
    return f"{hours / 24.0:.0f} days ago"


# --------------------------------------------------------------------- private


def _as_consideration(payload: dict[str, object], stamp: datetime) -> Consideration | None:
    """One log payload as a value, or None when it carries no spark to show."""
    spark = str(payload.get("spark") or "").strip()
    if not spark:
        return None
    return Consideration(
        at=stamp,
        speaker=str(payload.get("speaker") or "").strip(),
        spark=spark,
        why_now=str(payload.get("why_now") or "").strip(),
        origin=str(payload.get("origin") or "").strip(),
        # NOT stripped of anything but whitespace, and never reflowed. It is the
        # operator's own sentence, and a tidied quote takes from the reader the
        # judgement the quote exists to allow.
        prompted_by=str(payload.get("prompted_by") or "").strip(),
        prompt_echo=_echo(payload.get("prompt_echo")),
    )


def _echo(value: object) -> float | None:
    """The echo ratio, or None. A bool is not a number here, whatever Python says."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


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


def _as_items(store: dict[str, _Seen]) -> dict[str, NewsItem]:
    return {
        text: NewsItem(text=text, first_seen=s.first, last_seen=s.last, cycles=s.cycles)
        for text, s in store.items()
    }


def _ordered(store: dict[str, _Seen], limit: int) -> list[NewsItem]:
    items = list(_as_items(store).values())
    items.sort(key=lambda i: (i.first_seen, i.last_seen), reverse=True)
    return items[: max(0, limit)]


def _aware(stamp: datetime) -> datetime:
    """Treat a naive timestamp as UTC rather than raising on the comparison.

    Everything the loop writes is timezone-aware, but the log is append-only
    and read tolerantly, so a hand-edited or older line must not take the
    reader down. Same principle as `audit._timestamp`.
    """
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
