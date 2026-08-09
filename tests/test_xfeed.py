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
