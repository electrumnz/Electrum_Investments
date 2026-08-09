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


def httpx_getter(timeout: float = DEFAULT_TIMEOUT_SECONDS) -> JsonGetter:
    """The real implementation, built lazily so importing this module is cheap."""

    def _get(url: str, params: dict[str, str]) -> Any:
        import httpx

        response = httpx.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        return response.json()

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
    except Exception as exc:  # noqa: BLE001 - see docstring
        detail = f"{type(exc).__name__}: {exc}"
        log.warning("feed_fetch_failed", feed=feed, url=url, error=detail)
        return FeedResult.failed(detail)
