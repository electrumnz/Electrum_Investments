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
enough that a handful of accounts per cycle is nowhere near the ceiling. The
binding constraint on the paid tiers is instead a **monthly cap on posts
retrieved**, which is a budget rather than a rate. The cache is therefore a
quota control in the same sense as Marketaux's, and the TTL is short because
the entire value of this feed is timeliness: caching a presidential post for
half an hour would defeat the point of having it.

Free access does not meaningfully allow reading timelines. Running without a
token is a supported configuration and simply yields no posts.
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


@dataclass(frozen=True)
class Post:
    account: str
    text: str
    created_at: datetime | None = None

    def render(self) -> str:
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

    def _timeline(self, handle: str, account_id: str, since: datetime) -> list[Post] | None:
        assert self.getter is not None
        result = fetch_json(
            self.getter,
            f"{API_ROOT}/users/{account_id}/tweets",
            {
                # Replies and reposts are noise for this purpose: what matters
                # is what the account said, not what it agreed with.
                "exclude": "replies,retweets",
                # The API's floor is 5, so asking for fewer is rejected outright.
                "max_results": str(max(5, min(self.max_posts, 100))),
                "start_time": since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "tweet.fields": "created_at",
            },
            feed="x",
        )
        if not result.ok:
            return None
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
        posts.append(Post(account=handle, text=text, created_at=_when(row.get("created_at"))))
    return posts


def _when(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
