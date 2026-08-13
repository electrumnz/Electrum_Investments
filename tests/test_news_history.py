"""Recalling the feeds the loop was shown, out of the audit log.

The assertions that matter here are the honesty ones rather than the recall.
Getting the headlines back is easy; the failure this module exists to prevent
is a stopped loop reading as a quiet market, and a stale headline reading as
current news. Both are the "confident partial answer" this repository is built
around avoiding, arriving through the chat surface instead of the model.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.audit import AuditLog
from bot.models import Decision, MarketInputs
from bot.news_history import recall, render

NOW = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)


def _cycle(
    minutes_ago: int,
    *,
    headlines: list[str] | None = None,
    posts: list[str] | None = None,
    windows: list[str] | None = None,
    social_degraded: bool = False,
    calendar_degraded: bool = False,
) -> Decision:
    return Decision(
        timestamp=NOW - timedelta(minutes=minutes_ago),
        inputs=MarketInputs(
            headlines=headlines or [],
            social_posts=posts or [],
            news_windows=windows or [],
            social_degraded=social_degraded,
            calendar_degraded=calendar_degraded,
        ),
    )


def _view(*decisions: Decision, tmp_path):
    log = AuditLog(tmp_path)
    for decision in decisions:
        log.record(decision)
    return log.read()


def test_a_headline_repeated_across_cycles_is_one_story(tmp_path):
    """The 30-minute cache re-serves the same headline; three rows is a lie."""
    view = _view(
        _cycle(45, headlines=["[SPY] Index closes at a record (2026-08-10)"]),
        _cycle(30, headlines=["[SPY] Index closes at a record (2026-08-10)"]),
        _cycle(15, headlines=["[SPY] Index closes at a record (2026-08-10)"]),
        tmp_path=tmp_path,
    )

    result = recall(view, now=NOW)

    assert len(result.headlines) == 1
    item = result.headlines[0]
    assert item.cycles == 3
    # First seen is the oldest sighting, so the age is the story's age.
    assert item.first_seen == NOW - timedelta(minutes=45)
    assert item.last_seen == NOW - timedelta(minutes=15)
    assert item.age_minutes(NOW) == 45.0


def test_items_are_ordered_by_when_they_first_appeared(tmp_path):
    """A cached old story is not newer than one that just broke."""
    view = _view(
        _cycle(300, headlines=["old story"]),
        # The old story is still in the feed, so it has the newer `last_seen`.
        _cycle(5, headlines=["old story", "fresh story"]),
        tmp_path=tmp_path,
    )

    result = recall(view, now=NOW)

    assert [i.text for i in result.headlines] == ["fresh story", "old story"]


def test_nothing_recorded_is_not_reported_as_no_news(tmp_path):
    """The load-bearing one. An empty window means nobody looked."""
    view = _view(tmp_path=tmp_path)

    result = recall(view, now=NOW)

    assert result.has_cycles is False
    assert result.headlines == []

    lines = " ".join(render(result, now=NOW))
    assert "does NOT mean there was no news" in lines
    assert "was not running" in lines


def test_a_quiet_window_is_distinguished_from_an_empty_one(tmp_path):
    """The loop looked and found nothing. That IS a reading, unlike the above."""
    view = _view(_cycle(10), tmp_path=tmp_path)

    result = recall(view, now=NOW)

    assert result.has_cycles is True
    assert result.headlines == []

    lines = " ".join(render(result, now=NOW))
    assert "every feed came back empty" in lines
    assert "does NOT mean there was no news" not in lines


def test_cycles_outside_the_window_are_excluded(tmp_path):
    view = _view(
        _cycle(minutes_ago=60 * 30, headlines=["last week"]),
        _cycle(minutes_ago=20, headlines=["today"]),
        tmp_path=tmp_path,
    )

    result = recall(view, hours=24, now=NOW)

    assert [i.text for i in result.headlines] == ["today"]
    assert result.cycles_read == 1


def test_a_story_older_than_the_window_reports_its_age_as_a_floor(tmp_path):
    """The window clips `first_seen`, and it clips it towards FRESHER.

    A story on file for three days and still in the feed comes back from a
    24-hour window with `first_seen` at the oldest cycle inside it — measured at
    23 hours against a real 72 — and `get_recent_news` tells the agent to quote
    that age. Understating the age of a headline is the exact failure this
    module exists to prevent, arriving through the arithmetic rather than
    through the wording.

    `Sightings.is_edge` already answered this for the surface rendering one
    cycle; `NewsRecall` carried `oldest_cycle_at` and nothing used it.
    """
    view = _view(
        *(
            _cycle(minutes_ago=60 * h, headlines=["cicada brood hits the sesame belt"])
            for h in (72, 30, 23, 12, 1)
        ),
        _cycle(minutes_ago=30, headlines=["broke inside the window"]),
        tmp_path=tmp_path,
    )

    result = recall(view, hours=24, now=NOW)
    by_text = {i.text: i for i in result.headlines}

    old = by_text["cicada brood hits the sesame belt"]
    assert old.at_window_edge is True
    # The figure itself is unchanged and still a floor: 23 hours, not 72.
    assert old.age_minutes(NOW) == 60 * 23

    # A story that genuinely appeared inside the window is NOT flagged. Marking
    # everything would make the caveat furniture and it would stop being read.
    assert by_text["broke inside the window"].at_window_edge is False

    assert result.ages_are_floors is True
    assert any("floors rather than ages" in line for line in render(result, now=NOW))


def test_a_window_that_reaches_past_every_story_states_no_floor(tmp_path):
    """The other half: a caveat that always fires says nothing."""
    view = _view(
        _cycle(minutes_ago=200, headlines=["older"]),
        _cycle(minutes_ago=20, headlines=["newer"]),
        tmp_path=tmp_path,
    )

    result = recall(view, hours=24, now=NOW)

    # "older" IS the oldest cycle read, so it sits on the edge; "newer" does not.
    assert {i.text: i.at_window_edge for i in result.headlines} == {
        "older": True,
        "newer": False,
    }


def test_a_degraded_feed_anywhere_in_the_window_marks_the_list_incomplete(tmp_path):
    """An empty post list from an expired token looks like a quiet morning."""
    view = _view(
        _cycle(40, posts=["[@elonmusk 14:20] something"], social_degraded=True),
        _cycle(10, posts=["[@elonmusk 14:20] something"]),
        tmp_path=tmp_path,
    )

    result = recall(view, now=NOW)

    assert result.social_degraded is True
    assert result.is_degraded is True

    lines = " ".join(render(result, now=NOW))
    assert "does NOT mean nothing was posted" in lines


def test_a_degraded_calendar_is_called_out_separately(tmp_path):
    view = _view(_cycle(10, calendar_degraded=True), tmp_path=tmp_path)

    result = recall(view, now=NOW)

    assert result.calendar_degraded is True
    assert "Zero windows is not the same as no" in " ".join(render(result, now=NOW))


def test_cycles_written_before_input_recording_are_counted_not_ignored(tmp_path):
    """A gap in the record is not a cycle that saw nothing."""
    view = _view(
        Decision(timestamp=NOW - timedelta(minutes=30)),  # no inputs at all
        _cycle(10, headlines=["current"]),
        tmp_path=tmp_path,
    )

    result = recall(view, now=NOW)

    assert result.cycles_read == 1
    assert result.cycles_without_inputs == 1
    assert "predate" in " ".join(render(result, now=NOW))


def test_a_stale_reading_says_so(tmp_path):
    """Three hours since the last cycle describes a stopped loop."""
    view = _view(_cycle(180, headlines=["stale"]), tmp_path=tmp_path)

    result = recall(view, now=NOW)

    assert result.is_stale(NOW) is True
    assert result.latest_cycle_age_minutes(NOW) == 180.0
    assert "Check the loop is running" in " ".join(render(result, now=NOW))


def test_an_empty_recall_is_empty_rather_than_stale(tmp_path):
    """A box that never ran must not read as one that paused."""
    result = recall(_view(tmp_path=tmp_path), now=NOW)

    assert result.has_cycles is False
    assert result.is_stale(NOW) is False
    assert result.latest_cycle_age_minutes(NOW) is None


def test_the_recall_states_it_is_a_recording_not_a_search(tmp_path):
    """Whatever quotes this must not present it as a live news lookup."""
    view = _view(_cycle(5, headlines=["something"]), tmp_path=tmp_path)

    lines = " ".join(render(recall(view, now=NOW), now=NOW))

    assert "not a live news search" in lines


def test_the_limit_applies_per_feed(tmp_path):
    view = _view(
        _cycle(5, headlines=[f"story {n}" for n in range(10)]),
        tmp_path=tmp_path,
    )

    result = recall(view, limit=3, now=NOW)

    assert len(result.headlines) == 3


def test_malformed_audit_lines_are_reported_rather_than_swallowed(tmp_path):
    log = AuditLog(tmp_path)
    log.record(_cycle(5, headlines=["good"]))
    path = next(iter(tmp_path.glob("*.jsonl")))
    with path.open("a", encoding="utf-8") as f:
        f.write("{ this is not json\n")

    result = recall(log.read(), now=NOW)

    assert result.malformed_lines == 1
    assert result.is_degraded is True
    assert [i.text for i in result.headlines] == ["good"]
    assert "unparseable audit line" in " ".join(render(result, now=NOW))


def test_the_newest_post_sighting_is_the_newest_LAST_seen(tmp_path):
    """Ordered lists sort by `first_seen`, so the head of the list is regularly
    an old story still sitting in the feed. "When did a read last work" is the
    other question and needs the other field."""
    view = _view(
        _cycle(90, posts=["[@a 13:30] old post"]),
        _cycle(10, posts=["[@a 13:30] old post"]),
        tmp_path=tmp_path,
    )

    result = recall(view, now=NOW)

    assert result.social_posts[0].first_seen == NOW - timedelta(minutes=90)
    assert result.social_last_seen_at == NOW - timedelta(minutes=10)


def test_no_posts_at_all_leaves_the_last_sighting_unknown(tmp_path):
    """`None` rather than a stamp, because the absence is not a reading."""
    view = _view(_cycle(10, headlines=["something"]), tmp_path=tmp_path)

    assert recall(view, now=NOW).social_last_seen_at is None


# ------------------------------------------------------------------ sightings
# The index a per-cycle surface uses to tell a story it has already shown from
# one that has just broken.


def test_the_index_collapses_repeats_and_keeps_both_ends(tmp_path):
    from bot.news_history import sightings

    view = _view(
        _cycle(90, headlines=["Fed holds"]),
        _cycle(45, headlines=["Fed holds"]),
        _cycle(10, headlines=["Fed holds", "Chips soften"]),
        tmp_path=tmp_path,
    )

    index = sightings(view)

    assert index.headlines["Fed holds"].cycles == 3
    assert index.headlines["Fed holds"].first_seen == NOW - timedelta(minutes=90)
    assert index.headlines["Fed holds"].last_seen == NOW - timedelta(minutes=10)
    assert index.headlines["Chips soften"].cycles == 1


def test_the_index_names_its_own_edge(tmp_path):
    """It knows only about the cycles it was handed, so the oldest of them
    cannot support a claim that anything in it is new."""
    from bot.news_history import sightings

    view = _view(
        _cycle(90, headlines=["a"]), _cycle(10, headlines=["b"]), tmp_path=tmp_path
    )

    index = sightings(view)

    assert index.oldest_cycle_at == NOW - timedelta(minutes=90)
    assert index.is_edge(NOW - timedelta(minutes=90)) is True
    assert index.is_edge(NOW - timedelta(minutes=10)) is False


def test_an_empty_index_treats_every_cycle_as_its_edge(tmp_path):
    """A caller with no index must not be handed a confident "new this cycle"."""
    from bot.news_history import Sightings

    assert Sightings().is_edge(NOW) is True


def test_cycles_with_no_inputs_do_not_enter_the_index(tmp_path):
    """They have no feeds on file, so they can neither contribute an item nor
    set the edge to a moment nothing was recorded at."""
    from bot.news_history import sightings

    view = _view(
        Decision(timestamp=NOW - timedelta(minutes=200)),
        _cycle(10, headlines=["only this"]),
        tmp_path=tmp_path,
    )

    index = sightings(view)

    assert index.oldest_cycle_at == NOW - timedelta(minutes=10)
    assert list(index.headlines) == ["only this"]


def test_a_naive_timestamp_does_not_crash_the_reader(tmp_path):
    """The log is append-only and read tolerantly; a hand-edited line happens."""
    log = AuditLog(tmp_path)
    log.record(
        Decision(
            timestamp=(NOW - timedelta(minutes=5)).replace(tzinfo=None),
            inputs=MarketInputs(headlines=["naive"]),
        )
    )

    result = recall(log.read(), now=NOW)

    assert [i.text for i in result.headlines] == ["naive"]
