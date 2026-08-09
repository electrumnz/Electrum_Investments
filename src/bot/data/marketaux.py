"""Marketaux headlines — context for Claude, nothing more.

This feed is *informational*. Headlines land in the market context block and
influence what Claude proposes; they gate nothing. If Marketaux is down the bot
reasons with less information, which is a degradation rather than a fault, and
the risk gate is unaffected either way.

That is the opposite of `finnhub.py`, which supplies the calendar a risk rule
reads. Keep the two straight when changing either.

Free tier is **100 requests/day** against a loop that wakes 96 times, so the
cache TTL below is a hard requirement rather than tuning. See `_http.TtlCache`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from ._http import DEFAULT_TIMEOUT_SECONDS, JsonGetter, TtlCache, fetch_json, httpx_getter

log = structlog.get_logger(__name__)

MARKETAUX_URL = "https://api.marketaux.com/v1/news/all"

# 30 minutes: two cycles' worth. Well inside the free tier's daily budget with
# room for retries, and headlines do not turn over faster than that.
DEFAULT_CACHE_TTL_SECONDS = 1800.0


@dataclass
class MarketauxNews:
    """Recent headlines for the traded symbols, with sentiment where offered.

    Satisfies `news.NewsFeed`.
    """

    api_key: str
    getter: JsonGetter = field(default_factory=lambda: httpx_getter(DEFAULT_TIMEOUT_SECONDS))
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS
    _cache: TtlCache = field(init=False)

    def __post_init__(self) -> None:
        self._cache = TtlCache(ttl_seconds=self.cache_ttl_seconds)

    def recent_headlines(self, symbols: list[str], *, limit: int = 10) -> list[str]:
        if not symbols:
            return []

        cached, value = self._cache.get()
        if cached:
            return list(value)[:limit]

        result = fetch_json(
            self.getter,
            MARKETAUX_URL,
            {
                "api_token": self.api_key,
                # Equity tickers only. Crypto pairs carry a slash, which is not a
                # symbol Marketaux recognises, and sending one poisons the whole
                # query rather than being ignored.
                "symbols": ",".join(s for s in symbols if "/" not in s),
                "filter_entities": "true",
                "language": "en",
                "limit": str(max(limit, 10)),
            },
            feed="marketaux",
        )
        if not result.ok:
            # Headlines gate nothing, so an outage costs context and no more.
            # Deliberately not cached: retry on the next cycle.
            return []

        headlines = _parse(result.payload)
        self._cache.put(headlines)
        return headlines[:limit]


def _parse(payload: Any) -> list[str]:
    """Turn a Marketaux response into one plain line per story.

    Tolerant on purpose. A feed that changes shape should cost us headlines, not
    raise inside the trading loop, so anything unrecognised is skipped.
    """
    if not isinstance(payload, dict):
        return []
    articles = payload.get("data")
    if not isinstance(articles, list):
        return []

    lines: list[str] = []
    for article in articles:
        if not isinstance(article, dict):
            continue
        title = str(article.get("title") or "").strip()
        if not title:
            continue

        tickers = sorted(
            {
                str(e.get("symbol"))
                for e in article.get("entities") or []
                if isinstance(e, dict) and e.get("symbol")
            }
        )
        published = str(article.get("published_at") or "")[:16]

        prefix = f"[{', '.join(tickers)}] " if tickers else ""
        suffix = f" ({published})" if published else ""
        lines.append(f"{prefix}{title}{suffix}")

    return lines
