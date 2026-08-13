"""The X feed: posts from accounts whose words move prices before the wire does.

No test here touches a network. The getter is injected, exactly as it is for
Marketaux and Finnhub, so the whole feed is exercised against canned payloads.

The assertions that matter most are the negative ones. This feed's failure mode
is worse than Marketaux's: an empty list from a dead token looks identical to a
quiet morning, and the second is the one you would act on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from bot.data.xfeed import API_ROOT, XFeed

NOW = datetime(2026, 8, 9, 15, 0, tzinfo=UTC)


def fake_getter(responses: dict[str, Any], *, calls: list[str] | None = None):
    """Serve canned payloads by URL. Anything unexpected raises, loudly."""

    def _get(url: str, params: dict[str, str]) -> Any:
        if calls is not None:
            calls.append(url)
        for fragment, payload in responses.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise AssertionError(f"unexpected URL: {url}")

    return _get


def user(handle: str, uid: str) -> tuple[str, Any]:
    return f"/users/by/username/{handle}", {"data": {"id": uid, "username": handle}}


def timeline(uid: str, *texts: str) -> tuple[str, Any]:
    return (
        f"/users/{uid}/tweets",
        {
            "data": [
                {
                    "id": str(1000 + i),
                    "text": text,
                    "created_at": (NOW - timedelta(minutes=i * 5)).isoformat().replace(
                        "+00:00", "Z"
                    ),
                }
                for i, text in enumerate(texts)
            ],
            "meta": {"result_count": len(texts)},
        },
    )


def feed(responses: dict[str, Any], **kw: Any) -> XFeed:
    return XFeed(
        bearer_token="t",
        accounts=kw.pop("accounts", ["realDonaldTrump"]),
        getter=fake_getter(responses),
        **kw,
    )


def test_posts_come_back_newest_first_with_the_handle_and_time():
    f = feed(dict([user("realDonaldTrump", "77"), timeline("77", "Newest", "Older")]))

    posts = f.recent_posts(now=NOW)

    assert [p.text for p in posts] == ["Newest", "Older"]
    rendered = posts[0].render()
    assert "@realDonaldTrump" in rendered
    assert "Newest" in rendered
    assert f.is_degraded is False


def test_a_quiet_account_is_not_an_error():
    """A timeline with nothing in the window omits `data` entirely."""
    f = feed(
        dict(
            [
                user("realDonaldTrump", "77"),
                (f"{API_ROOT}/users/77/tweets", {"meta": {"result_count": 0}}),
            ]
        )
    )

    assert f.recent_posts(now=NOW) == []
    assert f.is_degraded is False


def test_a_failed_fetch_is_reported_as_degraded_not_as_silence():
    """The whole reason this flag exists.

    An empty list from an expired token is indistinguishable from a quiet
    morning, and only one of them should change how a price move is read.
    """
    f = feed(
        dict(
            [
                user("realDonaldTrump", "77"),
                (f"{API_ROOT}/users/77/tweets", RuntimeError("401 Unauthorized")),
            ]
        )
    )

    assert f.recent_posts(now=NOW) == []
    assert f.is_degraded is True


def test_a_degraded_result_is_not_cached():
    """Otherwise one bad minute silences the feed for the whole TTL."""
    calls: list[str] = []
    responses: dict[str, Any] = dict(
        [
            user("realDonaldTrump", "77"),
            (f"{API_ROOT}/users/77/tweets", RuntimeError("503")),
        ]
    )
    f = XFeed(
        bearer_token="t",
        accounts=["realDonaldTrump"],
        getter=fake_getter(responses, calls=calls),
    )

    f.recent_posts(now=NOW)
    f.recent_posts(now=NOW)

    assert sum(1 for c in calls if "tweets" in c) == 2


def test_a_good_result_is_cached_so_a_cycle_costs_one_call():
    calls: list[str] = []
    responses = dict([user("realDonaldTrump", "77"), timeline("77", "Only once")])
    f = XFeed(
        bearer_token="t",
        accounts=["realDonaldTrump"],
        getter=fake_getter(responses, calls=calls),
    )

    f.recent_posts(now=NOW)
    f.recent_posts(now=NOW)

    assert sum(1 for c in calls if "tweets" in c) == 1


def test_the_handle_is_resolved_once_and_remembered():
    """A user ID never changes, so re-resolving would double the request count."""
    calls: list[str] = []
    responses = dict([user("realDonaldTrump", "77"), timeline("77", "a")])
    f = XFeed(
        bearer_token="t",
        accounts=["realDonaldTrump"],
        getter=fake_getter(responses, calls=calls),
        cache_ttl_seconds=0.0,
    )

    f.recent_posts(now=NOW)
    f.recent_posts(now=NOW)

    assert sum(1 for c in calls if "by/username" in c) == 1


def test_one_broken_account_marks_the_whole_answer_incomplete():
    """A partial answer presented as a complete one is what this guards against."""
    responses: dict[str, Any] = dict(
        [
            user("realDonaldTrump", "77"),
            timeline("77", "This one worked"),
            (f"{API_ROOT}/users/by/username/federalreserve", RuntimeError("429")),
        ]
    )
    f = feed(responses, accounts=["realDonaldTrump", "federalreserve"])

    posts = f.recent_posts(now=NOW)

    assert [p.text for p in posts] == ["This one worked"]
    assert f.is_degraded is True


def test_no_token_means_no_calls_at_all():
    f = XFeed(bearer_token="", accounts=["realDonaldTrump"], getter=fake_getter({}))
    assert f.recent_posts(now=NOW) == []


def test_long_posts_are_truncated_rather_than_flooding_the_prompt():
    f = feed(dict([user("realDonaldTrump", "77"), timeline("77", "x" * 900)]))

    text = f.recent_posts(now=NOW)[0].text

    assert len(text) <= 240
    assert text.endswith("…")


def test_whitespace_is_collapsed_so_one_post_is_one_line():
    """Posts carry newlines; the market context is line-oriented."""
    f = feed(dict([user("realDonaldTrump", "77"), timeline("77", "Tariffs\n\n  on   steel")]))

    assert f.recent_posts(now=NOW)[0].text == "Tariffs on steel"


def test_a_malformed_payload_costs_posts_rather_than_raising():
    f = feed(dict([user("realDonaldTrump", "77"), (f"{API_ROOT}/users/77/tweets", ["nope"])]))

    assert f.recent_posts(now=NOW) == []


# ------------------------------------------------- the request X actually gets
#
# Checked against docs.x.com on 2026-08-10 and NOT exercised live: no bearer
# token exists on this box, so these pin what the code sends rather than what
# the API accepted. That distinction is the point of saying it here — a green
# suite is evidence about this repository, never about a third party.


def _params_for(handle: str, uid: str) -> dict[str, str]:
    seen: dict[str, str] = {}

    def _get(url: str, params: dict[str, str]) -> Any:
        if "by/username" in url:
            return {"data": {"id": uid, "username": handle}}
        seen.update(params)
        return {"meta": {"result_count": 0}}

    XFeed(bearer_token="t", accounts=[handle], getter=_get).recent_posts(now=NOW)
    return seen


def test_the_timeline_request_matches_the_documented_v2_shape():
    params = _params_for("realDonaldTrump", "77")

    # `tweet.fields`, NOT `post.fields`. The documentation renamed Tweets to
    # Posts throughout and the parameter did not follow.
    assert params["tweet.fields"] == "created_at"
    # A non-exploded array: one comma-separated value, never repeated keys.
    assert params["exclude"] == "replies,retweets"
    # RFC3339 in UTC, which is what the docs mean by ISO 8601.
    assert params["start_time"] == "2026-08-09T13:30:00Z"


def test_the_page_asked_for_is_larger_than_the_page_kept():
    """`exclude` filters AFTER retrieval, so `max_results` bounds what is
    CONSIDERED. Asking for exactly `max_posts` lets a run of replies push the
    originals out of the page — and the answer then looks like a quiet account,
    which is the one reading this feed exists to prevent."""
    params = _params_for("realDonaldTrump", "77")

    assert int(params["max_results"]) > XFeed(
        bearer_token="t", accounts=["x"], getter=lambda u, p: {}
    ).max_posts


def test_the_page_size_respects_the_apis_own_floor_and_ceiling():
    """Below 5 the request is rejected outright rather than clamped, so a
    small `max_posts` must not become a small `max_results`."""
    tiny = XFeed(bearer_token="t", accounts=["a"], getter=lambda u, p: {}, max_posts=1)
    huge = XFeed(bearer_token="t", accounts=["a"], getter=lambda u, p: {}, max_posts=100)

    assert tiny.page_size == 5
    assert huge.page_size == 100


def test_a_post_carries_a_permalink_built_from_the_id_the_payload_gave():
    f = feed(dict([user("realDonaldTrump", "77"), timeline("77", "Tariffs")]))

    post = f.recent_posts(now=NOW)[0]

    assert post.post_id == "1000"
    assert post.url == "https://x.com/realDonaldTrump/status/1000"
    # The prompt line stays as it was: the model cannot open a link, and a URL
    # it cannot follow is an invitation to pretend it did.
    assert "https://" not in post.render()


def test_a_post_with_no_id_has_no_link_rather_than_a_broken_one():
    """`https://x.com/handle/status/` is a 404 wearing a permalink's clothes."""
    f = feed(
        dict(
            [
                user("realDonaldTrump", "77"),
                (f"{API_ROOT}/users/77/tweets", {"data": [{"text": "no id here"}]}),
            ]
        )
    )

    assert f.recent_posts(now=NOW)[0].url is None


def test_a_snowflake_id_survives_as_a_string():
    """19 digits is past the range a float keeps intact, and JSON has one
    number type. A coerced id links to a neighbouring post, not to an error."""
    f = feed(
        dict(
            [
                user("realDonaldTrump", "77"),
                (
                    f"{API_ROOT}/users/77/tweets",
                    {"data": [{"id": 1953451234567890123, "text": "big id"}]},
                ),
            ]
        )
    )

    assert f.recent_posts(now=NOW)[0].post_id == "1953451234567890123"


# ------------------------------------------------------------- reported state


def test_a_complete_read_is_timestamped_and_a_partial_one_is_not():
    """The claim the timestamp makes is that the list was COMPLETE at that
    moment, and one account that 429ed makes it false."""
    responses: dict[str, Any] = dict(
        [
            user("realDonaldTrump", "77"),
            timeline("77", "worked"),
            (f"{API_ROOT}/users/by/username/federalreserve", RuntimeError("429")),
        ]
    )
    partial = feed(responses, accounts=["realDonaldTrump", "federalreserve"])
    partial.recent_posts(now=NOW)

    assert partial.last_complete_read_at is None

    whole = feed(dict([user("realDonaldTrump", "77"), timeline("77", "worked")]))
    whole.recent_posts(now=NOW)

    assert whole.last_complete_read_at == NOW


def test_never_having_read_is_not_reported_as_a_healthy_read():
    f = feed(dict([user("realDonaldTrump", "77"), timeline("77", "a")]))

    assert f.last_complete_read_at is None


def test_the_feed_state_keeps_off_apart_from_broken():
    """Off is the normal, fully-functional configuration. A surface that showed
    it as a fault would train an operator to ignore the real one."""
    from bot.data.xfeed import FeedState

    off = FeedState(enabled=False, token_present=False)
    assert off.status == "off"
    assert off.running is False
    assert "gates nothing" in off.headline()

    no_token = FeedState(enabled=True, token_present=False)
    assert no_token.status == "no token"
    assert no_token.running is False

    broken = FeedState(enabled=True, token_present=True, degraded=True)
    assert broken.status == "degraded"


def test_the_feed_state_reports_unknown_rather_than_healthy():
    """`degraded=None` is "nobody looked", which is not "nothing failed"."""
    from bot.data.xfeed import FeedState

    unknown = FeedState(enabled=True, token_present=True, degraded=None)

    assert unknown.status == "on"
    assert any("only of how it is configured" in c for c in unknown.caveats())
