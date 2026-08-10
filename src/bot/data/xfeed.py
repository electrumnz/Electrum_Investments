"""Posts from a handful of accounts whose words move prices.

## Why this is not a joke

A head of state posting about tariffs, a named company, or the Fed moves SPY
within minutes, and the bot trades SPY. The move usually precedes the wire
story, so by the time Marketaux carries a headline the gap has already opened.
That is the whole reason this feed exists: not sentiment, but *knowing an
unscheduled event just happened*.

## It gates nothing, exactly like Marketaux

This is **context**. It lands in the market block, the model reads it, and the
risk gate never sees it. That is deliberate and should stay that way: the gate
is deterministic Python precisely because it must not be persuadable, and "the
model thought this post sounded bearish" is the opposite of a deterministic
input.

The obvious next idea — a blackout window after a high-impact post, mirroring
`news_blackout_minutes_after` — is *not* implemented here, on purpose. It would
change what the gate refuses, and a limit changes deliberately, in its own
commit, with a reason and a test that proves it rejects. See the note in
`docs/HANDOFF.md`.

## Unlike Marketaux, "no posts" is ambiguous

Marketaux returning nothing means a quiet news day and costs a little context.
This feed returning nothing could mean the account has not posted, or that the
token expired, or that the tier stopped allowing reads. Those look identical
from the outside, and the second two are exactly when you would most want to
know. So `is_degraded` exists here and is reported every cycle — the same
lesson `FinnhubCalendar` already taught this repository.

## Cost and quota

The read endpoints below are rate limited per fifteen minutes, generously
enough that a handful of accounts per cycle is nowhere near the ceiling:
`GET /2/users/:id/tweets` is 3,500 requests per 15 minutes app-only, and the
username lookup is 300. The binding constraint is instead a **cap on posts
retrieved**, which is a budget rather than a rate. The cache is therefore a
quota control in the same sense as Marketaux's, and the TTL is short because
the entire value of this feed is timeliness: caching a presidential post for
half an hour would defeat the point of having it.

**There is no free read tier.** X replaced its fixed tiers with pay-per-use on
2026-02-06, metered per post read, and the legacy Basic/Pro subscriptions are
closed to new customers. So running without a token is not a degraded
configuration, it is the DEFAULT one, and every surface that reports on this
feed has to say "off" rather than "broken".

## What was verified, and how

The endpoints, parameters and their limits below were checked against
`docs.x.com` on 2026-08-10 and the findings are recorded beside the code they
constrain. **Nothing here has been exercised against the live API**: no bearer
token exists on this box, so this is a read of the documentation and not a
measurement. The three claims worth treating as documented-rather-than-observed
are the `max_results` floor of 5, the post-retrieval ordering of the `exclude`
filter, and the shape of an empty timeline response.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from ._http import DEFAULT_TIMEOUT_SECONDS, JsonGetter, TtlCache, fetch_json, httpx_getter

log = structlog.get_logger(__name__)

API_ROOT = "https://api.x.com/2"

# Deliberately shorter than the Marketaux cache. That one exists to protect a
# 100-a-day quota; this one only dedupes repeat calls inside a single cycle,
# because a post that is thirty minutes stale is no longer the thing that made
# this feed worth building.
DEFAULT_CACHE_TTL_SECONDS = 600.0

# How far back to look. Longer than one cycle so a post is not missed when a
# cycle is slow or the loop restarts, short enough that yesterday's news does
# not read as breaking.
DEFAULT_LOOKBACK_MINUTES = 90

# Posts are one line each in the prompt. More than this and a busy account
# crowds out the market data it is supposed to be read alongside.
DEFAULT_MAX_POSTS = 12

MAX_TEXT_CHARS = 240

# The API's own bounds on `max_results` for the timeline endpoint. Verified
# against docs.x.com: below the floor the request is rejected outright rather
# than clamped, which is why `max_posts=1` must not become `max_results=1`.
API_MIN_RESULTS = 5
API_MAX_RESULTS = 100

# Ask for several times what we intend to keep.
#
# `exclude=replies,retweets` filters AFTER retrieval rather than before it, so
# `max_results` is an upper bound on what is *considered* and not on what comes
# back. An account that has just posted a run of replies would therefore return
# a handful of originals — or none — while originals sat inside the window
# unread, and nothing would say so: the answer looks exactly like a quiet
# account. Over-fetching costs nothing on a quiet account, because `start_time`
# already bounds the result set to the lookback window; it costs at most this
# multiple on a busy one, which is the case where the missing posts are the
# whole reason this feed exists.
EXCLUDE_OVERFETCH = 3

# A post's public permalink. The handle is the one we asked for rather than one
# read back from the payload, which is why `expansions=author_id` is not
# requested: the timeline endpoint is keyed by a single user, so the author is
# already known and a second field would be a round trip to learn it twice.
POST_URL = "https://x.com/{account}/status/{post_id}"


@dataclass(frozen=True)
class Post:
    account: str
    text: str
    created_at: datetime | None = None

    # X's own id for the post. Present by default on every timeline response —
    # `id`, `text` and `edit_history_tweet_ids` are returned with no
    # `tweet.fields` at all — so this costs nothing to carry.
    post_id: str = ""

    @property
    def url(self) -> str | None:
        """A permalink, or `None` when the payload carried no id.

        `None` rather than a constructed guess: a link to
        `https://x.com/handle/status/` is a broken page, and a surface that
        rendered one would be asserting a post it cannot point at.
        """
        return (
            POST_URL.format(account=self.account, post_id=self.post_id)
            if self.post_id and self.account
            else None
        )

    def render(self) -> str:
        """One line for the prompt.

        Deliberately WITHOUT the permalink. The model cannot open a link, so a
        URL on every post would be output tokens spent on something unreadable
        — and a model handed a link it cannot follow is being invited to
        pretend it did. `url` is there for surfaces that can render an anchor.
        """
        when = (
            self.created_at.astimezone(UTC).strftime("%H:%M")
            if self.created_at
            else "time unknown"
        )
        return f"[@{self.account} {when}] {self.text}"


@dataclass
class XFeed:
    """Recent posts from the configured accounts.

    Account IDs are resolved once and kept: X's timeline endpoint takes a
    numeric ID, not a handle, and an ID never changes. Re-resolving every cycle
    would double the request count to learn something already known.
    """

    bearer_token: str
    accounts: list[str]
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES
    max_posts: int = DEFAULT_MAX_POSTS
    getter: JsonGetter | None = None
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS

    _cache: TtlCache = field(init=False)
    _ids: dict[str, str] = field(init=False, default_factory=dict)
    _degraded: bool = field(init=False, default=False)
    _last_complete_read_at: datetime | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self._cache = TtlCache(ttl_seconds=self.cache_ttl_seconds)
        if self.getter is None:
            self.getter = httpx_getter(
                DEFAULT_TIMEOUT_SECONDS,
                headers={"Authorization": f"Bearer {self.bearer_token}"},
            )

    @property
    def is_degraded(self) -> bool:
        """True when the last fetch failed.

        Reported rather than swallowed: an empty list from a broken token looks
        exactly like a quiet morning, and the difference matters when this is
        being used to explain a move.
        """
        return self._degraded

    @property
    def last_complete_read_at(self) -> datetime | None:
        """When every configured account was last read with nothing failing.

        `None` means it has never happened in this process — never having read
        must not look like a healthy read, the same rule `tailnet` follows for
        a check that has never run.

        A PARTIAL sweep deliberately does not count. The claim this timestamp
        makes is that the post list was complete at that moment, and one
        account that 429ed makes it false.
        """
        return self._last_complete_read_at

    def recent_posts(self, *, now: datetime | None = None) -> list[Post]:
        if not self.bearer_token or not self.accounts:
            return []

        cached, value = self._cache.get()
        if cached:
            return list(value)

        moment = now or datetime.now(UTC)
        since = moment - timedelta(minutes=self.lookback_minutes)

        posts: list[Post] = []
        failures = 0
        for account in self.accounts:
            handle = account.lstrip("@").strip()
            if not handle:
                continue
            account_id = self._resolve(handle)
            if account_id is None:
                failures += 1
                continue
            fetched = self._timeline(handle, account_id, since)
            if fetched is None:
                failures += 1
                continue
            posts.extend(fetched)

        # Any failure marks the feed degraded. A partial answer presented as a
        # complete one is the specific thing this flag exists to prevent.
        self._degraded = failures > 0
        posts.sort(key=lambda p: p.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        trimmed = posts[: self.max_posts]

        # A degraded result is not cached, so the next cycle retries rather than
        # serving a known-incomplete answer for the life of the TTL.
        if not self._degraded:
            self._last_complete_read_at = moment
            self._cache.put(trimmed)
        return trimmed

    def _resolve(self, handle: str) -> str | None:
        if handle in self._ids:
            return self._ids[handle]

        assert self.getter is not None
        result = fetch_json(
            self.getter, f"{API_ROOT}/users/by/username/{handle}", {}, feed="x"
        )
        if not result.ok:
            return None

        payload = result.payload
        data = payload.get("data") if isinstance(payload, dict) else None
        account_id = str(data.get("id")) if isinstance(data, dict) and data.get("id") else None
        if account_id is None:
            log.warning("x_username_not_resolved", handle=handle)
            return None

        self._ids[handle] = account_id
        return account_id

    @property
    def page_size(self) -> int:
        """What to ask the API for, which is not what we intend to keep.

        See `EXCLUDE_OVERFETCH`: the exclusion runs after retrieval, so asking
        for exactly `max_posts` silently under-reports a busy account.
        """
        return max(
            API_MIN_RESULTS, min(self.max_posts * EXCLUDE_OVERFETCH, API_MAX_RESULTS)
        )

    def _timeline(self, handle: str, account_id: str, since: datetime) -> list[Post] | None:
        assert self.getter is not None
        result = fetch_json(
            self.getter,
            f"{API_ROOT}/users/{account_id}/tweets",
            {
                # Replies and reposts are noise for this purpose: what matters
                # is what the account said, not what it agreed with. Comma
                # separated and NOT repeated — the parameter is a
                # non-exploded array, so `exclude=replies&exclude=retweets`
                # is a different request and only the second value survives.
                "exclude": "replies,retweets",
                "max_results": str(self.page_size),
                # RFC3339 in UTC, which is what "ISO 8601" means here. The API
                # rejects anything before 2010-11-06; a lookback of at most a
                # day cannot reach it.
                "start_time": since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                # Still `tweet.fields`, not `post.fields`, despite the
                # documentation having renamed Tweets to Posts throughout.
                # Checked 2026-08-10: the parameter name did not follow the
                # rebrand and `post.fields` is not accepted.
                "tweet.fields": "created_at",
            },
            feed="x",
        )
        if not result.ok:
            return None
        # No pagination, on purpose rather than by omission. The timeline comes
        # back reverse-chronological and `start_time` bounds it to the lookback
        # window, so `meta.next_token` leads only to posts OLDER than the ones
        # already in hand — which is precisely what `max_posts` would trim away.
        # Following it would spend quota to fetch what is then discarded.
        return _parse(handle, result.payload)


def _parse(handle: str, payload: Any) -> list[Post]:
    """Turn a timeline response into posts, skipping anything unrecognised.

    Tolerant for the same reason every other adapter here is: a feed that
    changes shape should cost posts, not raise inside a trading loop.

    An account with nothing in the window returns `{"meta": {"result_count": 0}}`
    and no `data` key at all, which is a successful empty answer rather than a
    malformed one.
    """
    if not isinstance(payload, dict):
        return []
    rows = payload.get("data")
    if not isinstance(rows, list):
        return []

    posts: list[Post] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = " ".join(str(row.get("text") or "").split())
        if not text:
            continue
        if len(text) > MAX_TEXT_CHARS:
            text = text[: MAX_TEXT_CHARS - 1].rstrip() + "…"
        # The id is a string in the payload and must stay one. It is a 19-digit
        # snowflake, past the range a float survives intact, and JSON has one
        # number type — so anything that coerced it would produce a permalink
        # to a neighbouring post rather than an error.
        raw_id = row.get("id")
        posts.append(
            Post(
                account=handle,
                text=text,
                created_at=_when(row.get("created_at")),
                post_id=str(raw_id) if isinstance(raw_id, (str, int)) else "",
            )
        )
    return posts


def _when(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ------------------------------------------------------------- reported state


@dataclass(frozen=True)
class FeedState:
    """What an operator surface is allowed to say about this feed.

    The dashboard never holds an `XFeed` — the loop owns it, in another
    process — so every field here is either read from configuration or read
    back out of the audit log. Nothing on it is inferred from an absence, which
    is the whole reason it carries its own words rather than leaving a renderer
    to paraphrase. Same arrangement as `tailnet.headline()` and
    `news_history.render()`.

    `degraded` is deliberately three-valued. `None` is "no cycle in the window
    recorded its feeds", which is not the same as a window in which nothing
    failed — the `FinnhubCalendar.is_degraded` lesson, one module further out.
    """

    enabled: bool
    token_present: bool
    accounts: tuple[str, ...] = ()
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES
    max_posts: int = DEFAULT_MAX_POSTS
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS

    degraded: bool | None = None
    #: The newest cycle on file that recorded at least one post. Positive
    #: evidence of a read that worked; its ABSENCE is not evidence of one that
    #: failed, because a watched account that said nothing looks identical.
    last_post_at: datetime | None = None

    @property
    def running(self) -> bool:
        """Whether the loop would build a feed at all from this configuration."""
        return self.enabled and self.token_present

    @property
    def status(self) -> str:
        """One word, for a badge. Configuration first, observation second."""
        if not self.enabled:
            return "off"
        if not self.token_present:
            return "no token"
        if self.degraded:
            return "degraded"
        return "on"

    def headline(self) -> str:
        """One sentence saying what state this is and why that is or is not fine."""
        if not self.enabled:
            return (
                "Off. social.enabled is false in config/rules.yaml, so no posts "
                "are fetched and none reach the model. This feed gates nothing, "
                "so a deployment without it is fully functional."
            )
        if not self.token_present:
            return (
                "Enabled in config/rules.yaml but X_BEARER_TOKEN is unset, so "
                "nothing is fetched. Reading timelines needs a paid X tier — "
                "there has been no free read tier since 2026-02-06."
            )
        if self.degraded:
            return (
                f"Reading {len(self.accounts)} account(s), and at least one "
                "fetch FAILED inside this window. An empty post list is "
                "therefore not evidence of a quiet morning."
            )
        return (
            f"Reading {len(self.accounts)} account(s) every cycle. Context "
            "only: nothing here reaches the risk gate."
        )

    def caveats(self) -> list[str]:
        """What a reader would otherwise assume wrongly. Empty is a real answer."""
        out: list[str] = []
        if self.degraded is None:
            out.append(
                "No cycle in this window recorded its feeds, so nothing here is "
                "an observation of the feed running — only of how it is configured."
            )
        if self.running and self.last_post_at is None and self.degraded is False:
            out.append(
                "No post is on file for this window. The loop does not record a "
                "successful-but-empty read apart from no read at all, so this is "
                "not evidence that the fetch failed."
            )
        return out
