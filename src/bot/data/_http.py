"""Shared plumbing for the external data feeds.

Two things every feed here needs, and both are about not being the reason the
bot stops.

**Failure is data, not an exception.** A third-party news API going down must
not take the trading loop with it, so `FeedResult` carries whatever arrived
*plus* whether the fetch actually succeeded. Callers decide what a failure
means: for headlines it means slightly less context, which is fine; for the
earnings calendar it means the risk gate's news blackout is running blind,
which the operator has to be told about. Returning a bare empty list would make
those two indistinguishable.

**Caching is a rate-limit requirement, not an optimisation.** Marketaux's free
tier allows 100 requests a day and the loop wakes 96 times, so one uncached
call per cycle would exhaust the quota before the day was out and leave no room
for a single retry. News does not change meaningfully inside fifteen minutes
anyway.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

log = structlog.get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class FeedResult:
    """What a feed returned, and whether the fetch behind it worked.

    `ok=False` with an empty payload is the important case: it means "we do not
    know", which is not the same as "there is nothing", and the difference
    matters when a risk control is reading it.
    """

    payload: Any
    ok: bool = True
    error: str = ""

    @classmethod
    def failed(cls, error: str) -> FeedResult:
        return cls(payload=None, ok=False, error=error)


class JsonGetter(Protocol):
    """The one operation the feeds need. Injected so tests never touch a network."""

    def __call__(self, url: str, params: dict[str, str]) -> Any: ...


def httpx_getter(
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    headers: dict[str, str] | None = None,
) -> JsonGetter:
    """The real implementation, built lazily so importing this module is cheap.

    `headers` is closed over rather than added to `JsonGetter.__call__`, so the
    protocol every feed and every test fake implements stays two arguments wide.
    X needs an `Authorization: Bearer` header; Marketaux and Finnhub pass their
    key as a query parameter and are unaffected.
    """

    def _get(url: str, params: dict[str, str]) -> Any:
        import httpx

        response = httpx.get(url, params=params, timeout=timeout, headers=headers or {})
        response.raise_for_status()
        return response.json()

    return _get


class TextGetter(Protocol):
    """Fetch a document as TEXT, for feeds that are not JSON.

    RSS is XML, a filing is HTML, a news page is HTML. `JsonGetter` cannot
    carry them, and the difference is not only the parse: a 10-Q runs to
    megabytes, so this contract includes a byte cap where the JSON one does
    not.
    """

    def __call__(self, url: str, params: dict[str, str]) -> str: ...


#: How much of any one document is read. A 10-Q can be tens of megabytes and
#: this box has 1.9 GB of RAM shared with the trading loop, so an uncapped read
#: is one bad URL away from an OOM kill that lands on `mudhorn-bot` rather than
#: on the reader. Generous enough that the interesting part of a filing is
#: inside it, and the truncation is REPORTED rather than silent — a quote
#: extracted from a document we only half read is still honest, but "we read
#: all of it and found nothing" is a different claim.
MAX_DOCUMENT_BYTES = 4_000_000


def httpx_text_getter(
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    headers: dict[str, str] | None = None,
    max_bytes: int = MAX_DOCUMENT_BYTES,
) -> TextGetter:
    """The real text implementation, built lazily as `httpx_getter` is.

    Streams and stops at `max_bytes` rather than reading whatever arrives. A
    `Content-Length` cannot be trusted — it is absent on chunked responses and
    it is a claim by the server either way — so the cap is applied to what has
    actually been read.
    """

    def _get(url: str, params: dict[str, str]) -> str:
        import httpx

        chunks: list[bytes] = []
        size = 0
        with httpx.stream(
            "GET",
            url,
            params=params,
            timeout=timeout,
            headers=headers or {},
            follow_redirects=True,
        ) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= max_bytes:
                    break
        return b"".join(chunks).decode("utf-8", errors="replace")

    return _get


@dataclass
class TtlCache:
    """A single-slot cache. Each feed makes one kind of call, so one slot is enough."""

    ttl_seconds: float
    _value: Any = None
    _stored_at: float = field(default=0.0)
    _has_value: bool = False

    def get(self, *, now: float | None = None) -> tuple[bool, Any]:
        clock = time.monotonic() if now is None else now
        if not self._has_value or clock - self._stored_at >= self.ttl_seconds:
            return False, None
        return True, self._value

    def put(self, value: Any, *, now: float | None = None) -> None:
        self._value = value
        self._stored_at = time.monotonic() if now is None else now
        self._has_value = True


def fetch_json(
    getter: JsonGetter,
    url: str,
    params: dict[str, str],
    *,
    feed: str,
) -> FeedResult:
    """GET some JSON, converting any failure into a FeedResult rather than raising.

    Deliberately catches broadly. The callers are a trading loop and a risk
    gate: there is no exception from an HTTP client worth crashing either over,
    and an unanticipated one is exactly the case where crashing would be worst.
    """
    try:
        return FeedResult(payload=getter(url, params))
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        log.warning("feed_fetch_failed", feed=feed, url=url, error=detail)
        return FeedResult.failed(detail)
