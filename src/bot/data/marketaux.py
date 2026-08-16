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

import re
from dataclasses import dataclass, field
from typing import Any

import structlog

from ._http import DEFAULT_TIMEOUT_SECONDS, JsonGetter, TtlCache, fetch_json, httpx_getter
from .news import Sourced

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
        """Plain lines, for the decision loop. See `data/news.py` for why two."""
        return [item.line for item in self.recent_attributed(symbols, limit=limit)]

    def recent_attributed(self, symbols: list[str], *, limit: int = 10) -> list[Sourced]:
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


def _parse(payload: Any) -> list[Sourced]:
    """Turn a Marketaux response into one line per story, WITH its attribution.

    Tolerant on purpose. A feed that changes shape should cost us headlines, not
    raise inside the trading loop, so anything unrecognised is skipped.

    **This used to return bare strings and drop `url` and `source` on the
    floor**, which cost the dreamer the ability to cite the very stories it was
    reasoning from. Observed on a live dream: the chain's opening hop was
    "Cisco's AI networking supercycle claim, made today" — sparked by a headline
    this parser had fetched, from a publisher with a live URL — and it was
    stored UNCHECKED, because by the time it reached the prompt there was
    nothing to put in `Hop.source`. The attribution was in the payload and this
    function discarded it, which is `research.Citation`'s laundering rule broken
    one layer below where that rule is written down.
    """
    if not isinstance(payload, dict):
        return []
    articles = payload.get("data")
    if not isinstance(articles, list):
        return []

    lines: list[Sourced] = []
    for article in articles:
        if not isinstance(article, dict):
            continue
        # `" ".join(...split())` rather than `.strip()`, and the difference is
        # a prompt-injection channel rather than tidiness.
        #
        # `.strip()` removes leading and trailing whitespace and leaves an
        # EMBEDDED newline intact. `context.py` renders each headline as
        # `f"- {h}"` into a markdown document the model reads, so a title
        # carrying a newline can close the bullet and open its own `##`
        # section — and the section it would most usefully forge is
        # "Gate verdicts (previous cycle)", which the prompt tells the model
        # is deterministic and not to be argued with.
        #
        # This is NOT a gate bypass and must not be described as one:
        # `RiskGate` reads no prompt and no headline widens a cap. It is a
        # steering channel into the half the gate deliberately does not
        # second-guess — direction, symbol, entry, where the stop goes.
        #
        # `data/xfeed.py` was already safe here, but only by ACCIDENT: it
        # normalises whitespace for formatting reasons, with nothing saying
        # that is load-bearing and no test pinning it. Both are pinned now.
        title = " ".join(str(article.get("title") or "").split())
        if not title:
            continue

        # EVERY field that lands in the rendered line gets the same treatment,
        # not only the title. The title was fixed first because it is the
        # obvious one, and that left two other feed-controlled strings — the
        # entity symbols and the publication stamp — going into the same bullet
        # untouched. Both were measured emitting a multi-line bullet from a
        # single article, so the injection channel the title fix closed was
        # still open twice over beside it.
        #
        # `[{tickers}] {title} ({published})` is one line of a markdown document
        # the model reads, so a newline in ANY of the three closes the bullet
        # and lets whatever follows open its own `##` section — the section
        # worth forging being "Gate verdicts (previous cycle)", which the prompt
        # tells the model is deterministic and not to be argued with.
        #
        # `[:16]` does not help: it bounds the length and says nothing about
        # what is in those sixteen characters, and a newline fits in one.
        tickers = sorted(
            {
                " ".join(str(e.get("symbol")).split())
                for e in article.get("entities") or []
                if isinstance(e, dict) and e.get("symbol")
            }
            - {""}
        )
        published = " ".join(str(article.get("published_at") or "").split())[:16]

        # The attribution gets EXACTLY the same treatment as the title, the
        # tickers and the stamp, and for exactly the same reason. These are
        # feed-controlled strings landing in a markdown bullet, and a newline in
        # a publisher name closes that bullet just as effectively as one in a
        # headline does. It is easy to think of a URL as structurally inert
        # because it cannot contain a space; it can contain a newline, and the
        # `source` field is free text with no such excuse at all.
        #
        # `_looks_like_a_url` is the second half: a `source` field carrying
        # `javascript:` or a bare word is not something a reader can open, and
        # `Sourced.is_attributable` is what the dreamer checks before citing.
        url = " ".join(str(article.get("url") or "").split())
        publisher = " ".join(str(article.get("source") or "").split())

        prefix = f"[{', '.join(tickers)}] " if tickers else ""
        suffix = f" ({published})" if published else ""
        lines.append(
            Sourced(
                line=f"{prefix}{title}{suffix}",
                url=url if _looks_like_a_url(url) else "",
                publisher=publisher,
            )
        )

    return lines


_URL = re.compile(r"^https?://[^\s<>\"']+$", re.IGNORECASE)


def _looks_like_a_url(url: str) -> bool:
    """Syntactic only, and deliberately the same rule `research.py` applies.

    Nothing here fetches the URL to confirm it resolves. What this establishes
    is that the field holds something a person can paste into a browser, so a
    `source` naming a scheme nobody can open is dropped rather than rendered as
    a citable link. Two copies of one regex would be two rules to drift apart,
    but importing `research` here would put a web-facing module in the trading
    loop's import graph — which is precisely what that module's AST test exists
    to prevent. Duplicated deliberately, and pinned by a test that compares
    the two.
    """
    return bool(_URL.match(url.strip()))
